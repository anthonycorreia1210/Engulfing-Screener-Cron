#!/usr/bin/env python3
"""
Alpaca paper trading of scanner signals
=======================================
Turns the scanner's starred BULLISH signals into long-call paper trades on an
Alpaca paper account, so the strategy can prove (or hang) itself with fake
money and a real market before any real dollars are used.

The strategy (agreed rules):
  - Every engulfing signal recorded today. Starred (body >= 1.5x prior
    range) at full stake; unstarred at HALF (2026-08-05) — the backtest
    found no tier-0 edge, so that half-stake is an explicit test of it.
  - BEARISH signals buy PUTS only (14-24 DTE, delta ~-0.65, 5% of equity;
    no short-share leg). Everything below describes the bullish side.
  - TWO legs per bullish signal (2026-08-04 split — was 10% all-options):
      calls:  1+ CALLs, 30-45 DTE (widened to 25-60 when the window has no
              expirations), delta closest to 0.65, limit at the quote mid,
              sized to ~5% of account equity.
      shares: straight stock, ~5% of equity, whole shares, limit at mid.
    Same thesis, two payoff shapes — the ledger shows which expresses the
    edge better.
  - Always at least 1 contract / 1 share, but skip a leg entirely if its
    minimum size would cost > 30% of equity.
  - Profit take (options only): the moment an option leg trades at 2x its
    entry price, sell 60% of the contracts — cost basis off the table — and
    let the other 40% run to the normal exit. Independent of the calendar.
  - Exit: sell BOTH legs to close on the 10th trading day after the trigger
    (the backtested edge window). First exit attempt is a limit at mid; if
    that day order expires unfilled, the next run sends a market order.

Entry orders are DAY limit orders placed ~20 minutes before the close: if the
mid doesn't fill by the bell the order expires and the trade is recorded as
'unfilled' — a missed fill is data too. All trades, skips, and misses are
recorded in signals.db (paper_trades) next to the signals they came from.

Honesty caveats: Alpaca paper fills are idealized (no queue, fills at NBBO
touch), and the options feed is the free indicative one. Limit-at-mid entries
and exits keep the fills conservative-ish, but treat results as an upper
bound on live performance.

Usage:
  python3 paper_trader.py            # reconcile -> exits -> entries (cron)
  python3 paper_trader.py --dry-run  # show what would be traded, place nothing
  python3 paper_trader.py --status   # print the trade ledger
  python3 paper_trader.py --force    # skip the market-hours gate on entries
"""
import argparse
import math
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from engulfing_scanner import (
    ET,
    SCRIPT_DIR,
    conviction_tier,
    is_market_open_window,
    load_dotenv,
    send_ntfy,
    send_telegram,
    stars,
)
import history

DB_PATH = history.DB_PATH  # single source of truth (honors SIGNALS_DB override)

TARGET_DELTA = 0.65
DTE_WINDOWS = ((30, 45), (25, 60))
# Puts ride the 10-trading-day exit window with minimal time premium: the
# nearest expiry that comfortably outlives the exit (~2-3 weeks), not the
# 30-45 DTE call window (2026-08-05, owner: "Aug 21 exp are probably best"
# for an Aug 5 trigger — that's 16 DTE).
PUT_DTE_WINDOWS = ((14, 24), (10, 35))
# Each signal opens TWO legs (2026-08-04): half the old 10% into calls, half
# into straight shares — same thesis, two payoff shapes to compare.
TARGET_PCT_OPTION = 0.05  # of account equity into the call leg
TARGET_PCT_STOCK = 0.05   # of account equity into the share leg
# Unstarred (tier 0) signals trade at HALF size (2026-08-05, owner). The 5y
# backtest found no edge at tier 0, so this is a deliberate half-stake test
# of that finding, not a backtested allocation.
UNSTARRED_SCALE = 0.5
MAX_PCT = 0.30      # hard skip if 1 contract costs more than this
MAX_SPREAD = 0.60   # skip contracts whose bid/ask spread exceeds 60% of mid
# Profit-take (2026-08-05): when an option leg doubles, sell 60% of the
# contracts immediately — cost basis off the table, 40% runs to the +10td
# exit. Options only; share legs always ride the full window.
PROFIT_TAKE_MULT = 2.0
PROFIT_TAKE_FRAC = 0.60
MIN_TIER = 0        # trade every engulfer; tier 0 at half size
CHASE_MIN_TIER = 1  # chase unfilled option entries on ALL starred signals
                    # (was 3; widened 2026-08-04 — a ★★ IBM entry nearly
                    # expired unfilled with no chase coverage)
EXIT_AFTER = history.TRACK_DAYS  # sell on the Nth trading day after trigger

_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id       INTEGER REFERENCES signals(id),
    symbol          TEXT NOT NULL,
    trigger_date    TEXT NOT NULL,
    tier            INTEGER,
    contract        TEXT,
    expiry          TEXT,
    strike          REAL,
    delta           REAL,
    qty             INTEGER,
    status          TEXT NOT NULL,
    note            TEXT,
    equity_at_entry REAL,
    entry_order_id  TEXT,
    entry_limit     REAL,
    entry_price     REAL,
    entry_date      TEXT,
    exit_due        TEXT,
    exit_attempts   INTEGER NOT NULL DEFAULT 0,
    exit_order_id   TEXT,
    exit_price      REAL,
    exit_date       TEXT,
    pnl             REAL,
    pnl_pct         REAL,
    asset_type      TEXT NOT NULL DEFAULT 'option',
    partial_order_id TEXT,
    partial_qty     INTEGER,
    partial_price   REAL,
    partial_date    TEXT,
    partial_pnl     REAL,
    partial_attempts INTEGER NOT NULL DEFAULT 0,
    UNIQUE (symbol, trigger_date, asset_type)
);
"""
# status lifecycle:
#   submitted -> open -> exit_pending -> closed
#   submitted -> unfilled              (entry day order expired with no fill)
#   skipped                            (no viable contract; never retried)
# asset_type: 'option' (call leg, x100 multiplier) or 'stock' (share leg;
#   contract == the equity symbol, expiry/strike/delta NULL).


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    # Pre-2026-08-04 DBs: single-leg rows + UNIQUE(symbol, trigger_date).
    # SQLite can't alter an inline constraint, so rebuild once.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(paper_trades)")}
    for col, decl in (("partial_order_id", "TEXT"), ("partial_qty", "INTEGER"),
                      ("partial_price", "REAL"), ("partial_date", "TEXT"),
                      ("partial_pnl", "REAL"),
                      ("partial_attempts", "INTEGER NOT NULL DEFAULT 0")):
        if col not in cols and "asset_type" in cols:
            with conn:
                conn.execute(f"ALTER TABLE paper_trades ADD COLUMN {col} {decl}")
    if "asset_type" not in cols:
        with conn:
            conn.execute("ALTER TABLE paper_trades RENAME TO paper_trades_old")
            conn.executescript(_SCHEMA)
            old_cols = ",".join(sorted(cols))
            conn.execute(f"INSERT INTO paper_trades ({old_cols}, asset_type)"
                         f" SELECT {old_cols}, 'option' FROM paper_trades_old")
            conn.execute("DROP TABLE paper_trades_old")
    return conn


def _clients():
    """(TradingClient, OptionHistoricalDataClient, StockHistoricalDataClient)
    or (None, None, None) if no keys."""
    import os
    key = os.environ.get("ALPACA_PAPER_KEY")
    secret = os.environ.get("ALPACA_PAPER_SECRET")
    if not (key and secret):
        return None, None, None
    from alpaca.data.historical.option import OptionHistoricalDataClient
    from alpaca.data.historical.stock import StockHistoricalDataClient
    from alpaca.trading.client import TradingClient
    return (TradingClient(key, secret, paper=True),
            OptionHistoricalDataClient(key, secret),
            StockHistoricalDataClient(key, secret))


def _tick_round(px: float) -> float:
    """Round to the option tick: $0.05 above $3.00 premium, else $0.01."""
    tick = 0.05 if px >= 3 else 0.01
    return round(round(px / tick) * tick, 2)


def _parse_occ(sym: str) -> tuple[str, float]:
    """OCC symbol -> (expiry ISO date, strike)."""
    strike = int(sym[-8:]) / 1000
    yy, mm, dd = sym[-15:-13], sym[-13:-11], sym[-11:-9]
    return f"20{yy}-{mm}-{dd}", strike


def _exit_due(tc, trigger_date: str) -> str | None:
    """Date of the EXIT_AFTER-th trading day after the trigger (Alpaca calendar)."""
    from alpaca.trading.requests import GetCalendarRequest
    trig = date.fromisoformat(trigger_date)
    try:
        cal = tc.get_calendar(GetCalendarRequest(
            start=trig, end=trig + timedelta(days=EXIT_AFTER * 2 + 10)))
    except Exception as exc:
        print(f"  [warn] calendar fetch failed ({exc})", file=sys.stderr)
        return None
    sessions = [c.date for c in cal if c.date > trig]
    return sessions[EXIT_AFTER - 1].isoformat() if len(sessions) >= EXIT_AFTER else None


def _latest_quote(data_client, contract: str):
    """(bid, ask, mid) for one contract, or None if no two-sided quote."""
    from alpaca.data.requests import OptionLatestQuoteRequest
    try:
        q = data_client.get_option_latest_quote(
            OptionLatestQuoteRequest(symbol_or_symbols=contract))[contract]
    except Exception:
        return None
    bid, ask = float(q.bid_price or 0), float(q.ask_price or 0)
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    return bid, ask, (bid + ask) / 2


def _latest_stock_quote(stock_client, symbol: str):
    """(bid, ask, mid) for an equity, or None if no two-sided quote."""
    from alpaca.data.requests import StockLatestQuoteRequest
    try:
        q = stock_client.get_stock_latest_quote(
            StockLatestQuoteRequest(symbol_or_symbols=symbol))[symbol]
    except Exception:
        return None
    bid, ask = float(q.bid_price or 0), float(q.ask_price or 0)
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    return bid, ask, (bid + ask) / 2


def _stock_ref_price(stock_client, symbol: str) -> float | None:
    """Sizing price for the share leg: last trade first — the free IEX quote
    can be wildly stale (2026-08-04: IBM quoted ~247 ask while trading ~234) —
    falling back to the quote mid only if there's no trade."""
    from alpaca.data.requests import StockLatestTradeRequest
    try:
        t = stock_client.get_stock_latest_trade(
            StockLatestTradeRequest(symbol_or_symbols=symbol))[symbol]
        px = float(t.price or 0)
        if px > 0:
            return px
    except Exception:
        pass
    quote = _latest_stock_quote(stock_client, symbol)
    return quote[2] if quote else None


def _quote_for(row, option_client, stock_client):
    """Quote dispatch: option rows quote the OCC contract, stock rows the equity."""
    if row["asset_type"] == "stock":
        return _latest_stock_quote(stock_client, row["contract"])
    return _latest_quote(option_client, row["contract"])


def _mult(row) -> int:
    """P&L multiplier: option contracts control 100 shares."""
    return 1 if row["asset_type"] == "stock" else 100


def _stock_tick_round(px: float) -> float:
    """Equities tick in pennies (sub-$1 stocks can quote finer; penny is fine
    for a paper limit)."""
    return round(px, 2)


def pick_contract(data_client, symbol: str, equity: float, bearish: bool = False):
    """
    Choose the option to buy. Calls (bullish): 30-45 DTE (else 25-60), delta
    closest to +0.65. Puts (bearish): 14-24 DTE (else 10-35), delta closest
    to -0.65. Both need a two-sided quote and a sane spread, affordable
    within MAX_PCT of equity. Returns (contract dict, None) or (None, reason).
    """
    from alpaca.data.requests import OptionChainRequest
    from alpaca.trading.enums import ContractType
    today = date.today()
    windows = PUT_DTE_WINDOWS if bearish else DTE_WINDOWS
    target = -TARGET_DELTA if bearish else TARGET_DELTA
    cands = []
    for lo, hi in windows:
        try:
            chain = data_client.get_option_chain(OptionChainRequest(
                underlying_symbol=symbol,
                type=ContractType.PUT if bearish else ContractType.CALL,
                expiration_date_gte=today + timedelta(days=lo),
                expiration_date_lte=today + timedelta(days=hi)))
        except Exception as exc:
            return None, f"chain fetch failed ({exc})"
        for occ, snap in chain.items():
            q, g = snap.latest_quote, snap.greeks
            if not q or not g or g.delta is None:
                continue
            bid, ask = float(q.bid_price or 0), float(q.ask_price or 0)
            if bid <= 0 or ask <= 0 or ask < bid:
                continue
            mid = (bid + ask) / 2
            if (ask - bid) / mid > MAX_SPREAD:
                continue
            expiry, strike = _parse_occ(occ)
            cands.append({"contract": occ, "expiry": expiry, "strike": strike,
                          "delta": float(g.delta), "bid": bid, "ask": ask,
                          "mid": mid})
        if cands:
            break
    if not cands:
        return None, ("no liquid puts 10-35 DTE" if bearish
                      else "no liquid calls 25-60 DTE")
    cands.sort(key=lambda c: abs(c["delta"] - target))
    affordable = [c for c in cands if c["mid"] * 100 <= equity * MAX_PCT]
    if not affordable:
        best = cands[0]
        return None, (f"1 contract (~${best['mid'] * 100:.0f}) exceeds "
                      f"{MAX_PCT:.0%} of equity")
    return affordable[0], None


def _tier_scale(tier: int) -> float:
    """Unstarred signals take half the normal stake."""
    return UNSTARRED_SCALE if tier < 1 else 1.0


def _size(mid: float, equity: float, tier: int = 1) -> int:
    """Contracts for ~TARGET_PCT_OPTION of equity; never fewer than 1."""
    pct = TARGET_PCT_OPTION * _tier_scale(tier)
    return max(1, math.floor(equity * pct / (mid * 100)))


# ----------------------------------------------------------------------
# The three phases of a run
# ----------------------------------------------------------------------
def reconcile(conn, tc) -> list[str]:
    """Resolve outstanding entry/exit orders against Alpaca. Returns events."""
    events = []
    # Outstanding profit-take sells live on rows still marked 'open' (the
    # remaining 40% is still a live position), so they reconcile separately.
    for r in conn.execute(
            "SELECT * FROM paper_trades WHERE partial_order_id IS NOT NULL"
            " AND partial_price IS NULL").fetchall():
        try:
            o = tc.get_order_by_id(r["partial_order_id"])
        except Exception as exc:
            print(f"  [warn] partial lookup failed for {r['symbol']} ({exc})",
                  file=sys.stderr)
            continue
        st = o.status.value
        filled_qty = int(float(o.filled_qty or 0))
        avg = float(o.filled_avg_price) if o.filled_avg_price else None
        with conn:
            if filled_qty > 0 and avg is not None:
                pnl = (avg - r["entry_price"]) * _mult(r) * filled_qty
                conn.execute(
                    "UPDATE paper_trades SET qty = qty - ?, partial_qty=?,"
                    " partial_price=?, partial_date=?, partial_pnl=?,"
                    " partial_order_id=NULL,"
                    " note=? WHERE id=?",
                    (filled_qty, filled_qty, avg,
                     (o.filled_at.date().isoformat() if o.filled_at
                      else datetime.now(ET).date().isoformat()), pnl,
                     f"took profit on {filled_qty} at {avg:.2f} (2x rule)",
                     r["id"]))
                events.append(f"profit-take filled {r['symbol']} {filled_qty}x"
                              f" @ {avg} (+${pnl:,.0f})")
            elif st in ("canceled", "expired", "rejected", "done_for_day"):
                # Clear the marker so the next pass retries (at market).
                conn.execute("UPDATE paper_trades SET partial_order_id=NULL"
                             " WHERE id=?", (r["id"],))
                events.append(f"profit-take unfilled {r['symbol']} ({st}),"
                              " will retry")
    rows = conn.execute("SELECT * FROM paper_trades WHERE status IN"
                        " ('submitted', 'exit_pending')").fetchall()
    for r in rows:
        oid = r["entry_order_id"] if r["status"] == "submitted" else r["exit_order_id"]
        try:
            o = tc.get_order_by_id(oid)
        except Exception as exc:
            print(f"  [warn] order lookup failed for {r['symbol']} ({exc})",
                  file=sys.stderr)
            continue
        st = o.status.value
        filled_qty = int(float(o.filled_qty or 0))
        avg = float(o.filled_avg_price) if o.filled_avg_price else None
        terminal_unfilled = st in ("canceled", "expired", "rejected", "done_for_day")
        with conn:
            if r["status"] == "submitted":
                if st == "filled" or (terminal_unfilled and filled_qty > 0):
                    note = r["note"]
                    if filled_qty < r["qty"]:
                        note = f"partial fill {filled_qty}/{r['qty']}"
                    conn.execute(
                        "UPDATE paper_trades SET status='open', qty=?,"
                        " entry_price=?, entry_date=?, note=? WHERE id=?",
                        (filled_qty or r["qty"], avg,
                         (o.filled_at.date().isoformat() if o.filled_at
                          else r["trigger_date"]), note, r["id"]))
                    events.append(f"filled {r['symbol']} {filled_qty or r['qty']}x"
                                  f" {r['contract']} @ {avg}")
                elif terminal_unfilled:
                    conn.execute("UPDATE paper_trades SET status='unfilled',"
                                 " note=? WHERE id=?",
                                 (f"entry order {st} unfilled", r["id"]))
                    events.append(f"entry unfilled {r['symbol']} ({st})")
            else:  # exit_pending
                if st == "filled":
                    pnl = pnl_pct = None
                    if avg is not None and r["entry_price"]:
                        # Total P&L spans both slices; the % stays per-contract
                        # on the runner so it reads as a price move, not a blend.
                        pnl = ((avg - r["entry_price"]) * _mult(r) * r["qty"]
                               + (r["partial_pnl"] or 0))
                        pnl_pct = (avg - r["entry_price"]) / r["entry_price"] * 100
                    conn.execute(
                        "UPDATE paper_trades SET status='closed', exit_price=?,"
                        " exit_date=?, pnl=?, pnl_pct=? WHERE id=?",
                        (avg, (o.filled_at.date().isoformat() if o.filled_at
                               else datetime.now(ET).date().isoformat()),
                         pnl, pnl_pct, r["id"]))
                    events.append(f"closed {r['symbol']} @ {avg}"
                                  f" ({pnl_pct:+.1f}%)" if pnl_pct is not None
                                  else f"closed {r['symbol']} @ {avg}")
                elif terminal_unfilled:
                    # Back to open; do_exits() escalates to a market order.
                    conn.execute("UPDATE paper_trades SET status='open',"
                                 " note='exit limit expired, will retry at market'"
                                 " WHERE id=?", (r["id"],))
                    events.append(f"exit unfilled {r['symbol']}, retrying")
    return events


def do_profit_takes(conn, tc, data_client, dry_run=False) -> list[str]:
    """
    Sell PROFIT_TAKE_FRAC of any option leg trading at >= PROFIT_TAKE_MULT x
    its entry price, whatever the calendar says. Runs before do_exits so a
    position that doubled on its exit day still books the runner normally.
    First attempt is a limit at mid; a retry (the previous one expired) goes
    to market so the gain can't evaporate waiting for a fill.
    Share legs are untouched — they always ride the full +10td window.
    A 1-lot can't be split 60/40, so it exits whole at the trigger.
    """
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
    events = []
    rows = conn.execute(
        "SELECT * FROM paper_trades WHERE status='open' AND asset_type='option'"
        " AND partial_qty IS NULL AND partial_order_id IS NULL"
        " AND entry_price IS NOT NULL AND qty > 0").fetchall()
    for r in rows:
        quote = _latest_quote(data_client, r["contract"])
        if quote is None:
            continue
        mid = quote[2]
        if mid < r["entry_price"] * PROFIT_TAKE_MULT:
            continue
        sell_qty = min(r["qty"], max(1, round(r["qty"] * PROFIT_TAKE_FRAC)))
        gain = (mid / r["entry_price"] - 1) * 100
        use_market = r["partial_attempts"] >= 1
        desc = (f"{r['symbol']} {sell_qty}/{r['qty']}x {r['contract']} at"
                f" {mid:.2f} (+{gain:.0f}%,"
                f" {'market' if use_market else 'limit'})")
        if dry_run:
            events.append("would take profit " + desc)
            continue
        try:
            if use_market:
                req = MarketOrderRequest(symbol=r["contract"], qty=sell_qty,
                                         side=OrderSide.SELL,
                                         time_in_force=TimeInForce.DAY)
            else:
                req = LimitOrderRequest(symbol=r["contract"], qty=sell_qty,
                                        side=OrderSide.SELL,
                                        time_in_force=TimeInForce.DAY,
                                        limit_price=_tick_round(mid))
            o = tc.submit_order(req)
        except Exception as exc:
            print(f"  [warn] profit-take failed for {r['symbol']} ({exc})",
                  file=sys.stderr)
            continue
        with conn:
            conn.execute("UPDATE paper_trades SET partial_order_id=?,"
                         " partial_attempts=partial_attempts+1 WHERE id=?",
                         (str(o.id), r["id"]))
        events.append("profit-take sent " + desc)
    return events


def do_exits(conn, tc, data_client, stock_client, dry_run=False) -> list[str]:
    """Sell positions whose 10-trading-day window is up (or expiry is near)."""
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
    events = []
    today = datetime.now(ET).date().isoformat()
    near_expiry = (datetime.now(ET).date() + timedelta(days=5)).isoformat()
    rows = conn.execute(
        "SELECT * FROM paper_trades WHERE status='open' AND"
        " (COALESCE(exit_due, '9999') <= ? OR COALESCE(expiry, '9999') <= ?)",
        (today, near_expiry)).fetchall()
    for r in rows:
        quote = _quote_for(r, data_client, stock_client)
        use_market = r["exit_attempts"] >= 1 or quote is None
        if dry_run:
            events.append(f"would exit {r['symbol']} {r['qty']}x {r['contract']}"
                          f" ({'market' if use_market else 'limit at mid'})")
            continue
        try:
            if use_market:
                req = MarketOrderRequest(symbol=r["contract"], qty=r["qty"],
                                         side=OrderSide.SELL,
                                         time_in_force=TimeInForce.DAY)
            else:
                req = LimitOrderRequest(symbol=r["contract"], qty=r["qty"],
                                        side=OrderSide.SELL,
                                        time_in_force=TimeInForce.DAY,
                                        limit_price=_tick_round(quote[2]))
            o = tc.submit_order(req)
        except Exception as exc:
            print(f"  [warn] exit order failed for {r['symbol']} ({exc})",
                  file=sys.stderr)
            continue
        with conn:
            conn.execute(
                "UPDATE paper_trades SET status='exit_pending', exit_order_id=?,"
                " exit_attempts=exit_attempts+1 WHERE id=?",
                (str(o.id), r["id"]))
        events.append(f"exit sent {r['symbol']} {r['qty']}x {r['contract']}"
                      f" ({'market' if use_market else 'limit'})")
    return events


def _enter_option_leg(conn, tc, data_client, s, tier, equity, dry_run,
                      bearish: bool = False) -> str | None:
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import LimitOrderRequest
    contract, reason = pick_contract(data_client, s["symbol"], equity, bearish)
    kind = "puts" if bearish else "calls"
    if contract is None:
        if not dry_run:
            with conn:
                conn.execute(
                    "INSERT OR IGNORE INTO paper_trades"
                    " (signal_id, symbol, trigger_date, tier, status, note,"
                    "  asset_type)"
                    " VALUES (?,?,?,?,'skipped',?,'option')",
                    (s["id"], s["symbol"], s["trigger_date"], tier, reason))
        return f"skipped {s['symbol']} {stars(tier)} {kind}: {reason}"
    qty = _size(contract["mid"], equity, tier)
    limit = _tick_round(contract["mid"])
    cost = limit * 100 * qty
    desc = (f"{s['symbol']} {stars(tier)} {qty}x {contract['contract']}"
            f" Δ{contract['delta']:.2f} limit {limit:.2f}"
            f" (~${cost:,.0f}, {cost / equity:.1%} of equity)")
    if dry_run:
        return "would buy " + desc
    try:
        o = tc.submit_order(LimitOrderRequest(
            symbol=contract["contract"], qty=qty, side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY, limit_price=limit))
    except Exception as exc:
        print(f"  [warn] entry order failed for {s['symbol']} ({exc})",
              file=sys.stderr)
        return None
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO paper_trades"
            " (signal_id, symbol, trigger_date, tier, contract, expiry,"
            "  strike, delta, qty, status, equity_at_entry, entry_order_id,"
            "  entry_limit, exit_due, asset_type)"
            " VALUES (?,?,?,?,?,?,?,?,?,'submitted',?,?,?,?,'option')",
            (s["id"], s["symbol"], s["trigger_date"], tier,
             contract["contract"], contract["expiry"], contract["strike"],
             contract["delta"], qty, equity, str(o.id), limit,
             _exit_due(tc, s["trigger_date"])))
    return "buy " + desc


def _enter_stock_leg(conn, tc, stock_client, s, tier, equity, dry_run) -> str | None:
    """Share legs enter at MARKET — they must always fill (2026-08-04 rule);
    the limit-at-mid honesty experiment applies to the option leg only."""
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest
    ref = _stock_ref_price(stock_client, s["symbol"])
    if ref is None:
        if not dry_run:
            with conn:
                conn.execute(
                    "INSERT OR IGNORE INTO paper_trades"
                    " (signal_id, symbol, trigger_date, tier, status, note,"
                    "  asset_type)"
                    " VALUES (?,?,?,?,'skipped','no stock price for sizing',"
                    "  'stock')",
                    (s["id"], s["symbol"], s["trigger_date"], tier))
        return f"skipped {s['symbol']} {stars(tier)} shares: no price"
    if ref > equity * MAX_PCT:
        reason = f"1 share (~${ref:,.0f}) exceeds {MAX_PCT:.0%} of equity"
        if not dry_run:
            with conn:
                conn.execute(
                    "INSERT OR IGNORE INTO paper_trades"
                    " (signal_id, symbol, trigger_date, tier, status, note,"
                    "  asset_type)"
                    " VALUES (?,?,?,?,'skipped',?,'stock')",
                    (s["id"], s["symbol"], s["trigger_date"], tier, reason))
        return f"skipped {s['symbol']} {stars(tier)} shares: {reason}"
    qty = max(1, math.floor(equity * TARGET_PCT_STOCK * _tier_scale(tier) / ref))
    cost = ref * qty
    desc = (f"{s['symbol']} {stars(tier)} {qty} share{'s' if qty != 1 else ''}"
            f" at market (~${cost:,.0f}, {cost / equity:.1%} of equity)")
    if dry_run:
        return "would buy " + desc
    try:
        o = tc.submit_order(MarketOrderRequest(
            symbol=s["symbol"], qty=qty, side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY))
    except Exception as exc:
        print(f"  [warn] stock entry order failed for {s['symbol']} ({exc})",
              file=sys.stderr)
        return None
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO paper_trades"
            " (signal_id, symbol, trigger_date, tier, contract, qty, status,"
            "  equity_at_entry, entry_order_id, exit_due, asset_type)"
            " VALUES (?,?,?,?,?,?,'submitted',?,?,?,'stock')",
            (s["id"], s["symbol"], s["trigger_date"], tier, s["symbol"], qty,
             equity, str(o.id), _exit_due(tc, s["trigger_date"])))
    return "buy " + desc


def do_entries(conn, tc, data_client, stock_client, dry_run=False) -> list[str]:
    """
    Open positions for today's starred signals:
      BULLISH -> 5% calls (30-45 DTE) + 5% shares at market
      BEARISH -> 5% puts (14-24 DTE); no short-share leg (2026-08-05)
    Legs retry independently: a signal missing only one leg (e.g. an order
    submit failed) gets just that leg on the next pass.
    """
    events = []
    today = datetime.now(ET).date().isoformat()
    sigs = conn.execute(
        "SELECT * FROM signals WHERE trigger_date = ?"
        " ORDER BY body_mult DESC", (today,)).fetchall()
    if not sigs:
        return events
    existing = {(r["symbol"], r["asset_type"]) for r in conn.execute(
        "SELECT symbol, asset_type FROM paper_trades WHERE trigger_date = ?",
        (today,))}
    equity = float(tc.get_account().equity)
    for s in sigs:
        tier = conviction_tier({"body_mult": s["body_mult"],
                                "body_atr": s["body_atr"]})
        if tier < MIN_TIER:
            continue
        bearish = s["direction"] == "BEARISH"
        if (s["symbol"], "option") not in existing:
            ev = _enter_option_leg(conn, tc, data_client, s, tier, equity,
                                   dry_run, bearish=bearish)
            if ev:
                events.append(ev)
        # Long shares only express a bullish thesis; bearish signals are
        # puts-only (no shorting in this experiment).
        if not bearish and (s["symbol"], "stock") not in existing:
            ev = _enter_stock_leg(conn, tc, stock_client, s, tier, equity, dry_run)
            if ev:
                events.append(ev)
    return events


def chase(conn, tc, data_client, stock_client, dry_run=False) -> list[str]:
    """
    Convert still-unfilled DAY orders into marketable limits so they execute
    instead of expiring. Runs twice near the close (3:48 + 3:56 ET).
      - Entries: option legs only (stock legs enter at market and never need
        chasing), any starred tier >= CHASE_MIN_TIER.
      - Exits: any tier, both legs — the +10-trading-day exit is part of the
        strategy spec, so paying the spread beats selling a day late.
    Replacing an order issues a NEW order id; the ledger row is re-pointed.
    """
    from alpaca.trading.requests import ReplaceOrderRequest
    events = []
    today = datetime.now(ET).date().isoformat()
    rows = conn.execute(
        "SELECT * FROM paper_trades WHERE"
        " (status='submitted' AND trigger_date=? AND tier >= ?"
        "  AND asset_type != 'stock')"
        " OR status='exit_pending'", (today, CHASE_MIN_TIER)).fetchall()
    for r in rows:
        entry = r["status"] == "submitted"
        oid = r["entry_order_id"] if entry else r["exit_order_id"]
        try:
            o = tc.get_order_by_id(oid)
        except Exception as exc:
            print(f"  [warn] chase lookup failed for {r['symbol']} ({exc})",
                  file=sys.stderr)
            continue
        if o.status.value not in ("new", "accepted", "pending_new",
                                  "partially_filled"):
            continue  # filled or dead — reconcile handles it
        quote = _quote_for(r, data_client, stock_client)
        if quote is None:
            continue
        bid, ask, _ = quote
        # Marketable: buys clear the ask, sells undercut the bid, with a
        # cushion so a moving quote can't out-run the replacement (5% is an
        # option-sized cushion; equities move tighter — 0.5%).
        rnd = _stock_tick_round if r["asset_type"] == "stock" else _tick_round
        pad = 1.005 if r["asset_type"] == "stock" else 1.05
        new_limit = (rnd(ask * pad) if entry
                     else max(0.01, rnd(bid * (2 - pad))))
        old_limit = float(o.limit_price) if o.limit_price else None
        if old_limit is not None and \
                (new_limit <= old_limit if entry else new_limit >= old_limit):
            continue  # existing limit is already at least this aggressive
        what = "entry" if entry else "exit"
        desc = f"{r['symbol']} {what} limit {old_limit} -> {new_limit}"
        if dry_run:
            events.append("would chase " + desc)
            continue
        try:
            new = tc.replace_order_by_id(oid, ReplaceOrderRequest(
                limit_price=new_limit))
        except Exception as exc:
            print(f"  [warn] chase replace failed for {r['symbol']} ({exc})",
                  file=sys.stderr)
            continue
        col = "entry_order_id" if entry else "exit_order_id"
        lim = ", entry_limit=?" if entry else ""
        args = [str(new.id)] + ([new_limit] if entry else []) + \
               [f"chased {what} at close: {old_limit} -> {new_limit}", r["id"]]
        with conn:
            conn.execute(f"UPDATE paper_trades SET {col}=?{lim}, note=?"
                         " WHERE id=?", args)
        events.append("chased " + desc)
    return events


def run_chase(dry_run=False) -> None:
    """The 12:56 PM PT cron pass: reconcile, then chase what's still open."""
    tc, data_client, stock_client = _clients()
    if tc is None:
        print("ALPACA_PAPER_KEY / ALPACA_PAPER_SECRET not set — nothing to do.")
        return
    conn = _connect()
    try:
        now_str = datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")
        print(f"Chase pass at {now_str}")
        events = reconcile(conn, tc)
        # A doubling can happen after the 3:40 pass; catch it before the bell.
        events += do_profit_takes(conn, tc, data_client, dry_run=dry_run)
        events += chase(conn, tc, data_client, stock_client, dry_run=dry_run)
        for e in events:
            print("  " + e)
        if not events:
            print("  nothing to chase")
    finally:
        conn.close()


def run_profit_take(dry_run=False) -> None:
    """
    Intraday pass (every 30 min while the market is open): reconcile, then
    take profits on anything that has doubled. No entries, no exits — those
    belong to the near-close passes. Cheap: it only quotes open option legs,
    and stays silent unless something actually fires, so a quiet day costs
    nothing in logs or alerts.
    """
    if not is_market_open_window() and not dry_run:
        return
    tc, data_client, _ = _clients()
    if tc is None:
        return
    conn = _connect()
    try:
        events = reconcile(conn, tc)
        events += do_profit_takes(conn, tc, data_client, dry_run=dry_run)
        if not events:
            return
        now_str = datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")
        print(f"Profit-take check at {now_str}")
        for e in events:
            print("  " + e)
        if not dry_run:
            body = f"Paper trader — {now_str}\n\n" + "\n".join(events)
            for send in (send_ntfy, send_telegram):
                try:
                    send(f"Profit take: {len(events)} event(s)", body) \
                        if send is send_ntfy else send(body)
                except Exception:
                    pass
    finally:
        conn.close()


def run(dry_run=False, force=False) -> None:
    tc, data_client, stock_client = _clients()
    if tc is None:
        print("ALPACA_PAPER_KEY / ALPACA_PAPER_SECRET not set — nothing to do.")
        return
    conn = _connect()
    try:
        now_str = datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")
        print(f"Paper trader run at {now_str}")
        events = reconcile(conn, tc)
        events += do_profit_takes(conn, tc, data_client, dry_run=dry_run)
        events += do_exits(conn, tc, data_client, stock_client, dry_run=dry_run)
        if is_market_open_window() or force or dry_run:
            events += do_entries(conn, tc, data_client, stock_client,
                                 dry_run=dry_run)
        else:
            print("  market closed — entries skipped (exits/reconcile only)")
        for e in events:
            print("  " + e)
        if not events:
            print("  nothing to do")
        if events and not dry_run:
            body = f"Paper trader — {now_str}\n\n" + "\n".join(events)
            subject = f"Paper trader: {len(events)} event(s)"
            for send in (send_ntfy, send_telegram):
                try:
                    send(subject, body) if send is send_ntfy else send(body)
                except Exception:
                    pass
    finally:
        conn.close()


# ----------------------------------------------------------------------
# Read side (CLI --status and the web app's Paper tab)
# ----------------------------------------------------------------------
def get_trades() -> list[dict]:
    conn = _connect()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM paper_trades"
            " ORDER BY trigger_date DESC, symbol").fetchall()]
    finally:
        conn.close()


def web_summary() -> dict:
    """Everything the Paper tab needs, in one payload."""
    tc, data_client, stock_client = _clients()
    if tc is None:
        return {"enabled": False}
    conn = _connect()
    try:
        reconcile(conn, tc)
    except Exception as exc:
        print(f"[warn] paper reconcile failed: {exc}", file=sys.stderr)
    finally:
        conn.close()
    trades = get_trades()
    # Mark open positions to the current mid for unrealized P&L.
    for t in trades:
        if t["status"] in ("open", "exit_pending") and t["entry_price"]:
            quote = _quote_for(t, data_client, stock_client)
            if quote:
                t["mark"] = quote[2]
                t["unreal_pnl"] = (quote[2] - t["entry_price"]) * _mult(t) * t["qty"]
                t["unreal_pct"] = ((quote[2] - t["entry_price"])
                                   / t["entry_price"] * 100)
    try:
        acct = tc.get_account()
        account = {"equity": float(acct.equity), "cash": float(acct.cash)}
    except Exception:
        account = None
    return {"enabled": True, "account": account, "trades": trades,
            "exitAfter": EXIT_AFTER}


def print_status() -> None:
    trades = get_trades()
    if not trades:
        print("No paper trades yet.")
        return
    for t in trades:
        line = (f"{t['trigger_date']}  {t['symbol']:<6} {stars(t['tier'] or 0):<3}"
                f" {t['status']:<12}")
        if t["asset_type"] == "stock" and t["qty"]:
            line += f" {t['qty']} shares"
        elif t["contract"]:
            line += f" {t['qty']}x {t['contract']} Δ{t['delta']:.2f}"
        if t["entry_price"]:
            line += f" in {t['entry_price']:.2f}"
        if t["exit_price"]:
            line += f" out {t['exit_price']:.2f}"
        if t["pnl"] is not None:
            line += f" pnl ${t['pnl']:+,.0f} ({t['pnl_pct']:+.1f}%)"
        if t["note"]:
            line += f"  [{t['note']}]"
        print(line)


def main() -> None:
    parser = argparse.ArgumentParser(description="Alpaca paper trader for scanner signals")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be traded without placing orders")
    parser.add_argument("--force", action="store_true",
                        help="Skip the market-hours gate on entries")
    parser.add_argument("--status", action="store_true",
                        help="Print the trade ledger and exit")
    parser.add_argument("--chase", action="store_true",
                        help="Close-time pass: make still-unfilled orders"
                             " marketable (entries: 3-star only)")
    parser.add_argument("--profit-take", action="store_true",
                        help="Intraday pass: reconcile + take profits on"
                             " doubled option legs; no entries or exits")
    args = parser.parse_args()
    load_dotenv(SCRIPT_DIR / ".env")
    if args.status:
        print_status()
        return
    if args.profit_take:
        run_profit_take(dry_run=args.dry_run)
        return
    if args.chase:
        run_chase(dry_run=args.dry_run)
        return
    run(dry_run=args.dry_run, force=args.force)


if __name__ == "__main__":
    main()

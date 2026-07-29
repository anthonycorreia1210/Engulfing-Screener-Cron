#!/usr/bin/env python3
"""
Historical backtest of the body-engulfer rule
=============================================
Runs the same signal logic the live scanner uses over years of daily bars for
the whole watchlist, and stores every hit with its +1..+10 trading-day forward
returns (cumulative % from the engulfing day's close — the same definition as
the History tab, so the stats are directly comparable).

Deliberate approximations vs. the live scanner:
- "current price ~30 min before close" becomes the day's official close;
- the confirmed-open and missing-day guards need 1-minute data that doesn't
  exist historically, so they can't be applied;
- only the current watchlist is tested — tickers that delisted along the way
  aren't seen, so results skew optimistic (survivorship bias);
- flat prior days (high == low, common in stale microcaps) are skipped as
  data noise rather than counted as engulfed ranges.

Usage:
  python3 backtest.py                    # 5-year lookback, tickers.txt
  python3 backtest.py --years 10
  python3 backtest.py --tickers AAPL,TSLA

Results land in signals.db (backtest_* tables, replaced on each run) and are
shown in the web UI's Backtest tab.
"""
import argparse
import math
import sqlite3
import statistics
import sys
from datetime import datetime

import pandas as pd
import yfinance as yf

from engulfing_scanner import load_tickers
from history import DB_PATH, ET, TRACK_DAYS, _last_complete_session

CHUNK = 25  # tickers per batched yfinance download

_SCHEMA = """
CREATE TABLE IF NOT EXISTS backtest_meta (
    id        INTEGER PRIMARY KEY CHECK (id = 1),
    run_at    TEXT NOT NULL,
    years     INTEGER NOT NULL,
    n_tickers INTEGER NOT NULL,
    n_signals INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS backtest_signals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT NOT NULL,
    direction     TEXT NOT NULL CHECK (direction IN ('BULLISH', 'BEARISH')),
    trigger_date  TEXT NOT NULL,
    trigger_close REAL NOT NULL,
    today_open    REAL,
    prev_high     REAL,
    prev_low      REAL,
    body_mult     REAL,
    body_atr      REAL
);
CREATE TABLE IF NOT EXISTS backtest_performance (
    signal_id  INTEGER NOT NULL REFERENCES backtest_signals(id) ON DELETE CASCADE,
    day_offset INTEGER NOT NULL,
    date       TEXT NOT NULL,
    close      REAL NOT NULL,
    pct        REAL NOT NULL,
    PRIMARY KEY (signal_id, day_offset)
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(backtest_signals)")}
    if "vol_mult" in cols:
        # Old schema from when volume was tracked (backtested: noise, not
        # signal). Results are regenerated on every run, so just rebuild.
        with conn:
            conn.execute("DROP TABLE backtest_performance")
            conn.execute("DROP TABLE backtest_signals")
            conn.execute("DELETE FROM backtest_meta")
        conn.executescript(_SCHEMA)
    else:
        for col in ("body_mult", "body_atr"):
            if col not in cols:
                with conn:
                    conn.execute(f"ALTER TABLE backtest_signals ADD COLUMN {col} REAL")
    return conn


def find_signals(symbol: str, df) -> list[dict]:
    """
    Scan one symbol's daily bars (ascending) for body engulfers and attach
    each hit's forward closes. Offsets are positions in the symbol's own bar
    series — over multi-year daily data any lingering feed gaps are rare
    enough that rank-based offsets are fine here.
    """
    o, h = df["Open"].to_list(), df["High"].to_list()
    l, c = df["Low"].to_list(), df["Close"].to_list()
    dates = [idx.date() for idx in df.index]

    # True range per bar, for the ATR-relative strength metric.
    tr = [None] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
                   for i in range(1, len(df))]

    out = []
    for i in range(1, len(df)):
        ph, pl = h[i - 1], l[i - 1]
        vals = (o[i], c[i], ph, pl)
        if not all(math.isfinite(v) for v in vals) or pl <= 0 or ph <= pl or c[i] <= 0:
            continue
        if o[i] < pl and c[i] > ph:
            direction = "BULLISH"
        elif o[i] > ph and c[i] < pl:
            direction = "BEARISH"
        else:
            continue
        # Strength: an engulfing body is > 1x the prior range by definition;
        # near 1x is a trivial engulfer of a narrow day, 2x+ is a violent one.
        # body_atr says whether the day was big for THIS stock (20-day ATR).
        body = abs(c[i] - o[i])
        body_mult = body / (ph - pl)
        body_atr = None
        if i > 20:
            window = [t for t in tr[i - 20:i] if t is not None and math.isfinite(t)]
            if len(window) >= 15:
                atr = sum(window) / len(window)
                if atr > 0:
                    body_atr = body / atr
        days = []
        for k in range(1, TRACK_DAYS + 1):
            j = i + k
            if j >= len(df):
                break
            if not math.isfinite(c[j]):
                continue
            days.append({"day_offset": k, "date": dates[j].isoformat(),
                         "close": c[j], "pct": (c[j] - c[i]) / c[i] * 100})
        out.append({"symbol": symbol, "direction": direction,
                    "trigger_date": dates[i].isoformat(), "trigger_close": c[i],
                    "today_open": o[i], "prev_high": ph, "prev_low": pl,
                    "body_mult": body_mult, "body_atr": body_atr,
                    "days": days})
    return out


def backtest_stream(tickers: list[str], years: int):
    """
    Run the backtest, yielding one progress dict per ticker and a final
    {'type': 'done'} after results are saved — consumed by both the CLI
    and the web app's SSE stream (same pattern as scan_stream).
    """
    last_final = _last_complete_session()
    all_signals: list[dict] = []
    total, done = len(tickers), 0
    for start in range(0, total, CHUNK):
        chunk = tickers[start:start + CHUNK]
        try:
            data = yf.download(chunk, period=f"{years}y", interval="1d",
                               group_by="ticker", auto_adjust=True,
                               progress=False, threads=True)
        except Exception as exc:
            print(f"  [warn] batch fetch failed ({exc})", file=sys.stderr)
            data = None
        for sym in chunk:
            done += 1
            found = 0
            if data is not None and not data.empty:
                try:
                    df = data[sym] if isinstance(data.columns, pd.MultiIndex) else data
                    df = df.dropna(subset=["Open", "High", "Low", "Close"])
                    # Exclude any still-trading partial bar.
                    df = df[[idx.date() <= last_final for idx in df.index]]
                    if not df.empty:
                        sigs = find_signals(sym, df)
                        all_signals.extend(sigs)
                        found = len(sigs)
                except Exception as exc:
                    print(f"  [warn] {sym}: {exc}", file=sys.stderr)
            yield {"type": "progress", "index": done, "total": total,
                   "symbol": sym, "found": found}
    _save(all_signals, years, total)
    yield {"type": "done", "signals": len(all_signals), "tickers": total}


def _save(signals: list[dict], years: int, n_tickers: int) -> None:
    conn = _connect()
    try:
        with conn:
            conn.execute("DELETE FROM backtest_performance")
            conn.execute("DELETE FROM backtest_signals")
            for s in signals:
                cur = conn.execute(
                    "INSERT INTO backtest_signals"
                    " (symbol, direction, trigger_date, trigger_close,"
                    "  today_open, prev_high, prev_low, body_mult, body_atr)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (s["symbol"], s["direction"], s["trigger_date"],
                     s["trigger_close"], s["today_open"], s["prev_high"],
                     s["prev_low"], s["body_mult"], s["body_atr"]))
                conn.executemany(
                    "INSERT INTO backtest_performance VALUES (?,?,?,?,?)",
                    [(cur.lastrowid, d["day_offset"], d["date"], d["close"],
                      d["pct"]) for d in s["days"]])
            conn.execute(
                "INSERT OR REPLACE INTO backtest_meta"
                " (id, run_at, years, n_tickers, n_signals) VALUES (1,?,?,?,?)",
                (datetime.now(ET).strftime("%Y-%m-%d %H:%M ET"), years,
                 n_tickers, len(signals)))
    finally:
        conn.close()


def get_backtest() -> dict:
    """Stored backtest results, shaped like the History records so the web
    UI can reuse the same renderers."""
    conn = _connect()
    try:
        meta = conn.execute("SELECT * FROM backtest_meta WHERE id = 1").fetchone()
        if meta is None:
            return {"meta": None, "records": []}
        perf_by_sig: dict[int, list[dict]] = {}
        for p in conn.execute("SELECT * FROM backtest_performance"
                              " ORDER BY signal_id, day_offset"):
            perf_by_sig.setdefault(p["signal_id"], []).append(
                {"day_offset": p["day_offset"], "date": p["date"],
                 "close": p["close"], "pct": p["pct"]})
        records = []
        for s in conn.execute("SELECT * FROM backtest_signals"
                              " ORDER BY trigger_date DESC, symbol"):
            records.append({
                "symbol": s["symbol"],
                "direction": s["direction"],
                "trigger_date": s["trigger_date"],
                "trigger_price": s["trigger_close"],
                "trigger_close": s["trigger_close"],
                "body_mult": s["body_mult"],
                "body_atr": s["body_atr"],
                "days": perf_by_sig.get(s["id"], []),
                "complete": True,
            })
        return {"meta": dict(meta), "records": records}
    finally:
        conn.close()


def _stat_line(rs: list[dict], direction: str, offsets=(1, 3, 7, 10)) -> str:
    parts = []
    for off in offsets:
        vals = [d["pct"] for r in rs for d in r["days"] if d["day_offset"] == off]
        if not vals:
            continue
        if direction == "BULLISH":
            wins = sum(1 for v in vals if v > 0)
        else:
            wins = sum(1 for v in vals if v < 0)
        parts.append(f"+{off}d med {statistics.median(vals):+6.2f}% "
                     f"win {wins / len(vals) * 100:3.0f}%")
    return "  ".join(parts)


def _bucket_block(rs: list[dict], direction: str, key: str, title: str,
                  buckets: list[tuple], suffix: str = "",
                  offsets=(3, 10)) -> None:
    print(f"  {title}")
    for lo, hi in buckets:
        sub = [r for r in rs if r[key] is not None and lo <= r[key] < hi]
        label = (f"{lo:.1f}-{hi:.1f}{suffix}" if math.isfinite(hi)
                 else f">={lo:.1f}{suffix}")
        if not sub:
            print(f"    {label:<9} n=0")
            continue
        print(f"    {label:<9} n={len(sub):<5} {_stat_line(sub, direction, offsets)}")


def print_summary() -> None:
    recs = get_backtest()["records"]
    for direction in ("BULLISH", "BEARISH"):
        rs = [r for r in recs if r["direction"] == direction]
        print(f"\n{direction} — {len(rs)} signals")
        for off in (1, 3, 7, 10):
            vals = [d["pct"] for r in rs for d in r["days"]
                    if d["day_offset"] == off]
            if not vals:
                continue
            if direction == "BULLISH":
                wins = sum(1 for v in vals if v > 0)
            else:
                wins = sum(1 for v in vals if v < 0)
            print(f"  +{off:>2}d  med {statistics.median(vals):+7.2f}%"
                  f"  avg {statistics.mean(vals):+7.2f}%"
                  f"  win {wins / len(vals) * 100:3.0f}%  n={len(vals)}")
        _bucket_block(rs, direction, "body_mult", "by body x prior range:",
                      [(1.0, 1.5), (1.5, 2.0), (2.0, 3.0), (3.0, math.inf)], "x")
        _bucket_block(rs, direction, "body_atr", "by body / ATR20:",
                      [(0, 1.0), (1.0, 1.5), (1.5, 2.0), (2.0, math.inf)])


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest the engulfing rule")
    parser.add_argument("--years", type=int, default=5,
                        help="Lookback in years (default 5)")
    parser.add_argument("--tickers",
                        help="Comma-separated symbols (overrides tickers.txt)")
    args = parser.parse_args()

    tickers = load_tickers(args.tickers)
    print(f"Backtesting {len(tickers)} tickers over {args.years}y…")
    for ev in backtest_stream(tickers, args.years):
        if ev["type"] == "progress":
            mark = f"  <- {ev['found']} signal(s)" if ev["found"] else ""
            print(f"  [{ev['index']}/{ev['total']}] {ev['symbol']}{mark}")
        elif ev["type"] == "done":
            print(f"\n{ev['signals']} signals across {ev['tickers']} tickers.")
    print_summary()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Signal history & forward performance (SQLite)
=============================================
Records every confirmed signal (one per symbol per trigger day) in signals.db
and tracks the cumulative % change from the trigger price at each subsequent
trading-day close, up to +10 trading days (~2 calendar weeks). After that the
signal is complete and no longer updated.

Used by the cron scanner (records hits + refreshes performance after each run)
and by the web app's History tab (refreshes lazily on load). Percentages are
measured from the trigger day's OFFICIAL CLOSE (so +1d matches the next day's
move as charted), not from the ~3:30 PM print the alert fired at — that print
is kept as trigger_price for reference only. The close isn't known when the
signal is recorded, so trigger_close is filled in by the first performance
update afterward.
"""
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yfinance as yf

ET = ZoneInfo("America/New_York")
# SIGNALS_DB overrides for deployments where the DB lives on a mounted volume.
DB_PATH = Path(os.environ.get("SIGNALS_DB") or Path(__file__).resolve().parent / "signals.db")
TRACK_DAYS = 10  # trading days ~= 2 calendar weeks

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT NOT NULL,
    direction     TEXT NOT NULL CHECK (direction IN ('BULLISH', 'BEARISH')),
    trigger_date  TEXT NOT NULL,
    trigger_price REAL NOT NULL,
    today_open    REAL,
    prev_high     REAL,
    prev_low      REAL,
    prev_open     REAL,
    prev_close    REAL,
    UNIQUE (symbol, trigger_date)
);
CREATE TABLE IF NOT EXISTS performance (
    signal_id  INTEGER NOT NULL REFERENCES signals(id) ON DELETE CASCADE,
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
    cols = {r[1] for r in conn.execute("PRAGMA table_info(signals)")}
    for col in ("trigger_close", "body_mult", "body_atr"):
        if col not in cols:
            with conn:
                conn.execute(f"ALTER TABLE signals ADD COLUMN {col} REAL")
    if "vol_mult" in cols:  # removed metric — the backtest showed only noise
        with conn:
            conn.execute("ALTER TABLE signals DROP COLUMN vol_mult")
    return conn


def record_signal(symbol: str, direction: str, d: dict,
                  trigger_date: str | None = None) -> bool:
    """
    Record a confirmed signal. Idempotent per (symbol, trigger date) so the
    cron and manual web runs on the same day can't double-record.
    Returns True if a new row was inserted.
    """
    trigger_date = trigger_date or datetime.now(ET).date().isoformat()
    conn = _connect()
    try:
        with conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO signals"
                " (symbol, direction, trigger_date, trigger_price,"
                "  today_open, prev_high, prev_low, prev_open, prev_close,"
                "  body_mult, body_atr)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (symbol.upper(), direction, trigger_date, d["current_price"],
                 d.get("today_open"), d.get("prev_high"), d.get("prev_low"),
                 d.get("prev_open"), d.get("prev_close"), d.get("body_mult"),
                 d.get("body_atr")))
            return cur.rowcount > 0
    finally:
        conn.close()


def _get_meta(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _set_meta(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta (key, value) VALUES (?,?)"
                 " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, value))


def _weekdays_between(start: date, end: date) -> int:
    """Weekdays strictly after `start` through `end`. Holidays count as trading
    days here — close enough for a "has this gone quiet?" check."""
    n, d = 0, start + timedelta(days=1)
    while d <= end:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def dry_spell_warning(threshold: int) -> str | None:
    """
    A message when the scanner has gone `threshold` trading days without a
    single signal, else None.

    Fires ONCE per dry spell: the last signal's date is stored as an anchor
    when we warn, so the streak has to be broken by a real signal before
    another warning is possible. Without that anchor this would nag daily for
    the rest of the drought, which is exactly the noise a heartbeat is
    supposed to avoid.
    """
    conn = _connect()
    try:
        row = conn.execute("SELECT symbol, trigger_date FROM signals"
                           " ORDER BY trigger_date DESC LIMIT 1").fetchone()
        if not row:
            return None  # nothing recorded ever — no baseline to judge against
        last_date = date.fromisoformat(row["trigger_date"])
        streak = _weekdays_between(last_date, datetime.now(ET).date())
        if streak < threshold:
            return None
        if _get_meta(conn, "dry_spell_anchor") == row["trigger_date"]:
            return None  # already warned for this spell
        with conn:
            _set_meta(conn, "dry_spell_anchor", row["trigger_date"])
        return (f"\U0001F437 BullPiggy — scanner heartbeat\n\n"
                f"No engulfing signals in {streak} trading days.\n"
                f"Last one: {row['symbol']} on {row['trigger_date']}.\n\n"
                f"The scan itself is running fine — flagging it because a gap "
                f"this long is unusual and worth an eyeball.")
    finally:
        conn.close()


def signals_on(trigger_date: str) -> set[tuple[str, str]]:
    """(symbol, direction) pairs already recorded for a given trigger date —
    lets the spot-check crons alert only on what changed since the last run."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT symbol, direction FROM signals WHERE trigger_date = ?",
            (trigger_date,))
        return {(r["symbol"], r["direction"]) for r in rows}
    finally:
        conn.close()


def _last_complete_session() -> date:
    """
    Most recent weekday whose close is final (today only after 4 PM ET).
    Approximate — a holiday here just means one extra no-op fetch, never
    wrong data, because only real daily bars are ever stored.
    """
    now = datetime.now(ET)
    d = now.date()
    if now.weekday() >= 5 or now.hour * 60 + now.minute < 16 * 60:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _trading_calendar(start: str, last_final: date) -> list[date]:
    """
    The trading-day calendar, taken from SPY's daily bars (the most liquid,
    reliable feed). Used to label each close with its true trading-day offset
    even when a symbol's own daily feed is missing a session — Yahoo dropped
    2026-07-24 for PTEN but not LLY, which would silently shift rank-based
    offsets by a day. With a calendar, a missing bar leaves a visible hole
    instead, and heals in place if the bar shows up later.
    """
    daily = yf.Ticker("SPY").history(start=start, interval="1d")
    return [idx.date() for idx in daily.index if idx.date() <= last_final]


def update_performance() -> int:
    """
    Fill in missing forward-performance rows for every signal that hasn't
    reached its +10-trading-day window end. One daily-history fetch per
    symbol, and only for symbols that can actually have new completed closes.
    Only bars from fully closed sessions are stored (today's partial bar is
    skipped while the market is open). Returns the number of rows added.
    """
    last_final = _last_complete_session()
    added = 0
    conn = _connect()
    try:
        pending = conn.execute(
            "SELECT s.id, s.symbol, s.trigger_date, s.trigger_price, s.trigger_close,"
            "       MAX(p.date) AS last_perf_date"
            " FROM signals s LEFT JOIN performance p ON p.signal_id = s.id"
            " GROUP BY s.id HAVING COALESCE(MAX(p.day_offset), 0) < ?",
            (TRACK_DAYS,)).fetchall()

        by_symbol: dict[str, list[sqlite3.Row]] = {}
        for r in pending:
            # Skip when no new completed close can exist yet.
            newest = r["last_perf_date"] or r["trigger_date"]
            if date.fromisoformat(newest) >= last_final:
                continue
            by_symbol.setdefault(r["symbol"], []).append(r)
        if not by_symbol:
            return 0

        earliest = min(r["trigger_date"] for rows in by_symbol.values() for r in rows)
        calendar = _trading_calendar(earliest, last_final)
        if not calendar:
            return 0

        for symbol, rows in by_symbol.items():
            try:
                daily = yf.Ticker(symbol).history(
                    start=min(r["trigger_date"] for r in rows), interval="1d")
            except Exception as exc:
                print(f"  [warn] {symbol}: perf fetch failed ({exc})", file=sys.stderr)
                continue
            if daily.empty:
                continue
            closes = {idx.date(): float(c)
                      for idx, c in zip(daily.index, daily["Close"])}
            for r in rows:
                trig = date.fromisoformat(r["trigger_date"])
                # Baseline is the trigger day's official close, resolved from
                # the first fetch after the signal fired. If Yahoo permanently
                # lacks that bar, fall back to the alert-time price once so
                # the record stays internally consistent.
                base = r["trigger_close"]
                if base is None:
                    base = closes.get(trig)
                    if base is None:
                        base = r["trigger_price"]
                        print(f"  [warn] {symbol}: no {trig} close, using "
                              f"alert price as baseline", file=sys.stderr)
                    with conn:
                        conn.execute("UPDATE signals SET trigger_close = ?"
                                     " WHERE id = ?", (base, r["id"]))
                window = [d for d in calendar if d > trig][:TRACK_DAYS]
                with conn:
                    for offset, d in enumerate(window, 1):
                        if d not in closes:
                            continue
                        pct = (closes[d] - base) / base * 100
                        cur = conn.execute(
                            "INSERT OR IGNORE INTO performance VALUES (?,?,?,?,?)",
                            (r["id"], offset, d.isoformat(), closes[d], pct))
                        added += cur.rowcount
    finally:
        conn.close()
    return added


def get_history() -> list[dict]:
    """All recorded signals, newest first, each with its performance rows."""
    conn = _connect()
    try:
        sigs = conn.execute(
            "SELECT * FROM signals ORDER BY trigger_date DESC, symbol").fetchall()
        out = []
        for s in sigs:
            perf = conn.execute(
                "SELECT day_offset, date, close, pct FROM performance"
                " WHERE signal_id = ? ORDER BY day_offset", (s["id"],)).fetchall()
            out.append({
                "symbol": s["symbol"],
                "direction": s["direction"],
                "trigger_date": s["trigger_date"],
                "trigger_price": s["trigger_price"],
                "trigger_close": s["trigger_close"],
                "body_mult": s["body_mult"],
                "body_atr": s["body_atr"],
                "days": [dict(p) for p in perf],
                # Window is over once the final offset is filled; count can't
                # be used — a permanently missing bar would leave it short.
                "complete": any(p["day_offset"] >= TRACK_DAYS for p in perf),
            })
        return out
    finally:
        conn.close()

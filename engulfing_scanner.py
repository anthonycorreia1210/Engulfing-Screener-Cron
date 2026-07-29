#!/usr/bin/env python3
"""
Engulfing Candle Scanner
========================
Run ~30 minutes before the market close (3:30 PM ET) via cron.

Logic (per ticker) — a "body engulfer": today's real body clears the
previous day's entire range (both open and close sit beyond both extremes):
    BULLISH: today's open  < prev LOW   AND  current price > prev HIGH
    BEARISH: today's open  > prev HIGH  AND  current price < prev LOW
  ("current price" is the last print ~30 min before close.)

Alerts are printed to stdout and optionally sent via email (SMTP)
and/or Telegram, configured through environment variables or a .env file.

Usage:
  python3 engulfing_scanner.py                 # uses tickers.txt
  python3 engulfing_scanner.py --tickers AAPL,TSLA,NVDA
  python3 engulfing_scanner.py --dry-run       # skip market-hours check
"""

import argparse
import os
import re
import smtplib
import sys
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

import yfinance as yf

import history

ET = ZoneInfo("America/New_York")
SCRIPT_DIR = Path(__file__).resolve().parent


# ----------------------------------------------------------------------
# Config helpers
# ----------------------------------------------------------------------
def load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines, # comments)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def load_tickers(cli_tickers: str | None) -> list[str]:
    if cli_tickers:
        return [t.strip().upper() for t in cli_tickers.split(",") if t.strip()]
    tickers_file = SCRIPT_DIR / "tickers.txt"
    if not tickers_file.exists():
        sys.exit("No tickers given. Create tickers.txt (one symbol per line) "
                 "or use --tickers AAPL,MSFT,...")
    return [
        line.strip().upper()
        for line in tickers_file.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def load_tickers_by_sector() -> dict[str, list[str]]:
    """
    Parse tickers.txt into {sector: [symbols]}, preserving file order.
    Sector headers look like:  # --- Technology (67) ---
    (the count is ignored; the actual list is what matters). Symbols above
    the first header are grouped under 'Other'.
    """
    tickers_file = SCRIPT_DIR / "tickers.txt"
    if not tickers_file.exists():
        return {}
    sectors: dict[str, list[str]] = {}
    current = "Other"
    for line in tickers_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"#\s*---\s*(.+?)\s*(?:\(\d+\))?\s*---", line)
        if m:
            current = m.group(1)
            sectors.setdefault(current, [])
            continue
        if line.startswith("#"):
            continue
        sectors.setdefault(current, []).append(line.upper())
    return {s: t for s, t in sectors.items() if t}


# ----------------------------------------------------------------------
# Market data
# ----------------------------------------------------------------------
def _weekdays_between(d1, d2) -> int:
    """Count weekdays strictly between two dates (exclusive of both ends)."""
    n = 0
    d = d1 + timedelta(days=1)
    while d < d2:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def _prev_session_from_intraday(tkr, scan_date) -> dict | None:
    """
    Reconstruct the OHLC of the most recent completed regular-hours session
    strictly before scan_date, from 1-minute data (which yfinance retains ~7
    days even when a daily bar goes missing). Returns None if unavailable.
    """
    try:
        intr = tkr.history(period="7d", interval="1m", prepost=False)
        if intr.empty:
            return None
        rth = intr.between_time("09:30", "16:00")
        if rth.empty:
            return None
        sessions = [(d, g) for d, g in rth.groupby(rth.index.date) if d < scan_date]
        if not sessions:
            return None
        d, g = sessions[-1]
        return {
            "date": d,
            "high": float(g["High"].max()),
            "low": float(g["Low"].min()),
            "open": float(g["Open"].iloc[0]),
            "close": float(g["Close"].iloc[-1]),
        }
    except Exception:
        return None


def get_candle_data(symbol: str) -> dict | None:
    """
    Returns dict with prev_{high,low,open,close}, today_open, current_price
    or None if data is unavailable/incomplete.
    """
    try:
        tkr = yf.Ticker(symbol)

        # Daily bars: last row is today's partial bar during market hours.
        # 3 months so there are ~20 completed bars for the ATR baseline.
        daily = tkr.history(period="3mo", interval="1d")
        if daily.empty or len(daily) < 2:
            return None

        today_et = datetime.now(ET).date()
        last_bar_date = daily.index[-1].date()

        if last_bar_date == today_et:
            today_bar = daily.iloc[-1]
            prev_bar = daily.iloc[-2]
            prev_date = daily.index[-2].date()
            today_open = float(today_bar["Open"])
            completed = daily.iloc[:-1]
        else:
            # Today's daily bar not present yet; get open from 1-min data.
            prev_bar = daily.iloc[-1]
            prev_date = last_bar_date
            intraday = tkr.history(period="1d", interval="1m")
            if intraday.empty:
                return None
            today_open = float(intraday.iloc[0]["Open"])
            completed = daily

        # 20-day ATR from completed bars, for the conviction ranking
        # (see conviction_tier).
        atr20 = None
        h = completed["High"].to_list()
        l = completed["Low"].to_list()
        c = completed["Close"].to_list()
        trs = [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
               for i in range(1, len(completed))][-20:]
        if len(trs) >= 15:
            atr20 = sum(trs) / len(trs)

        prev_high = float(prev_bar["High"])
        prev_low = float(prev_bar["Low"])
        prev_open = float(prev_bar["Open"])
        prev_close = float(prev_bar["Close"])

        # Yahoo occasionally drops a whole trading day from the DAILY feed
        # (2026-07-24 went missing market-wide), which silently anchors the
        # scan to the wrong "previous day". If a weekday sits between prev_date
        # and today, reconstruct the previous session from 1-minute data and
        # prefer it when it proves a more recent session actually traded.
        # Holidays self-correct: the gap day has no intraday, so nothing wins.
        if _weekdays_between(prev_date, today_et) > 0:
            recon = _prev_session_from_intraday(tkr, today_et)
            if recon and recon["date"] > prev_date:
                print(f"  [gap-fix] {symbol}: daily feed skipped {recon['date']}; "
                      f"using it as prev day (was {prev_date})", file=sys.stderr)
                prev_date = recon["date"]
                prev_high, prev_low = recon["high"], recon["low"]
                prev_open, prev_close = recon["open"], recon["close"]

        # Current price: prefer fast_info, fall back to last 1-min close.
        current_price = None
        try:
            current_price = float(tkr.fast_info["last_price"])
        except Exception:
            pass
        if not current_price:
            intraday = tkr.history(period="1d", interval="1m")
            if intraday.empty:
                return None
            current_price = float(intraday.iloc[-1]["Close"])

        return {
            "prev_high": prev_high,
            "prev_low": prev_low,
            "prev_open": prev_open,
            "prev_close": prev_close,
            "today_open": today_open,
            "current_price": current_price,
            "atr20": atr20,
        }
    except Exception as exc:
        print(f"  [warn] {symbol}: data error ({exc})", file=sys.stderr)
        return None


def get_confirmed_open(symbol: str) -> float | None:
    """
    Yahoo's daily Open sometimes carries a pre-open print rather than the
    official opening cross (NOVT, 2026-07-20: reported 150.34, actual 148.97).
    Re-derive it as the close of the first regular-hours minute — the price one
    minute into the session — which a stray opening tick can't survive.
    Returns None if 1-min data is unavailable.
    """
    try:
        intraday = yf.Ticker(symbol).history(period="1d", interval="1m", prepost=False)
        if intraday.empty:
            return None
        rth = intraday.between_time("09:30", "16:00")
        if rth.empty:
            return None
        return float(rth.iloc[0]["Close"])
    except Exception as exc:
        print(f"  [warn] {symbol}: confirm-open failed ({exc})", file=sys.stderr)
        return None


def check_engulfing(d: dict) -> str | None:
    """
    Returns 'BEARISH' | 'BULLISH' | None.

    A body engulfer: today's real body clears the previous day's entire range —
    both open and close sit beyond both of yesterday's extremes.
      BULLISH: today's open < prev LOW  AND  current price > prev HIGH
      BEARISH: today's open > prev HIGH AND  current price < prev LOW
    """
    o, c = d["today_open"], d["current_price"]
    ph, pl = d["prev_high"], d["prev_low"]

    if o < pl and c > ph:
        return "BULLISH"
    if o > ph and c < pl:
        return "BEARISH"
    return None


def apply_strength(d: dict) -> None:
    """
    Attach conviction metrics to a candle dict (in place):
      body_mult  today's body / previous day's range (>1 by definition here)
      body_atr   today's body / 20-day ATR
    (Volume was tested in the backtest and removed — it added noise, not
    signal, and its apparent extremes flipped sign at longer horizons.)
    """
    body = abs(d["current_price"] - d["today_open"])
    rng = d["prev_high"] - d["prev_low"]
    d["body_mult"] = body / rng if rng > 0 else None
    d["body_atr"] = body / d["atr20"] if d.get("atr20") else None


def conviction_tier(d: dict) -> int:
    """
    0-3 ranking from the 5y backtest (validated on BULLISH signals; bearish
    gets the same yardstick for consistency but showed no edge at any tier):
      3  body >= 3x prior range AND >= 2x ATR20   (+10d med +2.3%, win 64%)
      2  body >= 2x prior range                   (+10d med +0.7%, win 55%)
      1  body >= 1.5x prior range
      0  a trivial engulfer of a narrow day — no edge in the backtest
    """
    bm, ba = d.get("body_mult"), d.get("body_atr")
    if not bm:
        return 0
    if bm >= 3 and (ba or 0) >= 2:
        return 3
    if bm >= 2:
        return 2
    if bm >= 1.5:
        return 1
    return 0


def stars(tier: int) -> str:
    return "★" * tier


# ----------------------------------------------------------------------
# Scan core (shared by the CLI and the web app)
# ----------------------------------------------------------------------
def scan_stream(tickers: list[str], confirm: bool = True):
    """
    Yield one result dict per ticker as it is scanned, so callers can stream
    progress. This is the single source of truth for the scan loop — both the
    CLI (main) and the web app consume it.

    Each yielded dict:
      index    1-based position
      total    len(tickers)
      symbol   the ticker
      signal   'BULLISH' | 'BEARISH' | None
      data     candle dict (with confirmed open applied) or None if no data
      rejected None, or {'confirmed': float, 'daily': float} when a raw hit
               was overturned by the confirmed open
    """
    total = len(tickers)
    for i, symbol in enumerate(tickers, 1):
        d = get_candle_data(symbol)
        signal = None
        rejected = None
        if d is not None:
            signal = check_engulfing(d)
            # Yahoo's daily Open can be a pre-open print; re-test any hit
            # against the confirmed open before reporting it.
            if signal and confirm:
                confirmed = get_confirmed_open(symbol)
                if confirmed is not None:
                    if check_engulfing({**d, "today_open": confirmed}) == signal:
                        d = {**d, "today_open": confirmed}
                    else:
                        rejected = {"confirmed": confirmed, "daily": d["today_open"]}
                        signal = None
            if signal:
                apply_strength(d)
        yield {"index": i, "total": total, "symbol": symbol,
               "signal": signal, "data": d, "rejected": rejected}


# ----------------------------------------------------------------------
# Alerting
# ----------------------------------------------------------------------
def format_alert(symbol: str, direction: str, d: dict) -> str:
    tier = conviction_tier(d)
    prefix = f"{stars(tier)} " if tier else ""
    strength = ""
    if d.get("body_mult"):
        strength = f"   Body: {d['body_mult']:.1f}× prior range"
        if d.get("body_atr"):
            strength += f" | {d['body_atr']:.1f}× ATR20"
        strength += "\n"
    return (
        f"{prefix}{'🔴' if direction == 'BEARISH' else '🟢'} {direction} Engulfing on {symbol}\n"
        f"{strength}"
        f"   Prev O/C: {d['prev_open']:.2f}/{d['prev_close']:.2f} | "
        f"Prev H/L: {d['prev_high']:.2f}/{d['prev_low']:.2f}\n"
        f"   Today Open: {d['today_open']:.2f} | Now: {d['current_price']:.2f}"
    )


def send_email(subject: str, body: str) -> None:
    host = os.environ.get("SMTP_HOST")
    if not host:
        return
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    to_addr = os.environ.get("ALERT_EMAIL", user)

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr

    with smtplib.SMTP(host, port) as server:
        server.starttls()
        server.login(user, password)
        server.send_message(msg)
    print(f"[email sent to {to_addr}]")


def send_ntfy(subject: str, body: str) -> None:
    """Push notification via ntfy.sh — just set NTFY_TOPIC in .env."""
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    import urllib.request

    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}",
        data=body.encode("utf-8"),
        headers={
            "Title": subject,
            "Priority": "high",
            "Tags": "chart_with_upwards_trend",
        },
    )
    urllib.request.urlopen(req, timeout=10)
    print("[ntfy push sent]")


def send_telegram(body: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return
    import urllib.parse
    import urllib.request

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": body}).encode()
    urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10)
    print("[telegram sent]")


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def is_market_open_window() -> bool:
    """True on weekdays between 9:30 AM and 4:00 PM ET."""
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= minutes <= 16 * 60


def main() -> None:
    parser = argparse.ArgumentParser(description="Engulfing candle scanner")
    parser.add_argument("--tickers", help="Comma-separated symbols (overrides tickers.txt)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run even outside market hours (for testing)")
    args = parser.parse_args()

    load_dotenv(SCRIPT_DIR / ".env")

    if not args.dry_run and not is_market_open_window():
        print("Market is closed (or it's a weekend). Use --dry-run to test anyway.")
        return

    tickers = load_tickers(args.tickers)
    now_str = datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")
    print(f"Scanning {len(tickers)} tickers at {now_str}\n")

    hits = []
    for r in scan_stream(tickers):
        d, signal, symbol = r["data"], r["signal"], r["symbol"]
        if d is None:
            continue
        note = ""
        if r["rejected"]:
            note = (f"  [rejected: confirmed open {r['rejected']['confirmed']:.2f}, "
                    f"daily bar said {r['rejected']['daily']:.2f}]")
        status = signal if signal else "no signal"
        if signal and d.get("body_mult"):
            status += f" {stars(conviction_tier(d))} ({d['body_mult']:.1f}×R)"
        print(f"  {symbol:<6} open {d['today_open']:.2f}  now {d['current_price']:.2f}  "
              f"prevH {d['prev_high']:.2f}  prevL {d['prev_low']:.2f}  -> {status}{note}")
        if signal:
            hits.append((symbol, signal, d))

    # Highest conviction first: tier, then raw body multiple.
    hits.sort(key=lambda t: (conviction_tier(t[2]), t[2].get("body_mult") or 0),
              reverse=True)
    alerts = [format_alert(sym, sig, d) for sym, sig, d in hits]

    if alerts:
        body = f"Engulfing Scanner — {now_str}\n\n" + "\n\n".join(alerts)
        print("\n" + "=" * 50 + "\nALERTS\n" + "=" * 50 + "\n" + body)
        subject = f"Engulfing Alert: {len(alerts)} signal(s)"
        send_email(subject, body)
        send_ntfy(subject, body)
        send_telegram(body)
    else:
        print("\nNo engulfing setups found.")

    # Persist today's confirmed signals and refresh forward performance for
    # earlier ones. Real runs only — dry-run can fire on stale, closed-market
    # prices that would pollute the record.
    if not args.dry_run:
        try:
            for symbol, signal, d in hits:
                history.record_signal(symbol, signal, d)
            n = history.update_performance()
            if n:
                print(f"[history] {n} performance row(s) added")
        except Exception as exc:
            print(f"  [warn] history update failed ({exc})", file=sys.stderr)


if __name__ == "__main__":
    main()

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
import smtplib
import sys
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

import yfinance as yf

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


# ----------------------------------------------------------------------
# Market data
# ----------------------------------------------------------------------
def get_candle_data(symbol: str) -> dict | None:
    """
    Returns dict with prev_{high,low,open,close}, today_open, current_price
    or None if data is unavailable/incomplete.
    """
    try:
        tkr = yf.Ticker(symbol)

        # Daily bars: last row is today's partial bar during market hours.
        daily = tkr.history(period="10d", interval="1d")
        if daily.empty or len(daily) < 2:
            return None

        today_et = datetime.now(ET).date()
        last_bar_date = daily.index[-1].date()

        if last_bar_date == today_et:
            today_bar = daily.iloc[-1]
            prev_bar = daily.iloc[-2]
            today_open = float(today_bar["Open"])
        else:
            # Today's daily bar not present yet; get open from 1-min data.
            prev_bar = daily.iloc[-1]
            intraday = tkr.history(period="1d", interval="1m")
            if intraday.empty:
                return None
            today_open = float(intraday.iloc[0]["Open"])

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
            "prev_high": float(prev_bar["High"]),
            "prev_low": float(prev_bar["Low"]),
            "prev_open": float(prev_bar["Open"]),
            "prev_close": float(prev_bar["Close"]),
            "today_open": today_open,
            "current_price": current_price,
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


# ----------------------------------------------------------------------
# Alerting
# ----------------------------------------------------------------------
def format_alert(symbol: str, direction: str, d: dict) -> str:
    return (
        f"{'🔴' if direction == 'BEARISH' else '🟢'} {direction} Engulfing on {symbol}\n"
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

    alerts = []
    for symbol in tickers:
        d = get_candle_data(symbol)
        if d is None:
            continue
        signal = check_engulfing(d)

        # Yahoo's daily Open can be a pre-open print; re-test any hit against
        # the confirmed open before alerting.
        note = ""
        if signal:
            confirmed = get_confirmed_open(symbol)
            if confirmed is not None:
                if check_engulfing({**d, "today_open": confirmed}) == signal:
                    d = {**d, "today_open": confirmed}
                else:
                    note = (f"  [rejected: confirmed open {confirmed:.2f}, "
                            f"daily bar said {d['today_open']:.2f}]")
                    signal = None

        status = signal if signal else "no signal"
        print(f"  {symbol:<6} open {d['today_open']:.2f}  now {d['current_price']:.2f}  "
              f"prevH {d['prev_high']:.2f}  prevL {d['prev_low']:.2f}  -> {status}{note}")
        if signal:
            alerts.append(format_alert(symbol, signal, d))

    if alerts:
        body = f"Engulfing Scanner — {now_str}\n\n" + "\n\n".join(alerts)
        print("\n" + "=" * 50 + "\nALERTS\n" + "=" * 50 + "\n" + body)
        subject = f"Engulfing Alert: {len(alerts)} signal(s)"
        send_email(subject, body)
        send_ntfy(subject, body)
        send_telegram(body)
    else:
        print("\nNo engulfing setups found.")


if __name__ == "__main__":
    main()

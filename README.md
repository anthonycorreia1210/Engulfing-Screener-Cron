# Engulfing Candle Scanner

Scans a watchlist ~30 minutes before the close for **body engulfers** — days
where today's *real body* clears the previous day's entire range, so both the
open and the current price sit beyond both of yesterday's extremes:

- **Bullish:** today's open < previous day's *low*, AND current price > previous day's *high*
- **Bearish:** today's open > previous day's *high*, AND current price < previous day's *low*

## Setup

```bash
pip install -r requirements.txt
```

Edit `tickers.txt` with your watchlist (one symbol per line).

Optional — for email, ntfy, or Telegram alerts:

```bash
cp .env.example .env
# fill in SMTP, ntfy topic, and/or Telegram credentials
```

## Web UI

A small Flask app wraps the scanner so you can run/re-run it from a browser and
watch results stream in live:

```bash
python3 app.py
# open http://127.0.0.1:5001
```

Click **Run scan** to sweep the full `tickers.txt` watchlist, or type a
comma-separated list into the box to scan just those. Manual runs ignore the
market-hours gate (that gate is only for the unattended cron), so you can re-run
any time — a badge notes when the market is closed. Bullish hits show 🟢, bearish
🔴, and any names overturned by the confirmed-open check are listed in a
collapsible section. The scan loop is shared with the CLI (`scan_stream()` in
`engulfing_scanner.py`), so the web app and cron can never drift apart.

## Test it

```bash
python3 engulfing_scanner.py --dry-run
python3 engulfing_scanner.py --dry-run --tickers AAPL,TSLA
```

`--dry-run` skips the market-hours check so you can test on weekends/evenings.
Note: outside market hours the "current price" is just the last close, so
signals during a dry run aren't meaningful — it's only for checking that
data and alerts flow correctly.

## Cron (runs at 3:30 PM ET, Mon–Fri)

The script itself checks that the market is open, so extra fires are harmless.

If your machine is on **Pacific time** (3:30 PM ET = 12:30 PM PT):

```cron
30 12 * * 1-5 /usr/bin/python3 /path/to/engulfing/engulfing_scanner.py >> /path/to/engulfing/scanner.log 2>&1
```

If your machine is on **Eastern time**:

```cron
30 15 * * 1-5 /usr/bin/python3 /path/to/engulfing/engulfing_scanner.py >> /path/to/engulfing/scanner.log 2>&1
```

Caveat: cron uses your machine's local clock, which doesn't track DST
transitions relative to ET automatically if you're in a different zone.
The PT line above works year-round since both zones shift on the same dates.
Best practice: also add a second cron entry one hour earlier/later if your
zone doesn't observe DST (e.g., Arizona).

Install with `crontab -e`, paste the line, save.

**Or use the checked-in `crontab.txt`** (source of truth for this machine — absolute
paths, since cron has no PATH or cwd):

```bash
crontab crontab.txt   # install / update
crontab -l            # verify
```

If you move the project, update the paths in `crontab.txt` and re-run `crontab crontab.txt`.
A path mismatch fails silently — cron can't even write `scanner.log` if its directory
doesn't exist, so there's no error trail. Confirm a run with `tail scanner.log`.

## Notes

- Data comes from Yahoo Finance via `yfinance` (free, no API key). Quotes can
  be delayed slightly; for a 30-minutes-before-close scan this is fine.
- **Confirmed open.** Yahoo's daily `Open` sometimes carries a pre-open print
  instead of the official opening cross, which can fake a gap and fire a false
  signal. (NOVT on 2026-07-20: Yahoo said the open was 150.34 — equal to the
  day's high, and right on the pre-market drift of 151.68 → 150.46 — while the
  real open was 148.97.) So any ticker that passes the screen is re-tested
  against a *confirmed* open: the close of the first regular-hours minute, i.e.
  the price one minute into the session. A stray opening tick doesn't survive
  that; a genuine gap still does. Rejections are logged with both values, and
  the extra 1-min fetch only happens for tickers that already passed.
- Early-close days (day after Thanksgiving, Christmas Eve, etc.) close at
  1:00 PM ET — the cron will fire after the close on those days and simply
  report against the final prices. Add a 10:30 AM PT entry if you want to
  cover them.
- Alerts are marked 🟢 for bullish and 🔴 for bearish.

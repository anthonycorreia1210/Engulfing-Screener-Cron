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

Click **Run scan** to sweep the full `tickers.txt` watchlist, or check one or
more sector chips (parsed from the `# --- Sector ---` headers in `tickers.txt`)
to scan just those groups. Manual runs ignore the
market-hours gate (that gate is only for the unattended cron), so you can re-run
any time — a badge notes when the market is closed. Bullish hits show 🟢, bearish
🔴, and any names overturned by the confirmed-open check are listed in a
collapsible section. The scan loop is shared with the CLI (`scan_stream()` in
`engulfing_scanner.py`), so the web app and cron can never drift apart.

## History tab

Every confirmed signal is recorded in `signals.db` (SQLite, gitignored) — once
per symbol per trigger day, whether it fired from the cron or a market-hours
web scan. The **History** tab shows each signal's forward performance: the
cumulative % change from the *engulfing day's official close* to each
subsequent trading day's close, for **10 trading days** (~2 weeks), after
which the record is frozen — so +1d matches the next day's move as charted.
(A +20d horizon was tested in the backtest and removed: the edge is fully
earned within ~10 trading days, and days 10–20 added only market drift.)
The alert-time price is kept as *Entry* for reference (the close isn't known
yet when the 3:30 PM scan records the signal; it's filled in by the next
performance update). A *Live* column shows the current move for
still-tracking signals while the market is open.

The table toggles between **Cumulative** (running change since the trigger)
and **Daily** (each day's individual move) views. Overview tiles up top show
the typical cumulative move at +1/+3/+7/+10 days, split bullish/bearish —
the headline is the **median** (robust to outliers like a microcap's −30%
run) with the raw average and sample size beside it.

Performance rows update lazily — the daily cron tops them up after each scan,
and loading the History tab fills in anything missed. Trading-day offsets are
labeled against SPY's calendar, so when Yahoo's daily feed drops a session for
a symbol (2026-07-24 is still missing for PTEN) the row shows a gap at that
offset instead of silently shifting every later column; the gap heals in place
if the bar ever appears. Closed-market manual scans are shown in the UI but
never recorded, since their prices are stale.

## Backtest

The **Backtest** tab (or `python3 backtest.py --years 5`) runs the same rule
over years of daily bars for the whole watchlist and shows the same tiles and
table, with hundreds-to-thousands of samples instead of a trickle. Results are
stored in `signals.db` (`backtest_*` tables, replaced each run) and kept
separate from the live record. Overview tiles (both tabs) show median, average,
win rate, and sample size at +1/+3/+7/+10 days.

Honest caveats, also shown in the tab: the alert-time price becomes the
official close, the confirmed-open/missing-day guards can't be applied
historically, flat prior days (high = low) are skipped as microcap noise, and
only the current watchlist is tested — delisted names are invisible, so
results skew optimistic.

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

## Deployment (bullpiggy.com)

The site is served **from this Mac** via a Cloudflare Tunnel — Cloudflare is
the front door (DNS, TLS, auth), not the host. If the Mac is asleep or off,
the site is down. Set System Settings → Energy → prevent sleep on power for
reliability, or migrate the whole stack to a small VPS for true 24/7.

Architecture: browser → Cloudflare edge (bullpiggy.com, TLS, Access login) →
`cloudflared` tunnel → Flask app on `127.0.0.1:5001`.

Pieces (all outside this repo):

- **DNS**: bullpiggy.com zone on Cloudflare (registrar: GoDaddy, nameservers
  `elly`/`fred.ns.cloudflare.com`). Apex is a CNAME to the tunnel; `www` is a
  CNAME to the apex.
- **Tunnel**: named `bullpiggy`. Config in `~/.cloudflared/config.yml`
  (ingress: apex + www → `http://127.0.0.1:5001`), credentials JSON alongside
  it. Manage with `cloudflared tunnel list|info|route`.
- **Auto-start**: two LaunchAgents in `~/Library/LaunchAgents/`:
  - `com.bullpiggy.app.plist` — runs `venv/bin/python app.py`
  - `com.bullpiggy.tunnel.plist` — runs `cloudflared tunnel run bullpiggy`
  Both RunAtLoad + KeepAlive; logs in `~/Library/Logs/bullpiggy-*.log`.
  Restart one with `launchctl kickstart -k gui/$UID/com.bullpiggy.app`.
  (Don't use `brew services start cloudflared` — it launches cloudflared
  without the `tunnel run` subcommand and silently does nothing.)
- **Auth**: Cloudflare Access (Zero Trust team `proud-mode-9523`) — a
  self-hosted app covering both hostnames with an Allow policy for a single
  email, one-time PIN login. Without it the app is wide open (it has no auth
  of its own); never expose it publicly unprotected.

To rebuild from scratch: add the zone to Cloudflare → point nameservers →
`cloudflared tunnel login` → `tunnel create bullpiggy` →
`tunnel route dns bullpiggy bullpiggy.com` → write config.yml → install the
two LaunchAgents → re-create the Access app.

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
- **Missing-day guard.** Yahoo occasionally drops a whole trading day from the
  *daily* feed (2026-07-24 went missing market-wide), which would silently
  anchor the scan to the wrong "previous day" and fire false signals. (MS on
  2026-07-27 flagged bearish against Thursday's low of 213.72, but Friday's
  real low was 211.21 — no engulfer.) When a weekday sits between the daily
  feed's previous bar and today, the previous session is rebuilt from 1-minute
  data (which retains ~7 days) and preferred when it proves a more recent
  session actually traded. Holidays self-correct — a genuine holiday has no
  intraday data, so nothing overrides. Overrides are logged as `[gap-fix]`.
- Early-close days (day after Thanksgiving, Christmas Eve, etc.) close at
  1:00 PM ET — the cron will fire after the close on those days and simply
  report against the final prices. Add a 10:30 AM PT entry if you want to
  cover them.
- Alerts are marked 🟢 for bullish and 🔴 for bearish.
- **Conviction ranking.** Every signal gets a 0–3 star tier from the 5-year
  backtest and alerts are sorted highest-conviction first (web scan cards
  re-order the same way when the scan finishes):
  - ★★★ body ≥ 3× the prior day's range AND ≥ 2× the stock's 20-day ATR
    (backtest: +10d median +2.3%, 64% win)
  - ★★ body ≥ 2× prior range · ★ body ≥ 1.5× · unstarred = trivial engulfer,
    no backtested edge
  - Tiers were validated on **bullish** signals; bearish signals show the same
    stars for reference but had no edge at any tier (they historically resolve
    upward — treat bearish alerts as informational for now).
  Strength metrics (body ×range, ×ATR20) are recorded with every signal in
  `signals.db` and shown in the History table. (Volume relative to its 20-day
  average was also backtested and dropped — noise, not signal.)

#!/usr/bin/env python3
"""
Engulfing Screener — web UI
===========================
A thin Flask wrapper around engulfing_scanner.py. Serves a single page with a
"Run scan" button that streams live progress and results over Server-Sent
Events. The scan itself reuses scan_stream() from the scanner module, so the
web app and the cron can never drift apart.

  python3 app.py            # then open http://127.0.0.1:5001

Manual runs intentionally ignore the market-hours gate (that gate is only for
the unattended cron) — you can re-run any time; a banner notes when the market
is closed so the prices are understood as the last available.
"""
import json
import threading
from datetime import datetime

import yfinance as yf
from flask import Flask, Response, render_template, request

import backtest
import history
import paper_trader
from engulfing_scanner import (
    ET,
    SCRIPT_DIR,
    conviction_tier,
    is_market_open_window,
    load_dotenv,
    load_tickers,
    load_tickers_by_sector,
    scan_stream,
    stars,
)

load_dotenv(SCRIPT_DIR / ".env")

app = Flask(__name__)

# yfinance is rate-limited and the scan is heavy; allow only one at a time.
_scan_lock = threading.Lock()


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


@app.route("/")
def index():
    sectors = [{"name": s, "count": len(t)}
               for s, t in load_tickers_by_sector().items()]
    return render_template("index.html", sectors=sectors)


@app.route("/api/scan")
def scan():
    # Optional ?sectors=Technology,Energy filter; default is the full watchlist.
    selected = [s.strip() for s in request.args.get("sectors", "").split(",") if s.strip()]
    if selected:
        by_sector = load_tickers_by_sector()
        tickers = [t for s in selected for t in by_sector.get(s, [])]
    else:
        tickers = load_tickers(None)
    if not tickers:
        tickers = load_tickers(None)

    def stream():
        if not _scan_lock.acquire(blocking=False):
            yield _sse({"type": "busy"})
            return
        try:
            started = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")
            market_open = is_market_open_window()
            yield _sse({
                "type": "start",
                "total": len(tickers),
                "started": started,
                "marketOpen": market_open,
            })
            signals = []
            for r in scan_stream(tickers):
                d = r["data"]
                msg = {
                    "type": "progress",
                    "index": r["index"],
                    "total": r["total"],
                    "symbol": r["symbol"],
                }
                if r["signal"] and d is not None:
                    # Record for the History tab — only while the market is
                    # open (closed-market prices are stale) and idempotently
                    # per symbol+day, so re-runs and the cron can't duplicate.
                    if market_open:
                        try:
                            history.record_signal(r["symbol"], r["signal"], d)
                        except Exception:
                            pass
                    tier = conviction_tier(d)
                    item = {
                        "symbol": r["symbol"],
                        "direction": r["signal"],
                        "today_open": round(d["today_open"], 2),
                        "current_price": round(d["current_price"], 2),
                        "prev_open": round(d["prev_open"], 2),
                        "prev_close": round(d["prev_close"], 2),
                        "prev_high": round(d["prev_high"], 2),
                        "prev_low": round(d["prev_low"], 2),
                        "tier": tier,
                        "stars": stars(tier),
                        "body_mult": d.get("body_mult"),
                        "body_atr": d.get("body_atr"),
                    }
                    signals.append(item)
                    msg["signal"] = item
                elif r["rejected"]:
                    msg["rejected"] = {
                        "symbol": r["symbol"],
                        "confirmed": round(r["rejected"]["confirmed"], 2),
                        "daily": round(r["rejected"]["daily"], 2),
                    }
                yield _sse(msg)
            finished = datetime.now(ET).strftime("%H:%M:%S ET")
            yield _sse({"type": "done", "count": len(signals),
                        "signals": signals, "finished": finished})
        finally:
            _scan_lock.release()

    return Response(
        stream(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/history")
def api_history():
    # Lazily fill in any newly completed trading days before serving.
    try:
        history.update_performance()
    except Exception as exc:
        print(f"[warn] performance update failed: {exc}")
    records = history.get_history()

    # For signals still inside their window, add a live % while the market
    # is open (when closed it would just duplicate the last stored close).
    # Baseline is the trigger day's close — signals triggered today don't
    # have one yet, so they show nothing until tomorrow.
    if is_market_open_window():
        prices: dict[str, float | None] = {}
        for r in records:
            if r["complete"] or not r["trigger_close"]:
                continue
            sym = r["symbol"]
            if sym not in prices:
                try:
                    prices[sym] = float(yf.Ticker(sym).fast_info["last_price"])
                except Exception:
                    prices[sym] = None
            if prices[sym]:
                r["live_pct"] = (prices[sym] - r["trigger_close"]) / r["trigger_close"] * 100
    return {"records": records, "trackDays": history.TRACK_DAYS}


@app.route("/api/backtest")
def run_backtest():
    years = max(1, min(10, request.args.get("years", 5, type=int)))
    tickers = load_tickers(None)

    def stream():
        # Shares the scan lock — both hammer yfinance.
        if not _scan_lock.acquire(blocking=False):
            yield _sse({"type": "busy"})
            return
        try:
            yield _sse({"type": "start", "total": len(tickers), "years": years})
            for ev in backtest.backtest_stream(tickers, years):
                yield _sse(ev)
        finally:
            _scan_lock.release()

    return Response(
        stream(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/paper")
def api_paper():
    # Reconciles outstanding orders against Alpaca, marks open positions to
    # the current mid, and returns the full ledger + account snapshot.
    return paper_trader.web_summary()


# NOT nested under /api/backtest: the Access owner-gate on the trigger
# endpoint matches by path prefix, and stored results must stay public.
@app.route("/api/backtest-results")
def backtest_results():
    data = backtest.get_backtest()
    data["trackDays"] = history.TRACK_DAYS
    # Stamp each signal with its sector (from tickers.txt) so the UI can
    # slice results without a re-run. Symbols no longer in the watchlist
    # fall under "Other".
    sector_of = {t: s for s, ts in load_tickers_by_sector().items() for t in ts}
    for r in data["records"]:
        r["sector"] = sector_of.get(r["symbol"], "Other")
    return data


if __name__ == "__main__":
    # Auto-reload code/templates on change (dev convenience), but keep the
    # werkzeug debugger OFF — it allows code execution and must never be on
    # if this app is ever exposed beyond localhost.
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.run(host="127.0.0.1", port=5001, threaded=True, debug=False,
            use_reloader=True)

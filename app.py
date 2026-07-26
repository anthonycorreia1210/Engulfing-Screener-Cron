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

from flask import Flask, Response, render_template, request

from engulfing_scanner import (
    ET,
    SCRIPT_DIR,
    is_market_open_window,
    load_dotenv,
    load_tickers,
    scan_stream,
)

load_dotenv(SCRIPT_DIR / ".env")

app = Flask(__name__)

# yfinance is rate-limited and the scan is heavy; allow only one at a time.
_scan_lock = threading.Lock()


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/scan")
def scan():
    # Optional ?tickers=AAPL,TSLA override; default is the full watchlist.
    override = request.args.get("tickers")
    tickers = load_tickers(override) if override else load_tickers(None)

    def stream():
        if not _scan_lock.acquire(blocking=False):
            yield _sse({"type": "busy"})
            return
        try:
            started = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")
            yield _sse({
                "type": "start",
                "total": len(tickers),
                "started": started,
                "marketOpen": is_market_open_window(),
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
                    item = {
                        "symbol": r["symbol"],
                        "direction": r["signal"],
                        "today_open": round(d["today_open"], 2),
                        "current_price": round(d["current_price"], 2),
                        "prev_open": round(d["prev_open"], 2),
                        "prev_close": round(d["prev_close"], 2),
                        "prev_high": round(d["prev_high"], 2),
                        "prev_low": round(d["prev_low"], 2),
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


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, threaded=True, debug=False)

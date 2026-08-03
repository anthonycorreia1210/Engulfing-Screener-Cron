#!/usr/bin/env python3
"""
Research: inside-day setups, breakout-confirmed
===============================================
Historical study (not wired into the UI): do inside-day coils on this
watchlist carry a tradable edge once the breakout confirms direction?

Pattern definitions (daily bars):
  inside(i)  = high[i] < high[i-1] AND low[i] > low[i-1]  (strict)
  ID1        = single inside day (previous bar NOT inside — disjoint from ID2)
  ID2        = double inside day (two consecutive inside bars; the structure
               is the mother bar two days back, which contains everything)

A setup only becomes a signal if the NEXT day breaks the structure:
  BULLISH: next day's high  > structure high
  BEARISH: next day's low   < structure low
  both sides in one day -> ambiguous, excluded (counted)
  neither               -> expired, no trade (counted)

Entry is the BREAKOUT day's close; forward returns are cumulative % to each
of the next +1..+10 trading-day closes — identical math to backtest.py, so
results are directly comparable with the engulfing tiles.

Per-signal context metrics, each reported as bucket splits:
  comp       inside range / mother range        (smaller = tighter coil)
  nr_atr     inside range / ATR20               (narrow vs the stock's norm)
  mother_atr mother range / ATR20               (big bar then a coil?)
  atr_quiet  ATR20 / avg TR of prior 100 bars   (<1 = stock quieter than usual)
  trend      close above/below its 20-day SMA on the coil day

Same caveats as backtest.py: survivorship bias (current watchlist only),
official closes, flat bars skipped as microcap noise.

Usage:
  python3 research_inside_days.py                # 5y, tickers.txt
  python3 research_inside_days.py --years 10 --tickers AAPL,TSLA
"""
import argparse
import math
import statistics
import sys

import pandas as pd
import yfinance as yf

from engulfing_scanner import load_tickers
from history import TRACK_DAYS, _last_complete_session

CHUNK = 25
OFFSETS = (1, 3, 7, 10)


def find_setups(symbol: str, df) -> tuple[list[dict], dict]:
    """All breakout-confirmed inside-day signals for one symbol, plus
    counters for setups that expired or broke both ways."""
    h, l = df["High"].to_list(), df["Low"].to_list()
    c = df["Close"].to_list()
    dates = [idx.date() for idx in df.index]
    n = len(df)

    tr = [None] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
                   for i in range(1, n)]

    def rng(i):
        return h[i] - l[i]

    def is_inside(i):
        return (i >= 1 and all(math.isfinite(v) for v in
                               (h[i], l[i], h[i - 1], l[i - 1]))
                and rng(i) > 0 and rng(i - 1) > 0
                and h[i] < h[i - 1] and l[i] > l[i - 1])

    signals, counts = [], {"expired": 0, "ambiguous": 0}
    for i in range(2, n - 1):
        if not is_inside(i):
            continue
        double = is_inside(i - 1)
        variant = "ID2" if double else "ID1"
        mother = i - 2 if double else i - 1
        sh, sl = h[mother], l[mother]
        if not (math.isfinite(sh) and math.isfinite(sl)) or sl <= 0 or sh <= sl:
            continue

        b = i + 1  # potential breakout day
        broke_up = math.isfinite(h[b]) and h[b] > sh
        broke_dn = math.isfinite(l[b]) and l[b] < sl
        if broke_up and broke_dn:
            counts["ambiguous"] += 1
            continue
        if not (broke_up or broke_dn):
            counts["expired"] += 1
            continue
        if not math.isfinite(c[b]) or c[b] <= 0:
            continue
        direction = "BULLISH" if broke_up else "BEARISH"

        atr = atr_quiet = None
        window = [t for t in tr[max(1, i - 20):i] if t is not None and math.isfinite(t)]
        if len(window) >= 15:
            atr = sum(window) / len(window)
        if i > 60:
            base = [t for t in tr[max(1, i - 100):i] if t is not None and math.isfinite(t)]
            if atr and len(base) >= 60:
                long_avg = sum(base) / len(base)
                if long_avg > 0:
                    atr_quiet = atr / long_avg
        trend = None
        if i >= 19:
            sma = [x for x in c[i - 19:i + 1] if math.isfinite(x)]
            if len(sma) == 20:
                trend = "up" if c[i] > sum(sma) / 20 else "down"

        days = []
        for k in range(1, TRACK_DAYS + 1):
            j = b + k
            if j >= n:
                break
            if not math.isfinite(c[j]):
                continue
            days.append({"day_offset": k, "pct": (c[j] - c[b]) / c[b] * 100})

        signals.append({
            "symbol": symbol, "variant": variant, "direction": direction,
            "trigger_date": dates[b].isoformat(),
            "comp": rng(i) / rng(mother) if rng(mother) > 0 else None,
            "nr_atr": rng(i) / atr if atr else None,
            "mother_atr": rng(mother) / atr if atr else None,
            "atr_quiet": atr_quiet, "trend": trend, "days": days,
        })
    return signals, counts


def collect(tickers: list[str], years: int) -> tuple[list[dict], dict]:
    last_final = _last_complete_session()
    all_signals: list[dict] = []
    totals = {"expired": 0, "ambiguous": 0}
    done = 0
    for start in range(0, len(tickers), CHUNK):
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
                    df = df[[idx.date() <= last_final for idx in df.index]]
                    if not df.empty:
                        sigs, counts = find_setups(sym, df)
                        all_signals.extend(sigs)
                        for k in totals:
                            totals[k] += counts[k]
                        found = len(sigs)
                except Exception as exc:
                    print(f"  [warn] {sym}: {exc}", file=sys.stderr)
            print(f"  [{done}/{len(tickers)}] {sym}"
                  + (f"  <- {found} signal(s)" if found else ""))
    return all_signals, totals


def _stats(rs: list[dict], direction: str, offsets=OFFSETS) -> str:
    parts = []
    for off in offsets:
        vals = [d["pct"] for r in rs for d in r["days"] if d["day_offset"] == off]
        if not vals:
            continue
        wins = sum(1 for v in vals if (v > 0) == (direction == "BULLISH") and v != 0)
        parts.append(f"+{off}d med {statistics.median(vals):+6.2f}% "
                     f"avg {statistics.mean(vals):+6.2f}% "
                     f"win {wins / len(vals) * 100:3.0f}%")
    return "  ".join(parts) or "no forward data"


def _buckets(rs: list[dict], direction: str, key: str, title: str,
             buckets: list[tuple], offsets=(3, 10)) -> None:
    print(f"    {title}")
    for lo, hi in buckets:
        sub = [r for r in rs if r[key] is not None and lo <= r[key] < hi]
        label = f"{lo:g}-{hi:g}" if math.isfinite(hi) else f">={lo:g}"
        print(f"      {label:<9} n={len(sub):<5} "
              + (_stats(sub, direction, offsets) if sub else ""))


def report(signals: list[dict], totals: dict) -> None:
    print(f"\n{'=' * 72}")
    print(f"{len(signals)} breakout-confirmed signals · "
          f"{totals['expired']} coils expired unbroken · "
          f"{totals['ambiguous']} broke both ways (excluded)")
    for variant in ("ID1", "ID2"):
        for direction in ("BULLISH", "BEARISH"):
            rs = [r for r in signals
                  if r["variant"] == variant and r["direction"] == direction]
            name = ("single inside day" if variant == "ID1"
                    else "double inside day")
            print(f"\n{variant} {direction} ({name}, breakout entry) — n={len(rs)}")
            if not rs:
                continue
            print(f"  all: {_stats(rs, direction)}")
            _buckets(rs, direction, "comp",
                     "by coil tightness (inside range / mother range):",
                     [(0, 0.3), (0.3, 0.5), (0.5, 0.7), (0.7, 1.0)])
            _buckets(rs, direction, "nr_atr",
                     "by inside range / ATR20:",
                     [(0, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, math.inf)])
            _buckets(rs, direction, "mother_atr",
                     "by mother range / ATR20:",
                     [(0, 1.0), (1.0, 1.5), (1.5, math.inf)])
            _buckets(rs, direction, "atr_quiet",
                     "by ATR20 vs 100-day norm (<1 = unusually quiet):",
                     [(0, 0.7), (0.7, 0.9), (0.9, 1.1), (1.1, math.inf)])
            for t in ("up", "down"):
                sub = [r for r in rs if r["trend"] == t]
                print(f"    trend {t:<4} n={len(sub):<5} "
                      + (_stats(sub, direction, (3, 10)) if sub else ""))


def main() -> None:
    parser = argparse.ArgumentParser(description="Inside-day breakout research")
    parser.add_argument("--years", type=int, default=5)
    parser.add_argument("--tickers")
    args = parser.parse_args()
    tickers = load_tickers(args.tickers)
    print(f"Studying inside-day setups: {len(tickers)} tickers over {args.years}y…")
    signals, totals = collect(tickers, args.years)
    report(signals, totals)


if __name__ == "__main__":
    main()

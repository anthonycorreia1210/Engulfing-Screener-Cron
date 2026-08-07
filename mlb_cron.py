#!/usr/bin/env python3
"""
Poke one of the MLB service's scheduled endpoints.

This container's cron drives the MLB service because that service has no
scheduler of its own. It deliberately does NOT use curl: the image is
python:3.12-slim, which has no curl binary, and shelling out to a missing one
fails with exit 127 — silently, from cron's point of view (2026-08-07: every
MLB cron entry had been no-oping this way, so no Top Picks alert ever fired).

Usage:  python mlb_cron.py <url>
"""
import sys
import urllib.error
import urllib.request
from datetime import datetime

TIMEOUT = 170  # under supercronic's patience, over a cold slate build


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: mlb_cron.py <url>", file=sys.stderr)
        return 2
    url = sys.argv[1]
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as res:
            body = res.read(600).decode("utf-8", "replace").strip()
            print(f"[{stamp}] {res.status} {url}\n  {body}")
            return 0
    except urllib.error.HTTPError as exc:
        body = exc.read(600).decode("utf-8", "replace").strip()
        print(f"[{stamp}] HTTP {exc.code} {url}\n  {body}", file=sys.stderr)
    except Exception as exc:
        print(f"[{stamp}] FAILED {url}: {exc}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())

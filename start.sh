#!/bin/sh
# Web + cron in one container (they share the SQLite volume — see Dockerfile).
set -e
supercronic /app/crontab.railway &
# 1 worker: the scan lock and SQLite writes assume a single process.
# --timeout 0: scans/backtests stream over SSE for many minutes.
exec gunicorn --workers 1 --threads 16 --timeout 0 \
  --bind "0.0.0.0:${PORT:-5001}" app:app

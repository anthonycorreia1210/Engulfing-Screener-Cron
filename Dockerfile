FROM python:3.12-slim

# Cron schedules and the scanner's market-hours gate both reason in ET.
ENV TZ=America/New_York \
    PYTHONUNBUFFERED=1

# supercronic: cron-for-containers (runs jobs in-process, logs to stdout).
# It must live in THIS container — the Railway volume holding signals.db can
# only attach to one service, so web + cron share it.
ADD https://github.com/aptible/supercronic/releases/download/v0.2.29/supercronic-linux-amd64 /usr/local/bin/supercronic
RUN chmod +x /usr/local/bin/supercronic

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x start.sh

# signals.db lives on the mounted volume in prod (SIGNALS_DB=/data/signals.db).
CMD ["./start.sh"]

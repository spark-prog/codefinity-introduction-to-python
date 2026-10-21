# Production image for the chip tournament app.
FROM python:3.13-slim

# tzdata lets the TZ setting (e.g. America/Chicago) show venue-local times.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Mount a persistent volume at /data so the tournament survives restarts and redeploys.
ENV CHIPS_DATA_DIR=/data \
    CHIPS_PRODUCTION=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000
RUN mkdir -p /data
EXPOSE 8000

# One worker on purpose: the tournament file and check-in clock must live in a single process.
CMD gunicorn --workers 1 --threads 8 --timeout 60 --bind 0.0.0.0:${PORT} "app:create_app()"

FROM python:3.12-slim

# ffmpeg is required for transcoding; kept in the same slim image to avoid
# a separate sidecar and its associated latency/coordination cost.
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /srv/data /srv/temp

ENV TEMP_DIR=/srv/temp \
    DATABASE_URL=sqlite:////srv/data/app.db \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# --workers can be raised for multi-core hosts; keep at 1 for SQLite (file
# locking) and scale via `--workers N` once on Postgres.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]

# Telegram-backed HLS Streaming Service

FastAPI backend that transcodes uploaded MP4s into HLS on the fly, stores
every playlist and segment as a Telegram document (Telegram = your object
store, free-tier), and streams them back to players with proper HTTP
semantics (Range requests, caching headers, correct MIME types).

Compatible with ExoPlayer, hls.js, native Safari HLS, and VLC.

## Project structure

```
telegram-hls-streaming/
├── app/
│   ├── main.py            # FastAPI app, lifespan, CORS, error handling
│   ├── config.py          # env-driven settings
│   ├── database.py        # async SQLAlchemy engine/session
│   ├── models.py          # Video, HLSFile ORM models
│   ├── schemas.py         # Pydantic request/response models
│   ├── telegram_client.py # Telegram Bot API client (upload/download/retry)
│   ├── ffmpeg_utils.py    # ffmpeg/ffprobe async subprocess helpers
│   ├── cache.py           # TTL caches for file paths + metadata
│   ├── utils.py           # validation helpers
│   └── routes/
│       ├── upload.py      # POST /upload
│       └── streaming.py   # GET /video/{id}/master.m3u8, /{segment}, /status
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── .gitignore
```

## How it works

1. **Upload** — `POST /upload` streams the MP4 to a temp file (size-capped,
   magic-byte validated), runs `ffmpeg` to produce an HLS variant
   (`stream.m3u8` + `segment000.ts`, `segment001.ts`, ...), builds a
   `master.m3u8`, then uploads every file to a Telegram chat via
   `sendDocument`, storing the returned `file_id`s in the DB. Temp files
   are deleted immediately after, success or failure.
2. **Playback** — `GET /video/{id}/master.m3u8` and
   `GET /video/{id}/{segment}` look up the matching `file_id` (DB result
   cached in-process), resolve it to a live Telegram CDN URL via `getFile`
   (also cached, since these links are only valid ~1 hour), and proxy the
   bytes straight through with `StreamingResponse` — nothing is buffered
   fully in memory. `.ts` segment requests forward the client's `Range`
   header to Telegram and mirror back `206 Partial Content` for true
   seeking support.

## Setup

```bash
cp .env.example .env
# edit .env: set BOT_TOKEN (from @BotFather), STORAGE_CHAT_ID,
# and (if using Turso) DATABASE_URL + DATABASE_AUTH_TOKEN
```

**Getting a `STORAGE_CHAT_ID`:** create a private Telegram channel, add
your bot as an admin, post any message in it, then forward that message to
`@userinfobot` (or call `getUpdates` on your bot) to read the channel's
numeric id — it looks like `-100xxxxxxxxxx`.

### Database options

| Backend | `DATABASE_URL` | Extra env |
|---------|----------------|-----------|
| Local SQLite (default) | `sqlite:///./data/app.db` | — |
| **Turso / libSQL** | `libsql://<db>-<org>.turso.io` | `DATABASE_AUTH_TOKEN` (or `TURSO_AUTH_TOKEN`) |
| Postgres | `postgresql+psycopg://user:pass@host/db` | — |

**Turso setup:**

```bash
# after installing the turso CLI and logging in
turso db show wmreel --url
# => libsql://wmreel-wallmob.aws-ap-south-1.turso.io

turso db tokens create wmreel
# => eyJ...   put this in DATABASE_AUTH_TOKEN
```

On first startup the app runs `create_all` against the configured database
(creates `videos` / `hls_files` tables if missing).

### Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# ffmpeg must be installed and on PATH: `apt install ffmpeg` / `brew install ffmpeg`
uvicorn app.main:app --reload
```

### Run with Docker

```bash
docker compose up --build
```

## API

| Method | Path                          | Description                                  |
|--------|-------------------------------|-----------------------------------------------|
| POST   | `/upload`                     | multipart `file` field, MP4 only              |
| GET    | `/video/{id}/status`          | processing/ready/failed + master URL          |
| GET    | `/video/{id}/master.m3u8`     | HLS master playlist                           |
| GET    | `/video/{id}/{segment}`       | variant playlist (`stream.m3u8`) or `.ts` file|
| GET    | `/health`                     | liveness check                                |

Example:

```bash
curl -F "file=@movie.mp4" http://localhost:8000/upload
# => {"video_id": "a1b2...", "status": "ready", "master_playlist_url": ".../video/a1b2/master.m3u8"}

# Feed the master_playlist_url straight into hls.js / ExoPlayer / VLC / Safari.
```

## Known limitations (Telegram Bot API constraints)

- **Standard Bot API caps file uploads at 50MB.** For longer/higher-bitrate
  videos, self-host the [Bot API server](https://github.com/tdlib/telegram-bot-api)
  (raises the limit to 2GB) and point `api_base`/`file_base` in
  `telegram_client.py` at your instance instead of `api.telegram.org`.
- Telegram's issued download links expire roughly an hour after `getFile`
  is called — handled here via a short-TTL cache that re-resolves
  transparently, so this is invisible to clients.
- A single Telegram chat is used as the "bucket." For very high upload
  throughput, consider round-robining across multiple bot tokens/chats to
  spread Telegram's per-chat flood limits.

## Notes on scaling

- Use **Turso** (`libsql://...`) or **Postgres** for multi-worker /
  multi-instance deployments — local SQLite is fine for a single process
  but does not like concurrent writers across containers.
- `uvicorn --workers N` can be raised once off local-file SQLite.
- The in-memory caches in `cache.py` are per-process; behind multiple
  instances, either accept the redundant `getFile` calls (cheap) or swap
  in Redis for a shared cache.

"""
Application entrypoint. Wires up the DB, the shared TelegramClient, CORS,
and the two route modules.

Startup is kept fast and cheap: table creation is a no-op after the first
run, and the TelegramClient constructor does no network I/O (the HTTP
client is opened lazily on first request).
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.database import init_db
from app.routes import streaming, upload
from app.telegram_client import TelegramClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.getLogger().setLevel(settings.log_level)

    init_db()
    app.state.telegram_client = TelegramClient()
    logger.info("Startup complete (db=%s)", settings.database_url.split("@")[-1] if "@" in settings.database_url else settings.database_url)

    yield

    await app.state.telegram_client.aclose()
    logger.info("Shutdown complete")


app = FastAPI(
    title="Telegram-backed HLS Streaming Service",
    description="Stores HLS video segments in Telegram and streams them on demand.",
    version="1.0.0",
    lifespan=lifespan,
)

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "HEAD", "OPTIONS"],
    allow_headers=["Range", "Content-Type"],
    expose_headers=["Content-Range", "Content-Length", "Accept-Ranges"],
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error while processing %s %s", request.method, request.url)
    return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content={"detail": "Internal server error"})


@app.get("/health")
async def health():
    return {"status": "ok"}


app.include_router(upload.router, tags=["upload"])
app.include_router(streaming.router, tags=["streaming"])

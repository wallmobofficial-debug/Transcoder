"""
Playback routes. Both endpoints resolve a (video_id, filename) pair to a
Telegram file_id via the DB (cached), resolve that to a live download URL
(cached, short TTL), then proxy-stream the bytes straight through to the
client — nothing is buffered fully in memory, and .ts segment requests
honor Range headers for real seeking support.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cache import file_path_cache, get_or_set, invalidate, metadata_cache
from app.database import get_db
from app.models import Video, VideoStatus
from app.telegram_client import TelegramAPIError, TelegramClient
from app.schemas import VideoStatusResponse
from app.utils import is_safe_segment_name

logger = logging.getLogger("streaming")
router = APIRouter()

DOWNLOAD_CHUNK_SIZE = 256 * 1024  # 256KB — good balance of syscall count vs. memory for proxying


def get_telegram_client(request: Request) -> TelegramClient:
    return request.app.state.telegram_client


async def _load_hls_file_map(video_id: str, db: Session) -> dict[str, dict] | None:
    """Cached lookup of every HLS artifact belonging to a video, keyed by
    filename, so repeat segment requests for a hot video skip the DB."""

    async def factory():
        result = db.execute(select(Video).where(Video.id == video_id))
        video = result.scalar_one_or_none()
        if video is None or video.status != VideoStatus.READY:
            return None
        return {f.filename: {"telegram_file_id": f.telegram_file_id, "file_size": f.file_size,
                              "content_type": f.content_type} for f in video.files}

    return await get_or_set(metadata_cache, f"video:{video_id}", factory)


async def _resolve_download_url(telegram: TelegramClient, telegram_file_id: str) -> str:
    async def factory():
        return await telegram.resolve_file_path(telegram_file_id)

    file_path = await get_or_set(file_path_cache, telegram_file_id, factory)
    return telegram.build_download_url(file_path)


async def _open_upstream(telegram: TelegramClient, telegram_file_id: str, range_header: str | None):
    """Resolve a Telegram download URL and open the upstream stream.

    On 401/403/404 (typically an expired getFile link), invalidate the
    cached path, re-resolve once, and retry the download.
    """
    download_url = await _resolve_download_url(telegram, telegram_file_id)
    upstream_ctx = telegram.open_download(download_url, range_header=range_header)
    upstream_resp = await upstream_ctx.__aenter__()

    if upstream_resp.status_code in (401, 403, 404):
        await upstream_ctx.__aexit__(None, None, None)
        invalidate(file_path_cache, telegram_file_id)
        logger.info("Telegram file path stale for %s (HTTP %s); re-resolving", telegram_file_id, upstream_resp.status_code)
        download_url = await _resolve_download_url(telegram, telegram_file_id)
        upstream_ctx = telegram.open_download(download_url, range_header=range_header)
        upstream_resp = await upstream_ctx.__aenter__()

    return upstream_ctx, upstream_resp


async def _proxy_stream(
    telegram: TelegramClient,
    telegram_file_id: str,
    content_type: str,
    file_size: int,
    range_header: str | None,
):
    """Core proxy logic shared by both playlist and segment responses."""
    try:
        upstream_ctx, upstream_resp = await _open_upstream(telegram, telegram_file_id, range_header)
    except TelegramAPIError as e:
        logger.error("Failed to resolve Telegram file: %s", e)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Upstream storage unavailable") from e

    if upstream_resp.status_code >= 400:
        await upstream_ctx.__aexit__(None, None, None)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Upstream storage returned an error")

    async def body_iterator():
        try:
            async for chunk in upstream_resp.aiter_bytes(DOWNLOAD_CHUNK_SIZE):
                yield chunk
        finally:
            await upstream_ctx.__aexit__(None, None, None)

    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "public, max-age=31536000, immutable" if content_type == "video/mp2t" else "no-cache",
        "Access-Control-Allow-Origin": "*",
    }

    if range_header and upstream_resp.status_code == 206:
        # Telegram already honored the Range request; mirror its headers back.
        content_range = upstream_resp.headers.get("content-range")
        if content_range:
            headers["Content-Range"] = content_range
        content_length = upstream_resp.headers.get("content-length")
        if content_length:
            headers["Content-Length"] = content_length
        status_code = status.HTTP_206_PARTIAL_CONTENT
    else:
        # Only emit Content-Length when we have a real value — an empty
        # header is invalid and can hang some players/proxies.
        content_length = str(file_size) if file_size else upstream_resp.headers.get("content-length")
        if content_length:
            headers["Content-Length"] = str(content_length)
        status_code = status.HTTP_200_OK

    return StreamingResponse(body_iterator(), status_code=status_code, media_type=content_type, headers=headers)


@router.get("/video/{video_id}/status", response_model=VideoStatusResponse)
async def get_video_status(video_id: str, request: Request, db: Session = Depends(get_db)):
    result = db.execute(select(Video).where(Video.id == video_id))
    video = result.scalar_one_or_none()
    if video is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Video not found")

    master_url = None
    if video.status == VideoStatus.READY:
        master_url = str(request.url_for("get_master_playlist", video_id=video_id))

    return VideoStatusResponse(
        video_id=video.id,
        status=video.status.value,
        original_filename=video.original_filename,
        duration_seconds=video.duration_seconds,
        error_message=video.error_message,
        master_playlist_url=master_url,
    )


@router.get("/video/{video_id}/master.m3u8", name="get_master_playlist")
async def get_master_playlist(
    video_id: str,
    db: Session = Depends(get_db),
    telegram: TelegramClient = Depends(get_telegram_client),
):
    file_map = await _load_hls_file_map(video_id, db)
    if file_map is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Video not found or not ready")

    entry = file_map.get("master.m3u8")
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Master playlist not found")

    return await _proxy_stream(
        telegram, entry["telegram_file_id"], entry["content_type"], entry["file_size"], range_header=None
    )


@router.get("/video/{video_id}/{segment:path}", name="get_segment")
async def get_segment(
    video_id: str,
    segment: str,
    request: Request,
    db: Session = Depends(get_db),
    telegram: TelegramClient = Depends(get_telegram_client),
):
    """Serves both the variant playlist (stream.m3u8) and individual .ts
    segments through the same route, matching the requested spec shape.
    """
    if not is_safe_segment_name(segment):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid segment name")

    file_map = await _load_hls_file_map(video_id, db)
    if file_map is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Video not found or not ready")

    entry = file_map.get(segment)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Segment not found")

    range_header = request.headers.get("range") if segment.endswith(".ts") else None

    return await _proxy_stream(
        telegram, entry["telegram_file_id"], entry["content_type"], entry["file_size"], range_header=range_header
    )

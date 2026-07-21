"""
POST /transcode

Accepts a Telegram file_id from the reels-backend, downloads the video,
transcodes it to HLS, stores everything in Telegram, and sends a callback
with the master playlist URL.

Flow:
  1. Validate the shared secret.
  2. Download the source video from Telegram by file_id.
  3. Wait for a processing slot (global concurrency cap — see app.main).
  4. Probe, transcode to HLS renditions, build master playlist.
  5. Upload all HLS artifacts to Telegram (same chat as source).
  6. Persist metadata in the DB.
  7. Call back the reels-backend with video_id + master_playlist_url +
     per-rendition URLs, and actually check the response status instead
     of assuming success.
"""
import asyncio
import logging
import os
import shutil
import uuid

import aiofiles
import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.ffmpeg_utils import build_master_playlist, probe_video, select_active_renditions, transcode_all_renditions
from app.models import HLSFile, Video, VideoStatus
from app.telegram_client import TelegramAPIError, TelegramClient
from app.utils import content_type_for

logger = logging.getLogger("transcode")
router = APIRouter()


class TranscodeRequest(BaseModel):
    file_id: str | None = None
    original_video_url: str | None = None
    video_id: str
    secret: str
    callback_url: str | None = None


def get_telegram_client(request: Request) -> TelegramClient:
    return request.app.state.telegram_client


@router.post("/transcode", status_code=status.HTTP_202_ACCEPTED)
async def transcode_from_telegram(
    request: Request,
    body: TranscodeRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    telegram: TelegramClient = Depends(get_telegram_client),
):
    settings = get_settings()
    transcoder_secret = os.environ.get("TRANSCODER_SECRET", "")
    if not transcoder_secret:
        logger.error("TRANSCODER_SECRET is not configured; rejecting /transcode request")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Service misconfigured: TRANSCODER_SECRET is not set",
        )
    if body.secret != transcoder_secret:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid secret")
    
    if not body.file_id and not body.original_video_url:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Either file_id or original_video_url is required")

    video_id = body.video_id or uuid.uuid4().hex
    work_dir = os.path.join(settings.temp_dir, f"tg-{video_id}")
    os.makedirs(work_dir, exist_ok=True)
    input_path = os.path.join(work_dir, "input.mp4")

    video = Video(id=video_id, original_filename=f"{video_id}.mp4", status=VideoStatus.PROCESSING)
    db.add(video)
    db.commit()

    background_tasks.add_task(
        _process_video,
        video_id,
        input_path,
        work_dir,
        body.file_id,
        body.original_video_url,
        settings.hls_segment_duration,
        body.callback_url,
        telegram,
        request.app.state.processing_semaphore,
    )

    return {"video_id": video_id, "status": VideoStatus.PROCESSING.value}

async def _process_video(
    video_id: str,
    input_path: str,
    work_dir: str,
    file_id: str | None,
    original_video_url: str | None,
    segment_duration: int,
    callback_url: str | None,
    telegram: TelegramClient,
    processing_semaphore: asyncio.Semaphore,
):
    hls_dir = os.path.join(work_dir, "hls")
    os.makedirs(hls_dir, exist_ok=True)

    settings = get_settings()
    from app.database import SessionLocal

    db = SessionLocal()
    master_url = None
    qualities: dict[str, str] = {}
    try:
        if original_video_url:
            logger.info("Downloading video %s from Supabase: %s", video_id, original_video_url)
            async with httpx.AsyncClient(timeout=300.0) as client:
                resp = await client.get(original_video_url)
                resp.raise_for_status()
                async with aiofiles.open(input_path, "wb") as f:
                    await f.write(resp.content)
        elif file_id:
            logger.info("Downloading video %s from Telegram: %s", video_id, file_id)
            await telegram.download_to_path(file_id, input_path)
        else:
            raise ValueError("Either file_id or original_video_url must be provided")

        async with processing_semaphore:
            probe = await probe_video(input_path)
            duration = max(float(probe["duration"] or 0), 0.1)

            active = select_active_renditions(probe.get("height") or 1080, settings.max_rendition_height)

            transcode_result = await transcode_all_renditions(
                input_path, hls_dir, segment_duration, active_renditions=active,
            )

            master_content = build_master_playlist(transcode_result["renditions"], duration)
            master_path = os.path.join(hls_dir, "master.m3u8")
            async with aiofiles.open(master_path, "w") as f:
                await f.write(master_content)

            async def upload_one(local_path: str, filename: str, order_index: int) -> HLSFile:
                fid, size = await telegram.upload_document(local_path, filename)
                return HLSFile(
                    video_id=video_id, filename=filename,
                    telegram_file_id=fid, file_size=size,
                    content_type=content_type_for(filename), order_index=order_index,
                )

            hls_file_rows: list[HLSFile] = []
            order = 0
            for rd in transcode_result["renditions"]:
                rd_dir = os.path.join(hls_dir, rd["name"])
                segs = rd["segments"]
                tasks = [
                    upload_one(os.path.join(rd_dir, s), f'{rd["name"]}/{s}', order + i)
                    for i, s in enumerate(segs)
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                failures = [r for r in results if isinstance(r, Exception)]
                if failures:
                    raise failures[0]
                hls_file_rows.extend(results)
                order += len(segs)
                pl = await upload_one(os.path.join(rd_dir, "stream.m3u8"), f'{rd["name"]}/stream.m3u8', -1)
                hls_file_rows.append(pl)
            master = await upload_one(master_path, "master.m3u8", -2)
            hls_file_rows.append(master)

            result = db.execute(select(Video).where(Video.id == video_id))
            video = result.scalar_one_or_none()
            if video:
                db.add_all(hls_file_rows)
                video.status = VideoStatus.READY
                video.duration_seconds = probe["duration"]
                db.commit()

                # Prefer an explicit public origin. Never fall back to
                # localhost in production — clients (phones, browsers) can't
                # reach the container's bind address, and reels-backend would
                # store/return that dead URL as masterPlaylistUrl.
                settings = get_settings()
                public_url = (
                    (settings.public_url or "")
                    or os.environ.get("PUBLIC_URL", "")
                    or os.environ.get("RENDER_EXTERNAL_URL", "")
                    or ""
                ).rstrip("/")
                if not public_url or "localhost" in public_url or "127.0.0.1" in public_url:
                    logger.warning(
                        "PUBLIC_URL/RENDER_EXTERNAL_URL missing or loopback; "
                        "set PUBLIC_URL to the public https origin of this service"
                    )
                    # Last resort for local dev only
                    if not public_url:
                        public_url = "http://localhost:8000"
                master_url = f"{public_url}/video/{video_id}/master.m3u8"
                qualities = {
                    rd["name"]: f"{public_url}/video/{video_id}/{rd['name']}/stream.m3u8"
                    for rd in transcode_result["renditions"]
                }
                quality_meta = {}
                for rd in transcode_result["renditions"]:
                    segs = rd["segments"]
                    rd_dir = os.path.join(hls_dir, rd["name"])
                    total_bytes = sum(
                        os.path.getsize(os.path.join(rd_dir, s))
                        for s in segs
                    )
                    bandwidth = max(int(total_bytes * 8 / duration * 1.1), 100_000)
                    width = int(rd["height"] * 16 / 9 / 2) * 2
                    quality_meta[rd["name"]] = {
                        "bandwidth": bandwidth, "width": width, "height": rd["height"],
                    }
                logger.info("Video %s processed successfully (%d renditions)", video_id, len(transcode_result["renditions"]))
    except Exception as e:
        logger.error("Background processing failed for %s: %s", video_id, e)
        try:
            result = db.execute(select(Video).where(Video.id == video_id))
            video = result.scalar_one_or_none()
            if video:
                video.status = VideoStatus.FAILED
                video.error_message = str(e)
                db.commit()
        except Exception:
            pass
    finally:
        db.close()
        shutil.rmtree(work_dir, ignore_errors=True)

    if callback_url and master_url:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(callback_url, json={
                    "videoId": video_id,
                    "qualities": qualities,
                    "masterPlaylistUrl": master_url,
                    "qualityMeta": quality_meta,
                    "secret": os.environ.get("TRANSCODER_SECRET", ""),
                })
            if resp.status_code >= 400:
                # Previously this branch didn't exist at all — a 400/500
                # response was logged as "Callback sent" just like a 200,
                # so failed callbacks were invisible without pulling raw
                # HTTP logs. Now the actual rejection reason is captured.
                logger.error(
                    "Callback to %s for video %s rejected: HTTP %d — %s",
                    callback_url, video_id, resp.status_code, resp.text[:500],
                )
            else:
                logger.info("Callback sent to %s for video %s", callback_url, video_id)
        except Exception as e:
            logger.error("Callback failed for %s: %s", video_id, e)
"""
POST /upload

Flow:
  1. Stream the incoming multipart file to a temp path on disk, enforcing
     a max-size cap while streaming (never buffering the whole upload in
     memory) and sniffing the header for a real MP4 signature.
  2. Transcode to HLS with ffmpeg (segments + variant playlist).
  3. Probe the source for bitrate/resolution, build a master playlist.
  4. Upload every HLS artifact (segments, variant playlist, master
     playlist) to Telegram concurrently (bounded by a semaphore inside
     TelegramClient), with retry/backoff on each.
  5. Persist metadata; clean up the temp directory either way.
"""
import asyncio
import logging
import os
import shutil
import uuid

import aiofiles
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from sqlalchemy import select

from app.config import get_settings
from app.database import get_db
from app.ffmpeg_utils import build_master_playlist, probe_video, transcode_all_renditions
from app.models import HLSFile, Video, VideoStatus
from app.schemas import UploadResponse
from app.telegram_client import TelegramAPIError, TelegramClient
from app.utils import content_type_for, looks_like_mp4, safe_upload_filename

logger = logging.getLogger("upload")
router = APIRouter()

CHUNK_SIZE = 1024 * 1024  # 1MB streaming chunks


def get_telegram_client(request: Request) -> TelegramClient:
    return request.app.state.telegram_client


@router.post("/upload", response_model=UploadResponse, status_code=status.HTTP_202_ACCEPTED)
async def upload_video(
    request: Request,
    file: UploadFile,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    telegram: TelegramClient = Depends(get_telegram_client),
):
    settings = get_settings()

    if file.content_type and file.content_type not in (
        "video/mp4", "application/mp4", "application/octet-stream",
    ):
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Only MP4 uploads are accepted")

    video_id = uuid.uuid4().hex
    work_dir = os.path.join(settings.temp_dir, video_id)
    os.makedirs(work_dir, exist_ok=True)
    safe_name = safe_upload_filename(file.filename or "upload.mp4")
    input_path = os.path.join(work_dir, safe_name)

    total_written = 0
    header_buffer = b""
    async with aiofiles.open(input_path, "wb") as out:
        while chunk := await file.read(CHUNK_SIZE):
            total_written += len(chunk)
            if total_written > settings.max_upload_size_bytes:
                shutil.rmtree(work_dir, ignore_errors=True)
                raise HTTPException(
                    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    f"File exceeds max allowed size of {settings.max_upload_size_mb}MB",
                )
            if len(header_buffer) < 64:
                header_buffer += chunk
            await out.write(chunk)

    if total_written == 0:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Empty file upload")
    if not looks_like_mp4(header_buffer):
        shutil.rmtree(work_dir, ignore_errors=True)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "File does not look like a valid MP4 container")

    video = Video(id=video_id, original_filename=safe_name, status=VideoStatus.PROCESSING)
    db.add(video)
    db.commit()

    background_tasks.add_task(
        _process_video, video_id, input_path, work_dir, settings.hls_segment_duration,
    )

    return UploadResponse(
        video_id=video_id,
        status=VideoStatus.PROCESSING.value,
        master_playlist_url=str(request.url_for("get_master_playlist", video_id=video_id)),
    )


async def _process_video(video_id: str, input_path: str, work_dir: str, segment_duration: int):
    """Background task: probe, transcode, upload to Telegram, update DB."""
    hls_dir = os.path.join(work_dir, "hls")
    os.makedirs(hls_dir, exist_ok=True)

    from app.database import SessionLocal
    from app.ffmpeg_utils import RENDITIONS
    from app.telegram_client import TelegramClient

    db = SessionLocal()
    telegram = TelegramClient()
    try:
        probe = await probe_video(input_path)
        duration = max(float(probe["duration"] or 0), 0.1)

        source_height = probe.get("height") or 1080
        active_renditions = [r for r in RENDITIONS if r.height <= source_height]
        if not active_renditions:
            active_renditions = [RENDITIONS[0]]

        transcode_result = await transcode_all_renditions(
            input_path, hls_dir, segment_duration,
        )

        master_content = build_master_playlist(transcode_result["renditions"], duration)
        master_path = os.path.join(hls_dir, "master.m3u8")
        async with aiofiles.open(master_path, "w") as f:
            await f.write(master_content)

        async def upload_one(local_path: str, filename: str, order_index: int) -> HLSFile:
            file_id, size = await telegram.upload_document(local_path, filename)
            return HLSFile(
                video_id=video_id, filename=filename,
                telegram_file_id=file_id, file_size=size,
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
        await telegram.aclose()
        shutil.rmtree(work_dir, ignore_errors=True)

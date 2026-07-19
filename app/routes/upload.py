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
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, status
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.ffmpeg_utils import FFmpegError, build_master_playlist, probe_video, transcode_to_hls
from app.models import HLSFile, Video, VideoStatus
from app.schemas import UploadResponse
from app.telegram_client import TelegramAPIError, TelegramClient
from app.utils import content_type_for, looks_like_mp4, safe_upload_filename

logger = logging.getLogger("upload")
router = APIRouter()

CHUNK_SIZE = 1024 * 1024  # 1MB streaming chunks


def get_telegram_client(request: Request) -> TelegramClient:
    return request.app.state.telegram_client


@router.post("/upload", response_model=UploadResponse, status_code=status.HTTP_201_CREATED)
async def upload_video(
    request: Request,
    file: UploadFile,
    db: Session = Depends(get_db),
    telegram: TelegramClient = Depends(get_telegram_client),
):
    settings = get_settings()

    # Some clients omit Content-Type; magic-byte sniffing below is the real
    # gate. Only reject when an explicit non-MP4 type is advertised.
    if file.content_type and file.content_type not in (
        "video/mp4",
        "application/mp4",
        "application/octet-stream",
    ):
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Only MP4 uploads are accepted")

    video_id = uuid.uuid4().hex
    work_dir = os.path.join(settings.temp_dir, video_id)
    hls_dir = os.path.join(work_dir, "hls")
    os.makedirs(hls_dir, exist_ok=True)
    safe_name = safe_upload_filename(file.filename or "upload.mp4")
    input_path = os.path.join(work_dir, safe_name)

    try:
        # --- 1. Stream to disk with size cap + magic-byte validation ---
        total_written = 0
        header_buffer = b""
        async with aiofiles.open(input_path, "wb") as out:
            while chunk := await file.read(CHUNK_SIZE):
                total_written += len(chunk)
                if total_written > settings.max_upload_size_bytes:
                    raise HTTPException(
                        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        f"File exceeds max allowed size of {settings.max_upload_size_mb}MB",
                    )
                if len(header_buffer) < 64:
                    header_buffer += chunk
                await out.write(chunk)

        if total_written == 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Empty file upload")
        if not looks_like_mp4(header_buffer):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "File does not look like a valid MP4 container")

        # --- 2. Probe + transcode ---
        try:
            probe = await probe_video(input_path)
            hls_result = await transcode_to_hls(input_path, hls_dir, settings.hls_segment_duration)
        except FFmpegError as e:
            logger.error("Transcode failed for %s: %s", video_id, e)
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Could not process video file") from e

        # Bandwidth must reflect the re-encoded HLS output, not the source
        # container bitrate (CRF output often differs substantially).
        duration = max(float(probe["duration"] or 0), 0.1)
        total_segment_bytes = sum(
            os.path.getsize(os.path.join(hls_dir, name)) for name in hls_result["segments"]
        )
        # Average bitrate with a small headroom factor so players don't
        # underestimate peak demand near scene changes.
        hls_bandwidth = max(int(total_segment_bytes * 8 / duration * 1.1), 100_000)

        # --- 3. Build master playlist ---
        master_content = build_master_playlist(
            bandwidth=hls_bandwidth,
            width=probe["width"],
            height=probe["height"],
            variant_filename="stream.m3u8",
        )
        master_path = os.path.join(hls_dir, "master.m3u8")
        async with aiofiles.open(master_path, "w") as f:
            await f.write(master_content)

        # --- 4. Upload everything to Telegram ---
        video = Video(id=video_id, original_filename=safe_name, status=VideoStatus.PROCESSING,
                       duration_seconds=probe["duration"])
        db.add(video)
        db.flush()

        # Segment filenames already come out of ffmpeg pre-sorted (segment000.ts, ...);
        # order_index preserves that ordering explicitly in the DB too.
        upload_plan = list(enumerate(hls_result["segments"]))
        upload_plan = [(name, idx) for idx, name in upload_plan]

        async def upload_one(filename: str, order_index: int) -> HLSFile:
            local_path = os.path.join(hls_dir, filename)
            try:
                file_id, size = await telegram.upload_document(local_path, filename)
            except TelegramAPIError as e:
                raise RuntimeError(f"Failed uploading {filename} to Telegram: {e}") from e
            return HLSFile(
                video_id=video_id,
                filename=filename,
                telegram_file_id=file_id,
                file_size=size,
                content_type=content_type_for(filename),
                order_index=order_index,
            )

        try:
            # return_exceptions=True so gather waits for *all* in-flight uploads
            # before we continue. Without that, the first failure returns while
            # sibling tasks are still reading segment files, and the finally
            # block's rmtree can delete files out from under them.
            segment_tasks = [upload_one(filename, order_index) for filename, order_index in upload_plan]
            segment_results = await asyncio.gather(*segment_tasks, return_exceptions=True)
            failures = [r for r in segment_results if isinstance(r, Exception)]
            if failures:
                raise failures[0]
            hls_file_rows = list(segment_results)
            # Playlists uploaded after segments so a partial failure never
            # leaves a master/variant playlist pointing at missing segments.
            hls_file_rows.append(await upload_one("stream.m3u8", -1))
            hls_file_rows.append(await upload_one("master.m3u8", -2))
        except Exception as e:
            video.status = VideoStatus.FAILED
            video.error_message = str(e)
            db.commit()
            logger.error("Upload pipeline failed for %s: %s", video_id, e)
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Failed to store video on backend") from e

        db.add_all(hls_file_rows)
        video.status = VideoStatus.READY
        db.commit()

        return UploadResponse(
            video_id=video_id,
            status=video.status.value,
            master_playlist_url=str(request.url_for("get_master_playlist", video_id=video_id)),
        )

    finally:
        # Always clean up local temp files regardless of success/failure —
        # Telegram (or nothing, on failure) is the system of record.
        shutil.rmtree(work_dir, ignore_errors=True)

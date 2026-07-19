"""
POST /transcode

Accepts a Telegram file_id from the reels-backend, downloads the video,
transcodes it to HLS, stores everything in Telegram, and sends a callback
with the master playlist URL.

Flow:
  1. Validate the shared secret.
  2. Download the source video from Telegram by file_id.
  3. Probe, transcode to HLS renditions, build master playlist.
  4. Upload all HLS artifacts to Telegram (same chat as source).
  5. Persist metadata in the DB.
  6. Call back the reels-backend with video_id + master_playlist_url.
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
from app.ffmpeg_utils import build_master_playlist, probe_video, transcode_all_renditions
from app.models import HLSFile, Video, VideoStatus
from app.telegram_client import TelegramAPIError, TelegramClient
from app.utils import content_type_for

logger = logging.getLogger("transcode")
router = APIRouter()


class TranscodeRequest(BaseModel):
    file_id: str
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
    if transcoder_secret and body.secret != transcoder_secret:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid secret")

    video_id = body.video_id or uuid.uuid4().hex
    work_dir = os.path.join(settings.temp_dir, f"tg-{video_id}")
    os.makedirs(work_dir, exist_ok=True)
    input_path = os.path.join(work_dir, "input.mp4")

    video = Video(id=video_id, original_filename=f"{video_id}.mp4", status=VideoStatus.PROCESSING)
    db.add(video)
    db.commit()

    background_tasks.add_task(
        _process_tg_video, video_id, input_path, work_dir, body.file_id,
        settings.hls_segment_duration, body.callback_url,
    )

    return {"video_id": video_id, "status": VideoStatus.PROCESSING.value}


async def _process_tg_video(
    video_id: str,
    input_path: str,
    work_dir: str,
    file_id: str,
    segment_duration: int,
    callback_url: str | None,
):
    hls_dir = os.path.join(work_dir, "hls")
    os.makedirs(hls_dir, exist_ok=True)

    from app.database import SessionLocal
    from app.ffmpeg_utils import RENDITIONS
    from app.telegram_client import TelegramClient

    db = SessionLocal()
    telegram = TelegramClient()
    master_url = None
    try:
        await telegram.download_to_path(file_id, input_path)

        probe = await probe_video(input_path)
        duration = max(float(probe["duration"] or 0), 0.1)

        source_height = probe.get("height") or 1080
        active = [r for r in RENDITIONS if r.height <= source_height]
        if not active:
            active = [RENDITIONS[0]]

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
            master_url = str(httpx.URL(f"{os.environ.get('PUBLIC_URL', 'http://localhost:8000')}/video/{video_id}/master.m3u8"))
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

    if callback_url and master_url:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                await client.post(callback_url, json={
                    "videoId": video_id,
                    "qualities": {},
                    "masterPlaylistUrl": master_url,
                    "secret": os.environ.get("TRANSCODER_SECRET", ""),
                })
            logger.info("Callback sent to %s for video %s", callback_url, video_id)
        except Exception as e:
            logger.error("Callback failed for %s: %s", video_id, e)
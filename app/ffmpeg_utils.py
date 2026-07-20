"""
FFmpeg integration: transcodes an uploaded MP4 into HLS renditions
(playlists + .ts segments) and probes it for bandwidth/resolution info
used to build the master playlist.

Supports multi-quality ABR (480p, 720p, 1080p). Preset/thread count and
the top rendition height are configurable (see app.config) so this can be
tuned down for constrained hosts like Render's free tier.
"""
import asyncio
import json
import logging
import os
from dataclasses import dataclass

from app.config import get_settings

logger = logging.getLogger("ffmpeg_utils")


class FFmpegError(Exception):
    pass


@dataclass
class Rendition:
    name: str
    height: int
    video_bitrate: str
    audio_bitrate: str = "128k"


# Bitrate ladder tuned for watchable ABR quality (not free-tier minimums).
# Previous values (800k / 2500k / 5000k + ultrafast) looked soft / muddy.
RENDITIONS = [
    Rendition(name="480", height=480, video_bitrate="1600k", audio_bitrate="128k"),
    Rendition(name="720", height=720, video_bitrate="3500k", audio_bitrate="160k"),
    Rendition(name="1080", height=1080, video_bitrate="6500k", audio_bitrate="192k"),
]


def select_active_renditions(source_height: int, max_height: int | None = None) -> list[Rendition]:
    """Picks which renditions to encode: never upscale past the source,
    and never exceed `max_height` (the free-tier cap on the heaviest
    rendition allowed). Always returns at least one rendition."""
    cap = source_height or RENDITIONS[0].height
    if max_height:
        cap = min(cap, max_height)
    active = [r for r in RENDITIONS if r.height <= cap]
    if not active:
        active = [RENDITIONS[0]]
    return active


async def probe_video(input_path: str) -> dict:
    """Runs ffprobe and returns duration, resolution, and bitrate."""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration,bit_rate:stream=width,height,codec_type",
        "-of", "json",
        input_path,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise FFmpegError(f"ffprobe failed: {stderr.decode(errors='ignore')}")

    data = json.loads(stdout.decode())
    fmt = data.get("format", {})
    duration = float(fmt.get("duration", 0) or 0)
    bit_rate = int(fmt.get("bit_rate", 0) or 0)

    width = height = None
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video":
            width = stream.get("width")
            height = stream.get("height")
            break

    return {
        "duration": duration,
        "bitrate": bit_rate or 2_000_000,
        "width": width or 1280,
        "height": height or 720,
    }


async def _transcode_rendition(
    input_path: str,
    output_dir: str,
    rendition: Rendition,
    segment_duration: int,
) -> dict:
    """Transcodes `input_path` into one HLS rendition (single resolution).
    Returns {"playlist_path": ..., "segments": [...]}.
    """
    settings = get_settings()
    os.makedirs(output_dir, exist_ok=True)
    playlist_path = os.path.join(output_dir, "stream.m3u8")
    segment_pattern = os.path.join(output_dir, "segment%03d.ts")

    # Parse e.g. "1600k" → 1600 for maxrate/bufsize caps.
    v_kbps = int(rendition.video_bitrate.rstrip("kK"))
    # high profile for 720p+ (better tools at same bitrate); main for 480p
    # for wider device compatibility on the lowest rung.
    profile = "high" if rendition.height >= 720 else "main"

    cmd = [
        "ffmpeg",
        "-y",
        "-i", input_path,
        "-c:v", "libx264",
        "-threads", str(settings.ffmpeg_threads),
        "-profile:v", profile,
        "-level", "4.1" if rendition.height >= 720 else "3.1",
        "-pix_fmt", "yuv420p",
        "-b:v", rendition.video_bitrate,
        "-maxrate", f"{v_kbps * 2}k",
        "-bufsize", f"{v_kbps * 4}k",
        "-preset", settings.ffmpeg_preset,
        # lanczos downscale stays sharper than the default bilinear scaler
        "-vf", f"scale=-2:{rendition.height}:flags=lanczos",
        "-c:a", "aac",
        "-ac", "2",
        "-b:a", rendition.audio_bitrate,
        "-ar", "48000",
        "-force_key_frames", f"expr:gte(t,n_forced*{segment_duration})",
        "-hls_time", str(segment_duration),
        "-hls_list_size", "0",
        "-hls_flags", "independent_segments",
        "-hls_segment_type", "mpegts",
        "-hls_segment_filename", segment_pattern,
        playlist_path,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise FFmpegError(f"ffmpeg {rendition.name}p failed: {stderr.decode(errors='ignore')[-2000:]}")

    if not os.path.exists(playlist_path):
        raise FFmpegError(f"ffmpeg {rendition.name}p reported success but no playlist produced")

    segments = sorted(f for f in os.listdir(output_dir) if f.endswith(".ts"))
    if not segments:
        raise FFmpegError(f"ffmpeg {rendition.name}p produced zero segments")

    return {"playlist_path": playlist_path, "segments": segments}


async def transcode_all_renditions(
    input_path: str,
    hls_dir: str,
    segment_duration: int,
    active_renditions: list[Rendition] | None = None,
) -> dict:
    """Transcodes into all configured renditions sequentially (deliberately
    NOT parallel — running multiple ffmpeg processes at once is exactly
    the kind of memory spike that kills a 512MB instance).
    Returns {"renditions": [{"name": ..., "height": ..., "segments": [...]}, ...]}
    """
    renditions = active_renditions or RENDITIONS
    results = []
    for rend in renditions:
        out = os.path.join(hls_dir, rend.name)
        result = await _transcode_rendition(input_path, out, rend, segment_duration)
        results.append({"name": rend.name, "height": rend.height, **result})
    return {"renditions": results}


def build_master_playlist(renditions_data: list[dict], duration: float) -> str:
    """Builds a master playlist from multiple rendition outputs.
    Each item in renditions_data: {"name": "480", "height": 480,
                                    "playlist_path": ..., "segments": [...]}
    """
    lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
    for rd in renditions_data:
        segments = rd["segments"]
        if not segments:
            continue
        total_bytes = sum(
            os.path.getsize(os.path.join(os.path.dirname(rd["playlist_path"]), s))
            for s in segments
        )
        bandwidth = max(int(total_bytes * 8 / duration * 1.1), 100_000)
        width = int(rd["height"] * 16 / 9 / 2) * 2
        lines.append(
            f'#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},RESOLUTION={width}x{rd["height"]}'
        )
        lines.append(f'{rd["name"]}/stream.m3u8')
    return "\n".join(lines) + "\n"
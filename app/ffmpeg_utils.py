"""
FFmpeg integration: transcodes an uploaded MP4 into HLS renditions
(playlists + .ts segments) and probes it for bandwidth/resolution info
used to build the master playlist.

Supports multi-quality ABR (480p, 720p, 1080p).
"""
import asyncio
import json
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger("ffmpeg_utils")


class FFmpegError(Exception):
    pass


@dataclass
class Rendition:
    name: str
    height: int
    video_bitrate: str
    audio_bitrate: str = "128k"


RENDITIONS = [
    Rendition(name="480", height=480, video_bitrate="800k"),
    Rendition(name="720", height=720, video_bitrate="2500k"),
    Rendition(name="1080", height=1080, video_bitrate="5000k"),
]


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
    os.makedirs(output_dir, exist_ok=True)
    playlist_path = os.path.join(output_dir, "stream.m3u8")
    segment_pattern = os.path.join(output_dir, "segment%03d.ts")

    cmd = [
        "ffmpeg",
        "-y",
        "-i", input_path,
        "-c:v", "h264",
        "-profile:v", "main",
        "-pix_fmt", "yuv420p",
        "-b:v", rendition.video_bitrate,
        "-maxrate", str(int(rendition.video_bitrate[:-1]) * 2) + "k",
        "-bufsize", str(int(rendition.video_bitrate[:-1]) * 4) + "k",
        "-preset", "veryfast",
        "-vf", f"scale=-2:{rendition.height}",
        "-c:a", "aac",
        "-ac", "2",
        "-b:a", rendition.audio_bitrate,
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
) -> dict:
    """Transcodes into all configured renditions sequentially.
    Returns mapping: {"renditions": [{"name": ..., "segments": [...]}, ...],
                      "results": [{"playlist_path": ..., "segments": [...]}, ...]}
    """
    results = []
    for rend in RENDITIONS:
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

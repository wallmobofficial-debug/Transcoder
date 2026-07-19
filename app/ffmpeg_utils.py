"""
FFmpeg integration: transcodes an uploaded MP4 into an HLS variant
(playlist + .ts segments) and probes it for bandwidth/resolution info
used to build the master playlist.

All subprocess calls are async (asyncio.create_subprocess_exec) so they
never block the event loop, and ffmpeg reads/writes files directly on
disk — the video bytes never pass through Python memory.
"""
import asyncio
import json
import logging
import os

logger = logging.getLogger("ffmpeg_utils")


class FFmpegError(Exception):
    pass


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
        "bitrate": bit_rate or 2_000_000,  # sane fallback if source has no bitrate tag
        "width": width or 1280,
        "height": height or 720,
    }


async def transcode_to_hls(input_path: str, output_dir: str, segment_duration: int) -> dict:
    """Transcodes `input_path` into HLS inside `output_dir`.

    Produces:
      output_dir/stream.m3u8
      output_dir/segment000.ts, segment001.ts, ...

    Uses H.264/AAC re-encoding for broad compatibility (ExoPlayer, hls.js,
    Safari, VLC) rather than `-c copy`, since source codecs vary and
    stream-copy HLS often breaks players when keyframes don't align to
    segment boundaries.
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
        # yuv420p is required for broad player support (Safari/ExoPlayer/hls.js);
        # without it, sources in yuv422/yuv444/10-bit often produce unplayable HLS.
        "-pix_fmt", "yuv420p",
        "-crf", "20",
        "-preset", "veryfast",
        "-c:a", "aac",
        "-ac", "2",
        "-b:a", "128k",
        # Force a keyframe at every segment boundary so players can seek cleanly
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
        raise FFmpegError(f"ffmpeg transcode failed: {stderr.decode(errors='ignore')[-2000:]}")

    if not os.path.exists(playlist_path):
        raise FFmpegError("ffmpeg reported success but no playlist was produced")

    segments = sorted(f for f in os.listdir(output_dir) if f.endswith(".ts"))
    if not segments:
        raise FFmpegError("ffmpeg produced a playlist with zero segments")

    return {"playlist_path": playlist_path, "segments": segments}


def build_master_playlist(bandwidth: int, width: int, height: int, variant_filename: str) -> str:
    """Builds a master playlist referencing a single variant stream.
    (Extending to multi-bitrate is straightforward: transcode multiple
    renditions and add one EXT-X-STREAM-INF line per rendition here.)
    """
    return (
        "#EXTM3U\n"
        "#EXT-X-VERSION:3\n"
        f'#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},RESOLUTION={width}x{height}\n'
        f"{variant_filename}\n"
    )

"""Small helpers: upload validation, safe filenames, content-type mapping."""
import os
import re

# Minimal MP4/MOV "ftyp" box signature check. The ftyp box isn't always at
# byte 0 in every muxer, but is within the first ~64 bytes for effectively
# all real-world MP4 files (including moov-first fast-start files), so we
# check a small header window rather than trusting the client-supplied
# Content-Type / extension alone.
_MP4_FTYP_MARKER = b"ftyp"

SEGMENT_NAME_RE = re.compile(r"^[a-zA-Z0-9._/-]+$")


def looks_like_mp4(header_bytes: bytes) -> bool:
    return _MP4_FTYP_MARKER in header_bytes[:64]


def safe_upload_filename(filename: str) -> str:
    """Strips path components and disallowed characters from a
    client-supplied filename before it ever touches the filesystem."""
    base = os.path.basename(filename or "upload.mp4")
    base = re.sub(r"[^a-zA-Z0-9._-]", "_", base)
    return base or "upload.mp4"


def is_safe_segment_name(name: str) -> bool:
    """Guards the GET /video/{id}/{segment} route against path traversal
    and only allows the filenames we ourselves generated at upload time."""
    if not name or ".." in name or "\\" in name or name.startswith("/") or "//" in name:
        return False
    return bool(SEGMENT_NAME_RE.match(name))


def content_type_for(filename: str) -> str:
    if filename.endswith(".m3u8"):
        return "application/vnd.apple.mpegurl"
    if filename.endswith(".ts"):
        return "video/mp2t"
    return "application/octet-stream"

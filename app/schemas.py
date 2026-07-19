"""Pydantic schemas for API request/response bodies."""
from pydantic import BaseModel


class UploadResponse(BaseModel):
    video_id: str
    status: str
    master_playlist_url: str


class VideoStatusResponse(BaseModel):
    video_id: str
    status: str
    original_filename: str
    duration_seconds: float | None = None
    error_message: str | None = None
    master_playlist_url: str | None = None


class ErrorResponse(BaseModel):
    detail: str

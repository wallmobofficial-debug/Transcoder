"""
ORM models.

Video          -> one row per uploaded video, tracks processing status.
HLSFile        -> one row per HLS artifact (master playlist, variant
                   playlist, or a .ts segment) belonging to a video, each
                   pointing at the Telegram file_id that actually holds
                   the bytes.

We deliberately do NOT store any video bytes locally: Telegram is the
sole storage backend. Only small text playlists and metadata live here.
"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class VideoStatus(str, enum.Enum):
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class Video(Base):
    __tablename__ = "videos"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    original_filename: Mapped[str] = mapped_column(String(512))
    status: Mapped[VideoStatus] = mapped_column(Enum(VideoStatus), default=VideoStatus.PROCESSING, index=True)
    duration_seconds: Mapped[float | None] = mapped_column(nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    files: Mapped[list["HLSFile"]] = relationship(
        back_populates="video", cascade="all, delete-orphan", lazy="selectin"
    )


class HLSFile(Base):
    __tablename__ = "hls_files"
    __table_args__ = (UniqueConstraint("video_id", "filename", name="uq_video_filename"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), index=True)
    # e.g. "master.m3u8", "stream.m3u8", "segment000.ts"
    filename: Mapped[str] = mapped_column(String(255), index=True)
    telegram_file_id: Mapped[str] = mapped_column(String(255))
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    content_type: Mapped[str] = mapped_column(String(100))
    order_index: Mapped[int] = mapped_column(Integer, default=0)  # segment ordering

    video: Mapped["Video"] = relationship(back_populates="files")

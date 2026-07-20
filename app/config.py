"""
Centralized configuration. All tunables live here and are loaded from
environment variables (or a .env file) via pydantic-settings, so nothing
is hardcoded in the business logic.

Defaults in this file are tuned for Render's free tier (512MB RAM, shared
vCPU, no persistent disk guarantees): lower upload cap, lower concurrency,
faster/lighter ffmpeg preset, and a global cap on simultaneous transcodes.
Bump these via env vars if you're on a paid plan with more headroom.
"""
from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # --- Telegram ---
    bot_token: str
    storage_chat_id: str  # keep as str; Telegram ids can be negative ints but str is safest for env parsing
    # Base URL of the Bot API. Default is the public Telegram endpoint.
    # Point this at a self-hosted Bot API server (e.g. on Render) to raise
    # the 50MB upload limit. Accepts TELEGRAM_API or Telegram_API.
    telegram_api: str = Field(
        default="https://api.telegram.org",
        validation_alias=AliasChoices("TELEGRAM_API", "Telegram_API", "telegram_api"),
    )

    # --- Database ---
    # Local SQLite default. For Turso set e.g.:
    #   DATABASE_URL=libsql://wmreel-wallmob.aws-ap-south-1.turso.io
    #   DATABASE_AUTH_TOKEN=<token from `turso db tokens create ...`>
    database_url: str = "sqlite:///./data/app.db"
    # Auth token for Turso / hosted libSQL (DATABASE_AUTH_TOKEN or TURSO_AUTH_TOKEN).
    database_auth_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DATABASE_AUTH_TOKEN", "TURSO_AUTH_TOKEN", "database_auth_token"),
    )

    # --- Upload ---
    # 512MB total RAM on Render free tier; ffmpeg's own working set scales
    # with source resolution/bitrate, not just file size, but a lower cap
    # keeps worst-case memory bounded. Raise this if you upgrade the plan.
    max_upload_size_mb: int = 200
    hls_segment_duration: int = 6  # seconds per .ts segment
    telegram_upload_concurrency: int = 2
    telegram_max_retries: int = 5

    # --- Transcoding ---
    # "veryfast" is a good quality/speed balance for ABR ladders.
    # Use "ultrafast" on very tight free-tier hosts (softer output at same bitrate).
    # Use "fast" / "medium" if you have spare CPU and want max quality.
    ffmpeg_preset: str = "veryfast"
    # Free tier typically grants a fraction of a CPU core; pinning threads
    # avoids ffmpeg spawning more encoder threads than the instance can
    # actually run in parallel, which just adds contention overhead.
    ffmpeg_threads: int = 1
    # Skip 1080p by default — it's the most expensive rendition and rarely
    # needed for free-tier-scale traffic. Set to 1080 to re-enable it.
    max_rendition_height: int = 1080
    # Hard cap on how many videos can be transcoding at the same time,
    # regardless of how many /upload or /transcode requests come in. Each
    # ffmpeg process + concurrent Telegram uploads can easily use 150-300MB;
    # two running at once on a 512MB instance risks an OOM kill. Keep at 1
    # unless you've upgraded the instance.
    video_processing_concurrency: int = 1

    # --- Caching ---
    file_path_cache_ttl: int = 2700  # telegram file links live ~1h; refresh well before that
    metadata_cache_ttl: int = 300
    metadata_cache_size: int = 200
    file_path_cache_size: int = 500

    # --- Misc ---
    temp_dir: str = "./temp"
    allowed_origins: str = "*"
    log_level: str = "INFO"
    # Public https origin of THIS service (no trailing slash). Used when
    # building masterPlaylistUrl for the reels-backend callback.
    # On Render you can also rely on RENDER_EXTERNAL_URL, but set this
    # explicitly so callbacks never embed http://localhost:8000.
    public_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PUBLIC_URL", "public_url"),
    )

    @property
    def max_upload_size_bytes(self) -> int:
        return self.max_upload_size_mb * 1024 * 1024

    @property
    def cors_origins(self) -> list[str]:
        if self.allowed_origins.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    @property
    def telegram_api_base(self) -> str:
        """Origin of the Bot API host, no trailing slash (e.g. https://api.telegram.org)."""
        return self.telegram_api.rstrip("/")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )


@lru_cache
def get_settings() -> Settings:
    """Settings are read once and cached for the lifetime of the process."""
    return Settings()
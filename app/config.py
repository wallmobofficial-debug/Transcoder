"""
Centralized configuration. All tunables live here and are loaded from
environment variables (or a .env file) via pydantic-settings, so nothing
is hardcoded in the business logic.
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
    max_upload_size_mb: int = 500
    hls_segment_duration: int = 6  # seconds per .ts segment
    telegram_upload_concurrency: int = 5
    telegram_max_retries: int = 5

    # --- Caching ---
    file_path_cache_ttl: int = 2700  # telegram file links live ~1h; refresh well before that
    metadata_cache_ttl: int = 300
    metadata_cache_size: int = 1000
    file_path_cache_size: int = 2000

    # --- Misc ---
    temp_dir: str = "./temp"
    allowed_origins: str = "*"
    log_level: str = "INFO"

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

"""
Database engine + session management.

Supports:
  - Local SQLite (default):  sqlite:///./data/app.db
  - Turso / libSQL remote:   libsql://<db>-<org>.turso.io  (+ DATABASE_AUTH_TOKEN)
  - Postgres:                postgresql+psycopg://user:pass@host/db

Turso's SQLAlchemy dialect is synchronous, so the whole data layer uses
sync SQLAlchemy. FastAPI still runs async routes for I/O (Telegram, ffmpeg);
metadata queries are small and fine on the event loop.
"""
from collections.abc import Generator
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

from app.config import get_settings

settings = get_settings()


def _ensure_sqlite_parent_dir(filesystem_path: str) -> None:
    if filesystem_path in (":memory:", "") or filesystem_path.startswith("file:"):
        return
    parent = Path(filesystem_path).expanduser().resolve().parent
    parent.mkdir(parents=True, exist_ok=True)


def _with_query_param(url: str, key: str, value: str) -> str:
    """Set a query parameter if it is not already present."""
    parsed = urlparse(url)
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if key not in params:
        params[key] = value
    return urlunparse(parsed._replace(query=urlencode(params)))


def build_engine_url(database_url: str) -> tuple[str, dict, dict]:
    """Normalize DATABASE_URL into a SQLAlchemy URL + connect/engine kwargs.

    Returns (sqlalchemy_url, connect_args, engine_kwargs).
    """
    url = database_url.strip()
    connect_args: dict = {}
    engine_kwargs: dict = {"pool_pre_ping": True, "future": True}

    # --- Turso / libSQL -------------------------------------------------
    # Accept both raw Turso URLs and already-prefixed SQLAlchemy forms:
    #   libsql://my-db-user.turso.io
    #   sqlite+libsql://my-db-user.turso.io
    if url.startswith("libsql://") or url.startswith("sqlite+libsql://"):
        if url.startswith("libsql://"):
            # sqlalchemy-libsql registers the "libsql" dialect under sqlite+
            url = "sqlite+" + url
        # Remote Turso endpoints speak TLS; secure=true is required.
        if ".turso.io" in url or url.startswith("sqlite+libsql://"):
            url = _with_query_param(url, "secure", "true")
        if settings.database_auth_token:
            connect_args["auth_token"] = settings.database_auth_token
        # Serverless-friendly: don't hold sticky pooled connections to Turso.
        engine_kwargs["poolclass"] = NullPool
        return url, connect_args, engine_kwargs

    # --- Legacy async SQLite URL from earlier revisions -----------------
    if url.startswith("sqlite+aiosqlite:///"):
        url = "sqlite:///" + url[len("sqlite+aiosqlite:///") :]

    # --- Local SQLite ---------------------------------------------------
    if url.startswith("sqlite:///"):
        raw_path = url[len("sqlite:///") :]
        # Absolute path form: sqlite:////abs/path → raw_path = /abs/path
        if raw_path.startswith("/") or raw_path == ":memory:" or raw_path.startswith("file:"):
            fs_path = raw_path
        else:
            fs_path = raw_path
        if raw_path != ":memory:":
            _ensure_sqlite_parent_dir(fs_path)
        # check_same_thread is required when the connection is used across
        # await points in async FastAPI handlers.
        connect_args["check_same_thread"] = False
        if raw_path == ":memory:":
            engine_kwargs["poolclass"] = StaticPool
        return url, connect_args, engine_kwargs

    # --- Postgres (sync driver) ----------------------------------------
    if url.startswith("postgresql+asyncpg://"):
        url = "postgresql+psycopg://" + url[len("postgresql+asyncpg://") :]
    elif url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://") :]
    elif url.startswith("postgresql://") and "+psycopg" not in url and "+psycopg2" not in url:
        url = "postgresql+psycopg://" + url[len("postgresql://") :]

    return url, connect_args, engine_kwargs


_sa_url, _connect_args, _engine_kwargs = build_engine_url(settings.database_url)

# Ensure the libsql dialect is registered when talking to Turso.
if "+libsql" in _sa_url:
    try:
        import sqlalchemy_libsql  # noqa: F401
    except ImportError as e:
        raise ImportError(
            "DATABASE_URL points at libSQL/Turso but sqlalchemy-libsql is not installed. "
            "Run: pip install sqlalchemy-libsql libsql"
        ) from e
    if not settings.database_auth_token:
        import logging

        logging.getLogger("database").warning(
            "Turso/libSQL URL configured without DATABASE_AUTH_TOKEN (or TURSO_AUTH_TOKEN); "
            "connections will fail until a token is set."
        )

engine: Engine = create_engine(
    _sa_url,
    echo=False,
    connect_args=_connect_args,
    **_engine_kwargs,
)


# Enable foreign keys on SQLite (including Turso/libSQL, which is SQLite-based).
@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record) -> None:  # noqa: ARG001
    # Only SQLite-family connections understand PRAGMA.
    module = type(dbapi_connection).__module__
    if "sqlite" in module or "libsql" in module:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()


SessionLocal = sessionmaker(
    bind=engine,
    class_=Session,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    """Create tables if they don't exist. For anything beyond a demo/
    small deployment, swap this for Alembic migrations."""
    # Import models so they're registered on Base.metadata
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

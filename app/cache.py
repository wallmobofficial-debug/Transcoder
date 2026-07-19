"""
Lightweight in-memory caching layer.

Two caches:
  - file_path_cache: telegram_file_id -> resolved file_path (short TTL,
    since Telegram's download links expire)
  - metadata_cache:  video_id -> list of HLSFile rows (dicts), so repeat
    playlist/segment requests for a hot video skip the DB round-trip

Both are process-local TTLCaches guarded by an asyncio.Lock per key to
avoid duplicate "thundering herd" lookups when many requests race for the
same uncached key (common right after a video goes viral / is first
requested by many player instances at once).
"""
import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from cachetools import TTLCache

from app.config import get_settings

T = TypeVar("T")

settings = get_settings()

file_path_cache: TTLCache = TTLCache(maxsize=settings.file_path_cache_size, ttl=settings.file_path_cache_ttl)
metadata_cache: TTLCache = TTLCache(maxsize=settings.metadata_cache_size, ttl=settings.metadata_cache_ttl)

_locks: dict[str, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()


async def _get_lock(key: str) -> asyncio.Lock:
    async with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _locks[key] = lock
        return lock


async def get_or_set(cache: TTLCache, key: str, factory: Callable[[], Awaitable[T]]) -> T:
    """Cache-aside helper: return cache[key] if present, else compute via
    `factory()` once (even under concurrent callers) and populate it.

    `None` results are returned but not cached, so a miss for a video that
    later becomes ready (or a transient DB blip) is not sticky for the full
    metadata TTL.
    """
    try:
        return cache[key]
    except KeyError:
        pass

    lock = await _get_lock(key)
    async with lock:
        # Re-check after acquiring the lock — another coroutine may have
        # populated it while we were waiting. TTLCache can also expire a key
        # between `__contains__` and `__getitem__`, so use try/except.
        try:
            return cache[key]
        except KeyError:
            pass
        value = await factory()
        if value is not None:
            cache[key] = value
        return value


def invalidate(cache: TTLCache, key: str) -> None:
    cache.pop(key, None)

"""
Thin async wrapper around the Telegram Bot API used purely as a "dumb"
file store:

  - upload_document()   -> streams a local file to Telegram, returns file_id
  - resolve_file_path() -> file_id -> Telegram's internal file path (needed
                            to build a download URL); cached by the caller
  - build_download_url() -> file_path -> full CDN URL
  - open_download()      -> opens a streaming GET against that URL, with
                            optional Range passthrough for seeking

All network calls go through `_call_with_retry`, which handles:
  - Telegram flood-control (HTTP 429 + `retry_after` in the response body)
  - Transient network errors / 5xx via exponential backoff with jitter
"""
import asyncio
import logging
import random

import httpx

from app.config import get_settings

logger = logging.getLogger("telegram_client")


class TelegramAPIError(Exception):
    """Raised when Telegram returns a non-recoverable error."""


class TelegramRateLimitError(Exception):
    """Internal signal carrying the server-mandated retry_after (seconds)."""

    def __init__(self, retry_after: float):
        self.retry_after = retry_after
        super().__init__(f"Rate limited, retry after {retry_after}s")


class TelegramClient:
    def __init__(self):
        settings = get_settings()
        self.bot_token = settings.bot_token
        self.chat_id = settings.storage_chat_id
        self.max_retries = settings.telegram_max_retries

        # Public Telegram or a self-hosted Bot API server (TELEGRAM_API).
        # Self-hosted form: https://<host>/bot<token>/METHOD
        #                   https://<host>/file/bot<token>/<path>
        api_origin = settings.telegram_api_base
        self.api_base = f"{api_origin}/bot{self.bot_token}"
        self.file_base = f"{api_origin}/file/bot{self.bot_token}"
        logger.info("Telegram Bot API origin: %s", api_origin)

        # One shared client for connection pooling. Generous timeouts because
        # video segments can be tens of MB and mobile/server links vary.
        # Self-hosted servers (e.g. on Render free tier) can be slow to wake.
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=30.0))
        self._upload_semaphore = asyncio.Semaphore(settings.telegram_upload_concurrency)

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Retry / backoff core
    # ------------------------------------------------------------------
    async def _call_with_retry(self, coro_fn, *args, **kwargs):
        """Runs coro_fn(*args, **kwargs) with exponential backoff + jitter,
        honoring Telegram's own retry_after when it flood-controls us.

        Permanent client errors (HTTP 4xx other than 429) are not retried.
        """
        attempt = 0
        while True:
            try:
                return await coro_fn(*args, **kwargs)
            except TelegramRateLimitError as e:
                attempt += 1
                if attempt > self.max_retries:
                    raise TelegramAPIError("Exceeded retries after repeated rate limiting") from e
                wait = e.retry_after + random.uniform(0, 0.5)
                logger.warning("Telegram rate limit hit, sleeping %.1fs (attempt %d)", wait, attempt)
                await asyncio.sleep(wait)
            except httpx.HTTPStatusError as e:
                status_code = e.response.status_code if e.response is not None else 0
                # 4xx (except 429, handled above) will not succeed on retry.
                if 400 <= status_code < 500:
                    raise TelegramAPIError(f"Telegram HTTP {status_code}: {e}") from e
                attempt += 1
                if attempt > self.max_retries:
                    raise TelegramAPIError(f"Exceeded retries: {e}") from e
                wait = min(2 ** attempt, 30) + random.uniform(0, 1)
                logger.warning("Telegram request failed (%s), retrying in %.1fs (attempt %d)", e, wait, attempt)
                await asyncio.sleep(wait)
            except httpx.TransportError as e:
                attempt += 1
                if attempt > self.max_retries:
                    raise TelegramAPIError(f"Exceeded retries: {e}") from e
                wait = min(2 ** attempt, 30) + random.uniform(0, 1)
                logger.warning("Telegram request failed (%s), retrying in %.1fs (attempt %d)", e, wait, attempt)
                await asyncio.sleep(wait)

    @staticmethod
    def _raise_for_telegram_response(resp: httpx.Response) -> dict:
        if resp.status_code == 429:
            body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            retry_after = float(body.get("parameters", {}).get("retry_after", 5))
            raise TelegramRateLimitError(retry_after)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise TelegramAPIError(f"Telegram API error: {data}")
        return data["result"]

    # ------------------------------------------------------------------
    # Upload
    # ------------------------------------------------------------------
    async def upload_document(self, file_path: str, filename: str) -> tuple[str, int]:
        """Uploads a local file to the storage chat as a document.
        Returns (telegram_file_id, file_size_bytes).

        Uses a file handle (not a fully-read buffer) so httpx streams the
        multipart body from disk rather than holding it all in memory.

        NOTE: the standard Telegram Bot API caps bot uploads at 50MB per
        file. For larger HLS segments, run a self-hosted Bot API server
        (https://github.com/tdlib/telegram-bot-api) which raises this to
        2GB, and point `api_base`/`file_base` at it instead.
        """

        async def _do_upload():
            async with self._upload_semaphore:
                with open(file_path, "rb") as fh:
                    files = {"document": (filename, fh, "application/octet-stream")}
                    data = {"chat_id": self.chat_id}
                    resp = await self._client.post(f"{self.api_base}/sendDocument", data=data, files=files)
                    return self._raise_for_telegram_response(resp)

        result = await self._call_with_retry(_do_upload)
        document = result.get("document") or result.get("video") or result.get("audio")
        if document is None:
            raise TelegramAPIError(f"Unexpected sendDocument response shape: {result}")
        return document["file_id"], document.get("file_size", 0)

    # ------------------------------------------------------------------
    # Resolve file_id -> downloadable path
    # ------------------------------------------------------------------
    async def resolve_file_path(self, file_id: str) -> str:
        """getFile: exchanges a file_id for the internal file_path needed to
        build a download URL. This is what should be cached upstream, since
        Telegram's returned links are only valid for ~1 hour."""

        async def _do_get_file():
            resp = await self._client.get(f"{self.api_base}/getFile", params={"file_id": file_id})
            return self._raise_for_telegram_response(resp)

        result = await self._call_with_retry(_do_get_file)
        return result["file_path"]

    def build_download_url(self, file_path: str) -> str:
        return f"{self.file_base}/{file_path}"

    # ------------------------------------------------------------------
    # Streaming download (with optional Range passthrough)
    # ------------------------------------------------------------------
    def open_download(self, url: str, range_header: str | None = None):
        """Returns an `httpx.AsyncClient.stream` async context manager for
        the given URL. Callers should `async with` this directly so the
        response is only opened (and connections held) for as long as
        needed to stream it out to the client.

        Telegram's file CDN honors standard HTTP Range headers, so we pass
        the client's Range header straight through for true byte-range
        seeking without buffering the whole segment.
        """
        headers = {"Range": range_header} if range_header else {}
        return self._client.stream("GET", url, headers=headers)

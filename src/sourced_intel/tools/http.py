"""Shared HTTP client: one UA, one timeout policy, one retry policy.

Also implements the global rate limiter required by GDELT, which answers 429 to
anything faster than roughly one request every 5 seconds from a shared egress IP.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import httpx

from ..config import Settings, get_settings

logger = logging.getLogger(__name__)

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Per-host minimum interval in seconds. GDELT is strict; others are lenient.
HOST_MIN_INTERVAL: dict[str, float] = {
    "api.gdeltproject.org": 20.0,
}

RETRY_STATUS = {429, 500, 502, 503, 504}


@dataclass
class FetchResult:
    url: str
    status: int
    text: str
    ok: bool
    error: str | None = None
    from_cache: bool = False


class RateLimiter:
    """Global, thread-safe, per-host pacing."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}

    def wait(self, host: str, min_interval: float) -> float:
        if min_interval <= 0:
            return 0.0
        with self._lock:
            now = time.monotonic()
            last = self._last.get(host, 0.0)
            delay = max(0.0, min_interval - (now - last))
            if delay > 0:
                time.sleep(delay)
            self._last[host] = time.monotonic()
            return delay


class HttpClient:
    """Thin wrapper over httpx with retries, pacing and honest error reporting."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.limiter = RateLimiter()
        self._client = httpx.Client(
            timeout=httpx.Timeout(self.settings.http_timeout, connect=8.0),
            follow_redirects=True,
            headers={
                "User-Agent": DEFAULT_UA,
                "Accept": "text/html,application/json,*/*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )

    def get(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        retries: int | None = None,
        min_interval: float | None = None,
        timeout: float | None = None,
    ) -> FetchResult:
        host = httpx.URL(url).host or ""
        interval = min_interval if min_interval is not None else HOST_MIN_INTERVAL.get(host, 0.0)
        max_attempts = (self.settings.http_max_retries if retries is None else retries) + 1
        backoff = 5.0
        last_error = "unknown"

        for attempt in range(1, max_attempts + 1):
            waited = self.limiter.wait(host, interval if attempt == 1 else max(interval, backoff))
            if waited:
                logger.debug("paced %.1fs before %s", waited, host)
            try:
                resp = self._client.get(
                    url, params=params, timeout=timeout or self.settings.http_timeout
                )
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("GET %s failed (attempt %d): %s", url, attempt, last_error)
            else:
                if resp.status_code < 400:
                    return FetchResult(
                        url=str(resp.url), status=resp.status_code, text=resp.text, ok=True
                    )
                last_error = f"http {resp.status_code}"
                if resp.status_code not in RETRY_STATUS:
                    return FetchResult(
                        url=url,
                        status=resp.status_code,
                        text=resp.text[:500],
                        ok=False,
                        error=last_error,
                    )
                logger.warning("GET %s -> %s (attempt %d)", url, resp.status_code, attempt)

            if attempt < max_attempts:
                backoff = min(backoff * 2, 40.0)
        return FetchResult(url=url, status=0, text="", ok=False, error=last_error)

    def close(self) -> None:
        self._client.close()


_client: HttpClient | None = None


def get_http(settings: Settings | None = None, reload: bool = False) -> HttpClient:
    global _client
    if _client is None or reload:
        _client = HttpClient(settings)
    return _client

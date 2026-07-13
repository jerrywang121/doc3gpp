from __future__ import annotations

import logging
import time
from collections.abc import Callable

import httpx

from doc3gpp.config import get_settings

logger = logging.getLogger(__name__)


# HTTP status codes that are worth retrying. 4xx is treated as terminal
# (the client request is bad and replaying it won't help).
_RETRYABLE_STATUS_CODES = frozenset({500, 502, 503, 504, 408, 429})


class ScraperClient:
    """Thin HTTP client wrapper for 3gpp.org requests with retry/backoff.

    Centralizes HTTP configuration, error handling, and transient-failure
    retries for all scraping operations performed against 3gpp.org.

    Transient failures (connection errors, timeouts, and ``5xx``/``408``/
    ``429`` responses) are retried with exponential backoff up to
    ``settings.http_max_retries`` times. Non-retryable failures (4xx other
    than 408/429, programming errors) raise immediately so callers don't
    silently swallow bad requests.
    """

    DEFAULT_TIMEOUT_SECONDS = 20.0
    USER_AGENT = "doc3gpp/0.1 (+https://github.com/jerrywang121/doc3gpp)"

    def __init__(self, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        """Create an HTTP client with configurable timeout and verification."""
        settings = get_settings()
        self._max_retries = settings.http_max_retries
        self._backoff = settings.http_retry_backoff
        self._client = httpx.Client(
            timeout=timeout_seconds,
            headers={"User-Agent": self.USER_AGENT},
            follow_redirects=True,
            verify=settings.http_verify,
        )

    def get_text(self, url: str) -> str:
        """Fetch a URL and return its response body as text.

        Retries transient HTTP failures with exponential backoff. Re-raises
        ``httpx.HTTPError`` (and its subclasses) on terminal failure.
        """
        logger.debug("Fetching text URL: %s", url)
        response = self._request_with_retry(url, lambda: self._client.get(url))
        return response.text

    def get_bytes(self, url: str) -> bytes:
        """Fetch a URL and return its raw response body bytes.

        Retries transient HTTP failures with exponential backoff. Re-raises
        ``httpx.HTTPError`` (and its subclasses) on terminal failure.
        """
        logger.debug("Fetching bytes URL: %s", url)
        response = self._request_with_retry(url, lambda: self._client.get(url))
        return response.content

    def head(self, url: str) -> httpx.Response:
        """Issue a HEAD request and return the response.

        Reuses the same retry/backoff policy as GET requests. HEAD is used
        to read headers (e.g. ``Last-Modified``) without downloading the
        response body.
        """
        logger.debug("Fetching HEAD URL: %s", url)
        return self._request_with_retry(url, lambda: self._client.head(url))

    def _request_with_retry(
        self, url: str, request_fn: Callable[[], httpx.Response]
    ) -> httpx.Response:
        """Issue an HTTP request, retrying on transient failures.

        Args:
            url: Target URL (used only for logging).
            request_fn: Zero-argument callable that performs one HTTP
                request and returns the response.

        Returns the response on success. Raises the last ``httpx.HTTPError``
        if all retries are exhausted or the error is non-retryable.
        """
        attempt = 0
        last_exc: httpx.HTTPError | None = None
        while attempt <= self._max_retries:
            try:
                response = request_fn()
            except httpx.HTTPError as exc:
                last_exc = exc
                if not self._is_retryable_exception(exc) or attempt == self._max_retries:
                    logger.error(
                        "Failed to fetch %s after %s attempt(s): %s",
                        url,
                        attempt + 1,
                        exc,
                    )
                    raise
                delay = self._backoff * (2**attempt)
                logger.warning(
                    "Transient error fetching %s (attempt %s/%s): %s; retrying in %.2fs",
                    url,
                    attempt + 1,
                    self._max_retries + 1,
                    exc,
                    delay,
                )
                self._sleep(delay)
                attempt += 1
                continue

            if response.status_code in _RETRYABLE_STATUS_CODES:
                msg = f"HTTP {response.status_code}"
                last_exc = httpx.HTTPStatusError(
                    msg, request=response.request, response=response
                )
                if attempt == self._max_retries:
                    logger.error(
                        "Failed to fetch %s after %s attempt(s): %s",
                        url,
                        attempt + 1,
                        msg,
                    )
                    raise last_exc
                delay = self._backoff * (2**attempt)
                logger.warning(
                    "Retryable status %s fetching %s (attempt %s/%s); retrying in %.2fs",
                    response.status_code,
                    url,
                    attempt + 1,
                    self._max_retries + 1,
                    delay,
                )
                self._sleep(delay)
                attempt += 1
                continue

            response.raise_for_status()
            return response

        # Loop exits only via raise above, but keep a defensive fallback.
        assert last_exc is not None  # pragma: no cover - loop invariant
        raise last_exc

    @staticmethod
    def _is_retryable_exception(exc: httpx.HTTPError) -> bool:
        """Decide whether a low-level HTTP exception is worth retrying.

        Connection errors, timeouts, and protocol errors are transient;
        other ``HTTPError`` subclasses (e.g. ``InvalidURL``) are programming
        mistakes that retrying cannot fix.
        """
        return isinstance(
            exc,
            (
                httpx.ConnectError,
                httpx.ConnectTimeout,
                httpx.ReadTimeout,
                httpx.WriteTimeout,
                httpx.PoolTimeout,
                httpx.NetworkError,
                httpx.RemoteProtocolError,
                httpx.ProtocolError,
            ),
        )

    @staticmethod
    def _sleep(seconds: float) -> None:
        """Sleep helper, isolated so tests can monkeypatch it."""
        if seconds > 0:
            time.sleep(seconds)

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def __enter__(self) -> "ScraperClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
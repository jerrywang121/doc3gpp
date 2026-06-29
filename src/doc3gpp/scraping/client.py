from __future__ import annotations

import logging
import httpx

from doc3gpp.config import get_settings

logger = logging.getLogger(__name__)


class ScraperClient:
    """Thin HTTP client wrapper for 3gpp.org requests.

    This client centralizes HTTP configuration and error handling for all
    scraping operations performed against 3gpp.org.
    """

    def __init__(self, timeout_seconds: float = 20.0) -> None:
        """Create an HTTP client with configurable timeout and verification."""
        settings = get_settings()
        self._client = httpx.Client(
            timeout=timeout_seconds,
            headers={"User-Agent": "doc3gpp/0.1 (+https://github.com)"},
            follow_redirects=True,
            verify=settings.http_verify,
        )

    def get_text(self, url: str) -> str:
        """Fetch a URL and return its response body as text."""
        logger.debug("Fetching text URL: %s", url)
        try:
            response = self._client.get(url)
            response.raise_for_status()
            return response.text
        except Exception:
            logger.exception("Failed to fetch text from %s", url)
            raise

    def get_bytes(self, url: str) -> bytes:
        """Fetch a URL and return its raw response body bytes."""
        logger.debug("Fetching bytes URL: %s", url)
        try:
            response = self._client.get(url)
            response.raise_for_status()
            return response.content
        except Exception:
            logger.exception("Failed to fetch bytes from %s", url)
            raise

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def __enter__(self) -> "ScraperClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

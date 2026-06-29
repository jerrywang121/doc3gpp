from __future__ import annotations

import httpx

from doc3gpp.config import get_settings


class ScraperClient:
    """Thin HTTP client wrapper for 3gpp.org requests."""

    def __init__(self, timeout_seconds: float = 20.0) -> None:
        settings = get_settings()
        self._client = httpx.Client(
            timeout=timeout_seconds,
            headers={"User-Agent": "doc3gpp/0.1 (+https://github.com)"},
            follow_redirects=True,
            verify=settings.http_verify,
        )

    def get_text(self, url: str) -> str:
        response = self._client.get(url)
        response.raise_for_status()
        return response.text

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ScraperClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

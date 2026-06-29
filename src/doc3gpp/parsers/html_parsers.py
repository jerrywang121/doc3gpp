from __future__ import annotations

from bs4 import BeautifulSoup


def parse_title(html: str) -> str:
    """Return page title from HTML content."""

    soup = BeautifulSoup(html, "lxml")
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    return ""

from __future__ import annotations

from doc3gpp.parsers.calendar_parser import parse_3gpp_calendar
from doc3gpp.scraping.client import ScraperClient


def fetch_calendar(calendar_url: str) -> list:
    """Fetch and parse 3GPP meetings calendar page."""

    with ScraperClient() as client:
        html = client.get_text(calendar_url)
    return parse_3gpp_calendar(html)

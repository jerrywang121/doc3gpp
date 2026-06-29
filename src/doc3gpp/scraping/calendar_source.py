from __future__ import annotations

import logging

from doc3gpp.parsers.calendar_parser import parse_3gpp_calendar
from doc3gpp.scraping.client import ScraperClient

logger = logging.getLogger(__name__)


def fetch_calendar(calendar_url: str) -> list:
    """Fetch and parse 3GPP meetings calendar page."""

    logger.info("Fetching calendar page from %s", calendar_url)
    with ScraperClient() as client:
        html = client.get_text(calendar_url)
    return parse_3gpp_calendar(html)

"""Scrapers for fetching 3GPP Work Item (WI) DynaReport pages."""

from __future__ import annotations

import logging

from doc3gpp.scraping.client import ScraperClient

logger = logging.getLogger(__name__)


_WI_URL_TEMPLATE = "https://www.3gpp.org/dynareport?code=TSG-WG--{tsg}--wis.htm"


def build_wi_url(tsg_short: str) -> str:
    """Compose the DynaReport URL for the WI list of a given TSG.

    The ``tsg_short`` argument is uppercased; the project's canonical short
    names (e.g. ``R5``, ``S2``) are stored uppercase in the ``tsgs`` table
    and the URL pattern is case-insensitive on the upstream side, but the
    DynaReport module normalises its ``code`` parameter to uppercase anyway.
    """
    return _WI_URL_TEMPLATE.format(tsg=tsg_short.upper())


def fetch_wis(tsg_short: str) -> str:
    """Fetch the raw HTML body of the WI DynaReport page for a TSG.

    Args:
        tsg_short: Canonical TSG short name (e.g. ``R5``). Case-insensitive.

    Returns:
        The response body decoded as text, ready to feed into
        :func:`doc3gpp.parsers.wi_parser.parse_3gpp_wis`.

    Raises:
        httpx.HTTPError: When the upstream request fails (propagated from
            :class:`ScraperClient`).
    """
    url = build_wi_url(tsg_short)
    logger.debug("Fetching WI page for TSG %s at %s", tsg_short, url)
    with ScraperClient() as client:
        return client.get_text(url)

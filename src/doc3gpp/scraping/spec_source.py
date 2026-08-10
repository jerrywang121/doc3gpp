"""Scrapers for fetching 3GPP spec DynaReport pages and follow-ups.

Network-only: each function returns raw body text; parsing lives in
:mod:`doc3gpp.parsers.spec_parser`.
"""

from __future__ import annotations

import logging

from doc3gpp.scraping.client import ScraperClient

logger = logging.getLogger(__name__)

_SPEC_LIST_URL_TEMPLATE = "https://www.3gpp.org/dynareport?code=TSG-WG--{tsg}.htm"
_SPEC_DETAIL_URL_TEMPLATE = "https://www.3gpp.org/DynaReport/{spec_id_no_dot}.htm"
_ETSI_URL_TEMPLATE = "https://portal.etsi.org/webapp/workprogram/Report_WorkItem.asp?WKI_ID={wki_id}"
_CR_LIST_URL_TEMPLATE = "https://portal.3gpp.org/ChangeRequests.aspx?q=1&versionId={version_id}"


def build_spec_list_url(tsg_short: str) -> str:
    """Compose the DynaReport list URL for a TSG (e.g. ``R5``)."""
    return _SPEC_LIST_URL_TEMPLATE.format(tsg=tsg_short.upper())


def build_spec_detail_url(spec_id_no_dot: str) -> str:
    """Compose the DynaReport detail URL from the dotless slug."""
    return _SPEC_DETAIL_URL_TEMPLATE.format(spec_id_no_dot=spec_id_no_dot)


def fetch_spec_list(tsg_short: str) -> str:
    """Fetch the raw HTML body of the per-TSG spec list page."""
    url = build_spec_list_url(tsg_short)
    logger.debug("Fetching spec list for TSG %s at %s", tsg_short, url)
    with ScraperClient() as client:
        return client.get_text(url)


def fetch_spec_detail(spec_id_no_dot: str) -> str:
    """Fetch the raw HTML body of a spec detail page."""
    url = build_spec_detail_url(spec_id_no_dot)
    logger.debug("Fetching spec detail at %s", url)
    with ScraperClient() as client:
        return client.get_text(url)


def fetch_etsi_pdf_text(wki_id: int, client: ScraperClient) -> str:
    """Fetch the ETSI work-item page body for ``wki_id``."""
    url = _ETSI_URL_TEMPLATE.format(wki_id=wki_id)
    logger.debug("Fetching ETSI work item %s at %s", wki_id, url)
    return client.get_text(url)


def fetch_cr_list(version_id: int, client: ScraperClient) -> str:
    """Fetch the 3GPP change-request list page body for a version."""
    url = _CR_LIST_URL_TEMPLATE.format(version_id=version_id)
    logger.debug("Fetching CR list for versionId %s at %s", version_id, url)
    return client.get_text(url)

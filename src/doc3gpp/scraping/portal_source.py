from __future__ import annotations

import logging

from doc3gpp.models.tdoc import TDoc
from doc3gpp.parsers.tdoc_parser import read_tdoc_sheet
from doc3gpp.scraping.client import ScraperClient
from doc3gpp.scraping.ftp_source import _normalize_optional_url

logger = logging.getLogger(__name__)


def fetch_tdocs_from_portal(
    meeting_id: int,
    url_template: str,
    client: ScraperClient | None = None,
) -> list[TDoc]:
    """Download a meeting's TDoc-list XLSX from the 3GPP portal and parse it.

    The portal endpoint returns the XLSX directly; the sheet format is the
    same as the legacy ``TDoc_List_Meeting_*.xlsx`` files, so the rows are
    handed straight to :func:`~doc3gpp.parsers.tdoc_parser.read_tdoc_sheet`.

    Args:
        meeting_id: The numeric 3GPP meeting identifier (matches
            ``Meeting.meeting_id``).
        url_template: A Python ``str.format`` template that must contain the
            literal placeholder ``{meeting_id}`` and produce a URL with the
            ``meetingId=`` query parameter.
        client: Optional :class:`~doc3gpp.scraping.client.ScraperClient` to
            use for the HTTP request. When omitted, a fresh client is created
            and closed before returning.

    Returns:
        Parsed TDoc rows with ``meeting_id`` stamped on every row.

    Raises:
        ValueError: If ``url_template`` is missing the ``{meeting_id}``
            placeholder or the formatted URL does not contain ``meetingId=``.
        httpx.HTTPError: On terminal HTTP failure (consistent with the rest
            of the scraping layer).
    """
    if "{meeting_id}" not in url_template:
        raise ValueError(
            f"url_template must contain '{{meeting_id}}' placeholder: {url_template!r}"
        )

    try:
        url = url_template.format(meeting_id=meeting_id)
    except (ValueError, KeyError, IndexError) as exc:
        raise ValueError(f"invalid url_template {url_template!r}: {exc}") from exc

    if "meetingId=" not in url:
        raise ValueError(
            f"url_template must produce a URL containing 'meetingId=': {url!r}"
        )

    logger.info(
        "Fetching TDoc list from portal for meeting_id=%s: %s", meeting_id, url
    )

    scraper = client or ScraperClient()
    try:
        xlsx_bytes = scraper.get_bytes(url)
    finally:
        if client is None:
            scraper.close()

    records = read_tdoc_sheet(xlsx_bytes)
    logger.info(
        "Parsed %s TDoc rows from portal for meeting_id=%s", len(records), meeting_id
    )

    return [
        TDoc(
            tdoc_id=row["tdoc"],
            title=row.get("title"),
            meeting_id=meeting_id,
            ftp_url=_normalize_optional_url(row.get("tdoc_url")),
            source=row.get("source"),
            type=row.get("type"),
            status=row.get("status"),
            reservation_date=row.get("reservation_date"),
            uploaded_date=row.get("uploaded_date"),
            cr_cat=row.get("cr_cat"),
            cr_pack=row.get("cr_pack"),
            tdoc_for=row.get("tdoc_for"),
            abstract=row.get("abstract"),
            secretary_remarks=row.get("secretary_remarks"),
            ls_to=row.get("ls_to"),
            ls_cc=row.get("ls_cc"),
            original_ls=row.get("original_ls"),
            is_revision_of=row.get("is_revision_of"),
            revised_to=row.get("revised_to"),
            release=row.get("release"),
            spec=row.get("spec"),
            version=row.get("version"),
            related_wis=row.get("related_wis"),
            cr_num=row.get("cr_num"),
        )
        for row in records
    ]

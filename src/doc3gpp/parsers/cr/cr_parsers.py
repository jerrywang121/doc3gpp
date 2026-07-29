"""Concrete :class:`TDocParser` implementations for CR TDocs."""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import fields
from datetime import date as _date
from datetime import datetime as _datetime
from typing import Any

from doc3gpp.models.tdoc_cr import (
    TDocCRChangeDetails,
    TDocCRDetails,
    TDocCRParseResult,
    TDocCRTTCNDetails,
)
from doc3gpp.parsers.cr.body_changes import extract_body_changes
from doc3gpp.parsers.cr.cover_page import CRCoverPageParser
from doc3gpp.parsers.cr.header import (
    CRHeaderMissingError,
    is_cr_header_present,
    is_ttcn_tdoc,
)
from doc3gpp.parsers.cr.helpers import _COVER_FIELDS
from doc3gpp.parsers.cr.ttcn_functions import extract_changed_functions
from doc3gpp.parsers.tdoc_parsers import SectionParser, TDocParser
from doc3gpp.parsers.cr.ttcn_sections import (
    TTCNCorrectionsParser,
    TTCNOverviewParser,
)
from doc3gpp.settings.loader import get_settings


logger = logging.getLogger(__name__)


def _parse_cover_date(raw: str) -> _date | None:
    """Parse a cover-page date cell, accepting ISO 8601 plus the
    slash and month-name shapes that appear in real 3GPP CRs."""
    text = raw.strip()
    if not text:
        return None
    try:
        return _date.fromisoformat(text)
    except ValueError:
        pass
    for fmt in (
        "%d/%m/%Y",
        "%m/%d/%Y",
        "%d %B %Y",
        "%B %d, %Y",
        "%d %b %Y",
        "%b %d, %Y",
        "%d-%b-%Y",
    ):
        try:
            return _datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    logger.warning(
        "Could not parse cover-page date %r as ISO 8601 or common "
        "fallback formats; leaving tdoc_cr_cover_page.date as None",
        raw,
    )
    return None


class CRParserBase(TDocParser):
    """Common orchestration for CR-type TDoc parsers.

    Concrete subclasses register themselves by overriding :meth:`supports`.
    """

    parser_version = "1.0.0"

    def __init__(
        self,
        cover: CRCoverPageParser,
        details: Sequence[SectionParser] = (),
    ) -> None:
        self._cover = cover
        self._details = list(details)

    def supports(
        self, tdoc_id: str, *, tdoc_type: str | None = None, spec: str | None = None
    ) -> bool:
        return tdoc_type in (None, "CR")

    def parse(
        self,
        markdown: str,
        *,
        tdoc_id: str,
        max_text_length: int = 0,
        full: bool = False,
    ) -> TDocCRParseResult:
        if not (tdoc_id or "").strip():
            raise ValueError("tdoc_id is required and cannot be empty")

        lines = markdown.splitlines()
        if not lines:
            raise CRHeaderMissingError(
                "Refusing to parse empty markdown as a CR document",
                snippet="",
            )

        present, header_blob = is_cr_header_present(markdown)
        if not present:
            raise CRHeaderMissingError(
                "Markdown does not contain a '3GPP TSG-' header; "
                "this does not look like a 3GPP CR document.",
                snippet=header_blob[:100],
            )

        if not re.search(
            r"3GPP\s+TSG-\w+\s+Meeting\s+#\d{4}-TTCN\s+email",
            header_blob,
            re.IGNORECASE,
        ):
            logger.warning(
                "CR header does not match the expected "
                "'3GPP TSG-<WG> Meeting #<YEAR>-TTCN email' layout; "
                "extraction may be incomplete"
            )

        _ok, cover_details, _adv = self._cover.parse(
            lines, max_text_length=max_text_length
        )

        details_payload: dict[str, Any] = {}
        for parser in self._details:
            ok, payload, _adv = parser.parse(
                lines, max_text_length=max_text_length, full=full
            )
            if ok:
                details_payload[parser.name] = payload

        extracted = cover_details.get("tdoc_id")
        final_tdoc_id = tdoc_id.strip()
        if extracted and extracted.lower() != final_tdoc_id.lower():
            logger.warning(
                "Header tdoc_id %r differs from input %r; using input as canonical",
                extracted,
                final_tdoc_id,
            )

        resolved_tsg = cover_details.get("tsg") or None
        if not resolved_tsg and len(final_tdoc_id) >= 2:
            fallback_tsg = final_tdoc_id[:2].upper()
            logger.warning(
                "Cover-page tsg was empty; falling back to '%s' from input tdoc_id",
                fallback_tsg,
            )
            resolved_tsg = fallback_tsg

        raw_date = cover_details.get("date")
        date_value: _date | None = None
        if raw_date:
            date_value = _parse_cover_date(raw_date)

        payload: dict[str, Any] = {"tdoc_id": final_tdoc_id}
        for key in _COVER_FIELDS:
            payload[key] = cover_details.get(key)
        payload["tsg"] = resolved_tsg
        payload["date"] = date_value
        payload["extracted_tdoc_id"] = extracted

        valid_keys = {f.name for f in fields(TDocCRDetails)}
        filtered = {k: v for k, v in payload.items() if k in valid_keys}
        cover = TDocCRDetails(**filtered)

        ttcn: TDocCRTTCNDetails | None = None
        overview = details_payload.get("overview")
        if overview is not None:
            corrections = details_payload.get("corrections", [])
            ttcn = TDocCRTTCNDetails(
                tdoc_id=final_tdoc_id,
                ftp_url=None,
                testcase=overview.get("testcase"),
                ue=overview.get("ue"),
                ss=overview.get("ss"),
                ats_version=overview.get("ats_version"),
                ttcn_release=overview.get("ttcn_release"),
                test_suite=overview.get("test_suite"),
                required_changes=list(corrections),
                changed_functions=extract_changed_functions(corrections),
            )

        settings = get_settings()
        changes: TDocCRChangeDetails | None = None
        if settings.tdoc_parse.body_change_enabled:
            changes = extract_body_changes(
                lines,
                gap_window=settings.tdoc_parse.body_change_gap_window,
                context_padding=settings.tdoc_parse.body_change_context_padding,
            )

        return TDocCRParseResult(cover=cover, ttcn=ttcn, changes=changes)


class CRParser(CRParserBase):
    """Generic CR parser (no TTCN-specific sections)."""

    def __init__(self) -> None:
        super().__init__(cover=CRCoverPageParser(), details=())

    def supports(
        self, tdoc_id: str, *, tdoc_type: str | None = None, spec: str | None = None
    ) -> bool:
        return super().supports(
            tdoc_id, tdoc_type=tdoc_type, spec=spec
        ) and not is_ttcn_tdoc(tdoc_id)


class TTCNCRParser(CRParserBase):
    """TTCN-specific CR parser that runs overview + corrections."""

    def __init__(self) -> None:
        super().__init__(
            cover=CRCoverPageParser(),
            details=[
                TTCNOverviewParser(),
                TTCNCorrectionsParser(),
            ],
        )

    def supports(
        self, tdoc_id: str, *, tdoc_type: str | None = None, spec: str | None = None
    ) -> bool:
        return super().supports(
            tdoc_id, tdoc_type=tdoc_type, spec=spec
        ) and is_ttcn_tdoc(tdoc_id)

    def parse(
        self,
        markdown: str,
        *,
        tdoc_id: str,
        max_text_length: int = 0,
        full: bool = False,
    ) -> TDocCRParseResult:
        """TTCN CRs run the base extraction and then drop the body
        sidecar (``changes=None``) — TTCN CRs write the
        ``tdoc_cr_ttcn_details`` sidecar instead."""
        result = super().parse(
            markdown, tdoc_id=tdoc_id,
            max_text_length=max_text_length, full=full,
        )
        return TDocCRParseResult(cover=result.cover, ttcn=result.ttcn, changes=None)
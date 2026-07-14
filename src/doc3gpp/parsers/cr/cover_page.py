"""Cover-page :class:`SectionParser` for CR markdown bodies."""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence

from doc3gpp.parsers.cr.header import _TDOC_HEADER_PATTERN
from doc3gpp.parsers.cr.helpers import (
    _search_pattern_in_lines,
)


logger = logging.getLogger(__name__)


# Cover-page row that holds spec / CR / rev / version.
_COVER_SPEC_RE = re.compile(
    r"\|\s*\b(\d{2}\.\d{3}(?:-\d)?)\b\s*\|\s*CR\s*\|"
    r"\s*([\d\w-]+)\s*\|\s*rev\s*\|\s*([\d\w-]+)\s*\|"
    r"\s*Current version:\s*\|\s*(\d{1,2}\.\d{1,2}\.\d{1,2})\s*\|",
    re.IGNORECASE,
)
_COVER_TITLE_RE = re.compile(r"\|\s*Title:\s*\|\s*(.*?)\s*\|", re.IGNORECASE)
_COVER_SOURCE_RE = re.compile(r"\|\s*Source to WG:\s*\|\s*(.*?)\s*\|", re.IGNORECASE)
_COVER_TSG_RE = re.compile(r"\|\s*Source to TSG:\s*\|\s*(.*?)\s*\|", re.IGNORECASE)
_COVER_WI_DATE_RE = re.compile(
    r"\|\s*Work item code:\s*\|\s*(.*?)\s*\|(?:\s+\|)*\s*Date:\s*\|\s*([\d\-]+)\s*\|",
    re.IGNORECASE,
)
# Category + Release on the same line.
_COVER_CATREL_RE = re.compile(
    r"\|\s*Category:\s*\|\s*([^\s|]+)(?:\s*\|)+"
    r"\s*Release:(?:\s*\|)*\s*([^\s|]+)\s*\|",
    re.IGNORECASE,
)
_COVER_REASON_RE = re.compile(r"\|\s*Reason for change:(?:\s*\|)+\s*(.*?)\s*\|", re.IGNORECASE)
_COVER_CONSEQUENCES_RE = re.compile(
    r"\|\s*Consequences if not approved:(?:\s*\|)+\s*(.*?)\s*\|",
    re.IGNORECASE,
)
_COVER_CLAUSES_RE = re.compile(r"\|\s*Clauses affected:(?:\s*\|)+\s*(.*?)\s*\|", re.IGNORECASE)
_COVER_OTHER_RE = re.compile(r"\|\s*Other comments:(?:\s*\|)+\s*(.*?)\s*\|", re.IGNORECASE)
_COVER_REVHIST_RE = re.compile(
    r"\|\s*This CR's revision history:(?:\s*\|)+\s*(.*?)\s*\|",
    re.IGNORECASE,
)


class CRCoverPageParser:
    """Parse the cover-page fields of a CR markdown body.

    Implements the :class:`SectionParser[dict[str, str]]` Protocol.
    Returns a dict keyed by :data:`_COVER_FIELDS` plus ``tdoc_id``.
    """

    name = "cover_page"

    def parse(
        self,
        lines: Sequence[str],
        *,
        max_text_length: int = 0,
        full: bool = False,
    ) -> tuple[bool, dict[str, str], int]:
        details: dict[str, str] = {}

        patterns: list[tuple[bool, list[str], list[int], re.Pattern[str]]] = [
            (
                False,
                ["tdoc_id"],
                [1],
                _TDOC_HEADER_PATTERN,
            ),
            (
                False,
                ["spec", "cr_num", "rev", "version"],
                [1, 2, 3, 4],
                _COVER_SPEC_RE,
            ),
            (False, ["title"], [1], _COVER_TITLE_RE),
            (False, ["source"], [1], _COVER_SOURCE_RE),
            (False, ["tsg"], [1], _COVER_TSG_RE),
            (True, ["related_wis", "date"], [1, 2], _COVER_WI_DATE_RE),
            (
                False,
                ["cr_cat", "release"],
                [1, 2],
                _COVER_CATREL_RE,
            ),
            (False, ["reason_for_change"], [1], _COVER_REASON_RE),
            (
                False,
                ["consequences_if_not_approved"],
                [1],
                _COVER_CONSEQUENCES_RE,
            ),
            (True, ["clauses_affected"], [1], _COVER_CLAUSES_RE),
            (True, ["other_comments"], [1], _COVER_OTHER_RE),
            (True, ["revision_history"], [1], _COVER_REVHIST_RE),
        ]

        for optional, field_names, group_nums, regex in patterns:
            matched_at = _search_pattern_in_lines(
                lines, details, field_names, group_nums, regex
            )
            if matched_at == 0:
                if not optional:
                    logger.warning(
                        "CR Cover Page does not contain a valid %s; "
                        "leaving the corresponding dataclass field as None",
                        field_names[0],
                    )
                else:
                    logger.debug(
                        "Optional field(s) missing in CR Cover Page: %s; skipping",
                        field_names,
                    )

        cr_num = details.get("cr_num", "") or ""
        if cr_num and not re.fullmatch(r"\d+", cr_num):
            logger.warning(
                "CR number in Cover Page is not digits-only: %r; clearing",
                cr_num,
            )
            details["cr_num"] = ""
        rev = details.get("rev", "") or ""
        if rev == "-":
            details["rev"] = "0"
        elif rev and not re.fullmatch(r"\d+", rev):
            logger.warning(
                "CR revision in Cover Page is not digits-only: %r; clearing", rev
            )
            details["rev"] = ""

        if max_text_length > 0:
            for field_name in (
                "reason_for_change",
                "consequences_if_not_approved",
                "other_comments",
                "revision_history",
            ):
                value = details.get(field_name)
                if value and len(value) > max_text_length:
                    logger.warning(
                        "Truncating %s to %d characters for cover page parsing",
                        field_name,
                        max_text_length,
                    )
                    details[field_name] = value[:max_text_length]

        _blank_cells_to_none(details)
        return True, details, len(lines)


def _blank_cells_to_none(details: dict[str, str | None]) -> None:
    """Coerce blank cover-page cell values to ``None`` in place."""
    for key in list(details):
        if not details[key]:
            details[key] = None
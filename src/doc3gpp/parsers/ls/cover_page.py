"""3GPP LS header-field extractor.

:class:`LSCoverPageParser` walks the first ~50 lines of a 3GPP LS
markdown body, extracts the eleven structured fields from the
template (title, response-to triple, release, work-item pair, source,
to / cc groups, attachments list), and returns them as a dict that
the :class:`LSParserBase` orchestrator maps onto a
:class:`TDocLSDetails`.

Non-3GPP variants inherit the class shape but override the regex
patterns and the ``supports_source`` predicate. v1 ships the 3GPP
implementation only — ``ieee`` and ``etsi`` stubs live as siblings
under :mod:`doc3gpp.parsers.ls.variants`.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence

logger = logging.getLogger(__name__)


_TITLE_RE = re.compile(r"^Title:\s*(.*)$", re.IGNORECASE)
_RESPONSE_TO_RAW_RE = re.compile(r"^Response to:\s*(.*)$", re.IGNORECASE)
_RELEASE_RE = re.compile(r"^Release:\s*(.*)$", re.IGNORECASE)
_WORK_ITEM_RE = re.compile(
    r"^Work Item:\s*(.+?)\s*(?:\(([^)]*)\))?\s*$", re.IGNORECASE
)
_SOURCE_RE = re.compile(r"^Source:\s*(.*)$", re.IGNORECASE)
_TO_RE = re.compile(r"^To:\s*(.*)$", re.IGNORECASE)
_CC_RE = re.compile(r"^Cc:\s*(.*)$", re.IGNORECASE)
_ATTACHMENTS_RE = re.compile(r"^Attachments:\s*(.*)$", re.IGNORECASE)

# Long / prose release labels: ``Release 20``, ``Release 1999``.
_LONG_RELEASE_RE = re.compile(r"^Release\s+(\d+)$", re.IGNORECASE)
# Compact short form without a hyphen: ``Rel17``, ``Rel9``.
_COMPACT_RELEASE_RE = re.compile(r"^Rel(\d+)$", re.IGNORECASE)


def _normalise_release(value: str | None) -> str | None:
    """Canonicalise an LS cover-page release label.

    Mirrors the CR cover-page normalisation
    (:func:`doc3gpp.parsers.cr.cover_page._normalize_release`) plus the
    pre-Rel-4 year markers: ``Release 20`` → ``Rel-20``, ``Release 9``
    → ``Rel-9``, ``Release 1999`` → ``R99``, ``Release 1998`` → ``R98``
    (the official pre-Rel-4 markers, matching
    :func:`doc3gpp.parsers.spec_release.normalise_release`). Already
    canonical values (``Rel-17``, ``R99``) pass through unchanged;
    blank / ``None`` values pass through unchanged; any other shape is
    left as-is so an unexpected value remains visible in the DB.
    """
    if value is None:
        return None
    match = _LONG_RELEASE_RE.match(value.strip())
    if match:
        digits = match.group(1)
        if digits.startswith("199"):
            return f"R{digits[2:]}"
        return f"Rel-{digits}"
    match = _COMPACT_RELEASE_RE.match(value.strip())
    if match:
        return f"Rel-{match.group(1)}"
    return value


def _normalise_groups(raw: str) -> str:
    """Split comma/semicolon-separated destination strings into newlines."""
    if not raw:
        return ""
    parts = re.split(r"[,;]\s*", raw.strip())
    return "\n".join(p for p in parts if p)


def _parse_attachments(raw: str) -> list[dict[str, str]]:
    """Parse a single Attachments: line into {doc_number, description} dicts.

    Format: ``DocNumber(s) [Description e.g. Draft TS 29.414 v0.1.0].``
    """
    if not raw:
        return []
    cleaned = raw.rstrip(" .").strip()
    if not cleaned:
        return []
    out: list[dict[str, str]] = []
    for chunk in re.split(r"\s*;\s*|\s{2,}", cleaned):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = re.match(r"^(?P<doc>\S.*?)\s*\[(?P<desc>[^\]]*)\]\s*$", chunk)
        if m:
            out.append(
                {"doc_number": m.group("doc").strip(),
                 "description": m.group("desc").strip()}
            )
        else:
            out.append({"doc_number": chunk, "description": ""})
    return out


class LSCoverPageParser:
    """3GPP LS header extractor.

    Implements the :class:`doc3gpp.parsers.tdoc_parsers.SectionParser`
    Protocol. Returns ``(True, payload, len(lines))`` when the lines
    look like an LS header; ``payload`` keys match the column names
    in the ``tdoc_cr_ls_details`` table (plus ``attachments`` as a
    list of dicts, ready for gzip-JSON storage).
    """

    name = "ls_cover_page"
    VARIANT = "3gpp"

    @staticmethod
    def supports_source(source: str | None) -> bool:
        """Return ``True`` for every non-None source.

        The 3GPP variant is the broad catch-all for LS rows in v1.
        Future PRs tighten this to a curated allowlist of 3GPP TSG /
        WG short names (``R5``, ``RAN WG2``, …). ``None`` source
        returns ``True`` so unannotated rows still hit the parser —
        the sidecar write fails closed if the variant stamp is later
        judged unsafe.
        """
        return source is None or bool(source.strip())

    def parse(
        self,
        lines: Sequence[str],
        *,
        max_text_length: int = 0,
        full: bool = False,
    ) -> tuple[bool, dict, int]:
        payload: dict = {
            "title": None,
            "response_to": None,
            "release": None,
            "work_item_name": None,
            "work_item_code": None,
            "source": None,
            "to_groups": "",
            "cc_groups": "",
            "attachments": [],
        }
        attachments: list[dict[str, str]] = []

        for line in lines:
            # The docx→md converter emits header cells as plain lines
            # (``Title:``) or as markdown headings (``# Title:``) when
            # the source document styles them as heading paragraphs.
            stripped = line.lstrip("#").strip()
            if (m := _TITLE_RE.match(stripped)):
                payload["title"] = m.group(1).strip() or None
                continue
            if (m := _RESPONSE_TO_RAW_RE.match(stripped)):
                raw = m.group(1).strip()
                if raw and raw != "N/A":
                    payload["response_to"] = raw
                continue
            if (m := _RELEASE_RE.match(stripped)):
                payload["release"] = _normalise_release(
                    m.group(1).strip() or None
                )
                continue
            if (m := _WORK_ITEM_RE.match(stripped)):
                payload["work_item_name"] = m.group(1).strip() or None
                payload["work_item_code"] = (
                    m.group(2).strip() if m.group(2) else None
                )
                continue
            if (m := _SOURCE_RE.match(stripped)):
                payload["source"] = m.group(1).strip() or None
                continue
            if (m := _TO_RE.match(stripped)):
                payload["to_groups"] = _normalise_groups(m.group(1))
                continue
            if (m := _CC_RE.match(stripped)):
                payload["cc_groups"] = _normalise_groups(m.group(1))
                continue
            if (m := _ATTACHMENTS_RE.match(stripped)):
                attachments.extend(_parse_attachments(m.group(1)))

        payload["attachments"] = attachments

        if max_text_length > 0:
            for field in ("title", "response_to", "source"):
                v = payload.get(field)
                if isinstance(v, str) and len(v) > max_text_length:
                    logger.warning(
                        "Truncating LS %s to %d characters",
                        field, max_text_length,
                    )
                    payload[field] = v[:max_text_length]

        if not payload["title"]:
            logger.warning(
                "LS header missing Title; leaving tdoc_cr_ls_details.title as None"
            )

        return True, payload, len(lines)


__all__ = ["LSCoverPageParser"]

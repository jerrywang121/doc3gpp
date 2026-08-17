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
_RESPONSE_TO_RE = re.compile(
    r"^Response to:\s*LS\s+(\S+)\s+on\s+(.+?)\s+from\s+(.+?)\s*$",
    re.IGNORECASE,
)
_RELEASE_RE = re.compile(r"^Release:\s*(.*)$", re.IGNORECASE)
_WORK_ITEM_RE = re.compile(
    r"^Work Item:\s*(.+?)\s*\(([^)]*)\)\s*$", re.IGNORECASE
)
_SOURCE_RE = re.compile(r"^Source:\s*(.*)$", re.IGNORECASE)
_TO_RE = re.compile(r"^To:\s*(.*)$", re.IGNORECASE)
_CC_RE = re.compile(r"^Cc:\s*(.*)$", re.IGNORECASE)
_ATTACHMENTS_RE = re.compile(r"^Attachments:\s*(.*)$", re.IGNORECASE)


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
            "response_to_doc": None,
            "response_to_title": None,
            "response_to_group": None,
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
            if (m := _TITLE_RE.match(line)):
                payload["title"] = m.group(1).strip() or None
                continue
            if (m := _RESPONSE_TO_RE.match(line)):
                payload["response_to_doc"] = m.group(1).strip()
                payload["response_to_title"] = m.group(2).strip()
                payload["response_to_group"] = m.group(3).strip()
                continue
            if (m := _RELEASE_RE.match(line)):
                payload["release"] = m.group(1).strip() or None
                continue
            if (m := _WORK_ITEM_RE.match(line)):
                payload["work_item_name"] = m.group(1).strip() or None
                payload["work_item_code"] = m.group(2).strip() or None
                continue
            if (m := _SOURCE_RE.match(line)):
                payload["source"] = m.group(1).strip() or None
                continue
            if (m := _TO_RE.match(line)):
                payload["to_groups"] = _normalise_groups(m.group(1))
                continue
            if (m := _CC_RE.match(line)):
                payload["cc_groups"] = _normalise_groups(m.group(1))
                continue
            if (m := _ATTACHMENTS_RE.match(line)):
                attachments.extend(_parse_attachments(m.group(1)))

        payload["attachments"] = attachments

        if max_text_length > 0:
            for field in ("title", "response_to_title", "source"):
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

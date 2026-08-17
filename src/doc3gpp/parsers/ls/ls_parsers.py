"""LS parser orchestrator (:class:`LSParserBase`).

Mirrors :class:`doc3gpp.parsers.cr.cr_parsers.CRParserBase`. Holds a
variant-specific :class:`LSCoverPageParser` injected at construction;
the orchestrator does the header detection, runs the cover extractor,
and assembles a :class:`TDocLSDetails`. ``parse()`` raises
``NotImplementedError`` — the service never calls it for LS rows,
because the existing CR sidecar writes assume a ``TDocCRParseResult``
and an LS row has nothing to write there.
"""

from __future__ import annotations

import logging
from typing import Any

from doc3gpp.models.tdoc_cr import TDocCRParseResult
from doc3gpp.models.tdoc_ls import TDocLSDetails, TDocLSParserResult
from doc3gpp.parsers.ls.header import LSHeaderMissingError, is_ls_header_present
from doc3gpp.parsers.tdoc_parsers import TDocParser

logger = logging.getLogger(__name__)


class LSParserBase(TDocParser):
    """Orchestrator for LS-family parsers.

    Subclasses bind ``VARIANT`` and ``parser_version`` and inject a
    variant-specific cover extractor at construction. The orchestrator
    itself does no header-detection work beyond delegating to
    :func:`is_ls_header_present`.
    """

    parser_version: str = "1.0.0"
    VARIANT: str = ""

    def __init__(self, cover: Any) -> None:
        self._cover = cover

    def supports(
        self,
        tdoc_id: str,
        *,
        tdoc_type: str | None = None,
        spec: str | None = None,
        source: str | None = None,
    ) -> bool:
        if tdoc_type != "LS":
            return False
        return bool(self._cover.supports_source(source))

    def parse(
        self,
        markdown: str,
        *,
        tdoc_id: str,
        max_text_length: int = 0,
        full: bool = False,
    ) -> TDocCRParseResult:
        raise NotImplementedError(
            "LSParserBase does not parse CR documents; use parse_ls()"
        )

    def parse_ls(
        self,
        markdown: str,
        *,
        tdoc_id: str,
        max_text_length: int = 0,
    ) -> TDocLSParserResult:
        present, header_blob = is_ls_header_present(markdown)
        if not present:
            raise LSHeaderMissingError(
                "Markdown does not contain a recognisable LS header "
                "(tabbed Meeting/TDoc line, 'LS on' title, and "
                "Source/To/Cc cell); this does not look like an LS "
                "TDoc.",
                snippet=header_blob[:100],
            )

        lines = markdown.splitlines()
        _ok, payload, _advanced = self._cover.parse(
            lines, max_text_length=max_text_length
        )

        final_tdoc_id = (tdoc_id or "").strip() or None
        details = TDocLSDetails(
            tdoc_id=final_tdoc_id,
            ftp_url=None,
            variant=self.VARIANT,
            title=payload.get("title"),
            response_to_doc=payload.get("response_to_doc"),
            response_to_title=payload.get("response_to_title"),
            response_to_group=payload.get("response_to_group"),
            release=payload.get("release"),
            work_item_name=payload.get("work_item_name"),
            work_item_code=payload.get("work_item_code"),
            source=payload.get("source"),
            to_groups=payload.get("to_groups") or "",
            cc_groups=payload.get("cc_groups") or "",
            attachments=tuple(payload.get("attachments") or ()),
            parser_version=self.parser_version,
            extracted_at=None,
        )
        return TDocLSParserResult(cover=details)


__all__ = ["LSParserBase"]

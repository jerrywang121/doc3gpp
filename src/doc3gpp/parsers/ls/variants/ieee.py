"""IEEE LS variant — v2 stub.

The class is intentionally not registered in
:func:`doc3gpp.parsers.tdoc_parsers.build_default_registry`; it exists
to make the seam for future work explicit. The cover extractor is a
placeholder — replace with an IEEE-specific :class:`LSCoverPageParser`
subclass when the format is documented.
"""

from __future__ import annotations

from doc3gpp.parsers.ls.ls_parsers import LSParserBase

__all__ = ["IEEELSParser"]


class IEEELSParser(LSParserBase):
    """Placeholder for IEEE-style LS documents. Not registered in v1."""

    parser_version = "0.0.0"
    VARIANT = "ieee"

    def __init__(self) -> None:
        super().__init__(cover=_IEEECoverPlaceholder())

    def parse_ls(self, markdown: str, *, tdoc_id: str, max_text_length: int = 0):  # type: ignore[override]
        raise NotImplementedError(
            "IEEELSParser is a v2 stub; register in "
            "build_default_registry once the IEEE LS header format "
            "is documented."
        )


class _IEEECoverPlaceholder:
    """Minimal stand-in until the IEEE LS header is documented."""

    VARIANT = "ieee"
    name = "ieee_cover_placeholder"

    @staticmethod
    def supports_source(source: str | None) -> bool:
        return source is not None and "ieee" in source.lower()

    def parse(self, lines, *, max_text_length: int = 0, full: bool = False):
        return True, {}, len(lines)

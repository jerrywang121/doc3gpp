"""ETSI LS variant — v2 stub. See :mod:`doc3gpp.parsers.ls.variants.ieee`."""

from __future__ import annotations

from doc3gpp.parsers.ls.ls_parsers import LSParserBase

__all__ = ["ETSILSParser"]


class ETSILSParser(LSParserBase):
    """Placeholder for ETSI TB-style LS documents. Not registered in v1."""

    parser_version = "0.0.0"
    VARIANT = "etsi"

    def __init__(self) -> None:
        super().__init__(cover=_ETSICoverPlaceholder())

    def parse_ls(self, markdown: str, *, tdoc_id: str, max_text_length: int = 0):  # type: ignore[override]
        raise NotImplementedError(
            "ETSILSParser is a v2 stub; register in "
            "build_default_registry once the ETSI TB LS header "
            "format is documented."
        )


class _ETSICoverPlaceholder:
    VARIANT = "etsi"
    name = "etsi_cover_placeholder"

    @staticmethod
    def supports_source(source: str | None) -> bool:
        return source is not None and "etsi" in source.lower()

    def parse(self, lines, *, max_text_length: int = 0, full: bool = False):
        return True, {}, len(lines)

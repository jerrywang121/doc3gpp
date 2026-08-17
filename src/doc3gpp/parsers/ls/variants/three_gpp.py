"""3GPP LS variant — v1 implementation."""

from __future__ import annotations

from doc3gpp.parsers.ls.cover_page import LSCoverPageParser
from doc3gpp.parsers.ls.ls_parsers import LSParserBase

__all__ = ["ThreeGPPLSParser"]


class ThreeGPPLSParser(LSParserBase):
    """LS parser for documents produced by 3GPP working groups.

    Binds :class:`LSCoverPageParser` as the cover extractor and stamps
    ``VARIANT = "3gpp"`` on the persisted row.
    """

    parser_version = "1.0.0"
    VARIANT = "3gpp"

    def __init__(self) -> None:
        super().__init__(cover=LSCoverPageParser())

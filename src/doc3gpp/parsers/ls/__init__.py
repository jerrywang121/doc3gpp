"""LS TDoc parsers.

Mirrors the layout of :mod:`doc3gpp.parsers.cr`. Shared header
detection lives in :mod:`doc3gpp.parsers.ls.header`; the
:class:`LSCoverPageParser` lives in :mod:`doc3gpp.parsers.ls.cover_page`;
the :class:`LSParserBase` orchestrator lives in
:mod:`doc3gpp.parsers.ls.ls_parsers`. Subclass-per-format variants
live in :mod:`doc3gpp.parsers.ls.variants`.
"""

from __future__ import annotations

from doc3gpp.parsers.ls.cover_page import LSCoverPageParser

__all__ = ["LSCoverPageParser"]

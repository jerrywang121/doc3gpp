"""TDoc parser abstractions and default registry.

This module defines the :class:`TDocParser` and :class:`SectionParser`
Protocols and a small registry that lets the rest of the app resolve a
parser for a given TDoc without importing concrete implementations.
"""

from __future__ import annotations

from typing import Protocol, TypeVar, runtime_checkable

from doc3gpp.models.tdoc_cr import TDocCRDetails


T = TypeVar("T")


@runtime_checkable
class SectionParser(Protocol[T]):
    """Parses one named section of a CR markdown body."""

    name: str

    def parse(
        self,
        lines: list[str],
        *,
        max_text_length: int = 0,
        full: bool = False,
    ) -> tuple[bool, T, int]: ...


@runtime_checkable
class TDocParser(Protocol):
    """Parses a CR markdown body into a :class:`TDocCRDetails`."""

    parser_version: str

    def supports(
        self, tdoc_id: str, *, tdoc_type: str | None = None, spec: str | None = None
    ) -> bool: ...

    def parse(
        self,
        markdown: str,
        *,
        tdoc_id: str,
        max_text_length: int = 0,
        full: bool = False,
    ) -> TDocCRDetails: ...


class TDocParserRegistry:
    """Resolves a :class:`TDocParser` for a given TDoc id / type / spec."""

    def __init__(self) -> None:
        self._parsers: list[TDocParser] = []

    def register(self, parser: TDocParser) -> None:
        """Add ``parser`` to the registry."""
        self._parsers.append(parser)

    def resolve(
        self, tdoc_id: str, *, tdoc_type: str | None = None, spec: str | None = None
    ) -> TDocParser:
        """Return the first registered parser that supports the inputs.

        Raises:
            LookupError: no registered parser supports the inputs.
        """
        for parser in self._parsers:
            if parser.supports(tdoc_id, tdoc_type=tdoc_type, spec=spec):
                return parser
        raise LookupError(f"No TDoc parser registered for {tdoc_id!r}")


def build_default_registry() -> TDocParserRegistry:
    """Return a registry with the built-in CR parsers.

    Specific parsers register before generic ones so TTCN CRs are
    handled by :class:`TTCNCRParser` and everything else falls through
    to the generic :class:`CRParser`.
    """
    from doc3gpp.parsers.cr.cr_parsers import CRParser, TTCNCRParser

    registry = TDocParserRegistry()
    registry.register(TTCNCRParser())
    registry.register(CRParser())
    return registry

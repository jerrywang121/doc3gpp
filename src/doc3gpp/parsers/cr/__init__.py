"""CR parser subpackage.

Public surface:

- :func:`build_cr_registry` returns a list of CR :class:`TDocParser`
  implementations ordered most-specific-first.
- :data:`default_parser` is the singleton :class:`CRParser` used by
  the legacy :func:`doc3gpp.parsers.cr_parser.parse_cr_details` shim.
"""

from __future__ import annotations

from doc3gpp.parsers.cr.cr_parsers import CRParser, TTCNCRParser


def build_cr_registry() -> list:
    """Return CR :class:`TDocParser` instances, specific before generic."""
    return [TTCNCRParser(), CRParser()]


default_parser = CRParser()


__all__ = [
    "CRParser",
    "TTCNCRParser",
    "build_cr_registry",
    "default_parser",
]
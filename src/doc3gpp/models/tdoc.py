from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class TDoc:
    """A 3GPP TDoc record."""

    tdoc_id: str
    title: str
    meeting: str | None = None
    url: str | None = None

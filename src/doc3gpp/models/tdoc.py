from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class TDoc:
    """A 3GPP TDoc record stored in the database.

    Attributes:
        tdoc_id: The canonical TDoc identifier (e.g. R5s260001).
        title: The document title or short description.
        meeting: Optional meeting identifier to associate this TDoc with.
        url: Optional URL where the TDoc list entry was discovered.
    """

    tdoc_id: str
    title: str
    meeting: str | None = None
    url: str | None = None

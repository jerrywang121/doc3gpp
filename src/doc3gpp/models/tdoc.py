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
    meeting_id: int | None = None
    # Convenience: human-readable meeting name populated when listing (not persisted on tdocs table)
    meeting_name: str | None = None
    url: str | None = None
    # Additional metadata extracted from TDoc list XLSX
    source: str | None = None
    type: str | None = None
    status: str | None = None
    reservation_date: str | None = None
    uploaded_date: str | None = None
    cr_cat: str | None = None
    is_revision_of: str | None = None
    revised_to: str | None = None
    release: str | None = None
    spec: str | None = None
    version: str | None = None
    related_wis: str | None = None
    cr_num: str | None = None
    # TSG CR Pack value from XLSX (nullable)
    cr_pack: str | None = None

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(slots=True)
class TDoc:
    """A 3GPP TDoc record stored in the database.

    Attributes:
        tdoc_id: The canonical TDoc identifier (e.g. R5s260001).
        title: The document title or short description. May be ``None`` when
            the source XLSX has no title cell; the parser converts empty cells
            to ``None`` rather than coercing to a placeholder string.
        meeting_id: Optional foreign key into ``meetings.meeting_id``.
        meeting_name: Human-readable meeting name populated when listing.
            Not persisted on the ``tdocs`` table.
        url: Optional URL where the TDoc list entry was discovered.
        reservation_date: Optional reservation date from the source XLSX,
            parsed as a ``date`` (not a free-form string).
        uploaded_date: Optional upload date from the source XLSX, parsed as
            a ``date``.
        updated_at: Timestamp of the most recent upsert for this row.
    """

    tdoc_id: str
    title: str | None = None
    meeting_id: int | None = None
    # Convenience: human-readable meeting name populated when listing (not persisted on tdocs table)
    meeting_name: str | None = None
    url: str | None = None
    # Additional metadata extracted from TDoc list XLSX
    source: str | None = None
    type: str | None = None
    status: str | None = None
    reservation_date: date | None = None
    uploaded_date: date | None = None
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
    updated_at: datetime | None = None
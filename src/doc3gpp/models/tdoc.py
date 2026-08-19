from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(slots=True)
class TDoc:
    """A 3GPP TDoc record stored in the database.

    Attributes:
        tdoc_id: The canonical TDoc identifier (e.g. R5s260001).
        title: The document title or short description. May be ``None`` when
            the source XLSX has no title cell; the parser converts empty cells
            to ``None`` rather than coercing to a placeholder string.
        meeting_id: Optional foreign key into ``meetings.meeting_id``.
        ftp_url: Optional relative download URL for this TDoc's zip file on
            the 3GPP FTP, extracted from the TDoc-column hyperlink in the
            source XLSX and stored as a path relative to
            ``https://www.3gpp.org/ftp/`` (the canonical 3GPP FTP root).
            ``None`` when the XLSX has no hyperlink for this row (e.g. a
            deleted or placeholder entry).
        reservation_date: Optional reservation date from the source XLSX,
            parsed as a ``date`` (not a free-form string).
        uploaded_date: Optional upload date from the source XLSX, parsed as
            a ``date``.
    """

    tdoc_id: str
    title: str | None = None
    meeting_id: int | None = None
    ftp_url: str | None = None
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
    # Six new XLSX metadata fields. All come from the meeting TDoc-list
    # spreadsheet; `None` when the cell is empty, the header is absent,
    # or the row was synced before this column existed.
    tdoc_for: str | None = None
    abstract: str | None = None
    secretary_remarks: str | None = None
    ls_to: str | None = None
    ls_cc: str | None = None
    original_ls: str | None = None


@dataclass(slots=True)
class TDocWithMeeting:
    """A TDoc joined with its parent meeting's display name.

    Presentation-time only: ``meeting_name`` is computed by a JOIN against the
    ``meetings`` table at read time and is not persisted on the ``tdocs``
    table. The CLI and CSV exporter consume this DTO; pure persistence code
    (upserts, schema migrations) should stick to :class:`TDoc`.
    """

    tdoc: TDoc
    meeting_name: str | None = None
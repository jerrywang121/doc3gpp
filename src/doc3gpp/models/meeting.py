from __future__ import annotations

from datetime import date
from dataclasses import dataclass


@dataclass(slots=True)
class Meeting:
    """A 3GPP meeting record stored in the database.

    Attributes:
        meeting_id: Numeric meeting identifier as published by 3GPP.
        name: Short meeting name used in 3GPP reports.
        title: Full meeting title.
        location: Meeting venue or online details.
        start_date: Meeting start date.
        end_date: Meeting end date.
        ftp_url: Optional FTP path used to discover meeting documents.
        start_doc: Optional start document for the meeting.
        end_doc: Optional end document for the meeting.
        tsg: Canonical TSG short name (e.g. ``R5``) owning this meeting;
            populated during sync from the ``--tsg`` flag. Foreign key into
            ``tsgs.short_name`` at the persistence layer; ``None`` for
            rows inserted before the column was added or for meetings
            imported without a known owning TSG.
    """

    meeting_id: int
    name: str
    title: str
    location: str
    start_date: date | None = None
    end_date: date | None = None
    ftp_url: str | None = None
    start_doc: str | None = None
    end_doc: str | None = None
    tsg: str | None = None

from __future__ import annotations

from datetime import date
from datetime import datetime
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
        updated_at: Timestamp of last record update.
    """

    meeting_id: int
    name: str
    title: str
    location: str
    start_date: date
    end_date: date
    ftp_url: str | None = None
    start_doc: str | None = None
    end_doc: str | None = None
    updated_at: datetime | None = None

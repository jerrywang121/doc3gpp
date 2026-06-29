from __future__ import annotations

from datetime import date
from datetime import datetime
from dataclasses import dataclass


@dataclass(slots=True)
class Meeting:
    """A 3GPP meeting."""

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

from __future__ import annotations

from datetime import date
from datetime import datetime

from sqlalchemy import Date, DateTime, Integer, String, Text, func, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from doc3gpp.storage.db.base import Base


class TDocORM(Base):
    """Persisted TDoc record."""

    __tablename__ = "tdocs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tdoc_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(500))
    # store as FK to meetings.meeting_id
    meeting_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("meetings.meeting_id"), nullable=True, index=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str | None] = mapped_column(String(256), nullable=True)
    type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reservation_date: Mapped[str | None] = mapped_column(String(64), nullable=True)
    uploaded_date: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cr_cat: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_revision_of: Mapped[str | None] = mapped_column(String(64), nullable=True)
    revised_to: Mapped[str | None] = mapped_column(String(64), nullable=True)
    release: Mapped[str | None] = mapped_column(String(64), nullable=True)
    spec: Mapped[str | None] = mapped_column(String(64), nullable=True)
    version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    related_wis: Mapped[str | None] = mapped_column(String(256), nullable=True)
    cr_num: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cr_pack: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class MeetingORM(Base):
    """Persisted 3GPP meeting calendar record."""

    __tablename__ = "meetings"

    meeting_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    location: Mapped[str] = mapped_column(String(300), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    ftp_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_doc: Mapped[str | None] = mapped_column(String(64), nullable=True)
    end_doc: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

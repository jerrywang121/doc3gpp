from __future__ import annotations

from datetime import date
from datetime import datetime

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from doc3gpp.storage.db.base import Base


class TDocORM(Base):
    """Persisted TDoc record."""

    __tablename__ = "tdocs"

    tdoc_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    # store as FK to meetings.meeting_id
    meeting_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("meetings.meeting_id"), nullable=True, index=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str | None] = mapped_column(String(256), nullable=True)
    type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reservation_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    uploaded_date: Mapped[date | None] = mapped_column(Date, nullable=True)
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
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class MeetingORM(Base):
    """Persisted 3GPP meeting calendar record."""

    __tablename__ = "meetings"

    meeting_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    location: Mapped[str] = mapped_column(String(300), nullable=False)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    ftp_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_doc: Mapped[str | None] = mapped_column(String(64), nullable=True)
    end_doc: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TsgORM(Base):
    """Persisted 3GPP Technical Specification Group (TSG) reference record.

    Holds the canonical list of TSGs (RAN WG1..5, RAN AH1, SA WG1..6, CT WG1..6)
    along with their short codes, descriptions, and 3GPP group URLs. Used to
    validate user-supplied TSG identifiers (for example ``--tsg``) and to
    surface reference metadata to the CLI.
    """

    __tablename__ = "tsgs"

    tsg_name: Mapped[str] = mapped_column(String(120), unique=True, index=True, nullable=False)
    short_name: Mapped[str] = mapped_column(String(16), primary_key=True)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)


class WiORM(Base):
    """Persisted 3GPP Work Item (WI) record scraped from DynaReport.

    Rows are unique on the ``(wi_id, tsg_short)`` composite because the same
    numeric work-item identifier can appear under multiple owning TSGs on the
    upstream pages. ``tsg_short`` is a foreign key into ``tsgs.short_name`` so
    every WI row can be joined back to its responsible group.
    """

    __tablename__ = "wis"

    wi_id: Mapped[int] = mapped_column(Integer, primary_key=True, nullable=False)
    acronym: Mapped[str] = mapped_column(String(256), nullable=False)
    release: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    tsg_short: Mapped[str] = mapped_column(
        String(16),
        ForeignKey("tsgs.short_name"),
        primary_key=True,
        nullable=False,
        index=True,
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TDocFileORM(Base):
    """Auxiliary file attached to a TDoc (revision, review, or support).

    One row per file observed in a meeting's FTP subfolders (``Inbox/``,
    ``Docs/``, ``Tdocs/``, or ``Review/``). Identity is the
    fully-qualified download URL: the same file lives at exactly one
    upstream location, so the ``url`` column is the natural upsert key
    and is the only unique index in the table.

    ``tdoc_id`` is a foreign key into ``tdocs.tdoc_id``; the sync flow
    populates ``tdocs`` first and only persists ``TDocFile`` rows for
    files whose owning TDoc ID is already known. Cascading the delete on
    the FK is intentionally disabled — removing a TDoc should not
    silently drop its revision history, and the sync flow uses an
    explicit ``delete_for_tdoc_ids`` pass on re-sync.
    """

    __tablename__ = "tdoc_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tdoc_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tdocs.tdoc_id"),
        nullable=False,
        index=True,
    )
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    file: Mapped[str] = mapped_column(String(256), nullable=False)
    url: Mapped[str] = mapped_column(Text, unique=True, nullable=False, index=True)
    uploaded_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

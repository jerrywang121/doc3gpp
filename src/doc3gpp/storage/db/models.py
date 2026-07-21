from __future__ import annotations

from datetime import date
from datetime import datetime

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
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
    ftp_url: Mapped[str | None] = mapped_column(Text, nullable=True)
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


class MeetingORM(Base):
    """Persisted 3GPP meeting calendar record.

    ``tsg`` is a foreign key into ``tsgs.short_name`` populated when the
    CLI passes ``--tsg`` to ``doc3gpp meeting sync``. Nullable so rows
    imported from older schemas (where the column did not exist) or
    scraped without a known owning TSG can still be persisted.
    Indexed because the ``meeting list --tsg`` filter runs a ``LIKE``
    lookup on every call.
    """

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
    tsg: Mapped[str | None] = mapped_column(
        String(16),
        ForeignKey("tsgs.short_name"),
        nullable=True,
        index=True,
    )
    tdoc_list_last_sync: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


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
    meeting_last_sync: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


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


class TDocFileORM(Base):
    """Auxiliary file attached to a TDoc (revision, review, or support).

    One row per file observed in a meeting's FTP subfolders (``Inbox/``,
    ``Docs/``, ``Tdocs/``, or ``Review/``). Identity is the
    download URL path relative to ``https://www.3gpp.org/ftp/``: the
    same file lives at exactly one upstream location, so the
    ``ftp_url`` column is the natural upsert key and is the only
    unique index in the table.

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
    ftp_url: Mapped[str] = mapped_column(Text, unique=True, nullable=False, index=True)
    uploaded_date: Mapped[date | None] = mapped_column(Date, nullable=True)


class TDocCrDetailOrm(Base):
    """Structured CR (Change Request) cover-page details extracted from a TDoc.

    One row per **immutable download URL** rather than per TDoc id —
    3GPP zip assets are byte-for-byte identical for the lifetime of the
    URL, while the logical ``tdoc_id`` may map to multiple URLs across
    revisions. Keying on ``ftp_url`` lets every revision of the same
    ``tdoc_id`` coexist: a fresh extract at a new URL writes a new
    row instead of clobbering the parsed record for the previous one.

    Carries the cover-page fields (spec, cr_num, title, ...) only.
    TTCN-specific details live in :class:`TDocCrTtcnDetailOrm`. See
    :class:`doc3gpp.models.tdoc_cr.TDocCRDetails` for the in-memory
    shape.

    ``ftp_url`` is the primary key — the same URL serves the same bytes
    forever, so re-extracting at the same URL is idempotent. ``tdoc_id``
    is a non-PK foreign key into ``tdocs.tdoc_id`` indexed for the
    ``get(tdoc_id)`` lookup; ``ondelete="CASCADE"`` keeps the detail
    rows in sync with their parent TDoc — unlike ``TDocFileORM`` the
    CR details are derived artefacts of the TDoc row and should be
    wiped when the TDoc itself is removed.

    The URL is stored as a path relative to the canonical 3GPP FTP
    root (``https://www.3gpp.org/ftp/``) to match the convention used
    by ``meetings.ftp_url``; the service layer is responsible for
    normalising at the boundary.
    """

    __tablename__ = "tdoc_cr_details"

    ftp_url: Mapped[str] = mapped_column(String(1024), primary_key=True)
    tdoc_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tdocs.tdoc_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    spec: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cr_num: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rev: Mapped[str | None] = mapped_column(String(64), nullable=True)
    version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str | None] = mapped_column(String(256), nullable=True)
    tsg: Mapped[str | None] = mapped_column(String(16), nullable=True)
    related_wis: Mapped[str | None] = mapped_column(String(512), nullable=True)
    date: Mapped[date | None] = mapped_column(Date, nullable=True)
    cr_cat: Mapped[str | None] = mapped_column(String(16), nullable=True)
    release: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason_for_change: Mapped[str | None] = mapped_column(Text, nullable=True)
    consequences_if_not_approved: Mapped[str | None] = mapped_column(Text, nullable=True)
    clauses_affected: Mapped[str | None] = mapped_column(Text, nullable=True)
    other_comments: Mapped[str | None] = mapped_column(Text, nullable=True)
    revision_history: Mapped[str | None] = mapped_column(Text, nullable=True)
    extracted_tdoc_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class TDocCrTtcnDetailOrm(Base):
    """TTCN-specific CR details extracted from a TTCN CR TDoc.

    One row per **immutable download URL** — the same URL serves the
    same bytes forever, so the sidecar row is keyed on ``ftp_url`` and
    shares its identity contract with :class:`TDocCrDetailOrm`.

    Stores six overview columns exposed by the TTCN parser plus a
    ``required_changes`` column that holds the list of correction dicts
    as gzip-compressed UTF-8 JSON. See
    :class:`doc3gpp.models.tdoc_cr.TDocCRTTCNDetails` for the in-memory
    shape; the repository compresses/decompresses the
    ``required_changes`` blob on write/read.

    ``tdoc_id`` is a non-PK foreign key into ``tdocs.tdoc_id`` indexed
    for the ``get(tdoc_id)`` lookup; ``ondelete="CASCADE"`` removes the
    sidecar when the parent TDoc row is deleted. Timestamps and parser
    versioning live in :class:`TDocExtractOrm`, which is the single
    source of truth for extraction metadata.
    """

    __tablename__ = "tdoc_cr_ttcn_details"

    ftp_url: Mapped[str] = mapped_column(String(1024), primary_key=True)
    tdoc_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tdocs.tdoc_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    testcase: Mapped[str | None] = mapped_column(String(256), nullable=True)
    ue: Mapped[str | None] = mapped_column(String(256), nullable=True)
    ss: Mapped[str | None] = mapped_column(String(256), nullable=True)
    ats_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ttcn_release: Mapped[str | None] = mapped_column(String(16), nullable=True)
    test_suite: Mapped[str | None] = mapped_column(String(256), nullable=True)
    required_changes: Mapped[bytes | None] = mapped_column(
        LargeBinary(length=16 * 1024 * 1024), nullable=True,
    )


class TDocExtractOrm(Base):
    """Metadata-only sidecar recording that a TDoc has been extracted.

    The "expensive" payload (the cached zip and the rendered markdown)
    lives on disk under the cache directory — these columns only hold
    *paths*. A row here means ``scraping.tdoc_zip_source`` and
    ``parsers.docx_converter`` have already produced artefacts
    on disk, so the next extract call can short-circuit the network
    and the python-docx render.

    Mirrors the URL-PK scheme of :class:`TDocCrDetailOrm`: identity
    is the immutable download URL (stored relative to the 3GPP FTP
    root), and ``tdoc_id`` is a non-PK FK into ``tdocs.tdoc_id`` with
    ``ondelete="CASCADE"`` — when the owning TDoc row is removed, the
    extract metadata loses its meaning and is dropped with it.
    """

    __tablename__ = "tdoc_extracts"

    ftp_url: Mapped[str] = mapped_column(String(1024), primary_key=True)
    tdoc_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tdocs.tdoc_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    zip_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    markdown_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    doc_filename: Mapped[str] = mapped_column(String(256), nullable=False)
    extracted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    parser_version: Mapped[str] = mapped_column(
        String(32), nullable=False, default="1.0.0"
    )

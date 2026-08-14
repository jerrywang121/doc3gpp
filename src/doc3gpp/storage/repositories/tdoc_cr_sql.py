"""SQLAlchemy-backed implementation of :class:`TDocCrDetailRepository`.

Stores the parsed CR cover-page row (``tdoc_cr_cover_page``) and the
cache-extract metadata sidecar (``tdoc_extracts``) that point at the
on-disk artefacts under :mod:`doc3gpp.scraping.cache`. Both tables are
owned by this repository, but writes are split into two upsert methods
so callers can update each table independently.

Both tables are keyed by the immutable download ``ftp_url`` — a path
relative to the canonical 3GPP FTP root (``https://www.3gpp.org/ftp/``)
that uniquely identifies the upstream asset. 3GPP assets are byte-for-byte
identical for the lifetime of a URL, so re-extracting the same URL is
idempotent, while multiple revisions of a single ``tdoc_id`` land at
distinct URLs and occupy distinct rows. ``tdoc_id`` is a non-PK FK into
``tdocs.tdoc_id`` (indexed for the per-tdoc lookup) with
``ondelete="CASCADE"`` so deleting a parent TDoc still cleans up every
revision's detail rows.
"""

from __future__ import annotations

import logging

from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from doc3gpp.models.tdoc_cr import TDocCRDetails, TDocExtractMeta
from doc3gpp.storage.db.models import TDocCrDetailOrm, TDocExtractOrm
from doc3gpp.storage.db.session import get_session_factory

logger = logging.getLogger(__name__)


class SQLAlchemyTDocCrRepository:
    """SQLAlchemy implementation of :class:`TDocCrDetailRepository`.

    Persists two tables — ``tdoc_cr_cover_page`` (parsed cover-page fields)
    and ``tdoc_extracts`` (paths to cached zip / markdown) — both keyed
    by the immutable download ``ftp_url``. Cover-page rows and extract
    metadata rows are written through separate upsert methods.
    """

    def __init__(self, session_factory=None) -> None:
        """Initialise the repository.

        Args:
            session_factory: Optional pre-built ``sessionmaker``. When
                omitted the function falls back to
                :func:`doc3gpp.storage.db.session.get_session_factory`.
                The parameter exists so unit tests can bind this class
                to an in-memory SQLite engine without touching
                :func:`get_settings`.
        """
        self._session_factory = session_factory or get_session_factory()
        self._ensured = False

    def _ensure_table_exists(self) -> None:
        if self._ensured:
            return
        from doc3gpp.storage.db.base import Base
        from doc3gpp.storage.db.session import get_engine

        try:
            with self._session_factory() as session:
                session.execute(text("SELECT 1 FROM tdoc_cr_cover_page LIMIT 0"))
        except OperationalError as exc:
            msg = str(exc).lower()
            if "no such table" in msg or "doesn't exist" in msg:
                Base.metadata.create_all(bind=get_engine())
                with self._session_factory() as session:
                    session.execute(text("SELECT 1 FROM tdoc_cr_cover_page LIMIT 0"))
            else:
                raise
        self._ensured = True

    def get(self, tdoc_id: str) -> list[TDocCRDetails]:
        """Return every detail row for ``tdoc_id``, ordered by ``ftp_url`` ASC."""
        self._ensure_table_exists()
        with self._session_factory() as session:
            rows = (
                session.scalars(
                    select(TDocCrDetailOrm)
                    .where(TDocCrDetailOrm.tdoc_id == tdoc_id)
                    .order_by(TDocCrDetailOrm.ftp_url.asc())
                )
                .all()
            )
        return [_orm_to_details(row) for row in rows]

    def get_by_url(self, url: str) -> TDocCRDetails | None:
        """Return the detail row whose URL matches, or ``None``.

        ``url`` is the relative ``ftp_url`` (the PK); callers that
        hold a full upstream URL must normalise via
        :func:`doc3gpp.scraping.ftp_source.normalize_ftp_path` first.
        """
        self._ensure_table_exists()
        with self._session_factory() as session:
            row = session.get(TDocCrDetailOrm, url)
        if row is None:
            return None
        return _orm_to_details(row)

    def upsert(self, details: TDocCRDetails) -> None:
        """Insert/update the cover-page row for ``details.ftp_url``."""
        self._ensure_table_exists()
        if not details.ftp_url:
            raise ValueError(
                "TDocCRDetails requires a non-empty ftp_url for URL-keyed upsert"
            )

        ftp_url = details.ftp_url
        with self._session_factory() as session:
            detail_row = session.get(TDocCrDetailOrm, ftp_url)
            if detail_row is None:
                detail_row = TDocCrDetailOrm(ftp_url=ftp_url, tdoc_id=details.tdoc_id)
                session.add(detail_row)
            else:
                # ``tdoc_id`` may shift if the underlying TDoc id was
                # reassigned; follow the new value so the FK stays
                # accurate. Same for ``ftp_url`` itself — except the
                # URL is the PK, so a different URL means a different
                # row.
                detail_row.tdoc_id = details.tdoc_id
            self._details_to_orm(detail_row, details)
            session.commit()

    def upsert_extract_meta(self, meta: TDocExtractMeta) -> None:
        """Insert/update the ``tdoc_extracts`` row for ``meta.ftp_url``."""
        self._ensure_table_exists()
        if not meta.ftp_url:
            raise ValueError(
                "TDocExtractMeta requires a non-empty ftp_url for URL-keyed upsert"
            )
        if not meta.tdoc_id:
            raise ValueError(
                "TDocExtractMeta requires a non-empty tdoc_id for URL-keyed upsert"
            )

        ftp_url = meta.ftp_url
        with self._session_factory() as session:
            extract_row = session.get(TDocExtractOrm, ftp_url)
            if extract_row is None:
                extract_row = TDocExtractOrm(ftp_url=ftp_url, tdoc_id=meta.tdoc_id)
                session.add(extract_row)
            else:
                extract_row.tdoc_id = meta.tdoc_id
            self._meta_to_orm(extract_row, meta)
            session.commit()

    def get_extract_meta(self, tdoc_id: str) -> list[TDocExtractMeta]:
        """Return every metadata row for ``tdoc_id``, newest first."""
        self._ensure_table_exists()
        with self._session_factory() as session:
            rows = (
                session.scalars(
                    select(TDocExtractOrm)
                    .where(TDocExtractOrm.tdoc_id == tdoc_id)
                    .order_by(
                        TDocExtractOrm.extracted_at.desc(),
                        TDocExtractOrm.ftp_url.asc(),
                    )
                )
                .all()
            )
        return [_orm_to_meta(row) for row in rows]

    def get_extract_meta_by_url(self, url: str) -> TDocExtractMeta | None:
        """Return the extract-metadata row whose URL matches.

        ``url`` is the relative ``ftp_url`` (the PK).
        """
        self._ensure_table_exists()
        with self._session_factory() as session:
            row = session.get(TDocExtractOrm, url)
        if row is None:
            return None
        return _orm_to_meta(row)

    def list_all(self) -> list[TDocCRDetails]:
        """Return every persisted detail row (CLI / debugging)."""
        self._ensure_table_exists()
        with self._session_factory() as session:
            rows = session.scalars(select(TDocCrDetailOrm)).all()
        return [_orm_to_details(row) for row in rows]

    @staticmethod
    def _details_to_orm(target: TDocCrDetailOrm, details: TDocCRDetails) -> None:
        """Copy :class:`TDocCRDetails` cover-page fields onto an ORM instance.

        Excludes ``ftp_url`` (PK, never overwritten after construction)
        and ``tdoc_id`` (handled by :meth:`upsert` because it is the FK).
        """
        target.spec = details.spec
        target.cr_num = details.cr_num
        target.rev = details.rev
        target.version = details.version
        target.title = details.title
        target.source = details.source
        target.tsg = details.tsg
        target.related_wis = details.related_wis
        target.date = details.date
        target.cr_cat = details.cr_cat
        target.release = details.release
        target.reason_for_change = details.reason_for_change
        target.consequences_if_not_approved = details.consequences_if_not_approved
        target.summary_of_change = details.summary_of_change
        target.clauses_affected = details.clauses_affected
        target.other_comments = details.other_comments
        target.revision_history = details.revision_history
        target.extracted_tdoc_id = details.extracted_tdoc_id

    @staticmethod
    def _meta_to_orm(target: TDocExtractOrm, meta: TDocExtractMeta) -> None:
        """Copy :class:`TDocExtractMeta` fields onto an ORM instance.

        Excludes ``ftp_url`` (PK) and ``tdoc_id`` (FK, handled by
        :meth:`upsert_extract_meta`).
        """
        target.cache_file = meta.cache_file
        target.doc_filename = meta.doc_filename
        target.parser_version = meta.parser_version
        if meta.extracted_at is not None:
            target.extracted_at = meta.extracted_at


def _orm_to_details(row: TDocCrDetailOrm) -> TDocCRDetails:
    """Reconstruct a :class:`TDocCRDetails` from an ORM row.

    ``ftp_url`` is read straight from the row — the URL is the PK.
    """
    return TDocCRDetails(
        tdoc_id=row.tdoc_id,
        spec=row.spec,
        cr_num=row.cr_num,
        rev=row.rev,
        version=row.version,
        title=row.title,
        source=row.source,
        tsg=row.tsg,
        related_wis=row.related_wis,
        date=row.date,
        cr_cat=row.cr_cat,
        release=row.release,
        reason_for_change=row.reason_for_change,
        consequences_if_not_approved=row.consequences_if_not_approved,
        summary_of_change=row.summary_of_change,
        clauses_affected=row.clauses_affected,
        other_comments=row.other_comments,
        revision_history=row.revision_history,
        extracted_tdoc_id=row.extracted_tdoc_id,
        ftp_url=row.ftp_url,
    )


def _orm_to_meta(row: TDocExtractOrm) -> TDocExtractMeta:
    """Reconstruct a :class:`TDocExtractMeta` from an ORM row."""
    return TDocExtractMeta(
        ftp_url=row.ftp_url,
        tdoc_id=row.tdoc_id,
        cache_file=row.cache_file,
        doc_filename=row.doc_filename,
        extracted_at=row.extracted_at,
        parser_version=row.parser_version,
    )

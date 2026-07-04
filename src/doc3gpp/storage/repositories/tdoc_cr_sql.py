"""SQLAlchemy-backed implementation of :class:`TDocCrDetailRepository`.

Stores both the parsed CR detail row (``tdoc_cr_details``) and the
cache-extract metadata sidecar (``tdoc_extracts``) that point at the
on-disk artefacts under :mod:`doc3gpp.scraping.cache`. ``upsert`` writes
both rows inside a single transaction so the two tables never disagree.

The ``corrections`` list of dicts on :class:`TDocCRDetails` is
serialised to a single ``TEXT`` column (``corrections``) via
:func:`json.dumps`. Reads use a tolerant decoder that falls back to
``[]`` on a ``None`` / empty / unparseable blob so a corrupt row never
breaks an unrelated read.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from doc3gpp.models.tdoc_cr import TDocCRDetails, TDocExtractMeta
from doc3gpp.storage.db.models import TDocCrDetailOrm, TDocExtractOrm
from doc3gpp.storage.db.session import get_session_factory

logger = logging.getLogger(__name__)


class SQLAlchemyTDocCrRepository:
    """SQLAlchemy implementation of :class:`TDocCrDetailRepository`.

    Persists two tables — ``tdoc_cr_details`` (parsed fields) and
    ``tdoc_extracts`` (paths to cached zip / markdown) — both keyed by
    ``tdoc_id`` (PK + FK into ``tdocs.tdoc_id``). The ``upsert`` call
    writes both rows inside a single transaction.
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

    def get(self, tdoc_id: str) -> TDocCRDetails | None:
        """Return the persisted detail row, or ``None`` on miss."""
        with self._session_factory() as session:
            row = session.get(TDocCrDetailOrm, tdoc_id)
        if row is None:
            return None
        return _orm_to_details(row)

    def upsert(
        self,
        details: TDocCRDetails,
        extract_meta: TDocExtractMeta,
    ) -> None:
        """Insert/update both rows in a single transaction.

        Both tables are keyed by ``tdoc_id``. The detail row gets a
        fresh ``updated_at`` timestamp on every write; the extract-meta
        row relies on its ``server_default`` for ``extracted_at`` and is
        rewritten in place so the on-disk paths always reflect the
        latest successful extract.
        """
        now = datetime.now(tz=timezone.utc)
        with self._session_factory() as session:
            detail_row = session.get(TDocCrDetailOrm, details.tdoc_id)
            if detail_row is None:
                detail_row = TDocCrDetailOrm(tdoc_id=details.tdoc_id)
                session.add(detail_row)
            self._details_to_orm(detail_row, details)
            detail_row.updated_at = now

            extract_row = session.get(TDocExtractOrm, extract_meta.tdoc_id)
            if extract_row is None:
                extract_row = TDocExtractOrm(tdoc_id=extract_meta.tdoc_id)
                session.add(extract_row)
            self._meta_to_orm(extract_row, extract_meta)

            session.commit()

    def get_extract_meta(self, tdoc_id: str) -> TDocExtractMeta | None:
        """Return the cached-extract metadata, or ``None`` on miss."""
        with self._session_factory() as session:
            row = session.get(TDocExtractOrm, tdoc_id)
        if row is None:
            return None
        return _orm_to_meta(row)

    def list_all(self) -> list[TDocCRDetails]:
        """Return every persisted detail row (CLI / debugging)."""
        with self._session_factory() as session:
            rows = session.scalars(select(TDocCrDetailOrm)).all()
        return [_orm_to_details(row) for row in rows]

    @staticmethod
    def _details_to_orm(target: TDocCrDetailOrm, details: TDocCRDetails) -> None:
        """Copy :class:`TDocCRDetails` fields onto an ORM instance.

        Excludes ``tdoc_id`` (PK, never overwritten) and ``extracted_at``
        / ``updated_at`` (stamped separately by :meth:`upsert` /
        server-side defaults).
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
        target.clauses_affected = details.clauses_affected
        target.other_comments = details.other_comments
        target.revision_history = details.revision_history
        target.ats_version = details.ats_version
        target.ttcn_release = details.ttcn_release
        target.test_case = details.test_case
        target.test_suite = details.test_suite
        target.ue = details.ue
        target.ss = details.ss
        target.year = details.year
        target.tech = details.tech
        target.extracted_tdoc_id = details.extracted_tdoc_id
        target.parser_version = details.parser_version
        target.corrections = json.dumps(details.corrections, ensure_ascii=False)

    @staticmethod
    def _meta_to_orm(target: TDocExtractOrm, meta: TDocExtractMeta) -> None:
        """Copy :class:`TDocExtractMeta` fields onto an ORM instance."""
        target.zip_path = meta.zip_path
        target.markdown_path = meta.markdown_path
        target.doc_filename = meta.doc_filename
        target.parser_version = meta.parser_version
        if meta.extracted_at is not None:
            target.extracted_at = meta.extracted_at


def _orm_to_details(row: TDocCrDetailOrm) -> TDocCRDetails:
    """Reconstruct a :class:`TDocCRDetails` from an ORM row.

    The ``corrections`` ``TEXT`` column is decoded with a tolerant
    fallback to ``[]`` so a ``None`` / empty / unparseable blob never
    breaks a read. The dataclass itself rejects blank ``tdoc_id`` in
    ``__post_init__``, so we pass it through verbatim from the ORM row.
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
        clauses_affected=row.clauses_affected,
        other_comments=row.other_comments,
        revision_history=row.revision_history,
        ats_version=row.ats_version,
        ttcn_release=row.ttcn_release,
        test_case=row.test_case,
        test_suite=row.test_suite,
        ue=row.ue,
        ss=row.ss,
        corrections=_decode_corrections(row.corrections),
        year=row.year,
        tech=row.tech,
        extracted_tdoc_id=row.extracted_tdoc_id,
        parser_version=row.parser_version,
    )


def _orm_to_meta(row: TDocExtractOrm) -> TDocExtractMeta:
    """Reconstruct a :class:`TDocExtractMeta` from an ORM row."""
    return TDocExtractMeta(
        tdoc_id=row.tdoc_id,
        zip_path=row.zip_path,
        markdown_path=row.markdown_path,
        doc_filename=row.doc_filename,
        extracted_at=row.extracted_at,
        parser_version=row.parser_version,
    )


def _decode_corrections(blob: str | None) -> list[dict[str, str]]:
    """Decode the ``corrections`` TEXT column back to a list of dicts.

    Tolerant by design: ``None``, empty strings, and JSON decode errors
    all fall back to an empty list so a corrupt row doesn't break the
    read path. The dataclass field defaults to ``[]`` already; this
    helper exists so the round-trip is explicit and testable.
    """
    if not blob:
        return []
    try:
        decoded = json.loads(blob)
    except (json.JSONDecodeError, TypeError, ValueError):
        logger.warning(
            "Could not decode corrections blob (length=%d); falling back to []",
            len(blob),
        )
        return []
    if not isinstance(decoded, list):
        logger.warning(
            "Decoded corrections is not a list (type=%s); falling back to []",
            type(decoded).__name__,
        )
        return []
    return [item for item in decoded if isinstance(item, dict)]
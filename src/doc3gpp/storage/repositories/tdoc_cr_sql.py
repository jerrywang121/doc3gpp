"""SQLAlchemy-backed implementation of :class:`TDocCrDetailRepository`.

Stores both the parsed CR detail row (``tdoc_cr_details``) and the
cache-extract metadata sidecar (``tdoc_extracts``) that point at the
on-disk artefacts under :mod:`doc3gpp.scraping.cache`. ``upsert`` writes
both rows inside a single transaction so the two tables never disagree.

Both tables are keyed by the immutable download ``url`` — 3GPP assets
are byte-for-byte identical for the lifetime of a URL, so re-extracting
the same URL is idempotent, while multiple revisions of a single
``tdoc_id`` land at distinct URLs and occupy distinct rows. ``tdoc_id``
is a non-PK FK into ``tdocs.tdoc_id`` (indexed for the per-tdoc lookup)
with ``ondelete="CASCADE"`` so deleting a parent TDoc still cleans up
every revision's detail rows.

The ``corrections`` list of dicts on :class:`TDocCRDetails` is
serialised to a single ``TEXT`` column (``corrections``) via
:func:`json.dumps`. Reads use a tolerant decoder that falls back to
``[]`` on a ``None`` / empty / unparseable blob so a corrupt row never
breaks an unrelated read.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import select

from doc3gpp.models.tdoc_cr import TDocCRDetails, TDocExtractMeta
from doc3gpp.storage.db.models import TDocCrDetailOrm, TDocExtractOrm
from doc3gpp.storage.db.session import get_session_factory

logger = logging.getLogger(__name__)


class SQLAlchemyTDocCrRepository:
    """SQLAlchemy implementation of :class:`TDocCrDetailRepository`.

    Persists two tables — ``tdoc_cr_details`` (parsed fields) and
    ``tdoc_extracts`` (paths to cached zip / markdown) — both keyed by
    the immutable download ``url``. The ``upsert`` call writes both
    rows inside a single transaction.
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

    def get(self, tdoc_id: str) -> list[TDocCRDetails]:
        """Return every detail row for ``tdoc_id``, newest first.

        Newest first means highest ``extracted_at``; ties (multiple
        upserts in the same second) break on the ``url`` so the order
        is deterministic across runs.
        """
        with self._session_factory() as session:
            rows = (
                session.scalars(
                    select(TDocCrDetailOrm)
                    .where(TDocCrDetailOrm.tdoc_id == tdoc_id)
                    .order_by(
                        TDocCrDetailOrm.extracted_at.desc(),
                        TDocCrDetailOrm.url.asc(),
                    )
                )
                .all()
            )
        return [_orm_to_details(row) for row in rows]

    def get_by_url(self, url: str) -> TDocCRDetails | None:
        """Return the detail row whose URL matches, or ``None``."""
        with self._session_factory() as session:
            row = session.get(TDocCrDetailOrm, url)
        if row is None:
            return None
        return _orm_to_details(row)

    def upsert(
        self,
        details: TDocCRDetails,
        extract_meta: TDocExtractMeta,
    ) -> None:
        """Insert/update both rows in a single transaction.

        ``upsert`` requires that ``details.url`` and ``extract_meta.url``
        agree (and both be non-empty) — the two tables share the URL as
        their primary key and a transactional write that put one row in
        one table and a different row in the other would corrupt the
        read contract. The guard here is the only place that assertion
        is centralised.
        """
        if not details.url:
            raise ValueError(
                "TDocCRDetails requires a non-empty url for URL-keyed upsert"
            )
        if not extract_meta.url:
            raise ValueError(
                "TDocExtractMeta requires a non-empty url for URL-keyed upsert"
            )
        if details.url != extract_meta.url:
            raise ValueError(
                "details.url and extract_meta.url must match "
                f"(got {details.url!r} vs {extract_meta.url!r})"
            )
        if details.tdoc_id != extract_meta.tdoc_id:
            raise ValueError(
                "details.tdoc_id and extract_meta.tdoc_id must match "
                f"(got {details.tdoc_id!r} vs {extract_meta.tdoc_id!r})"
            )

        url = details.url
        with self._session_factory() as session:
            detail_row = session.get(TDocCrDetailOrm, url)
            if detail_row is None:
                detail_row = TDocCrDetailOrm(url=url, tdoc_id=details.tdoc_id)
                session.add(detail_row)
            else:
                # ``tdoc_id`` may shift if the underlying TDoc id was
                # reassigned; follow the new value so the FK stays
                # accurate. Same for ``url`` itself — except the URL is
                # the PK, so a different URL means a different row.
                detail_row.tdoc_id = details.tdoc_id
            self._details_to_orm(detail_row, details)

            extract_row = session.get(TDocExtractOrm, url)
            if extract_row is None:
                extract_row = TDocExtractOrm(url=url, tdoc_id=extract_meta.tdoc_id)
                session.add(extract_row)
            else:
                extract_row.tdoc_id = extract_meta.tdoc_id
            self._meta_to_orm(extract_row, extract_meta)

            session.commit()

    def get_extract_meta(self, tdoc_id: str) -> list[TDocExtractMeta]:
        """Return every metadata row for ``tdoc_id``, newest first."""
        with self._session_factory() as session:
            rows = (
                session.scalars(
                    select(TDocExtractOrm)
                    .where(TDocExtractOrm.tdoc_id == tdoc_id)
                    .order_by(
                        TDocExtractOrm.extracted_at.desc(),
                        TDocExtractOrm.url.asc(),
                    )
                )
                .all()
            )
        return [_orm_to_meta(row) for row in rows]

    def get_extract_meta_by_url(self, url: str) -> TDocExtractMeta | None:
        """Return the extract-metadata row whose URL matches."""
        with self._session_factory() as session:
            row = session.get(TDocExtractOrm, url)
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

        Excludes ``url`` (PK, never overwritten after construction),
        ``tdoc_id`` (handled by :meth:`upsert` because it is the FK),
        and ``extracted_at`` (stamped by the server-side default).
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
        """Copy :class:`TDocExtractMeta` fields onto an ORM instance.

        Excludes ``url`` (PK) and ``tdoc_id`` (FK, handled by
        :meth:`upsert`).
        """
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
    breaks a read. ``url`` is read straight from the row — the URL is
    now the PK rather than a nullable provenance column.
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
        url=row.url,
        parser_version=row.parser_version,
    )


def _orm_to_meta(row: TDocExtractOrm) -> TDocExtractMeta:
    """Reconstruct a :class:`TDocExtractMeta` from an ORM row."""
    return TDocExtractMeta(
        url=row.url,
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

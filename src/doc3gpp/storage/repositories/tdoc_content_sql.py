"""Atomic SQL persistence for per-type TDoc content bundles, keyed by ZIP URL."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import replace

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from doc3gpp.models.tdoc_content_families import (
    TDOC_CONTENT_STORES,
    TDocContentStore,
    validate_content_bundle,
)
from doc3gpp.models.tdoc_cr import TDocCRDetails, TDocCRTTCNDetails, TDocExtractMeta
from doc3gpp.models.tdoc_cr_change_details import TDocCRChangeDetails
from doc3gpp.models.tdoc_document import (
    TDocDocument,
    markdown_member_name,
    ordered_sources,
)
from doc3gpp.storage.compression import compress_json, decompress_json
from doc3gpp.storage.db.models import (
    TDocCrAnnexOrm,
    TDocCrChangeDetailOrm,
    TDocCrDetailOrm,
    TDocCrTtcnDetailOrm,
    TDocDiscussionContentOrm,
    TDocExtractOrm,
    TDocLsContentOrm,
    TDocWidContentOrm,
)
from doc3gpp.storage.db.session import get_session_factory
from doc3gpp.storage.repositories.tdoc_cr_change_details_sql import (
    _details_to_orm as _change_details_to_orm,
)
from doc3gpp.storage.repositories.tdoc_cr_sql import (
    SQLAlchemyTDocCrRepository,
    _orm_to_meta,
)
from doc3gpp.storage.repositories.tdoc_cr_ttcn_sql import (
    _details_to_orm as _ttcn_details_to_orm,
)

_ContentOrm = (
    type[TDocCrAnnexOrm]
    | type[TDocWidContentOrm]
    | type[TDocLsContentOrm]
    | type[TDocDiscussionContentOrm]
)
_CONTENT_ORMS: tuple[_ContentOrm, ...] = (
    TDocCrAnnexOrm,
    TDocWidContentOrm,
    TDocLsContentOrm,
    TDocDiscussionContentOrm,
)
_ORM_BY_TABLE: dict[str, _ContentOrm] = {
    orm.__tablename__: orm for orm in _CONTENT_ORMS
}
_CR_STORE = next(store for store in TDOC_CONTENT_STORES if store.key == "cr")


class SQLAlchemyTDocContentRepository:
    """Persist and read per-type TDoc content rows grouped by immutable ZIP URL.

    Each replace method is one transaction. Validation runs before any SQL.
    The transaction then upserts the ``tdoc_extracts`` parent, deletes the
    URL's rows from every content table, and inserts the new rows, so a
    previous bundle of another type cannot survive a replacement.
    """

    def __init__(self, session_factory=None) -> None:
        """Initialize the repository with an optional SQLAlchemy session factory."""
        self._session_factory = session_factory or get_session_factory()

    def get_extract_meta(self, ftp_url: str) -> TDocExtractMeta | None:
        """Return the ``tdoc_extracts`` parent row for ``ftp_url``."""
        url = _normalize_url(ftp_url)
        with self._session_factory() as session:
            row = session.get(TDocExtractOrm, url)
        return None if row is None else _orm_to_meta(row)

    def replace_cr_bundle(
        self,
        details: TDocCRDetails,
        ttcn: TDocCRTTCNDetails | None,
        changes: TDocCRChangeDetails | None,
        meta: TDocExtractMeta,
        annexes: Sequence[TDocDocument],
    ) -> None:
        """Atomically replace a CR's sidecars, parent row, and annex rows.

        The CR main document is never stored in SQL, so ``annexes`` holds only
        the supporting documents.
        """
        ftp_url = _normalize_url(meta.ftp_url)
        if not details.ftp_url or _normalize_url(details.ftp_url) != ftp_url:
            raise ValueError("cover details and extraction metadata must share ftp_url")
        if details.tdoc_id != meta.tdoc_id:
            raise ValueError("cover details and extraction metadata must share tdoc_id")
        if ttcn is not None:
            if ttcn.tdoc_id != meta.tdoc_id:
                raise ValueError("TTCN details and extraction metadata must share tdoc_id")
            if ttcn.ftp_url and _normalize_url(ttcn.ftp_url) != ftp_url:
                raise ValueError("TTCN details and extraction metadata must share ftp_url")
        if changes is not None:
            if changes.tdoc_id != meta.tdoc_id:
                raise ValueError("body-change details and extraction metadata must share tdoc_id")
            if changes.ftp_url and _normalize_url(changes.ftp_url) != ftp_url:
                raise ValueError("body-change details and extraction metadata must share ftp_url")
        validate_content_bundle(_CR_STORE, meta, annexes)

        with self._session_factory.begin() as session:
            _delete_content_rows(session, ftp_url)
            session.execute(
                delete(TDocCrTtcnDetailOrm).where(TDocCrTtcnDetailOrm.ftp_url == ftp_url)
            )
            session.execute(
                delete(TDocCrChangeDetailOrm).where(TDocCrChangeDetailOrm.ftp_url == ftp_url)
            )

            cover_row = session.get(TDocCrDetailOrm, ftp_url)
            if cover_row is None:
                cover_row = TDocCrDetailOrm(ftp_url=ftp_url, tdoc_id=meta.tdoc_id)
                session.add(cover_row)
            else:
                cover_row.tdoc_id = meta.tdoc_id
            SQLAlchemyTDocCrRepository._details_to_orm(cover_row, details)

            _upsert_parent(session, ftp_url, meta, document_count=len(annexes))

            # The child rows reference the parent, so flush its upsert first.
            session.flush()

            if ttcn is not None:
                ttcn_row = TDocCrTtcnDetailOrm(ftp_url=ftp_url, tdoc_id=meta.tdoc_id)
                _ttcn_details_to_orm(ttcn_row, ttcn)
                session.add(ttcn_row)

            if changes is not None:
                changes_row = TDocCrChangeDetailOrm(ftp_url=ftp_url, tdoc_id=meta.tdoc_id)
                _change_details_to_orm(changes_row, changes)
                session.add(changes_row)

            session.add_all(
                _content_rows(TDocCrAnnexOrm, ftp_url, meta.tdoc_id, annexes)
            )

    def replace_family_bundle(
        self,
        store: TDocContentStore,
        meta: TDocExtractMeta,
        documents: Sequence[TDocDocument],
    ) -> None:
        """Atomically replace a family's parent and content rows.

        ``documents[0]`` is the main document and must be named by
        ``meta.doc_filename``.
        """
        if not store.includes_main:
            raise ValueError(f"{store.key} stores only annexes; use replace_cr_bundle")
        ftp_url = _normalize_url(meta.ftp_url)
        validate_content_bundle(store, meta, documents)

        with self._session_factory.begin() as session:
            _upsert_parent(session, ftp_url, meta, document_count=len(documents))

            # The child rows reference the parent, so flush its upsert first.
            session.flush()

            _delete_content_rows(session, ftp_url)
            session.add_all(
                _content_rows(_orm_for(store), ftp_url, meta.tdoc_id, documents)
            )

    def list_documents(
        self,
        store: TDocContentStore,
        ftp_url: str,
    ) -> tuple[TDocDocument, ...]:
        """Return one URL's documents from ``store`` in derived file order."""
        url = _normalize_url(ftp_url)
        orm = _orm_for(store)
        with self._session_factory() as session:
            parent = session.get(TDocExtractOrm, url)
            if parent is None:
                return ()
            main = parent.doc_filename
            rows = session.scalars(select(orm).where(orm.ftp_url == url)).all()

        rows_by_source = {row.doc_filename: row for row in rows}
        ordered = ordered_sources(tuple(rows_by_source), main=main)
        return tuple(
            _to_document(rows_by_source[source], file_order)
            for file_order, source in enumerate(ordered)
        )

    def get_document(
        self,
        store: TDocContentStore,
        ftp_url: str,
        source_file: str,
    ) -> TDocDocument | None:
        """Return one exact, case-preserving source member from ``store``."""
        url = _normalize_url(ftp_url)
        orm = _orm_for(store)
        with self._session_factory() as session:
            parent = session.get(TDocExtractOrm, url)
            if parent is None:
                return None
            main = parent.doc_filename
            sources = tuple(
                session.scalars(
                    select(orm.doc_filename).where(orm.ftp_url == url)
                ).all()
            )
            if source_file not in sources:
                return None
            row = session.scalars(
                select(orm).where(orm.ftp_url == url, orm.doc_filename == source_file)
            ).one()

        file_order = ordered_sources(sources, main=main).index(source_file)
        return _to_document(row, file_order)


def _normalize_url(ftp_url: str) -> str:
    return ftp_url.strip().lower()


def _orm_for(store: TDocContentStore) -> _ContentOrm:
    return _ORM_BY_TABLE[store.table_name]


def _upsert_parent(
    session: Session,
    ftp_url: str,
    meta: TDocExtractMeta,
    *,
    document_count: int,
) -> None:
    stored_meta = replace(
        meta,
        ftp_url=ftp_url,
        extraction_version=2,
        document_count=document_count,
    )
    extract_row = session.get(TDocExtractOrm, ftp_url)
    if extract_row is None:
        extract_row = TDocExtractOrm(ftp_url=ftp_url, tdoc_id=meta.tdoc_id)
        session.add(extract_row)
    else:
        extract_row.tdoc_id = meta.tdoc_id
    SQLAlchemyTDocCrRepository._meta_to_orm(extract_row, stored_meta)


def _delete_content_rows(session: Session, ftp_url: str) -> None:
    for orm in _CONTENT_ORMS:
        session.execute(delete(orm).where(orm.ftp_url == ftp_url))


def _content_rows(
    orm: _ContentOrm,
    ftp_url: str,
    tdoc_id: str,
    documents: Iterable[TDocDocument],
) -> list[object]:
    return [
        orm(
            ftp_url=ftp_url,
            doc_filename=document.source_file,
            document_id=document.document_id,
            tdoc_id=tdoc_id,
            content=compress_json(document.markdown_text),
            content_hash=document.content_hash,
            token_count=len(document.markdown_text.split()),
        )
        for document in documents
    ]


def _to_document(row, file_order: int) -> TDocDocument:
    markdown = decompress_json(row.content)
    if markdown is None:
        raise ValueError(f"stored content for {row.doc_filename!r} cannot be decoded")
    return TDocDocument(
        document_id=row.document_id,
        source_file=row.doc_filename,
        markdown_file=markdown_member_name(row.document_id, row.doc_filename),
        file_order=file_order,
        markdown_text=markdown,
        content_hash=row.content_hash,
    )

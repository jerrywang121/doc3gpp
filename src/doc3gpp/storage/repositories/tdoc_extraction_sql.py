"""Atomic SQL persistence for URL-owned TDoc extraction bundles."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from sqlalchemy import delete, select

from doc3gpp.models.tdoc_cr import (
    TDocCRDetails,
    TDocCRTTCNDetails,
    TDocExtractMeta,
)
from doc3gpp.models.tdoc_cr_change_details import TDocCRChangeDetails
from doc3gpp.models.tdoc_document import TDocDocument
from doc3gpp.models.tdoc_identity import document_id_for
from doc3gpp.storage.db.models import (
    TDocCrChangeDetailOrm,
    TDocCrDetailOrm,
    TDocCrTtcnDetailOrm,
    TDocDocumentOrm,
    TDocExtractOrm,
)
from doc3gpp.storage.db.session import get_session_factory
from doc3gpp.storage.repositories.tdoc_cr_change_details_sql import (
    _details_to_orm as _change_details_to_orm,
)
from doc3gpp.storage.repositories.tdoc_cr_sql import SQLAlchemyTDocCrRepository
from doc3gpp.storage.repositories.tdoc_cr_ttcn_sql import (
    _details_to_orm as _ttcn_details_to_orm,
)


class SQLAlchemyTDocExtractionRepository:
    """Persist and retrieve SQL-backed documents grouped by their source ZIP.

    ``replace_bundle`` is the only write boundary for an extraction: the cover,
    optional sidecars, parent metadata, and all document rows are committed or
    rolled back together.
    """

    def __init__(self, session_factory=None) -> None:
        """Initialize the repository with an optional SQLAlchemy session factory."""
        self._session_factory = session_factory or get_session_factory()

    def replace_bundle(
        self,
        details: TDocCRDetails,
        ttcn: TDocCRTTCNDetails | None,
        changes: TDocCRChangeDetails | None,
        meta: TDocExtractMeta,
        documents: Sequence[TDocDocument],
    ) -> None:
        """Atomically replace all persisted extraction state for ``meta.ftp_url``."""
        ftp_url = meta.ftp_url.strip().lower()
        if not details.ftp_url or details.ftp_url.strip().lower() != ftp_url:
            raise ValueError("cover details and extraction metadata must share ftp_url")
        if details.tdoc_id != meta.tdoc_id:
            raise ValueError("cover details and extraction metadata must share tdoc_id")
        if not documents:
            raise ValueError("a complete extraction bundle must contain a main document")

        document_values = tuple(documents)
        expected_file_orders = tuple(range(len(document_values)))
        actual_file_orders = tuple(document.file_order for document in document_values)
        if actual_file_orders != expected_file_orders:
            raise ValueError("document file_order values must be contiguous starting at zero")

        main_documents = [
            document for document in document_values
            if document.source_file == meta.doc_filename
        ]
        if len(main_documents) != 1 or main_documents[0].file_order != 0:
            raise ValueError(
                "doc_filename must identify exactly one main document at file_order zero"
            )
        for document in document_values:
            if document.document_id != document_id_for(ftp_url, document.source_file):
                raise ValueError(
                    f"document_id does not match ftp_url/source_file for {document.source_file!r}"
                )

        if ttcn is not None:
            if ttcn.tdoc_id != meta.tdoc_id:
                raise ValueError("TTCN details and extraction metadata must share tdoc_id")
            if ttcn.ftp_url and ttcn.ftp_url.strip().lower() != ftp_url:
                raise ValueError("TTCN details and extraction metadata must share ftp_url")
        if changes is not None:
            if changes.tdoc_id != meta.tdoc_id:
                raise ValueError("body-change details and extraction metadata must share tdoc_id")
            if changes.ftp_url and changes.ftp_url.strip().lower() != ftp_url:
                raise ValueError("body-change details and extraction metadata must share ftp_url")

        stored_meta = replace(
            meta,
            ftp_url=ftp_url,
            extraction_version=2,
            document_count=len(document_values),
        )

        with self._session_factory.begin() as session:
            # Delete the previous URL-owned children before adding any new ORM
            # state. A later insert/flush failure rolls these deletes back too.
            session.execute(
                delete(TDocDocumentOrm).where(TDocDocumentOrm.ftp_url == ftp_url)
            )
            session.execute(
                delete(TDocCrTtcnDetailOrm).where(
                    TDocCrTtcnDetailOrm.ftp_url == ftp_url
                )
            )
            session.execute(
                delete(TDocCrChangeDetailOrm).where(
                    TDocCrChangeDetailOrm.ftp_url == ftp_url
                )
            )

            cover_row = session.get(TDocCrDetailOrm, ftp_url)
            if cover_row is None:
                cover_row = TDocCrDetailOrm(
                    ftp_url=ftp_url,
                    tdoc_id=meta.tdoc_id,
                )
                session.add(cover_row)
            else:
                cover_row.tdoc_id = meta.tdoc_id
            SQLAlchemyTDocCrRepository._details_to_orm(cover_row, details)

            extract_row = session.get(TDocExtractOrm, ftp_url)
            if extract_row is None:
                extract_row = TDocExtractOrm(
                    ftp_url=ftp_url,
                    tdoc_id=meta.tdoc_id,
                )
                session.add(extract_row)
            else:
                extract_row.tdoc_id = meta.tdoc_id
            SQLAlchemyTDocCrRepository._meta_to_orm(extract_row, stored_meta)

            # The child table has an FK to this URL-owned parent. Flush the
            # cover/extract upserts before adding document rows, without ending
            # the enclosing atomic transaction.
            session.flush()

            if ttcn is not None:
                ttcn_row = TDocCrTtcnDetailOrm(
                    ftp_url=ftp_url,
                    tdoc_id=meta.tdoc_id,
                )
                _ttcn_details_to_orm(ttcn_row, ttcn)
                session.add(ttcn_row)

            if changes is not None:
                changes_row = TDocCrChangeDetailOrm(
                    ftp_url=ftp_url,
                    tdoc_id=meta.tdoc_id,
                )
                _change_details_to_orm(changes_row, changes)
                session.add(changes_row)

            session.add_all(
                TDocDocumentOrm(
                    document_id=document.document_id,
                    ftp_url=ftp_url,
                    source_file=document.source_file,
                    markdown_file=document.markdown_file,
                    file_order=document.file_order,
                    markdown_text=document.markdown_text,
                    content_hash=document.content_hash,
                )
                for document in document_values
            )

    def list_documents(self, ftp_url: str) -> tuple[TDocDocument, ...]:
        """Return the URL's document collection in file order."""
        with self._session_factory() as session:
            rows = session.scalars(
                select(TDocDocumentOrm)
                .where(TDocDocumentOrm.ftp_url == ftp_url.strip().lower())
                .order_by(TDocDocumentOrm.file_order.asc())
            ).all()
            return tuple(_orm_to_document(row) for row in rows)

    def get_document(
        self,
        ftp_url: str,
        source_file: str,
    ) -> TDocDocument | None:
        """Return a document by exact, case-preserving source member path."""
        with self._session_factory() as session:
            row = session.scalars(
                select(TDocDocumentOrm).where(
                    TDocDocumentOrm.ftp_url == ftp_url.strip().lower(),
                    TDocDocumentOrm.source_file == source_file,
                )
            ).one_or_none()
            return None if row is None else _orm_to_document(row)

    def get_documents_for_tdoc(self, tdoc_id: str) -> tuple[TDocDocument, ...]:
        """Return every stored URL's documents for a logical TDoc ID."""
        with self._session_factory() as session:
            rows = session.scalars(
                select(TDocDocumentOrm)
                .join(
                    TDocExtractOrm,
                    TDocDocumentOrm.ftp_url == TDocExtractOrm.ftp_url,
                )
                .where(TDocExtractOrm.tdoc_id == tdoc_id)
                .order_by(
                    TDocDocumentOrm.ftp_url.asc(),
                    TDocDocumentOrm.file_order.asc(),
                )
            ).all()
            return tuple(_orm_to_document(row) for row in rows)


def _orm_to_document(row: TDocDocumentOrm) -> TDocDocument:
    """Reconstruct an ORM-free document value from its SQL row."""
    return TDocDocument(
        document_id=row.document_id,
        source_file=row.source_file,
        markdown_file=row.markdown_file,
        file_order=row.file_order,
        markdown_text=row.markdown_text,
        content_hash=row.content_hash,
    )

"""SQLAlchemy-backed implementation of :class:`LSParserRepository`.

Stores LS header extractions in ``tdoc_cr_ls_details`` — one row per
immutable ``ftp_url`` with a foreign-key ``tdoc_id`` into
``tdocs.tdoc_id`` (``ON DELETE CASCADE``). The ``attachments_json``
column holds the parsed attachments as gzip-compressed UTF-8 JSON via
:func:`doc3gpp.storage.compression.compress_json`. The ``variant``
column tags the format family (``"3gpp"``, ``"ieee"``, ``"etsi"``)
so the show record and search index can branch on it.
"""

from __future__ import annotations

import logging

from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from doc3gpp.models.tdoc_ls import TDocLSDetails
from doc3gpp.storage.compression import compress_json, decompress_json
from doc3gpp.storage.db.models import TDocCrLSDetailOrm
from doc3gpp.storage.db.session import get_session_factory

logger = logging.getLogger(__name__)


class SQLAlchemyLSParserRepository:
    """SQLAlchemy implementation of :class:`LSParserRepository`."""

    def __init__(self, session_factory=None) -> None:
        self._session_factory = session_factory or get_session_factory()
        self._ensured = False

    def _ensure_table_exists(self) -> None:
        if self._ensured:
            return
        from doc3gpp.storage.db.base import Base
        from doc3gpp.storage.db.session import get_engine

        try:
            with self._session_factory() as session:
                session.execute(text("SELECT 1 FROM tdoc_cr_ls_details LIMIT 0"))
        except OperationalError as exc:
            msg = str(exc).lower()
            if "no such table" in msg or "doesn't exist" in msg:
                Base.metadata.create_all(bind=get_engine())
                with self._session_factory() as session:
                    session.execute(text("SELECT 1 FROM tdoc_cr_ls_details LIMIT 0"))
            else:
                raise
        self._ensured = True

    def upsert(self, details: TDocLSDetails) -> None:
        self._ensure_table_exists()
        if not details.ftp_url:
            raise ValueError(
                "TDocLSDetails requires a non-empty ftp_url for URL-keyed upsert"
            )
        ftp_url = details.ftp_url
        with self._session_factory() as session:
            row = session.get(TDocCrLSDetailOrm, ftp_url)
            if row is None:
                row = TDocCrLSDetailOrm(ftp_url=ftp_url, tdoc_id=details.tdoc_id)
                session.add(row)
            else:
                row.tdoc_id = details.tdoc_id
            _details_to_orm(row, details)
            session.commit()

    def get_by_url(self, ftp_url: str) -> TDocLSDetails | None:
        self._ensure_table_exists()
        with self._session_factory() as session:
            row = session.get(TDocCrLSDetailOrm, ftp_url)
        if row is None:
            return None
        return _orm_to_details(row)

    def get_by_tdoc_id(self, tdoc_id: str) -> list[TDocLSDetails]:
        self._ensure_table_exists()
        with self._session_factory() as session:
            rows = (
                session.scalars(
                    select(TDocCrLSDetailOrm)
                    .where(TDocCrLSDetailOrm.tdoc_id == tdoc_id)
                    .order_by(TDocCrLSDetailOrm.ftp_url.asc())
                ).all()
            )
        return [_orm_to_details(r) for r in rows]

    def get_by_variant(
        self, ftp_url: str, variant: str
    ) -> TDocLSDetails | None:
        self._ensure_table_exists()
        with self._session_factory() as session:
            row = session.get(TDocCrLSDetailOrm, ftp_url)
        if row is None or row.variant != variant:
            return None
        return _orm_to_details(row)


def _details_to_orm(target: TDocCrLSDetailOrm, details: TDocLSDetails) -> None:
    """Copy :class:`TDocLSDetails` fields onto an ORM instance.

    Excludes ``ftp_url`` (PK) and ``tdoc_id`` (FK, handled by
    :meth:`upsert`).
    """
    target.variant = details.variant
    target.title = details.title
    target.response_to_doc = details.response_to_doc
    target.response_to_title = details.response_to_title
    target.response_to_group = details.response_to_group
    target.release = details.release
    target.work_item_name = details.work_item_name
    target.work_item_code = details.work_item_code
    target.source = details.source
    target.to_groups = details.to_groups or None
    target.cc_groups = details.cc_groups or None
    target.attachments_json = (
        compress_json([dict(a) for a in details.attachments])
        if details.attachments
        else None
    )
    target.parser_version = details.parser_version


def _orm_to_details(row: TDocCrLSDetailOrm) -> TDocLSDetails:
    """Reconstruct a :class:`TDocLSDetails` from an ORM row."""
    raw = decompress_json(row.attachments_json) if row.attachments_json else []
    if not isinstance(raw, list):
        raw = []
    attachments: list[dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        attachments.append(
            {
                "doc_number": str(entry.get("doc_number", "")),
                "description": str(entry.get("description", "")),
            }
        )
    return TDocLSDetails(
        ftp_url=row.ftp_url,
        tdoc_id=row.tdoc_id,
        variant=row.variant,
        title=row.title,
        response_to_doc=row.response_to_doc,
        response_to_title=row.response_to_title,
        response_to_group=row.response_to_group,
        release=row.release,
        work_item_name=row.work_item_name,
        work_item_code=row.work_item_code,
        source=row.source,
        to_groups=row.to_groups or "",
        cc_groups=row.cc_groups or "",
        attachments=tuple(attachments),
        parser_version=row.parser_version,
        extracted_at=row.extracted_at,
    )

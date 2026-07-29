"""SQLAlchemy-backed implementation of :class:`TDocCrChangeDetailsRepository`.

Stores body-derived change details in ``tdoc_cr_change_details`` — one
row per immutable ``ftp_url``, with a foreign-key ``tdoc_id`` into
``tdocs.tdoc_id`` (``ON DELETE CASCADE``). The ``clauses`` column
holds a sorted, unique newline-delimited list of clause numbers; the
``changes`` column holds the captured change blocks as
gzip-compressed UTF-8 JSON.

Keying on ``ftp_url`` matches the existing sidecar convention
(``tdoc_cr_cover_page`` / ``tdoc_cr_ttcn_details`` /
``tdoc_extracts``).
"""

from __future__ import annotations

import logging

from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from doc3gpp.models.tdoc_cr_change_details import TDocCRChangeDetails
from doc3gpp.storage.compression import compress_json, decompress_json
from doc3gpp.storage.db.models import TDocCrChangeDetailOrm
from doc3gpp.storage.db.session import get_session_factory

logger = logging.getLogger(__name__)


class SQLAlchemyTDocCrChangeDetailsRepository:
    """SQLAlchemy implementation of :class:`TDocCrChangeDetailsRepository`."""

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
                session.execute(text("SELECT 1 FROM tdoc_cr_change_details LIMIT 0"))
        except OperationalError as exc:
            msg = str(exc).lower()
            if "no such table" in msg or "doesn't exist" in msg:
                Base.metadata.create_all(bind=get_engine())
                with self._session_factory() as session:
                    session.execute(text("SELECT 1 FROM tdoc_cr_change_details LIMIT 0"))
            else:
                raise
        self._ensured = True

    def upsert(self, details: TDocCRChangeDetails) -> None:
        """Insert/update the body-change row for ``details.ftp_url``."""
        self._ensure_table_exists()
        if not details.ftp_url:
            raise ValueError(
                "TDocCRChangeDetails requires a non-empty ftp_url for URL-keyed upsert"
            )
        if not details.tdoc_id:
            raise ValueError(
                "TDocCRChangeDetails requires a non-empty tdoc_id for URL-keyed upsert"
            )
        ftp_url = details.ftp_url
        with self._session_factory() as session:
            row = session.get(TDocCrChangeDetailOrm, ftp_url)
            if row is None:
                row = TDocCrChangeDetailOrm(
                    ftp_url=ftp_url, tdoc_id=details.tdoc_id
                )
                session.add(row)
            else:
                row.tdoc_id = details.tdoc_id
            _details_to_orm(row, details)
            session.commit()

    def get_by_url(self, url: str) -> TDocCRChangeDetails | None:
        """Return the body-change row for an immutable ``url``, or ``None``."""
        self._ensure_table_exists()
        with self._session_factory() as session:
            row = session.get(TDocCrChangeDetailOrm, url)
        if row is None:
            return None
        return _orm_to_details(row)

    def get_for_tdoc_id(self, tdoc_id: str) -> list[TDocCRChangeDetails]:
        """Return every body-change row for ``tdoc_id``."""
        self._ensure_table_exists()
        with self._session_factory() as session:
            rows = (
                session.scalars(
                    select(TDocCrChangeDetailOrm)
                    .where(TDocCrChangeDetailOrm.tdoc_id == tdoc_id)
                    .order_by(TDocCrChangeDetailOrm.ftp_url.asc())
                )
                .all()
            )
        return [_orm_to_details(row) for row in rows]


def _details_to_orm(target: TDocCrChangeDetailOrm, details: TDocCRChangeDetails) -> None:
    """Copy :class:`TDocCRChangeDetails` fields onto an ORM instance.

    Excludes ``ftp_url`` (PK) and ``tdoc_id`` (FK, handled by
    :meth:`upsert`).

    The ``changes`` column stores the per-block dicts as a list of
    ``{"clauses": [...], "text": "..."}`` records in gzip-compressed
    UTF-8 JSON.
    """
    target.clauses = "\n".join(details.clauses) if details.clauses else None
    target.changes = (
        compress_json(
            [{"clauses": list(b["clauses"]), "text": b["text"]} for b in details.changes]
        )
        if details.changes
        else None
    )


def _orm_to_details(row: TDocCrChangeDetailOrm) -> TDocCRChangeDetails:
    """Reconstruct a :class:`TDocCRChangeDetails` from an ORM row."""
    clauses = tuple(s for s in (row.clauses or "").split("\n") if s) if row.clauses else ()
    changes_raw = decompress_json(row.changes) if row.changes else []
    if not isinstance(changes_raw, list):
        changes_raw = []
    blocks: list[tuple[str, list[str]]] = []
    for entry in changes_raw:
        if not isinstance(entry, dict):
            continue
        clauses_list = entry.get("clauses") or []
        text = entry.get("text") or ""
        if not isinstance(clauses_list, list) or not isinstance(text, str):
            continue
        blocks.append((text, [str(c) for c in clauses_list]))
    changes = tuple({"clauses": cs, "text": tx} for tx, cs in blocks)
    return TDocCRChangeDetails(
        ftp_url=row.ftp_url,
        tdoc_id=row.tdoc_id,
        clauses=clauses,
        changes=changes,
    )

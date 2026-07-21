"""SQLAlchemy-backed implementation of :class:`TDocCrTTCNDetailRepository`.

Stores TTCN CR-specific details in ``tdoc_cr_ttcn_details`` — one row per
immutable ``ftp_url``, with a foreign-key ``tdoc_id`` into ``tdocs.tdoc_id``
(``ON DELETE CASCADE``). Six overview fields are stored as their own
columns; the ``required_changes`` correction list is stored as a
gzip-compressed UTF-8 JSON blob.

Keying on ``ftp_url`` (rather than ``tdoc_id``) lets every revision of a
single TDoc id occupy its own row, because 3GPP zip assets are
byte-for-byte identical for the lifetime of a URL.
"""

from __future__ import annotations

import logging

from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from doc3gpp.models.tdoc_cr import TDocCRTTCNDetails
from doc3gpp.storage.compression import compress_json, decompress_json
from doc3gpp.storage.db.models import TDocCrTtcnDetailOrm
from doc3gpp.storage.db.session import get_session_factory

logger = logging.getLogger(__name__)


class SQLAlchemyTDocCrTtcnRepository:
    """SQLAlchemy implementation of :class:`TDocCrTTCNDetailRepository`.

    Persists the TTCN CR sidecar table ``tdoc_cr_ttcn_details``. Each row
    is keyed by the immutable download ``ftp_url`` and carries six
    overview columns plus a compressed JSON blob for the
    ``required_changes`` correction list.
    """

    def __init__(self, session_factory=None) -> None:
        """Initialise the repository.

        Args:
            session_factory: Optional pre-built ``sessionmaker``. When
                omitted the function falls back to
                :func:`doc3gpp.storage.db.session.get_session_factory`.
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
                session.execute(text("SELECT 1 FROM tdoc_cr_ttcn_details LIMIT 0"))
        except OperationalError as exc:
            msg = str(exc).lower()
            if "no such table" in msg or "doesn't exist" in msg:
                Base.metadata.create_all(bind=get_engine())
                with self._session_factory() as session:
                    session.execute(text("SELECT 1 FROM tdoc_cr_ttcn_details LIMIT 0"))
            else:
                raise
        self._ensured = True

    def upsert(self, details: TDocCRTTCNDetails) -> None:
        """Insert/update the TTCN detail row for ``details.ftp_url``."""
        self._ensure_table_exists()
        if not details.ftp_url:
            raise ValueError(
                "TDocCRTTCNDetails requires a non-empty ftp_url for URL-keyed upsert"
            )
        if not details.tdoc_id:
            raise ValueError(
                "TDocCRTTCNDetails requires a non-empty tdoc_id for URL-keyed upsert"
            )

        ftp_url = details.ftp_url
        with self._session_factory() as session:
            row = session.get(TDocCrTtcnDetailOrm, ftp_url)
            if row is None:
                row = TDocCrTtcnDetailOrm(ftp_url=ftp_url, tdoc_id=details.tdoc_id)
                session.add(row)
            else:
                row.tdoc_id = details.tdoc_id
            _details_to_orm(row, details)
            session.commit()

    def get_by_url(self, url: str) -> TDocCRTTCNDetails | None:
        """Return the TTCN detail row for an immutable ``url``, or ``None``."""
        self._ensure_table_exists()
        with self._session_factory() as session:
            row = session.get(TDocCrTtcnDetailOrm, url)
        if row is None:
            return None
        return _orm_to_details(row)

    def get(self, tdoc_id: str) -> list[TDocCRTTCNDetails]:
        """Return every TTCN detail row for ``tdoc_id``, ordered by ``ftp_url`` ASC."""
        self._ensure_table_exists()
        with self._session_factory() as session:
            rows = (
                session.scalars(
                    select(TDocCrTtcnDetailOrm)
                    .where(TDocCrTtcnDetailOrm.tdoc_id == tdoc_id)
                    .order_by(TDocCrTtcnDetailOrm.ftp_url.asc())
                )
                .all()
            )
        return [_orm_to_details(row) for row in rows]

    def list_all(self) -> list[TDocCRTTCNDetails]:
        """Return every persisted TTCN detail row (CLI / debugging)."""
        self._ensure_table_exists()
        with self._session_factory() as session:
            rows = session.scalars(select(TDocCrTtcnDetailOrm)).all()
        return [_orm_to_details(row) for row in rows]


def _details_to_orm(target: TDocCrTtcnDetailOrm, details: TDocCRTTCNDetails) -> None:
    """Copy :class:`TDocCRTTCNDetails` fields onto an ORM instance.

    Excludes ``ftp_url`` (PK) and ``tdoc_id`` (FK, handled by
    :meth:`SQLAlchemyTDocCrTtcnRepository.upsert`).
    """
    target.testcase = details.testcase
    target.ue = details.ue
    target.ss = details.ss
    target.ats_version = details.ats_version
    target.ttcn_release = details.ttcn_release
    target.test_suite = details.test_suite
    target.required_changes = compress_json(details.required_changes)


def _orm_to_details(row: TDocCrTtcnDetailOrm) -> TDocCRTTCNDetails:
    """Reconstruct a :class:`TDocCRTTCNDetails` from an ORM row.

    The ``required_changes`` blob is decoded with a tolerant fallback to
    an empty list so a corrupt row never breaks the read path.
    """
    required_changes = decompress_json(row.required_changes)
    if not isinstance(required_changes, list):
        required_changes = []
    return TDocCRTTCNDetails(
        tdoc_id=row.tdoc_id,
        ftp_url=row.ftp_url,
        testcase=row.testcase,
        ue=row.ue,
        ss=row.ss,
        ats_version=row.ats_version,
        ttcn_release=row.ttcn_release,
        test_suite=row.test_suite,
        required_changes=required_changes,
    )

"""SQLAlchemy-backed implementation of :class:`TDocCrDetailRepository`.

Stores both the parsed CR detail row (``tdoc_cr_details``) and the
cache-extract metadata sidecar (``tdoc_extracts``) that point at the
on-disk artefacts under :mod:`doc3gpp.scraping.cache`. ``upsert`` writes
both rows inside a single transaction so the two tables never disagree.

Both tables are keyed by the immutable download ``ftp_url`` — a path
relative to the canonical 3GPP FTP root (``https://www.3gpp.org/ftp/``)
that uniquely identifies the upstream asset. 3GPP assets are byte-for-byte
identical for the lifetime of a URL, so re-extracting the same URL is
idempotent, while multiple revisions of a single ``tdoc_id`` land at
distinct URLs and occupy distinct rows. ``tdoc_id`` is a non-PK FK into
``tdocs.tdoc_id`` (indexed for the per-tdoc lookup) with
``ondelete="CASCADE"`` so deleting a parent TDoc still cleans up every
revision's detail rows.

The ``details`` dict on :class:`TDocCRDetails` is JSON-serialised and
gzip-compressed before being stored in the ``details`` ``LargeBinary``
column. Reads decompress transparently; a legacy uncompressed payload
or corrupt blob falls back to an empty dict so a bad row never breaks
a read.
"""

from __future__ import annotations

import gzip
import json
import logging
from typing import Any

from sqlalchemy import select

from doc3gpp.models.tdoc_cr import TDocCRDetails, TDocExtractMeta
from doc3gpp.storage.db.models import TDocCrDetailOrm, TDocExtractOrm
from doc3gpp.storage.db.session import get_session_factory

logger = logging.getLogger(__name__)


_GZIP_MAGIC = b"\x1f\x8b"


def _compress_details(details: dict[str, Any]) -> bytes:
    """gzip-compress UTF-8 JSON for the ``details`` blob."""
    payload = json.dumps(details, ensure_ascii=False).encode("utf-8")
    return gzip.compress(payload, compresslevel=9)


def _decompress_details(blob: bytes | None) -> dict[str, Any]:
    """Decode the ``details`` blob back to a dict.

    Tolerant by design: ``None``, empty bytes, gzip decompression
    errors, JSON decode errors, and non-dict results all fall back to
    an empty dict so a corrupt row never breaks the read path. Legacy
    uncompressed payloads (no gzip magic) are still parsed so a manual
    ``UPDATE`` survives a round-trip.
    """
    if not blob:
        return {}
    try:
        raw = gzip.decompress(blob) if blob[:2] == _GZIP_MAGIC else blob
        decoded = json.loads(raw.decode("utf-8"))
    except (gzip.BadGzipFile, json.JSONDecodeError, UnicodeDecodeError):
        logger.warning(
            "Could not decode details blob (length=%d); falling back to {}",
            len(blob),
        )
        return {}
    return decoded if isinstance(decoded, dict) else {}


class SQLAlchemyTDocCrRepository:
    """SQLAlchemy implementation of :class:`TDocCrDetailRepository`.

    Persists two tables — ``tdoc_cr_details`` (parsed fields) and
    ``tdoc_extracts`` (paths to cached zip / markdown) — both keyed by
    the immutable download ``ftp_url``. The ``upsert`` call writes both
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
        upserts in the same second) break on the ``ftp_url`` so the
        order is deterministic across runs.
        """
        with self._session_factory() as session:
            rows = (
                session.scalars(
                    select(TDocCrDetailOrm)
                    .where(TDocCrDetailOrm.tdoc_id == tdoc_id)
                    .order_by(
                        TDocCrDetailOrm.extracted_at.desc(),
                        TDocCrDetailOrm.ftp_url.asc(),
                    )
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

        ``upsert`` requires that ``details.ftp_url`` and
        ``extract_meta.ftp_url`` agree (and both be non-empty) — the
        two tables share the URL as their primary key and a
        transactional write that put one row in one table and a
        different row in the other would corrupt the read contract.
        The guard here is the only place that assertion is centralised.
        """
        if not details.ftp_url:
            raise ValueError(
                "TDocCRDetails requires a non-empty ftp_url for URL-keyed upsert"
            )
        if not extract_meta.ftp_url:
            raise ValueError(
                "TDocExtractMeta requires a non-empty ftp_url for URL-keyed upsert"
            )
        if details.ftp_url != extract_meta.ftp_url:
            raise ValueError(
                "details.ftp_url and extract_meta.ftp_url must match "
                f"(got {details.ftp_url!r} vs {extract_meta.ftp_url!r})"
            )
        if details.tdoc_id != extract_meta.tdoc_id:
            raise ValueError(
                "details.tdoc_id and extract_meta.tdoc_id must match "
                f"(got {details.tdoc_id!r} vs {extract_meta.tdoc_id!r})"
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

            extract_row = session.get(TDocExtractOrm, ftp_url)
            if extract_row is None:
                extract_row = TDocExtractOrm(ftp_url=ftp_url, tdoc_id=extract_meta.tdoc_id)
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

        Excludes ``ftp_url`` (PK, never overwritten after construction),
        ``tdoc_id`` (handled by :meth:`upsert` because it is the FK),
        and ``extracted_at`` (stamped by the server-side default).
        The ``details`` blob is gzip-compressed JSON.
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
        target.details = _compress_details(details.details)
        target.extracted_tdoc_id = details.extracted_tdoc_id
        target.parser_version = details.parser_version

    @staticmethod
    def _meta_to_orm(target: TDocExtractOrm, meta: TDocExtractMeta) -> None:
        """Copy :class:`TDocExtractMeta` fields onto an ORM instance.

        Excludes ``ftp_url`` (PK) and ``tdoc_id`` (FK, handled by
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

    The ``details`` blob is decoded with a tolerant fallback to ``{}``
    so a ``None`` / empty / unparseable blob never breaks a read.
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
        clauses_affected=row.clauses_affected,
        other_comments=row.other_comments,
        revision_history=row.revision_history,
        details=_decompress_details(row.details),
        extracted_tdoc_id=row.extracted_tdoc_id,
        ftp_url=row.ftp_url,
        parser_version=row.parser_version,
    )


def _orm_to_meta(row: TDocExtractOrm) -> TDocExtractMeta:
    """Reconstruct a :class:`TDocExtractMeta` from an ORM row."""
    return TDocExtractMeta(
        ftp_url=row.ftp_url,
        tdoc_id=row.tdoc_id,
        zip_path=row.zip_path,
        markdown_path=row.markdown_path,
        doc_filename=row.doc_filename,
        extracted_at=row.extracted_at,
        parser_version=row.parser_version,
    )
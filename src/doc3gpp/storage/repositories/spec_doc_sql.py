"""SQLAlchemy-backed implementation of :class:`SpecDocRepository`."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import sessionmaker

from doc3gpp.cli_filters import (
    is_not_null_token,
    is_null_token,
    split_not_like_prefix,
)
from doc3gpp.models.spec_doc import (
    ChunkDraft,
    SpecDocChunk,
    SpecDocSource,
    SpecDocToc,
    SpecDocTocEntry,
    SpecDocTocFile,
)
from doc3gpp.storage.compression import compress_json, decompress_json
from doc3gpp.storage.db.models import (
    SpecDocChunkORM,
    SpecDocSourceORM,
    SpecDocTocORM,
)
from doc3gpp.storage.db.session import get_specdata_session_factory


class SQLAlchemySpecDocRepository:
    """SQLAlchemy implementation storing rows in the ``spec_doc_*`` tables.

    Binds to the specdata engine (:func:`get_specdata_session_factory`)
    by default; pass an explicit ``session_factory`` to override
    (tests bind to a temporary SQLite file via ``sqlite_env``).
    """

    def __init__(self, session_factory: sessionmaker | None = None) -> None:
        self._session_factory = session_factory or get_specdata_session_factory()

    def get_source(self, spec_id: str, version: str) -> SpecDocSource | None:
        with self._session_factory() as session:
            row = session.get(SpecDocSourceORM, (spec_id, version))
        return _orm_to_source(row) if row is not None else None

    def record_download(
        self,
        spec_id: str,
        version: str,
        *,
        release: str | None,
        ftp_url: str,
        docx_count: int,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self._session_factory() as session:
            existing = session.get(SpecDocSourceORM, (spec_id, version))
            if existing is not None:
                existing.release = release
                existing.ftp_url = ftp_url
                existing.docx_count = docx_count
                existing.downloaded_at = now
                existing.parsed_at = None
                existing.chunk_count = 0
            else:
                session.add(
                    SpecDocSourceORM(
                        spec_id=spec_id,
                        version=version,
                        release=release,
                        ftp_url=ftp_url,
                        downloaded_at=now,
                        parsed_at=None,
                        chunk_count=0,
                        docx_count=docx_count,
                    )
                )
            session.commit()

    def record_parsed(
        self, spec_id: str, version: str, *, chunk_count: int
    ) -> None:
        now = datetime.now(timezone.utc)
        with self._session_factory() as session:
            existing = session.get(SpecDocSourceORM, (spec_id, version))
            if existing is not None:
                existing.parsed_at = now
                existing.chunk_count = chunk_count
            else:
                session.add(
                    SpecDocSourceORM(
                        spec_id=spec_id,
                        version=version,
                        release=None,
                        ftp_url="",
                        downloaded_at=None,
                        parsed_at=now,
                        chunk_count=chunk_count,
                        docx_count=0,
                    )
                )
            session.commit()

    def get_toc(self, spec_id: str, version: str) -> SpecDocToc | None:
        with self._session_factory() as session:
            row = session.get(SpecDocTocORM, (spec_id, version))
        return _orm_to_toc(row) if row is not None else None

    def upsert_toc(self, toc: SpecDocToc) -> None:
        payload = {
            "entries": [
                {
                    "level": e.level,
                    "section_no": e.section_no,
                    "title": e.title,
                    "source_file": e.source_file,
                    "file_order": e.file_order,
                }
                for e in toc.entries
            ],
            "files": [
                {
                    "source_file": f.source_file,
                    "file_order": f.file_order,
                    "first_section": f.first_section,
                }
                for f in toc.files
            ],
        }
        blob = compress_json(payload)
        file_order_json = json.dumps(
            {f.source_file: f.file_order for f in toc.files}
        )
        now = datetime.now(timezone.utc)
        with self._session_factory() as session:
            existing = session.get(SpecDocTocORM, (toc.spec_id, toc.version))
            if existing is not None:
                existing.release = toc.release
                existing.toc_json_gzip = blob
                existing.file_order_json = file_order_json
                existing.docx_count = toc.docx_count
                existing.created_at = now
            else:
                session.add(
                    SpecDocTocORM(
                        spec_id=toc.spec_id,
                        version=toc.version,
                        release=toc.release,
                        toc_json_gzip=blob,
                        file_order_json=file_order_json,
                        docx_count=toc.docx_count,
                        created_at=now,
                    )
                )
            session.commit()

    def replace_chunks(
        self,
        spec_id: str,
        version: str,
        *,
        release: str | None,
        drafts: list[ChunkDraft],
    ) -> list[SpecDocChunk]:
        with self._session_factory() as session:
            session.execute(
                delete(SpecDocChunkORM).where(
                    (SpecDocChunkORM.spec_id == spec_id)
                    & (SpecDocChunkORM.version == version)
                )
            )
            for index, draft in enumerate(drafts):
                session.add(
                    SpecDocChunkORM(
                        chunk_id=f"{spec_id}@{version}#{index}",
                        spec_id=spec_id,
                        version=version,
                        release=release,
                        file_order=draft.file_order,
                        source_file=draft.source_file,
                        chunk_index=index,
                        section_no=draft.section_no,
                        section_title=draft.section_title,
                        table_no=draft.table_no,
                        table_title=draft.table_title,
                        text=draft.text,
                    )
                )
            existing = session.get(SpecDocSourceORM, (spec_id, version))
            if existing is not None:
                if release is not None:
                    existing.release = release
            else:
                session.add(
                    SpecDocSourceORM(
                        spec_id=spec_id,
                        version=version,
                        release=release,
                        ftp_url="",
                        downloaded_at=None,
                        parsed_at=None,
                        chunk_count=0,
                        docx_count=0,
                    )
                )
            session.commit()
        return [
            SpecDocChunk(
                file_order=draft.file_order,
                source_file=draft.source_file,
                section_no=draft.section_no,
                section_title=draft.section_title,
                table_no=draft.table_no,
                table_title=draft.table_title,
                text=draft.text,
                chunk_id=f"{spec_id}@{version}#{index}",
                spec_id=spec_id,
                version=version,
                release=release,
                chunk_index=index,
            )
            for index, draft in enumerate(drafts)
        ]

    def list_chunks(
        self,
        spec_id: str,
        *,
        version: str | None = None,
        release: str | None = None,
        section: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SpecDocChunk]:
        with self._session_factory() as session:
            stmt = select(SpecDocChunkORM).where(
                SpecDocChunkORM.spec_id == spec_id
            )
            if version is not None:
                stmt = stmt.where(SpecDocChunkORM.version == version)
            if release is not None:
                stmt = stmt.where(SpecDocChunkORM.release == release)
            if section is not None:
                stmt = _apply_section_filter(stmt, section)
            stmt = (
                stmt.order_by(SpecDocChunkORM.version, SpecDocChunkORM.chunk_index)
                .offset(offset)
                .limit(limit)
            )
            rows = session.scalars(stmt).all()
        return [_orm_to_chunk(r) for r in rows]


def _apply_section_filter(stmt, value: str):
    """Filter ``stmt`` by the rich grammar against section no/title as a group.

    Mirrors :func:`build_text_filter_or_params` semantics: a plain pattern
    matches when *either* column matches (``OR``); a negated ``!`` pattern
    keeps the row only when *neither* column matches. ``null`` /
    ``not-null`` match when either column is (not) null.
    """
    title = SpecDocChunkORM.section_title
    number = SpecDocChunkORM.section_no
    if is_null_token(value):
        return stmt.where(or_(title.is_(None), number.is_(None)))
    if is_not_null_token(value):
        return stmt.where(or_(title.is_not(None), number.is_not(None)))
    negated, pattern = split_not_like_prefix(value)
    clause = or_(title.like(pattern), number.like(pattern))
    return stmt.where(~clause) if negated else stmt.where(clause)


def _orm_to_source(row: SpecDocSourceORM) -> SpecDocSource:
    return SpecDocSource(
        spec_id=row.spec_id,
        version=row.version,
        release=row.release,
        ftp_url=row.ftp_url,
        downloaded_at=_as_utc(row.downloaded_at),
        parsed_at=_as_utc(row.parsed_at),
        chunk_count=row.chunk_count,
        docx_count=row.docx_count,
    )


def _orm_to_toc(row: SpecDocTocORM) -> SpecDocToc:
    payload = decompress_json(row.toc_json_gzip)
    entries: list[SpecDocTocEntry] = []
    files: list[SpecDocTocFile] = []
    if isinstance(payload, dict):
        raw_entries = payload.get("entries") or []
        raw_files = payload.get("files") or []
        entries = [
            SpecDocTocEntry(
                level=e.get("level", 0),
                section_no=e.get("section_no"),
                title=e.get("title", ""),
                source_file=e.get("source_file", ""),
                file_order=e.get("file_order", 0),
            )
            for e in raw_entries
            if isinstance(e, dict)
        ]
        files = [
            SpecDocTocFile(
                source_file=f.get("source_file", ""),
                file_order=f.get("file_order", 0),
                first_section=f.get("first_section"),
            )
            for f in raw_files
            if isinstance(f, dict)
        ]
    return SpecDocToc(
        spec_id=row.spec_id,
        version=row.version,
        release=row.release,
        entries=entries,
        files=files,
        docx_count=row.docx_count,
        created_at=_as_utc(row.created_at),
    )


def _orm_to_chunk(row: SpecDocChunkORM) -> SpecDocChunk:
    return SpecDocChunk(
        file_order=row.file_order,
        source_file=row.source_file,
        section_no=row.section_no,
        section_title=row.section_title,
        table_no=row.table_no,
        table_title=row.table_title,
        text=row.text,
        chunk_id=row.chunk_id,
        spec_id=row.spec_id,
        version=row.version,
        release=row.release,
        chunk_index=row.chunk_index,
    )


def _as_utc(value: datetime | None) -> datetime | None:
    """Return ``value`` normalized to UTC, handling naive SQLite returns."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)

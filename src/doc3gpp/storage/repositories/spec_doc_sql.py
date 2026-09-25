"""SQLAlchemy-backed implementation of :class:`SpecDocRepository`."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from collections.abc import Iterable

from sqlalchemy import delete, select
from sqlalchemy.orm import sessionmaker

from doc3gpp.models.spec_doc import (
    ChunkDraft,
    SpecDocChunk,
    SpecDocSource,
    SpecDocToc,
    SpecDocTocEntry,
    SpecDocTocFile,
)
from doc3gpp.models.spec import spec_version_sort_key
from doc3gpp.storage.compression import compress_json, decompress_json
from doc3gpp.storage.db.models import (
    SpecDocChunkORM,
    SpecDocSourceORM,
    SpecDocTocORM,
)
from doc3gpp.storage.db.session import get_specdata_session_factory
from doc3gpp.storage.repositories.rich_filters import apply_text_filter


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

    def list_parsed_versions(
        self, spec_ids: Iterable[str] | None = None
    ) -> dict[str, list[str]]:
        requested = list(spec_ids) if spec_ids is not None else None
        if requested == []:
            return {}
        with self._session_factory() as session:
            stmt = select(SpecDocSourceORM.spec_id, SpecDocSourceORM.version).where(
                SpecDocSourceORM.parsed_at.is_not(None)
            )
            if requested is not None:
                stmt = stmt.where(SpecDocSourceORM.spec_id.in_(requested))
            rows = session.execute(stmt).all()
        parsed: dict[str, list[str]] = {}
        for spec_id, version in rows:
            parsed.setdefault(spec_id, []).append(version)
        for versions in parsed.values():
            versions.sort(key=spec_version_sort_key, reverse=True)
        return parsed

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

    def invalidate_parse_state(self, spec_id: str, version: str) -> None:
        with self._session_factory() as session:
            existing = session.get(SpecDocSourceORM, (spec_id, version))
            if existing is not None:
                existing.parsed_at = None
                existing.chunk_count = 0
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
                        sections=draft.sections,
                        tables=draft.tables,
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
                sections=draft.sections,
                tables=draft.tables,
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
        sections: str | None = None,
        tables: str | None = None,
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
            stmt = apply_text_filter(stmt, SpecDocChunkORM.sections, sections)
            stmt = apply_text_filter(stmt, SpecDocChunkORM.tables, tables)
            stmt = (
                stmt.order_by(SpecDocChunkORM.version, SpecDocChunkORM.chunk_index)
                .offset(offset)
                .limit(limit)
            )
            rows = session.scalars(stmt).all()
        return [_orm_to_chunk(r) for r in rows]


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
        sections=row.sections,
        tables=row.tables,
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

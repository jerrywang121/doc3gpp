"""Orchestration for spec-doc fetch/parse/toc.

Immutable ledger: a parsed ``(spec_id, version)`` pair skips
unconditionally; only ``force=True`` re-downloads + replaces chunks.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import Path

from doc3gpp.models.spec_doc import (
    ChunkDraft,
    SpecDocBatchResult,
    SpecDocChunk,
    SpecDocNoDocxError,
    SpecDocSource,
    SpecDocToc,
    SpecDocTooLargeError,
    SpecDocUnknownSpecError,
    SpecDocUnknownVersionError,
)
from doc3gpp.parsers.docx_converter import (
    HeadingBlock,
    ParagraphBlock,
    TableBlock,
    convert_document_to_blocks,
)
from doc3gpp.parsers.spec_doc import (
    SpecDocFileBlocks,
    extract_spec_toc,
    list_spec_docx,
    order_spec_files,
)
from doc3gpp.parsers.spec_doc_chunker import chunk_blocks
from doc3gpp.scraping.spec_doc_source import fetch_spec_doc_zip, resolve_spec_doc_version
from doc3gpp.settings.loader import get_settings

logger = logging.getLogger(__name__)

_SAFE_PART_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_part(value: str) -> str:
    """Make ``value`` safe for a single path segment (keeps dots/dashes)."""
    return _SAFE_PART_RE.sub("_", value)


SpecDocProgressFn = Callable[[str, dict], None]
"""Signature of the optional ``on_progress`` callback for :meth:`SpecDocService.parse_many`.

Fired once per success as ``("spec_done", {"spec_id": ..., "version": ...})``.
"""


def _render_blocks_markdown(blocks: list) -> str:
    """Render one file's blocks back to markdown text (debug cache)."""
    parts: list[str] = []
    for b in blocks:
        if isinstance(b, HeadingBlock):
            title = f"{b.section_no} {b.title}".strip() if b.section_no else b.title
            parts.append(f"{'#' * max(b.level, 1)} {title}".strip() or b.raw)
        elif isinstance(b, ParagraphBlock):
            parts.append(b.text)
        elif isinstance(b, TableBlock):
            parts.append(b.gfm)
        else:  # pragma: no cover - defensive; only the three block types exist
            parts.append(str(b))
    return "\n\n".join(p for p in parts if p).strip() + "\n"


class SpecDocService:
    """Fetch / parse / TOC orchestration over the spec-document corpus.

    Args:
        spec_repo: Main-DB spec repository (only ``list_versions`` is used).
        doc_repo: Specdata repository; defaults to
            :class:`SQLAlchemySpecDocRepository` (bound lazily so the
            ``sqlite_env`` fixture's env override applies).
        search_service: FTS5 hook with ``upsert_for_version`` (or ``None``).
        semantic_service: Vector hook with ``index_for_version`` (or a
            vector repo with ``upsert_for_version``; or ``None``).
        embedder: Remote embedder, used only for the vector-repo fallback.
        settings: Defaults to :func:`get_settings` (respects env + TOML).
        fetcher: ``url -> zip bytes``; defaults to :func:`fetch_spec_doc_zip`.
        cache_dir: Spec-document cache root holding ``zips`` + ``markdown``;
            defaults to ``settings.spec_doc.cache_dir``.
        search/semantic: Aliases for ``search_service``/``semantic_service``.
    """

    def __init__(
        self,
        *,
        spec_repo,
        doc_repo=None,
        search_service=None,
        semantic_service=None,
        embedder=None,
        settings=None,
        fetcher=None,
        cache_dir=None,
        search=None,
        semantic=None,
    ) -> None:
        from doc3gpp.storage.repositories.spec_doc_sql import SQLAlchemySpecDocRepository

        self._spec_repo = spec_repo
        self._settings = settings if settings is not None else get_settings()
        self._doc_repo = doc_repo if doc_repo is not None else SQLAlchemySpecDocRepository()
        self._search = search_service if search_service is not None else search
        self._semantic = semantic_service if semantic_service is not None else semantic
        self._embedder = embedder
        self._fetcher = fetcher if fetcher is not None else (lambda url: fetch_spec_doc_zip(url))
        root = cache_dir if cache_dir is not None else self._settings.spec_doc.cache_dir
        self._cache_dir = Path(root)

    # ------------------------------------------------------------------
    # Cache layout: {cache_dir}/zips/<spec_id>/<version>.zip and
    # {cache_dir}/markdown/<spec_id>/<version>/<file_order>-<stem>.md
    # ------------------------------------------------------------------

    def _zip_path(self, spec_id: str, version: str) -> Path:
        return (
            self._cache_dir
            / "zips"
            / _safe_part(spec_id)
            / f"{_safe_part(version)}.zip"
        )

    def _markdown_dir(self, spec_id: str, version: str) -> Path:
        return self._cache_dir / "markdown" / _safe_part(spec_id) / _safe_part(version)

    def _zip_cache_exists(self, spec_id: str, version: str) -> bool:
        return self._zip_path(spec_id, version).is_file()

    def _read_zip_cache(self, spec_id: str, version: str) -> bytes:
        return self._zip_path(spec_id, version).read_bytes()

    def _write_zip_cache(self, spec_id: str, version: str, raw: bytes) -> Path:
        path = self._zip_path(spec_id, version)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return path

    def _write_markdown_cache(self, spec_id: str, version: str, ordered: list) -> None:
        md_dir = self._markdown_dir(spec_id, version)
        md_dir.mkdir(parents=True, exist_ok=True)
        for f in ordered:
            stem = _safe_part(Path(f.source_file).stem)
            (md_dir / f"{f.file_order}-{stem}.md").write_text(
                _render_blocks_markdown(f.blocks), encoding="utf-8"
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _versions(self, spec_id: str):
        """Main-DB lookup; a miss points the operator at ``spec sync``."""
        rows = self._spec_repo.list_versions(spec_id, limit=500)
        if not rows:
            raise SpecDocUnknownSpecError(
                f"Unknown spec {spec_id!r}; run 'doc3gpp spec sync --spec-id {spec_id}' first"
            )
        return rows

    def _index_after_parse(self, spec_id: str, version: str) -> None:
        """Best-effort FTS5 upsert after a successful parse (never aborts)."""
        if not self._settings.spec_doc.auto_index_on_parse:
            return
        if self._search is None:
            return
        try:
            self._search.upsert_for_version(spec_id, version)
        except Exception as exc:  # noqa: BLE001 - best-effort hook must not abort a parse
            logger.warning("spec-doc auto-index failed for %s@%s: %s", spec_id, version, exc)

    def _embed_after_parse(self, spec_id: str, version: str, drafts: list[ChunkDraft]) -> None:
        """Best-effort vector upsert after a successful parse (never aborts)."""
        if not self._settings.spec_doc.auto_embed_on_parse:
            return
        if self._semantic is None:
            return
        try:
            index_fn = getattr(self._semantic, "index_for_version", None)
            if callable(index_fn):
                index_fn(spec_id, version)
                return
            upsert_fn = getattr(self._semantic, "upsert_for_version", None)
            if callable(upsert_fn) and self._embedder is not None:
                texts = [
                    "\n".join(part for part in (d.sections, d.tables, d.text) if part)
                    for d in drafts
                ]
                embeddings = self._embedder.encode(texts)
                upsert_fn(spec_id, version, [embeddings[i] for i in range(len(texts))])
        except Exception as exc:  # noqa: BLE001 - best-effort hook must not abort a parse
            logger.warning("spec-doc auto-embed failed for %s@%s: %s", spec_id, version, exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch(
        self,
        spec_id: str,
        *,
        release: str | None = None,
        version: str | None = None,
        force: bool = False,
    ) -> SpecDocSource:
        """Download the resolved version zip (or hit the zip cache) and record it."""
        rows = self._versions(spec_id)
        ver = resolve_spec_doc_version(rows, release, version)
        if force and self._doc_repo.get_source(spec_id, ver.version) is not None:
            self._doc_repo.invalidate_parse_state(spec_id, ver.version)
        # Immutable skip: a cached zip means fetch is done (no network).
        # A purged cache re-downloads — that is correct, not a skip violation.
        if not force and self._zip_cache_exists(spec_id, ver.version):
            existing = self._doc_repo.get_source(spec_id, ver.version)
            if existing is None:
                raw = self._read_zip_cache(spec_id, ver.version)
                self._doc_repo.record_download(
                    spec_id,
                    ver.version,
                    release=ver.release,
                    ftp_url=ver.ftp_url,
                    docx_count=len(list_spec_docx(raw)),
                )
                existing = self._doc_repo.get_source(spec_id, ver.version)
            return existing
        raw = self._fetcher(ver.ftp_url)  # network
        limit_kb = self._settings.spec_doc.max_zip_size_kb
        if limit_kb > 0 and len(raw) > limit_kb * 1024:
            raise SpecDocTooLargeError(
                f"zip for {spec_id}@{ver.version} exceeds max_zip_size_kb "
                f"({limit_kb} KB, got {len(raw)} bytes)"
            )
        self._write_zip_cache(spec_id, ver.version, raw)
        self._doc_repo.record_download(
            spec_id,
            ver.version,
            release=ver.release,
            ftp_url=ver.ftp_url,
            docx_count=len(list_spec_docx(raw)),
        )
        return self._doc_repo.get_source(spec_id, ver.version)

    def parse(
        self,
        spec_id: str,
        *,
        release: str | None = None,
        version: str | None = None,
        force: bool = False,
    ) -> SpecDocSource:
        """Fetch-if-missing, convert, chunk, and index one ``(spec_id, version)`` pair."""
        src = self.fetch(spec_id, release=release, version=version, force=force)
        if src.parsed_at is not None and not force:
            return src
        raw = self._read_zip_cache(spec_id, src.version)
        entries = list_spec_docx(raw)
        if not entries:
            raise SpecDocNoDocxError(f"zip for {spec_id}@{src.version} contains no .docx files")
        files = [
            SpecDocFileBlocks(
                file_order=i,
                source_file=name,
                blocks=convert_document_to_blocks(data, name),
            )
            for i, (name, data) in enumerate(entries)
        ]
        ordered = order_spec_files(files)
        toc_entries, toc_files = extract_spec_toc(ordered)
        self._doc_repo.upsert_toc(
            SpecDocToc(
                spec_id=spec_id,
                version=src.version,
                release=src.release,
                entries=toc_entries,
                files=toc_files,
                docx_count=len(entries),
            )
        )
        overlap = self._settings.spec_doc.chunk_overlap
        if overlap is None:
            overlap = self._settings.semantic_search.chunk_overlap
        drafts: list[ChunkDraft] = []
        for f in ordered:  # per-file runs keep file identity; chunker itself stays pure
            drafts.extend(
                chunk_blocks(
                    f.blocks,
                    self._settings.semantic_search.chunk_size,
                    overlap,
                    self._settings.spec_doc.max_chunk_chars,
                    file_order=f.file_order,
                    source_file=f.source_file,
                )
            )
        self._doc_repo.replace_chunks(spec_id, src.version, release=src.release, drafts=drafts)
        self._write_markdown_cache(spec_id, src.version, ordered)
        self._doc_repo.record_parsed(spec_id, src.version, chunk_count=len(drafts))
        self._index_after_parse(spec_id, src.version)
        self._embed_after_parse(spec_id, src.version, drafts)
        return self._doc_repo.get_source(spec_id, src.version)

    def parse_many(
        self,
        spec_ids: list[str],
        *,
        release: str | None = None,
        version: str | None = None,
        force: bool = False,
        on_progress: SpecDocProgressFn | None = None,
    ) -> SpecDocBatchResult:
        """Parse a batch; one spec never aborts the batch.

        Oversized zips land in ``skipped``; unknown versions and every
        other error land in ``failures``. An already-parsed pair lands
        in ``skipped`` unless ``force`` is set (identity skip).
        """
        out = SpecDocBatchResult()
        for sid in spec_ids:
            try:
                if not force:
                    rows = self._versions(sid)
                    ver = resolve_spec_doc_version(rows, release, version)
                    existing = self._doc_repo.get_source(sid, ver.version)
                    if existing is not None and existing.parsed_at is not None:
                        out.skipped[sid] = (
                            f"{sid}@{ver.version} already parsed; use --force to re-parse"
                        )
                        continue
                src = self.parse(sid, release=release, version=version, force=force)
            except SpecDocTooLargeError as exc:
                out.skipped[sid] = str(exc)
                continue
            except SpecDocUnknownVersionError as exc:
                # Unknown version is a failure, not a skip.
                out.failures[sid] = str(exc)
                continue
            except Exception as exc:  # noqa: BLE001 - one spec never aborts the batch
                out.failures[sid] = str(exc)
                continue
            out.successes[sid] = src
            if on_progress is not None:
                try:
                    on_progress("spec_done", {"spec_id": sid, "version": src.version})
                except Exception as exc:  # noqa: BLE001 - progress observers must not fail a parse
                    logger.warning("spec-doc on_progress failed for %s: %s", sid, exc)
        return out

    def get_source(self, spec_id: str, version: str) -> SpecDocSource | None:
        return self._doc_repo.get_source(spec_id, version)

    def count_chunks(
        self,
        spec_id: str,
        *,
        version: str,
        release: str | None = None,
        sections: str | None = None,
        tables: str | None = None,
    ) -> int:
        return self._doc_repo.count_chunks(
            spec_id,
            version=version,
            release=release,
            sections=sections,
            tables=tables,
        )

    def list_chunks(
        self,
        spec_id: str,
        *,
        version: str,
        release: str | None = None,
        sections: str | None = None,
        tables: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SpecDocChunk]:
        return self._doc_repo.list_chunks(
            spec_id,
            version=version,
            release=release,
            sections=sections,
            tables=tables,
            limit=limit,
            offset=offset,
        )

    def get_toc(
        self, spec_id: str, version: str, *, release: str | None = None
    ) -> SpecDocToc:
        """Return the stored TOC for ``(spec_id, version)`` (no network)."""
        _ = release  # TOC rows are keyed by (spec_id, version) only.
        toc = self._doc_repo.get_toc(spec_id, version)
        if toc is None:
            raise SpecDocUnknownVersionError(
                f"no parsed TOC for {spec_id}@{version}; "
                f"run 'doc3gpp spec doc parse --spec {spec_id} --version {version}'"
            )
        return toc

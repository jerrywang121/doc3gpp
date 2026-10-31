"""Orchestration for spec-doc fetch/parse/toc.

Immutable ledger: a parsed ``(spec_id, version)`` pair skips
unconditionally; only ``force=True`` re-downloads + replaces chunks.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from doc3gpp.models.spec_doc import (
    ChunkDraft,
    PreparedSpecDocInventoryFailure,
    PreparedSpecDocParse,
    SpecDocBatchResult,
    SpecDocChunk,
    SpecDocError,
    SpecDocNoDocxError,
    SpecDocPresentation,
    SpecDocSource,
    SpecDocToc,
    SpecDocTooLargeError,
    SpecDocUnknownSpecError,
    SpecDocUnknownVersionError,
)
from doc3gpp.models.spec_doc_zip_inventory import SpecDocZipInventory
from doc3gpp.parsers.docx_converter import (
    DocxMemberPool,
    HeadingBlock,
    InlineDocxPool,
    ParagraphBlock,
    TableBlock,
)
from doc3gpp.parsers.spec_doc import (
    SpecDocFileBlocks,
    extract_spec_toc,
    list_spec_docx,
    order_spec_files,
)
from doc3gpp.parsers.spec_doc_chunker import chunk_blocks
from doc3gpp.parsers.tdoc_zip import enumerate_zip_files
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
        docx_pool: Converts version .docx/.doc members; defaults to in-process
            :class:`InlineDocxPool`.
        search/semantic: Aliases for ``search_service``/``semantic_service``.
    """

    def __init__(
        self,
        *,
        spec_repo,
        doc_repo=None,
        inventory_repo=None,
        search_service=None,
        semantic_service=None,
        embedder=None,
        settings=None,
        fetcher=None,
        cache_dir=None,
        search=None,
        semantic=None,
        docx_pool: DocxMemberPool | None = None,
    ) -> None:
        from doc3gpp.storage.repositories.spec_doc_sql import SQLAlchemySpecDocRepository

        if inventory_repo is None:
            from doc3gpp.storage.repositories.spec_doc_zip_inventory_sql import (
                SQLAlchemySpecDocZipInventoryRepository,
            )

            inventory_repo = SQLAlchemySpecDocZipInventoryRepository()

        self._spec_repo = spec_repo
        self._settings = settings if settings is not None else get_settings()
        self._doc_repo = doc_repo if doc_repo is not None else SQLAlchemySpecDocRepository()
        self._inventory_repo = inventory_repo
        self._search = search_service if search_service is not None else search
        self._semantic = semantic_service if semantic_service is not None else semantic
        self._embedder = embedder
        self._docx_pool = docx_pool if docx_pool is not None else InlineDocxPool()
        self._fetcher = (
            fetcher if fetcher is not None else (lambda url: fetch_spec_doc_zip(url))
        )
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
        if force:
            rows = self._versions(spec_id)
            ver = resolve_spec_doc_version(rows, release, version)
            if self._doc_repo.get_source(spec_id, ver.version) is not None:
                # Preserve the synchronous API's retry semantics: a failed
                # forced download leaves the old parsed marker cleared. The
                # queued pipeline calls prepare_parse directly and never
                # performs this repository mutation during preparation.
                self._doc_repo.invalidate_parse_state(spec_id, ver.version)
        prepared = self.prepare_parse(
            spec_id, release=release, version=version, force=force
        )
        if isinstance(prepared, SpecDocSource):
            return prepared
        return self.persist_prepared_parse(prepared)

    def prepare_parse(
        self,
        spec_id: str,
        *,
        release: str | None = None,
        version: str | None = None,
        force: bool = False,
    ) -> PreparedSpecDocParse | PreparedSpecDocInventoryFailure | SpecDocSource:
        """Fetch and parse one document without mutating repositories."""
        rows = self._versions(spec_id)
        ver = resolve_spec_doc_version(rows, release, version)
        existing = self._doc_repo.get_source(spec_id, ver.version)
        if existing is not None and existing.parsed_at is not None and not force:
            return existing

        download_required = force or not self._zip_cache_exists(spec_id, ver.version)
        if download_required:
            raw = self._fetcher(ver.ftp_url)
            limit_kb = self._settings.spec_doc.max_zip_size_kb
            if limit_kb > 0 and len(raw) > limit_kb * 1024:
                raise SpecDocTooLargeError(
                    f"zip for {spec_id}@{ver.version} exceeds max_zip_size_kb "
                    f"({limit_kb} KB, got {len(raw)} bytes)"
                )
            self._write_zip_cache(spec_id, ver.version, raw)
        else:
            raw = self._read_zip_cache(spec_id, ver.version)

        inventory = SpecDocZipInventory(
            spec_id=spec_id,
            version=ver.version,
            release=ver.release,
            zip_files=enumerate_zip_files(raw),
        )
        try:
            entries = list_spec_docx(raw)
            if not entries:
                raise SpecDocNoDocxError(
                    f"zip for {spec_id}@{ver.version} contains no .docx, .doc, "
                    ".pptx, or .ppt files"
                )
            outcomes = self._docx_pool.convert_members("blocks", entries)
            files: list[SpecDocFileBlocks] = []
            presentation_markdowns: dict[str, str] = {}
            for i, ((name, _), outcome) in enumerate(zip(entries, outcomes, strict=True)):
                if isinstance(outcome, Exception):
                    raise outcome
                if name.lower().endswith((".pptx", ".ppt")):
                    presentation_markdowns[name] = (
                        outcome[0].text if outcome else ""
                    )
                files.append(
                    SpecDocFileBlocks(file_order=i, source_file=name, blocks=outcome)
                )
            ordered = order_spec_files(files)
            word_files = [
                file for file in ordered if file.source_file not in presentation_markdowns
            ]
            toc_entries, toc_files = extract_spec_toc(word_files)
            presentations = tuple(
                SpecDocPresentation(
                    spec_id=spec_id,
                    version=ver.version,
                    source_file=file.source_file,
                    file_order=file.file_order,
                    markdown=presentation_markdowns[file.source_file],
                )
                for file in ordered
                if file.source_file in presentation_markdowns
            )
            toc = SpecDocToc(
                spec_id=spec_id,
                version=ver.version,
                release=ver.release,
                entries=toc_entries,
                files=toc_files,
                docx_count=len(entries),
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
            source = replace(existing, docx_count=len(entries)) if existing else SpecDocSource(
                spec_id=spec_id,
                version=ver.version,
                release=ver.release,
                ftp_url=ver.ftp_url,
                docx_count=len(entries),
            )
            return PreparedSpecDocParse(
                source=source,
                toc=toc,
                drafts=tuple(drafts),
                ordered_files=tuple(ordered),
                inventory=inventory,
                presentations=presentations,
                download_required=download_required,
                invalidate_parse_state=force and existing is not None,
                record_source=existing is None or existing.docx_count != len(entries),
            )
        except Exception as exc:  # noqa: BLE001 - persist inventory before reporting parse failure
            return PreparedSpecDocInventoryFailure(inventory=inventory, error=exc)

    def persist_prepared_parse(
        self,
        prepared: PreparedSpecDocParse | PreparedSpecDocInventoryFailure,
    ) -> SpecDocSource:
        """Persist prepared output, or inventory before re-raising a parse error."""
        if isinstance(prepared, PreparedSpecDocInventoryFailure):
            try:
                self._inventory_repo.upsert(prepared.inventory)
            except Exception as inventory_error:  # noqa: BLE001 - preserve original failure
                logger.warning(
                    "Failed to persist spec-doc ZIP inventory for %s@%s: %s",
                    prepared.inventory.spec_id,
                    prepared.inventory.version,
                    inventory_error,
                )
            raise prepared.error

        source = prepared.source
        if prepared.invalidate_parse_state:
            self._doc_repo.invalidate_parse_state(source.spec_id, source.version)
        self._inventory_repo.upsert(prepared.inventory)
        if prepared.download_required or prepared.record_source:
            self._doc_repo.record_download(
                source.spec_id,
                source.version,
                release=source.release,
                ftp_url=source.ftp_url,
                docx_count=source.docx_count,
            )
        self._doc_repo.upsert_toc(prepared.toc)
        self._doc_repo.replace_version_content(
            source.spec_id,
            source.version,
            release=source.release,
            drafts=list(prepared.drafts),
            presentations=list(prepared.presentations),
        )
        self._write_markdown_cache(
            source.spec_id, source.version, list(prepared.ordered_files)
        )
        self._doc_repo.record_parsed(
            source.spec_id, source.version, chunk_count=len(prepared.drafts)
        )
        self._index_after_parse(source.spec_id, source.version)
        self._embed_after_parse(source.spec_id, source.version, list(prepared.drafts))
        return self._doc_repo.get_source(source.spec_id, source.version)

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

    def list_presentations(
        self, spec_id: str, version: str
    ) -> list[SpecDocPresentation]:
        """Return the stored full Markdown for each presentation in file order."""
        return self._doc_repo.list_presentations(spec_id, version)

    def get_presentation(
        self, spec_id: str, version: str, source_file: str
    ) -> SpecDocPresentation:
        """Return one presentation by its exact ZIP member path (no network)."""
        presentation = self._doc_repo.get_presentation(spec_id, version, source_file)
        if presentation is None:
            raise SpecDocError(
                f"presentation {source_file!r} not found for {spec_id}@{version}"
            )
        return presentation

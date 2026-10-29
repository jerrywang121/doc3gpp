from __future__ import annotations

import json
import re
import zipfile
from collections.abc import Sequence
from dataclasses import replace
from enum import Enum
from pathlib import Path
from typing import Any

from doc3gpp.models.tdoc_content import TDocContent, TDocContentDocument
from doc3gpp.models.tdoc_content_families import (
    TDocContentStore,
    is_complete_cr_extraction,
    is_complete_family_extraction,
    store_for_type,
)
from doc3gpp.models.tdoc_cr import TDocExtractMeta
from doc3gpp.models.tdoc_document import MarkdownArchive, TDocDocument
from doc3gpp.parsers.normalizers import normalize_ftp_path
from doc3gpp.parsers.tdoc_zip import document_id_for, read_markdown_archive
from doc3gpp.repository.protocols import (
    TDocContentRepository,
    TDocCrDetailRepository,
    TDocRepository,
    TDocZipInventoryRepository,
)

_CACHE_FILE_RE = re.compile(r"[A-Za-z0-9._-]{1,200}\Z")
_LINE_BREAK = chr(10)


class TDocContentNotFoundReason(str, Enum):
    """Typed cause for an unavailable TDoc content selector or cache."""

    CONTENT_UNAVAILABLE = "content_unavailable"
    TDOC_NOT_FOUND = "tdoc_not_found"
    URL_NOT_FOUND = "url_not_found"
    SOURCE_NOT_FOUND = "source_not_found"
    EXTRACTION_MISSING = "extraction_missing"
    LEGACY_METADATA_INVALID = "legacy_metadata_invalid"
    LEGACY_CACHE_INVALID = "legacy_cache_invalid"
    CACHE_UNAVAILABLE = "cache_unavailable"


class TDocContentError(ValueError):
    """Base error for unavailable or inconsistent TDoc Markdown content."""


class TDocContentNotFoundError(TDocContentError):
    """A TDoc, extraction, cache body, or requested source is unavailable."""

    def __init__(
        self,
        message: str,
        *,
        reason: TDocContentNotFoundReason = (
            TDocContentNotFoundReason.CONTENT_UNAVAILABLE
        ),
    ) -> None:
        super().__init__(message)
        self.reason = reason


class TDocContentIncompleteError(TDocContentError):
    """Stored version-2 metadata and document rows are inconsistent."""


class TDocContentIneligibleError(TDocContentError):
    """The TDoc's stored type has no raw Markdown content family."""

    def __init__(self, tdoc_id: str, observed_type: str | None) -> None:
        self.tdoc_id = tdoc_id
        self.observed_type = observed_type
        super().__init__(
            f"TDoc {tdoc_id!r} has type {observed_type!r}; raw Markdown is available "
            "for CR, WID, LS, and discussion TDocs"
        )


class TDocContentService:
    """Retrieve TDoc Markdown and ZIP inventory from SQL.

    Version-1 Markdown cache reads remain constrained to known sources.
    """

    def __init__(
        self,
        *,
        tdoc_repository: TDocRepository,
        cr_repository: TDocCrDetailRepository,
        content_repository: TDocContentRepository,
        inventory_repository: TDocZipInventoryRepository,
        cache_root: Path,
    ) -> None:
        self._tdoc_repository = tdoc_repository
        self._cr_repository = cr_repository
        self._content_repository = content_repository
        self._inventory_repository = inventory_repository
        self._cache_root = Path(cache_root)

    def get(
        self,
        *,
        tdoc_id: str | None = None,
        ftp_url: str | None = None,
        source_file: str | None = None,
    ) -> TDocContent:
        """Get all content or one exact source for a TDoc ID or ZIP URL."""
        if (tdoc_id is None) == (ftp_url is None):
            raise ValueError("exactly one of tdoc_id or ftp_url is required")

        resolved_tdoc_id, resolved_url, observed_type = self._resolve_parent(
            tdoc_id=tdoc_id,
            ftp_url=ftp_url,
        )
        store = store_for_type(observed_type)
        if store is None:
            raise TDocContentIneligibleError(resolved_tdoc_id, observed_type)

        inventory = self._inventory_repository.get_by_url(resolved_url)
        zip_files = inventory.zip_files if inventory is not None else None
        meta = self._content_repository.get_extract_meta(resolved_url)
        if meta is None:
            raise self._content_not_found(
                resolved_tdoc_id,
                resolved_url,
                "no extraction metadata is stored",
                category=TDocContentNotFoundReason.EXTRACTION_MISSING,
            )
        self._validate_meta_identity(meta, resolved_tdoc_id, resolved_url)

        if store.includes_main:
            content = self._family_content(store, meta, resolved_tdoc_id, resolved_url)
        elif meta.extraction_version == 1:
            content = self._legacy_cr_content(meta, resolved_tdoc_id, resolved_url)
        else:
            content = self._cr_content(
                store,
                meta,
                resolved_tdoc_id,
                resolved_url,
                source_file,
            )

        content = replace(content, zip_files=zip_files)
        if source_file is None:
            return content
        return self._select_source(content, source_file, resolved_tdoc_id, resolved_url)

    def _resolve_parent(
        self,
        *,
        tdoc_id: str | None,
        ftp_url: str | None,
    ) -> tuple[str, str, str | None]:
        if tdoc_id is not None:
            normalized_id = tdoc_id.strip()
            if not normalized_id:
                raise ValueError("tdoc_id must be a non-empty string")
            parent = self._tdoc_repository.get_by_id(normalized_id)
            if parent is None:
                raise self._content_not_found(
                    normalized_id,
                    "",
                    "the TDoc is not stored",
                    category=TDocContentNotFoundReason.TDOC_NOT_FOUND,
                )
            resolved_url = normalize_ftp_path(parent.ftp_url or "")
            if not resolved_url:
                raise self._content_not_found(
                    parent.tdoc_id,
                    "",
                    "the TDoc has no stored ftp_url",
                    category=TDocContentNotFoundReason.TDOC_NOT_FOUND,
                )
            return parent.tdoc_id, resolved_url, parent.type

        normalized_url = normalize_ftp_path(ftp_url or "")
        if not normalized_url:
            raise ValueError("ftp_url must be a non-empty ZIP URL")
        parent = self._tdoc_repository.get_by_ftp_url(normalized_url)
        if parent is not None:
            return parent.tdoc_id, normalized_url, parent.type

        # A TDoc's canonical URL can change over time while extraction rows stay
        # keyed by their immutable source URL. Resolve historical URLs through
        # that persisted metadata instead of treating them as an unknown TDoc.
        meta = self._cr_repository.get_extract_meta_by_url(normalized_url)
        if meta is not None:
            owner = self._tdoc_repository.get_by_id(meta.tdoc_id)
            return meta.tdoc_id, normalized_url, None if owner is None else owner.type

        raise self._content_not_found(
            "",
            normalized_url,
            "the URL does not resolve to a stored TDoc or extraction",
            category=TDocContentNotFoundReason.URL_NOT_FOUND,
        )

    @staticmethod
    def _validate_meta_identity(
        meta: TDocExtractMeta,
        tdoc_id: str,
        ftp_url: str,
    ) -> None:
        if (
            meta.tdoc_id != tdoc_id
            or normalize_ftp_path(meta.ftp_url) != ftp_url
        ):
            raise TDocContentIncompleteError(
                f"TDoc {tdoc_id!r} extraction metadata does not match URL {ftp_url!r}; "
                f"re-parse with 'doc3gpp tdoc parse --tdoc {tdoc_id} --force'"
            )

    def _family_content(
        self,
        store: TDocContentStore,
        meta: TDocExtractMeta,
        tdoc_id: str,
        ftp_url: str,
    ) -> TDocContent:
        """Family content comes only from SQL rows, main first; the cache is never read."""
        documents = self._stored_documents(store, meta, tdoc_id, ftp_url)
        return TDocContent(
            tdoc_id=tdoc_id,
            ftp_url=ftp_url,
            extraction_version=meta.extraction_version,
            complete=is_complete_family_extraction(meta, documents),
            documents=tuple(
                self._content_document(
                    document,
                    is_main=document.source_file == meta.doc_filename,
                )
                for document in documents
            ),
            skipped_sources=meta.skipped_sources,
        )

    def _cr_content(
        self,
        store: TDocContentStore,
        meta: TDocExtractMeta,
        tdoc_id: str,
        ftp_url: str,
        source_file: str | None,
    ) -> TDocContent:
        """CR content is the archive main followed by SQL annexes, or fails closed."""
        if meta.extraction_version != 2:
            raise self._incomplete(
                tdoc_id,
                f"unsupported extraction version {meta.extraction_version}",
            )
        annexes = self._stored_documents(store, meta, tdoc_id, ftp_url)
        annex_documents = tuple(
            self._content_document(annex, is_main=False) for annex in annexes
        )
        if source_file is not None and source_file != meta.doc_filename:
            # Annexes are SQL-authoritative, so a single annex needs no archive.
            documents = annex_documents
        else:
            documents = (
                self._cr_main_document(meta, tdoc_id, ftp_url),
                *annex_documents,
            )
        has_cover = self._cr_repository.get_by_url(ftp_url) is not None
        return TDocContent(
            tdoc_id=tdoc_id,
            ftp_url=ftp_url,
            extraction_version=2,
            complete=is_complete_cr_extraction(meta, annexes, has_cover=has_cover),
            documents=documents,
            skipped_sources=meta.skipped_sources,
        )

    def _legacy_cr_content(
        self,
        meta: TDocExtractMeta,
        tdoc_id: str,
        ftp_url: str,
    ) -> TDocContent:
        return TDocContent(
            tdoc_id=tdoc_id,
            ftp_url=ftp_url,
            extraction_version=1,
            complete=False,
            documents=(self._legacy_document(meta, tdoc_id, ftp_url),),
            skipped_sources=meta.skipped_sources,
        )

    def _stored_documents(
        self,
        store: TDocContentStore,
        meta: TDocExtractMeta,
        tdoc_id: str,
        ftp_url: str,
    ) -> tuple[TDocDocument, ...]:
        try:
            documents = tuple(self._content_repository.list_documents(store, ftp_url))
        except (TypeError, ValueError) as exc:
            raise self._incomplete(
                tdoc_id,
                f"stored SQL document rows failed validation: {exc}",
            ) from exc
        if meta.document_count != len(documents):
            raise self._incomplete(
                tdoc_id,
                "stored SQL document rows do not match the declared document_count",
            )
        return documents

    def _cr_main_document(
        self,
        meta: TDocExtractMeta,
        tdoc_id: str,
        ftp_url: str,
    ) -> TDocContentDocument:
        archive = self._read_cache_archive(meta, tdoc_id, ftp_url)
        if (
            archive.is_legacy
            or normalize_ftp_path(archive.ftp_url or "") != ftp_url
            or archive.main_source_file != meta.doc_filename
        ):
            raise self._cache_not_found(tdoc_id, ftp_url, meta)
        main = next(
            (
                document
                for document in archive.documents
                if document.source_file == meta.doc_filename
            ),
            None,
        )
        if main is None:
            raise self._cache_not_found(tdoc_id, ftp_url, meta)
        return TDocContentDocument(
            document_id=main.document_id,
            source_file=main.source_file,
            is_main=True,
            markdown=main.markdown_text,
        )

    def _read_cache_archive(
        self,
        meta: TDocExtractMeta,
        tdoc_id: str,
        ftp_url: str,
    ) -> MarkdownArchive:
        if not _CACHE_FILE_RE.fullmatch(meta.cache_file):
            raise self._cache_not_found(tdoc_id, ftp_url, meta)

        # The cache key is a validated basename. ZIP member names are never
        # joined to this local cache path.
        cache_path = self._cache_root / "markdown" / meta.cache_file
        try:
            raw = cache_path.read_bytes()
        except OSError as exc:
            raise self._cache_not_found(tdoc_id, ftp_url, meta) from exc
        if not raw:
            raise self._cache_not_found(tdoc_id, ftp_url, meta)
        try:
            return read_markdown_archive(raw)
        except (OSError, EOFError, UnicodeDecodeError, ValueError, zipfile.BadZipFile) as exc:
            raise self._cache_not_found(tdoc_id, ftp_url, meta) from exc

    @staticmethod
    def _content_document(
        document: TDocDocument,
        *,
        is_main: bool,
    ) -> TDocContentDocument:
        return TDocContentDocument(
            document_id=document.document_id,
            source_file=document.source_file,
            is_main=is_main,
            markdown=document.markdown_text,
        )

    def _select_source(
        self,
        content: TDocContent,
        source_file: str,
        tdoc_id: str,
        ftp_url: str,
    ) -> TDocContent:
        selected = tuple(
            document
            for document in content.documents
            if document.source_file == source_file
        )
        if not selected:
            raise self._content_not_found(
                tdoc_id,
                ftp_url,
                f"source {source_file!r} is not present in this ZIP",
                category=TDocContentNotFoundReason.SOURCE_NOT_FOUND,
            )
        return replace(content, documents=selected)

    def _legacy_document(
        self,
        meta: TDocExtractMeta,
        tdoc_id: str,
        ftp_url: str,
    ) -> TDocContentDocument:
        source_file = meta.doc_filename
        if not source_file or not source_file.strip():
            raise self._content_not_found(
                tdoc_id,
                ftp_url,
                "legacy extraction metadata does not identify a source file",
                category=TDocContentNotFoundReason.LEGACY_METADATA_INVALID,
            )
        if not _CACHE_FILE_RE.fullmatch(meta.cache_file):
            raise self._content_not_found(
                tdoc_id,
                ftp_url,
                "legacy extraction metadata has an invalid cache key",
                category=TDocContentNotFoundReason.LEGACY_METADATA_INVALID,
            )

        archive = self._read_cache_archive(meta, tdoc_id, ftp_url)
        if archive.is_legacy:
            markdown = archive.legacy_markdown or ""
            try:
                document_id = document_id_for(ftp_url, source_file)
            except ValueError as exc:
                raise self._content_not_found(
                    tdoc_id,
                    ftp_url,
                    "legacy extraction metadata has an invalid source identity",
                    category=TDocContentNotFoundReason.LEGACY_METADATA_INVALID,
                ) from exc
        else:
            if normalize_ftp_path(archive.ftp_url or "") != ftp_url:
                raise self._content_not_found(
                    tdoc_id,
                    ftp_url,
                    "cached archive manifest URL does not match the requested ZIP URL",
                    category=TDocContentNotFoundReason.LEGACY_CACHE_INVALID,
                )
            if archive.main_source_file != source_file:
                raise self._content_not_found(
                    tdoc_id,
                    ftp_url,
                    "the cached archive does not contain the known legacy main source",
                    category=TDocContentNotFoundReason.LEGACY_CACHE_INVALID,
                )
            main_document = next(
                (
                    document
                    for document in archive.documents
                    if document.source_file == source_file
                ),
                None,
            )
            if main_document is None:
                raise self._content_not_found(
                    tdoc_id,
                    ftp_url,
                    "the cached archive does not contain the known legacy main source",
                    category=TDocContentNotFoundReason.LEGACY_CACHE_INVALID,
                )
            markdown = main_document.markdown_text
            document_id = main_document.document_id

        if not markdown:
            raise self._cache_not_found(tdoc_id, ftp_url, meta)
        return TDocContentDocument(
            document_id=document_id,
            source_file=source_file,
            is_main=True,
            markdown=markdown,
        )

    @staticmethod
    def _cache_not_found(
        tdoc_id: str,
        ftp_url: str,
        meta: TDocExtractMeta,
    ) -> TDocContentNotFoundError:
        return TDocContentNotFoundError(
            f"Markdown cache for TDoc {tdoc_id!r} is empty or unreadable "
            f"(cache_file: {meta.cache_file}, cache_dir: the configured cache). "
            f"Run 'doc3gpp tdoc parse --tdoc {tdoc_id} --force' to rebuild it, or "
            f"'doc3gpp tdoc parse --from-url {ftp_url}' first.",
            reason=TDocContentNotFoundReason.CACHE_UNAVAILABLE,
        )

    @staticmethod
    def _content_not_found(
        tdoc_id: str,
        ftp_url: str,
        reason: str,
        *,
        category: TDocContentNotFoundReason = (
            TDocContentNotFoundReason.CONTENT_UNAVAILABLE
        ),
    ) -> TDocContentNotFoundError:
        id_hint = (
            f"Run 'doc3gpp tdoc parse --tdoc {tdoc_id}'"
            if tdoc_id
            else "Run 'doc3gpp tdoc parse --from-url <url>'"
        )
        url_hint = (
            f" or 'doc3gpp tdoc parse --from-url {ftp_url}'"
            if ftp_url
            else ""
        )
        return TDocContentNotFoundError(
            f"No Markdown content for TDoc {tdoc_id!r} at {ftp_url!r}: {reason}. "
            f"{id_hint}{url_hint} first.",
            reason=category,
        )

    @staticmethod
    def _incomplete(tdoc_id: str, reason: str) -> TDocContentIncompleteError:
        return TDocContentIncompleteError(
            f"TDoc {tdoc_id!r} has an incomplete or inconsistent extraction: {reason}; "
            f"re-parse with 'doc3gpp tdoc parse --tdoc {tdoc_id} --force' to repair it"
        )


def tdoc_content_to_dict(content: TDocContent) -> dict[str, Any]:
    """Serialize content with the shared HTTP, MCP, and CLI JSON envelope."""
    return {
        "tdoc_id": content.tdoc_id,
        "ftp_url": content.ftp_url,
        "zip_files": (
            list(content.zip_files) if content.zip_files is not None else None
        ),
        "extraction_version": content.extraction_version,
        "complete": content.complete,
        "skipped_sources": list(content.skipped_sources),
        "documents": [
            {
                "document_id": document.document_id,
                "source_file": document.source_file,
                "is_main": document.is_main,
                "markdown": document.markdown,
            }
            for document in content.documents
        ],
    }


def render_tdoc_documents_markdown(
    documents: Sequence[TDocContentDocument],
    *,
    include_source_headers: bool,
) -> str:
    """Render one body unchanged or multiple main-first Markdown blocks."""
    main_first = tuple(
        document for document in documents if document.is_main
    ) + tuple(document for document in documents if not document.is_main)
    if not main_first:
        return ""
    if len(main_first) == 1:
        return main_first[0].markdown
    if not include_source_headers:
        return _LINE_BREAK.join(document.markdown for document in main_first)

    blocks: list[str] = []
    for document in main_first:
        source_json = json.dumps(document.source_file, ensure_ascii=False)
        source_json = source_json.replace(">", chr(92) + "u003e")
        blocks.append(
            f"<!-- doc3gpp-source: {source_json} -->{_LINE_BREAK}{document.markdown}"
        )
    return _LINE_BREAK.join(blocks)

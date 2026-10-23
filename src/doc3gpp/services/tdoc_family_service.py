"""Family extraction for WID, LS, and discussion TDocs into per-type content tables."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from doc3gpp.models.tdoc import TDoc
from doc3gpp.models.tdoc_content_families import (
    TDocContentStore,
    is_complete_family_extraction,
)
from doc3gpp.models.tdoc_cr import TDocExtractMeta
from doc3gpp.models.tdoc_document import TDocDocument
from doc3gpp.parsers.normalizers import build_ftp_url, normalize_ftp_path
from doc3gpp.parsers.tdoc_zip import build_markdown_archive
from doc3gpp.parsers.tdoc_zip_convert import TDocZipConversion, convert_tdoc_zip
from doc3gpp.scraping.cache_keys import derive_cache_file
from doc3gpp.scraping.tdoc_zip_source import download_tdoc_zip
from doc3gpp.services.tdoc_errors import TDocNotYetOnFTPError

if TYPE_CHECKING:
    from doc3gpp.repository.protocols import TDocContentRepository
    from doc3gpp.scraping.client import ScraperClient
    from doc3gpp.scraping.tdoc_zip_source import TDocCacheLike


@dataclass(frozen=True, slots=True)
class PreparedFamilyExtract:
    """Converted family documents waiting for one atomic SQL replacement."""

    tdoc_id: str
    store: TDocContentStore
    extract_meta: TDocExtractMeta
    documents: tuple[TDocDocument, ...]
    skipped_sources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FamilyExtractResult:
    """Family documents for one TDoc, served from SQL or freshly persisted."""

    tdoc_id: str
    store: TDocContentStore
    extract_meta: TDocExtractMeta | None
    documents: tuple[TDocDocument, ...]
    skipped_sources: tuple[str, ...]
    from_cache: bool
    persisted: bool = True

    @property
    def complete(self) -> bool:
        if self.extract_meta is None:
            return False
        return is_complete_family_extraction(self.extract_meta, self.documents)


class TDocFamilyService:
    """Extract a family TDoc's main document and annexes without touching CR state."""

    def __init__(
        self,
        *,
        scraper: ScraperClient,
        cache: TDocCacheLike,
        content_repo: TDocContentRepository,
        max_tdoc_size_bytes: int,
        max_documents_per_zip: int,
        max_uncompressed_size_bytes: int,
    ) -> None:
        self._scraper = scraper
        self._cache = cache
        self._content_repo = content_repo
        self._max_tdoc_size_bytes = max_tdoc_size_bytes
        self._max_documents_per_zip = max_documents_per_zip
        self._max_uncompressed_size_bytes = max_uncompressed_size_bytes

    def cached_extract(
        self,
        tdoc_id: str,
        store: TDocContentStore,
        ftp_url: str,
    ) -> FamilyExtractResult | None:
        """Return the complete stored bundle for ``ftp_url``, or ``None``."""
        canonical = normalize_ftp_path(build_ftp_url(ftp_url))
        cached_meta = self._content_repo.get_extract_meta(canonical)
        cached_documents = self._content_repo.list_documents(store, canonical)
        if cached_meta is None or not is_complete_family_extraction(
            cached_meta, cached_documents
        ):
            return None
        return FamilyExtractResult(
            tdoc_id=tdoc_id,
            store=store,
            extract_meta=cached_meta,
            documents=cached_documents,
            skipped_sources=cached_meta.skipped_sources,
            from_cache=True,
        )

    def prepare_extract(
        self,
        tdoc: TDoc,
        store: TDocContentStore,
        *,
        force: bool = False,
    ) -> PreparedFamilyExtract | FamilyExtractResult:
        """Convert a family TDoc's ZIP without SQL writes; a complete stored bundle is a cache hit."""
        if not store.includes_main:
            raise ValueError(f"{store.key} is not a family store")
        if not tdoc.ftp_url:
            raise TDocNotYetOnFTPError(tdoc.tdoc_id)
        canonical = normalize_ftp_path(build_ftp_url(tdoc.ftp_url))
        cache_file = derive_cache_file(tdoc.ftp_url)

        if not force:
            cached = self.cached_extract(tdoc.tdoc_id, store, tdoc.ftp_url)
            if cached is not None:
                return cached

        downloaded = download_tdoc_zip(
            tdoc.tdoc_id,
            self._scraper,
            self._cache,
            primary_url=build_ftp_url(tdoc.ftp_url),
            ftp_url=tdoc.ftp_url,
            max_bytes=self._max_tdoc_size_bytes,
        )
        conversion = convert_tdoc_zip(
            downloaded.path.read_bytes(),
            tdoc_id=tdoc.tdoc_id,
            identity=canonical,
            max_documents=self._max_documents_per_zip,
            max_uncompressed_bytes=self._max_uncompressed_size_bytes,
        )
        return self.prepare_conversion(
            tdoc_id=tdoc.tdoc_id,
            store=store,
            ftp_url=tdoc.ftp_url,
            cache_file=cache_file,
            conversion=conversion,
        )

    def prepare_conversion(
        self,
        *,
        tdoc_id: str,
        store: TDocContentStore,
        ftp_url: str,
        cache_file: str,
        conversion: TDocZipConversion,
    ) -> PreparedFamilyExtract:
        """Build the parent row and write the archive for an already-converted bundle."""
        if not store.includes_main:
            raise ValueError(f"{store.key} is not a family store")
        canonical = normalize_ftp_path(build_ftp_url(ftp_url))
        meta = TDocExtractMeta(
            ftp_url=canonical,
            tdoc_id=tdoc_id,
            cache_file=cache_file,
            doc_filename=conversion.main_source_file,
            extraction_version=2,
            document_count=len(conversion.documents),
            skipped_sources=conversion.skipped_sources,
        )
        self._cache.put_bytes(
            cache_file,
            build_markdown_archive(
                conversion.documents,
                ftp_url=canonical,
                main_source_file=conversion.main_source_file,
            ),
            "markdown",
        )
        return PreparedFamilyExtract(
            tdoc_id=tdoc_id,
            store=store,
            extract_meta=meta,
            documents=conversion.documents,
            skipped_sources=conversion.skipped_sources,
        )

    def persist_prepared_extract(
        self,
        prepared: PreparedFamilyExtract,
    ) -> FamilyExtractResult:
        """Replace the URL's content rows in one transaction."""
        self._content_repo.replace_family_bundle(
            prepared.store,
            prepared.extract_meta,
            prepared.documents,
        )
        return FamilyExtractResult(
            tdoc_id=prepared.tdoc_id,
            store=prepared.store,
            extract_meta=prepared.extract_meta,
            documents=prepared.documents,
            skipped_sources=prepared.skipped_sources,
            from_cache=False,
        )

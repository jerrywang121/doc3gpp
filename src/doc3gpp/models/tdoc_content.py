from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TDocContentDocument:
    """Markdown content and identity for one source document in a TDoc ZIP."""

    document_id: str
    source_file: str
    is_main: bool
    markdown: str


@dataclass(frozen=True, slots=True)
class TDocContent:
    """Ordered Markdown documents retrieved for one TDoc ZIP extraction."""

    tdoc_id: str
    ftp_url: str
    extraction_version: int
    complete: bool
    documents: tuple[TDocContentDocument, ...]
    skipped_sources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        documents = tuple(self.documents)
        if documents != self.documents:
            object.__setattr__(self, "documents", documents)
        skipped_sources = tuple(self.skipped_sources)
        if skipped_sources != self.skipped_sources:
            object.__setattr__(self, "skipped_sources", skipped_sources)

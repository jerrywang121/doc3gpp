"""DTOs for the spec-document corpus (separate specdata sqlite file)."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime


class SpecDocError(Exception):
    pass


class SpecDocUnknownSpecError(SpecDocError):
    pass


class SpecDocUnknownVersionError(SpecDocError):
    def __init__(self, msg: str, available: list[str] | None = None):
        super().__init__(msg)
        self.available = available or []


class SpecDocNoDocxError(SpecDocError):
    pass


class SpecDocTooLargeError(SpecDocError):
    pass


@dataclass(slots=True)
class SpecDocSource:
    spec_id: str
    version: str
    release: str | None = None
    ftp_url: str = ""
    downloaded_at: datetime | None = None
    parsed_at: datetime | None = None
    chunk_count: int = 0
    docx_count: int = 0


@dataclass(slots=True, frozen=True)
class SpecDocTocEntry:
    level: int
    section_no: str | None
    title: str
    source_file: str
    file_order: int


@dataclass(slots=True, frozen=True)
class SpecDocTocFile:
    source_file: str
    file_order: int
    first_section: str | None = None


@dataclass(slots=True)
class SpecDocToc:
    spec_id: str
    version: str
    release: str | None = None
    entries: list[SpecDocTocEntry] = field(default_factory=list)
    files: list[SpecDocTocFile] = field(default_factory=list)
    docx_count: int = 0
    created_at: datetime | None = None


@dataclass(slots=True)
class ChunkDraft:
    file_order: int
    source_file: str
    section_no: str | None = None
    section_title: str | None = None
    table_no: str | None = None
    table_title: str | None = None
    text: str = ""


@dataclass(slots=True)
class SpecDocChunk(ChunkDraft):
    chunk_id: str = ""
    spec_id: str = ""
    version: str = ""
    release: str | None = None
    chunk_index: int = 0


@dataclass(slots=True)
class SpecDocSearchFilters:
    spec_id: str | None = None
    release: str | None = None
    version: str | None = None
    section: str | None = None
    limit: int = 20
    offset: int = 0


@dataclass(slots=True, frozen=True)
class SpecDocHit:
    chunk_id: str
    spec_id: str
    version: str
    release: str | None
    section_no: str | None
    section_title: str | None
    table_no: str | None
    table_title: str | None
    chunk_index: int
    text: str
    score: float
    previews: dict[str, str]


@dataclass(slots=True)
class SpecDocBatchResult:
    successes: dict[str, SpecDocSource] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class SpecDocSemanticHit:
    chunk_id: str
    rrf_score: float
    hit: SpecDocHit | None
    rank_fts5: int | None = None
    rank_vec: int | None = None
    min_chunk_distance: float | None = None

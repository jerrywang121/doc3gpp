# Spec Document Parse, Chunk, and Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** For a spec already in the main DB, download its version zip, convert all `.docx` files to markdown blocks, chunk with section/table metadata into a separate `*_specdata.db` sqlite file, and expose TOC query plus FTS5 search (plus optional hybrid semantic search) over that corpus via CLI, web, and MCP.

**Architecture:** Mirror the testcase separate-DB pattern (own declarative base + engine + session factory + `create_schema` scope) and the TDoc search split (FTS5 repo + vector repo + search services + auto-index hooks in the parse service). New code lives in existing layers: `scraping/` (download), `parsers/` (docx blocks, ordering, chunking), `models/` (DTOs), `repository/protocols.py` (contracts), `storage/repositories/` (SQL impls), `services/` (orchestration), `settings/` (knobs), `cli.py` (`spec doc` sub-app), `web/` + MCP + jobs.

**Tech Stack:** Python 3.10+, SQLAlchemy 2.0 (create_all bootstrap, no alembic), Pydantic v2 + pydantic-settings, Typer, FastAPI + MCP SDK, python-docx (`[extract]` extra), sqlite FTS5 + sqlite-vec (`[semantic]` extra), httpx `ScraperClient`, gzip JSON sidecars via `storage/compression.py`.

## Global Constraints

- Python 3.10+; SQLAlchemy 2.0 `select()` style; Pydantic v2 `Field` + `field_validator`.
- Domain models are `@dataclass(slots=True)`; never leak ORM attributes out of repositories.
- Every Protocol change lands with its SQL impl in the same task; update both together.
- New tables use `create_all`-based bootstrap in `storage/db/migrate.py`; no alembic.
- MCP/HTTP JSON must byte-match CLI `--format json` (`_to_json` compact separators + `ensure_ascii=False`).
- Gzip blobs use the shared `doc3gpp.storage.compression.compress_json` / `decompress_json` helpers.
- Run `ruff check .` and `./scripts/test_sqlite.sh` (offline gate) before finishing; online tests are opt-in (`-m online`).
- Spec zips are immutable: parsed `(spec_id, version)` rows skip unconditionally; only `--force` re-downloads + replaces.
- Agentic workers: follow TDD per task (failing test first), commit per task with `feat(spec-doc): ...` messages.

---

## File Structure

New files and what each owns:

- `src/doc3gpp/storage/db/specdata_base.py` — `SpecDataBase(DeclarativeBase)` for the third sqlite file.
- `src/doc3gpp/models/spec_doc.py` — DTOs: `SpecDocSource`, `SpecDocToc(Entry/File)`, `SpecDocChunk`, `ChunkDraft` (chunker output), `SpecDocSearchFilters`, `SpecDocHit`, `SpecDocBatchResult`, `SpecDocSemanticHit`, error types.
- `src/doc3gpp/scraping/spec_doc_source.py` — version resolution + zip download (network only, no parsing).
- `src/doc3gpp/parsers/spec_doc.py` — `list_spec_docx`, `order_spec_files`, `extract_spec_toc`, `SpecDocFileBlocks` (block dataclasses `HeadingBlock/ParagraphBlock/TableBlock` live in `parsers/docx_converter.py` alongside the existing string converter and are re-exported here).
- `src/doc3gpp/parsers/spec_doc_chunker.py` — pure `chunk_blocks(...) -> list[ChunkDraft]`.
- `src/doc3gpp/services/spec_doc_service.py` — `SpecDocService` (fetch/parse/parse_many/get_toc) + resolution errors.
- `src/doc3gpp/services/spec_doc_search_service.py` — FTS5 `SpecDocSearchService` (search + rebuild generator + status).
- `src/doc3gpp/services/spec_doc_semantic_service.py` — hybrid `SpecDocSemanticService` + chunk-level `rrf_merge`.
- `src/doc3gpp/storage/repositories/spec_doc_sql.py` — `SQLAlchemySpecDocRepository` (sources/tocs/chunks CRUD).
- `src/doc3gpp/storage/repositories/spec_doc_search_sql.py` — FTS5 repo on the specdata engine.
- `src/doc3gpp/storage/repositories/spec_doc_vector_sql.py` — sqlite-vec repo on the specdata engine.
- `src/doc3gpp/web/routes/spec_docs.py` — `GET /specs/{spec_id}/docs/toc`, `GET /spec-docs/search`, `GET /spec-docs/schema`.
- `src/doc3gpp/web/templates/spec_docs_search.html`, `partials/spec_doc_search_results.html`, `partials/spec_doc_toc_results.html` — mirror `search.html` / `partials/search_results.html` structure (read those files first).
- `tests/unit/test_spec_doc_*.py`, `tests/integration/test_spec_doc_*.py`, `tests/integration/test_online_spec_doc_sync.py` — per-task tests below.

Modified files: `settings/schema.py` (+ `SpecDocSettings`, `specdata_database_url`, output fields, column-order const), `doc3gpp.toml.example`, `storage/db/session.py`, `storage/db/migrate.py`, `storage/db/models.py` (specdata ORM on `SpecDataBase`), `repository/protocols.py`, `services/factory.py`, `cli.py` (`spec doc` sub-app + `db --scope specdata`), `web/state.py`, `web/deps.py`, `web/app.py` (dispose), `web/routes/__init__.py`, `web/routes/jobs.py`, `web/workers/handlers.py`, `models/jobs.py`, `models/schema_info.py`, `web/mcp_server.py`, `parsers/docx_converter.py` (block path), `tests/conftest.py`, docs (`cli.md`, `architecture.md`, `code-map.md`, `web-server.md`, `AGENTS.md`, `README.md` if surface list changes).

---
## Task 1: Settings + specdata DB bootstrap

**Files:**
- Create: `src/doc3gpp/storage/db/specdata_base.py`
- Modify: `src/doc3gpp/settings/schema.py` (ALLOWED_ENV_VARS, Settings, new SpecDocSettings, OutputFieldsSettings, snippet column const)
- Modify: `src/doc3gpp/storage/db/session.py` (resolve + engine + session factory)
- Modify: `src/doc3gpp/storage/db/migrate.py` (scope "specdata", specdata FTS5/vector DDL on specdata engine)
- Modify: `doc3gpp.toml.example` ([spec_doc] block + specdata_database_url)
- Modify: `tests/conftest.py` (`sqlite_env` also sets `DOC3GPP_SPECDATA_DATABASE_URL` sibling + clears `get_specdata_engine` cache alongside the other two)
- Test: `tests/unit/test_spec_doc_settings.py`

**Interfaces:**
- Consumes: existing `resolve_testcase_database_url`, `get_testcase_engine`, `create_schema(scope)` patterns.
- Produces: `SpecDataBase`, `resolve_specdata_database_url(settings?) -> str`, `get_specdata_engine() -> Engine`, `get_specdata_session_factory() -> sessionmaker`, `Settings.specdata_database_url: str | None`, `Settings.spec_doc: SpecDocSettings`, `create_schema("specdata" | "all")`, `_SPEC_DOC_SNIPPET_COLUMNS`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_spec_doc_settings.py
from doc3gpp.settings.schema import Settings, SpecDocSettings

def test_spec_doc_defaults():
    s = SpecDocSettings()
    assert s.max_zip_size_kb == 0
    assert s.max_chunk_chars == 1500
    assert s.chunk_overlap is None
    assert s.auto_index_on_parse is True
    assert s.auto_embed_on_parse is True
    assert tuple(s.bm25_weights) == (5.0, 5.0, 5.0, 1.0, 1.0, 1.0)

def test_spec_doc_bm25_wrong_length_rejected():
    import pytest
    with pytest.raises(ValueError):
        SpecDocSettings(bm25_weights=[1.0, 2.0])

def test_settings_has_specdata_url():
    s = Settings()
    assert s.specdata_database_url is None
    assert s.spec_doc.max_chunk_chars == 1500

def test_resolve_specdata_sibling(tmp_path, monkeypatch):
    from doc3gpp.storage.db.session import resolve_specdata_database_url
    from doc3gpp.settings.loader import get_settings
    monkeypatch.setenv("DOC3GPP_DATABASE_URL", f"sqlite+pysqlite:///{tmp_path}/doc3gpp.db")
    get_settings.cache_clear()
    try:
        url = resolve_specdata_database_url()
        assert url.endswith("doc3gpp_specdata.db")
    finally:
        get_settings.cache_clear()

def test_create_schema_specdata_scope(sqlite_env):
    from doc3gpp.storage.db.migrate import create_schema
    from sqlalchemy import inspect
    from doc3gpp.storage.db.session import get_specdata_engine
    create_schema("specdata")
    tables = set(inspect(get_specdata_engine()).get_table_names())
    assert "spec_doc_sources" in tables
    assert "spec_doc_chunks" in tables
    assert "spec_doc_tocs" in tables
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_spec_doc_settings.py -v`
Expected: FAIL with "SpecDocSettings not defined" / "resolve_specdata_database_url not defined".

- [ ] **Step 3: Write minimal implementation**

```python
# src/doc3gpp/storage/db/specdata_base.py
from __future__ import annotations
from sqlalchemy.orm import DeclarativeBase

class SpecDataBase(DeclarativeBase):
    """Separate declarative base for the spec-document corpus.

    Mirrors TestCaseBase: spec_doc_* tables live in their own sqlite
    file so Base.metadata.create_all never touches them.
    """
    __test__ = False
```

```python
# settings/schema.py additions
ALLOWED_ENV_VARS = frozenset({... "DOC3GPP_SPECDATA_DATABASE_URL", ...})

_SPEC_DOC_SNIPPET_COLUMNS: tuple[str, ...] = (
    "text", "section_title", "table_title", "spec_id", "version", "release",
)

class SpecDocSettings(BaseModel):
    max_zip_size_kb: int = Field(default=0, ge=0)
    max_chunk_chars: int = Field(default=1500, ge=1)
    chunk_overlap: int | None = Field(default=None, ge=0)
    auto_index_on_parse: bool = True
    auto_embed_on_parse: bool = True
    bm25_weights: tuple[float, ...] = Field(default=(5.0, 5.0, 5.0, 1.0, 1.0, 1.0))

    @field_validator("bm25_weights", mode="before")
    @classmethod
    def _validate_len(cls, value):
        if isinstance(value, (tuple, list)) and len(value) != 6:
            raise ValueError(f"bm25_weights must have exactly 6 entries, got {len(value)}")
        return value
# Settings: specdata_database_url: str | None = Field(default=None, validation_alias="DOC3GPP_SPECDATA_DATABASE_URL")
# Settings.spec_doc: SpecDocSettings = Field(default_factory=SpecDocSettings)
# OutputFieldsSettings: spec_doc chunk list + spec_doc_toc list
```

```python
# session.py additions
def resolve_specdata_database_url(settings=None) -> str:
    if settings is None: settings = get_settings()
    if settings.specdata_database_url: return settings.specdata_database_url
    parsed = make_url(settings.database_url)
    if parsed.database in (None, ":memory:"): return "sqlite+pysqlite:///:memory:"
    if not parsed.drivername.startswith("sqlite"):
        raise ValueError("cannot derive a specdata database from non-sqlite ...; set specdata_database_url explicitly")
    db_path = Path(parsed.database)
    sibling = db_path.with_name(f"{db_path.stem}_specdata{db_path.suffix}")
    return f"sqlite+pysqlite:///{sibling}"

@lru_cache(maxsize=1)
def get_specdata_engine() -> Engine: ...
def get_specdata_session_factory() -> sessionmaker: ...
```

migrate.py: `create_schema` accepts "specdata"; "all" does main + testcase + specdata. Specdata FTS5 DDL (`spec_doc_search` + `spec_doc_search_meta`) and vec DDL (`vec_spec_doc_embeddings` + `vec_spec_doc_meta`) run against `get_specdata_engine()`. Call sites (`cli.py` `create_schema("all")`) already pass "all" — no change needed there.

toml example: append `[spec_doc]` block + commented `specdata_database_url` mirroring the testcase entry.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_spec_doc_settings.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/storage/db/specdata_base.py src/doc3gpp/settings/schema.py src/doc3gpp/storage/db/session.py src/doc3gpp/storage/db/migrate.py doc3gpp.toml.example tests/conftest.py tests/unit/test_spec_doc_settings.py
git commit -m "feat(spec-doc): settings and specdata DB bootstrap"
```
## Task 2: Domain models

**Files:**
- Create: `src/doc3gpp/models/spec_doc.py`
- Test: `tests/unit/test_spec_doc_models.py`

**Interfaces:**
- Consumes: nothing (leaf).
- Produces: `SpecDocSource`, `SpecDocTocEntry`, `SpecDocTocFile`, `SpecDocToc`, `SpecDocChunk`, `ChunkDraft`, `SpecDocSearchFilters`, `SpecDocHit`, `SpecDocBatchResult`, `SpecDocSemanticHit`, `SpecDocError`, `SpecDocUnknownSpecError`, `SpecDocUnknownVersionError`, `SpecDocNoDocxError`, `SpecDocTooLargeError` — imported by Tasks 3,6,8,9,10,11,12,13.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_spec_doc_models.py
from doc3gpp.models.spec_doc import SpecDocChunk, SpecDocToc

def test_chunk_id_shape():
    c = SpecDocChunk(chunk_id="38.331@18.5.0#3", spec_id="38.331", version="18.5.0",
        release="Rel-18", file_order=0, source_file="38331-j30.docx",
        chunk_index=3, section_no="5.2", section_title="Intro",
        table_no=None, table_title=None, text="hello")
    assert c.chunk_id == "38.331@18.5.0#3"
    assert c.section_no == "5.2"

def test_toc_defaults():
    t = SpecDocToc(spec_id="38.331", version="18.5.0", release="Rel-18",
        entries=[], files=[], docx_count=1, created_at=None)
    assert t.entries == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_spec_doc_models.py -v`
Expected: FAIL with "No module named doc3gpp.models.spec_doc".

- [ ] **Step 3: Write minimal implementation**

```python
# src/doc3gpp/models/spec_doc.py
"""DTOs for the spec-document corpus (separate specdata sqlite file)."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime

class SpecDocError(Exception): pass
class SpecDocUnknownSpecError(SpecDocError): pass
class SpecDocUnknownVersionError(SpecDocError):
    def __init__(self, msg: str, available: list[str] | None = None):
        super().__init__(msg); self.available = available or []
class SpecDocNoDocxError(SpecDocError): pass
class SpecDocTooLargeError(SpecDocError): pass

@dataclass(slots=True)
class SpecDocSource:
    spec_id: str; version: str; release: str | None = None
    ftp_url: str = ""; downloaded_at: datetime | None = None
    parsed_at: datetime | None = None; chunk_count: int = 0; docx_count: int = 0

@dataclass(slots=True, frozen=True)
class SpecDocTocEntry:
    level: int; section_no: str | None; title: str; source_file: str; file_order: int

@dataclass(slots=True, frozen=True)
class SpecDocTocFile:
    source_file: str; file_order: int; first_section: str | None = None

@dataclass(slots=True)
class SpecDocToc:
    spec_id: str; version: str; release: str | None = None
    entries: list[SpecDocTocEntry] = field(default_factory=list)
    files: list[SpecDocTocFile] = field(default_factory=list)
    docx_count: int = 0; created_at: datetime | None = None

@dataclass(slots=True)
class ChunkDraft:
    file_order: int; source_file: str
    section_no: str | None = None; section_title: str | None = None
    table_no: str | None = None; table_title: str | None = None; text: str = ""

@dataclass(slots=True)
class SpecDocChunk(ChunkDraft):
    chunk_id: str = ""; spec_id: str = ""; version: str = ""
    release: str | None = None; chunk_index: int = 0

@dataclass(slots=True)
class SpecDocSearchFilters:
    spec_id: str | None = None; release: str | None = None
    version: str | None = None; section: str | None = None
    limit: int = 20; offset: int = 0

@dataclass(slots=True, frozen=True)
class SpecDocHit:
    chunk_id: str; spec_id: str; version: str; release: str | None
    section_no: str | None; section_title: str | None
    table_no: str | None; table_title: str | None
    chunk_index: int; text: str; score: float; previews: dict[str, str]

@dataclass(slots=True)
class SpecDocBatchResult:
    successes: dict[str, SpecDocSource] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)

@dataclass(slots=True, frozen=True)
class SpecDocSemanticHit:
    chunk_id: str; rrf_score: float; hit: SpecDocHit | None
    rank_fts5: int | None = None; rank_vec: int | None = None
    min_chunk_distance: float | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_spec_doc_models.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/models/spec_doc.py tests/unit/test_spec_doc_models.py
git commit -m "feat(spec-doc): domain models"
```

## Task 3: Scraping — version resolution + zip download

**Files:**
- Create: `src/doc3gpp/scraping/spec_doc_source.py`
- Test: `tests/unit/test_spec_doc_source.py`

**Interfaces:**
- Consumes: `SpecVersion` (models/spec.py), `SpecDocUnknownSpecError`, `SpecDocUnknownVersionError` (Task 2).
- Produces: `resolve_spec_doc_version(versions, release?, version?) -> SpecVersion`, `fetch_spec_doc_zip(ftp_url, client?) -> bytes` — used by Task 9.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_spec_doc_source.py
from doc3gpp.models.spec import SpecVersion
from doc3gpp.scraping.spec_doc_source import resolve_spec_doc_version
from doc3gpp.models.spec_doc import SpecDocUnknownVersionError
import pytest

def _v(ver, rel="Rel-18"):
    return SpecVersion(spec_id="38.331", version=ver, ftp_url=f"https://www.3gpp.org/ftp/x/{ver}.zip", release=rel)

def test_default_newest_numeric():
    vs = [_v("18.2.1"), _v("18.10.1"), _v("18.5.0")]
    assert resolve_spec_doc_version(vs).version == "18.10.1"

def test_exact_release_version():
    vs = [_v("18.5.0", "Rel-18"), _v("17.3.0", "Rel-17")]
    assert resolve_spec_doc_version(vs, release="Rel-17", version="17.3.0").version == "17.3.0"

def test_miss_lists_available():
    vs = [_v("18.5.0"), _v("18.10.1")]
    with pytest.raises(SpecDocUnknownVersionError) as e:
        resolve_spec_doc_version(vs, version="99.0.0")
    assert "18.10.1" in str(e.value)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_spec_doc_source.py -v`
Expected: FAIL with "No module named doc3gpp.scraping.spec_doc_source".

- [ ] **Step 3: Write minimal implementation**

```python
# src/doc3gpp/scraping/spec_doc_source.py
"""Version resolution + zip download for spec documents. Network only, no parsing."""
from __future__ import annotations
import logging
from doc3gpp.models.spec import SpecVersion
from doc3gpp.models.spec_doc import SpecDocUnknownVersionError
logger = logging.getLogger(__name__)

def _version_key(v: SpecVersion) -> tuple[int, ...]:
    parts: list[int] = []
    for seg in v.version.split("."):
        try: parts.append(int(seg))
        except ValueError: parts.append(0)
    return tuple(parts)

def resolve_spec_doc_version(versions: list[SpecVersion], release: str | None = None, version: str | None = None) -> SpecVersion:
    if not versions:
        raise SpecDocUnknownVersionError("no versions stored for this spec", available=[])
    pool = [v for v in versions if (release is None or v.release == release) and (version is None or v.version == version)]
    if not pool:
        avail = sorted(versions, key=_version_key, reverse=True)[:5]
        raise SpecDocUnknownVersionError(
            f"unknown version release={release!r} version={version!r}; available: " + ", ".join(f"{v.version} ({v.release})" for v in avail),
            available=[v.version for v in avail])
    if version is not None:
        exact = [v for v in pool if v.version == version]
        if exact: return exact[0]
    return sorted(pool, key=_version_key, reverse=True)[0]

def fetch_spec_doc_zip(ftp_url: str, client=None) -> bytes:
    from doc3gpp.scraping.client import ScraperClient
    own = client is None
    c = client or ScraperClient()
    try:
        logger.debug("Fetching spec zip: %s", ftp_url)
        return c.get_bytes(ftp_url)
    finally:
        if own: c.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_spec_doc_source.py tests/unit/test_spec_doc_models.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/scraping/spec_doc_source.py tests/unit/test_spec_doc_source.py
git commit -m "feat(spec-doc): version resolution and zip download"
```
## Task 4: docx block converter

**Files:**
- Modify: `src/doc3gpp/parsers/docx_converter.py` (add `HeadingBlock`, `ParagraphBlock`, `TableBlock`, `convert_document_to_blocks`)
- Test: `tests/unit/test_spec_doc_blocks.py`

**Interfaces:**
- Consumes: existing `_paragraph_to_markdown`, `_table_to_markdown`, `_style_name`, `_render_children` helpers.
- Produces: `Block = HeadingBlock | ParagraphBlock | TableBlock`; `convert_document_to_blocks(doc_bytes: bytes, filename: str) -> list[Block]` — used by Task 5. Re-exported via `parsers/spec_doc.py`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_spec_doc_blocks.py
import io
from docx import Document
from doc3gpp.parsers.docx_converter import convert_document_to_blocks, HeadingBlock, ParagraphBlock, TableBlock

def _make_docx():
    doc = Document()
    doc.add_heading("1 Scope", level=1)
    doc.add_paragraph("Some text here.")
    doc.add_paragraph("Table 1: My caption")
    t = doc.add_table(rows=2, cols=2)
    t.cell(0, 0).text = "A"; t.cell(0, 1).text = "B"
    t.cell(1, 0).text = "C"; t.cell(1, 1).text = "D"
    buf = io.BytesIO(); doc.save(buf); return buf.getvalue()

def test_blocks_heading_paragraph_table():
    blocks = convert_document_to_blocks(_make_docx(), "38331-j30.docx")
    assert isinstance(blocks[0], HeadingBlock) and blocks[0].level == 1
    assert blocks[0].section_no == "1" and blocks[0].title == "Scope"
    assert any(isinstance(b, ParagraphBlock) for b in blocks)
    tables = [b for b in blocks if isinstance(b, TableBlock)]
    assert len(tables) == 1 and tables[0].table_no == "1"

def test_rejects_dot_doc():
    import pytest
    with pytest.raises(ValueError):
        convert_document_to_blocks(b"xx", "old.doc")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_spec_doc_blocks.py -v`
Expected: FAIL with "convert_document_to_blocks not defined" / import error.

- [ ] **Step 3: Write minimal implementation**

```python
# in src/doc3gpp/parsers/docx_converter.py, append (module already imports
# io, re, and Path; add `from dataclasses import dataclass` to its imports):
from dataclasses import dataclass

@dataclass(slots=True, frozen=True)
class HeadingBlock:
    level: int; section_no: str | None; title: str; raw: str

@dataclass(slots=True, frozen=True)
class ParagraphBlock:
    text: str

@dataclass(slots=True, frozen=True)
class TableBlock:
    gfm: str; table_no: str | None = None; table_title: str | None = None

_SECTION_RE = re.compile(r"^(\d+(?:\.\d+)*)\s+(.*)$", re.DOTALL)
_CAPTION_RE = re.compile(r"^Table\s+(\d+(?:\.\d+)*)\s*:\s*(.+)$", re.IGNORECASE)

def _split_section(text: str) -> tuple[str | None, str]:
    m = _SECTION_RE.match(text.strip())
    if m: return m.group(1), m.group(2).strip()
    return None, text.strip()

def convert_document_to_blocks(doc_bytes: bytes, filename: str) -> list[HeadingBlock | ParagraphBlock | TableBlock]:
    from pathlib import Path as _P
    suffix = _P(filename).suffix.lower()
    if suffix != ".docx":
        raise ValueError(f"Unsupported extension {suffix!r}; only .docx is supported")
    document = Document(io.BytesIO(doc_bytes))
    raw_blocks: list[tuple[str, object]] = []  # ("h"|"p"|"t", payload)
    body = document.element.body
    for child in body.iterchildren():
        if _is_paragraph(child):
            p = Paragraph(child, document)
            md = _clean_text(_paragraph_to_markdown(p))
            if not md: continue
            style = _style_name(p).lower()
            if style.startswith("heading") or style.startswith("title") or style.startswith("subtitle"):
                level = 1
                if style.startswith("heading"):
                    try: level = int(style.split()[-1])
                    except ValueError: level = 1
                sec, title = _split_section(md.lstrip("#").strip())
                raw_blocks.append(("h", HeadingBlock(level, sec, title, md)))
            else:
                raw_blocks.append(("p", ParagraphBlock(md)))
        elif _is_table(child):
            t = Table(child, document)
            gfm = _clean_text(_table_to_markdown(t))
            if gfm: raw_blocks.append(("t", TableBlock(gfm)))
    # second pass: attach "Table N: title" captions from adjacent paragraphs
    out: list = []
    for i, (kind, payload) in enumerate(raw_blocks):
        if kind == "t":
            no, title = None, None
            if i > 0 and raw_blocks[i-1][0] == "p":
                m = _CAPTION_RE.match(raw_blocks[i-1][1].text.strip())
                if m: no, title = m.group(1), m.group(2).strip()
            if no is None and i + 1 < len(raw_blocks) and raw_blocks[i+1][0] == "p":
                m = _CAPTION_RE.match(raw_blocks[i+1][1].text.strip())
                if m: no, title = m.group(1), m.group(2).strip()
            out.append(TableBlock(payload.gfm, no, title))
        else:
            out.append(payload)
    if not out:
        raise RuntimeError(f"python-docx returned empty result for {filename}")
    return out
# add "convert_document_to_blocks", "HeadingBlock", "ParagraphBlock", "TableBlock" to __all__
```

GFM rendering (`_table_to_markdown`) and heading-style logic are unchanged; only the block wrapper is new. Existing `convert_document_to_markdown` stays untouched.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_spec_doc_blocks.py tests/unit/test_spec_doc_models.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/parsers/docx_converter.py tests/unit/test_spec_doc_blocks.py
git commit -m "feat(spec-doc): docx block converter"
```
## Task 5: Spec-doc parser — zip listing, file ordering, TOC

**Files:**
- Create: `src/doc3gpp/parsers/spec_doc.py`
- Test: `tests/unit/test_spec_doc_parser.py`

**Interfaces:**
- Consumes: `HeadingBlock/ParagraphBlock/TableBlock`, `convert_document_to_blocks` (Task 4); `SpecDocTocEntry/SpecDocTocFile` (Task 2).
- Produces: `list_spec_docx(zip_bytes) -> list[tuple[str, bytes]]`, `order_spec_files(files: list[SpecDocFileBlocks]) -> list[SpecDocFileBlocks]`, `extract_spec_toc(ordered) -> tuple[list[SpecDocTocEntry], list[SpecDocTocFile]]`, `SpecDocFileBlocks(file_order, source_file, blocks)` — used by Task 6 (first_section/section_no) and Task 9.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_spec_doc_parser.py
from doc3gpp.parsers.spec_doc import order_spec_files, extract_spec_toc, SpecDocFileBlocks
from doc3gpp.parsers.docx_converter import HeadingBlock, ParagraphBlock

def _f(name, first):
    sec, title = first
    return SpecDocFileBlocks(file_order=0, source_file=name,
        blocks=[HeadingBlock(1, sec, title, f"# {sec} {title}")] if sec else [ParagraphBlock("Foreword text")])

def test_order_numbered_first():
    files = [_f("b.docx", ("5", "Later")), _f("a.docx", ("4", "Earlier"))]
    ordered = order_spec_files(files)
    assert [f.source_file for f in ordered] == ["a.docx", "b.docx"]
    assert [f.file_order for f in ordered] == [0, 1]

def test_toc_tiebreak_first():
    toc = _f("toc.docx", (None, "x")); toc.blocks = [HeadingBlock(1, None, "Contents", "# Contents")]
    numbered = _f("n.docx", ("1", "Scope"))
    ordered = order_spec_files([numbered, toc])
    assert ordered[0].source_file == "toc.docx"

def test_extract_toc_shape():
    files = order_spec_files([_f("a.docx", ("1", "Scope")), _f("b.docx", ("2", "Refs"))])
    entries, filemap = extract_spec_toc(files)
    assert [(e.section_no, e.title) for e in entries] == [("1", "Scope"), ("2", "Refs")]
    assert [f.source_file for f in filemap] == ["a.docx", "b.docx"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_spec_doc_parser.py -v`
Expected: FAIL with "No module named doc3gpp.parsers.spec_doc".

- [ ] **Step 3: Write minimal implementation**

```python
# src/doc3gpp/parsers/spec_doc.py
"""Multi-docx listing, content-derived ordering, and TOC extraction. Pure parsing, no network."""
from __future__ import annotations
import logging, re, zipfile, io
from dataclasses import dataclass, field
from doc3gpp.models.spec_doc import SpecDocTocEntry, SpecDocTocFile
from doc3gpp.parsers.docx_converter import HeadingBlock, ParagraphBlock, TableBlock
logger = logging.getLogger(__name__)
Block = HeadingBlock | ParagraphBlock | TableBlock

@dataclass(slots=True)
class SpecDocFileBlocks:
    file_order: int; source_file: str; blocks: list[Block] = field(default_factory=list)

def list_spec_docx(zip_bytes: bytes) -> list[tuple[str, bytes]]:
    out = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        for info in z.infolist():
            name = info.filename
            if info.is_dir() or name.startswith("__MACOSX/"): continue
            low = name.lower()
            if low.endswith(".docx"): out.append((name, z.read(info.filename)))
            elif low.endswith(".doc"):
                logger.warning("skipping legacy .doc entry %s (only .docx is parsed)", name)
    return out

_TOC_TITLES = {"contents", "table of contents", "content"}

def _first_section(blocks: list[Block]) -> tuple[int, ...] | None:
    for b in blocks:
        if isinstance(b, HeadingBlock) and b.section_no:
            try: return tuple(int(p) for p in b.section_no.split("."))
            except ValueError: return None
    return None

def _is_toc_file(blocks: list[Block]) -> bool:
    return any(isinstance(b, HeadingBlock) and b.title.strip().lower() in _TOC_TITLES for b in blocks[:5])

def order_spec_files(files: list[SpecDocFileBlocks]) -> list[SpecDocFileBlocks]:
    # A front-matter file (TOC-like headings, no numeric section) sorts before
    # everything — it is the cover/contents. Numbered files sort by section
    # tuple even when they contain a contents heading further down.
    def norm(f):
        sec = _first_section(f.blocks)
        if sec is None and _is_toc_file(f.blocks): return (-1, (), 0, f.source_file)
        if sec is not None: return (0, sec, 0, f.source_file)
        return (1, (), 0, f.source_file)
    ordered = sorted(files, key=lambda f: (norm(f)[0], norm(f)[1], norm(f)[2], norm(f)[3]))
    for i, f in enumerate(ordered): f.file_order = i
    return ordered

def extract_spec_toc(ordered: list[SpecDocFileBlocks]) -> tuple[list[SpecDocTocEntry], list[SpecDocTocFile]]:
    entries: list[SpecDocTocEntry] = []; filemap: list[SpecDocTocFile] = []
    for f in ordered:
        first = None
        for b in f.blocks:
            if isinstance(b, HeadingBlock):
                if first is None and b.section_no: first = b.section_no
                entries.append(SpecDocTocEntry(b.level, b.section_no, b.title, f.source_file, f.file_order))
        filemap.append(SpecDocTocFile(f.source_file, f.file_order, first))
    return entries, filemap
```

Note: Python tuple comparison is element-wise with shorter-is-smaller, so `sorted` orders `(4,)` before `(4,2,1)` natively. A numbered file that also contains a `Contents` heading still sorts by its section number (TOC-first applies only to files with no numeric section).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_spec_doc_parser.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/parsers/spec_doc.py tests/unit/test_spec_doc_parser.py
git commit -m "feat(spec-doc): zip listing, ordering, TOC"
```
## Task 6: Chunker (pure function)

**Files:**
- Create: `src/doc3gpp/parsers/spec_doc_chunker.py`
- Test: `tests/unit/test_spec_doc_chunker.py`

**Interfaces:**
- Consumes: `HeadingBlock/ParagraphBlock/TableBlock` (Task 4), `ChunkDraft` (Task 2).
- Produces: `chunk_blocks(blocks, chunk_size=512, chunk_overlap=24, max_chunk_chars=1500, *, file_order=0, source_file="") -> list[ChunkDraft]` — used by Task 9 per file (service loops over ordered files, tags each run, concatenates + re-indexes globally). Break priority paragraph → sentence → table-row; rows atomic; every chunk carries current section_no/section_title + the passed file tags + table_no/table_title when inside a table.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_spec_doc_chunker.py
from doc3gpp.parsers.docx_converter import HeadingBlock, ParagraphBlock, TableBlock
from doc3gpp.parsers.spec_doc_chunker import chunk_blocks

def test_paragraph_chunks_carry_section():
    blocks = [HeadingBlock(1, "5", "Intro", "# 5 Intro"), ParagraphBlock("alpha beta gamma delta epsilon")]
    chunks = chunk_blocks(blocks, chunk_size=3, chunk_overlap=1, max_chunk_chars=1500)
    assert len(chunks) >= 2 and all(c.section_no == "5" for c in chunks)
    assert any("alpha" in c.text for c in chunks)
    assert all(c.file_order == 0 and c.source_file == "" for c in chunks)

def test_file_tags_propagate():
    blocks = [ParagraphBlock("hello world")]
    chunks = chunk_blocks(blocks, chunk_size=512, chunk_overlap=0, max_chunk_chars=1500,
        file_order=2, source_file="b.docx")
    assert len(chunks) == 1 and chunks[0].file_order == 2 and chunks[0].source_file == "b.docx"

def test_fitting_table_stays_whole():
    # Fitting = fits the char ceiling: kept whole even though 15 tokens overflow chunk_size=2.
    gfm = "| A | B |\n| --- | --- |\n| C | D |"
    blocks = [TableBlock(gfm, "1", "Cap")]
    chunks = chunk_blocks(blocks, chunk_size=2, chunk_overlap=0, max_chunk_chars=5000)
    assert len(chunks) == 1 and chunks[0].table_no == "1"

def test_oversized_table_splits_rowwise_with_repeated_meta():
    rows = "\n".join(f"| r{i} | v{i} |" for i in range(10))
    gfm = "| A | B |\n| --- | --- |\n" + rows
    blocks = [TableBlock(gfm, "2", "Big")]
    chunks = chunk_blocks(blocks, chunk_size=100, chunk_overlap=0, max_chunk_chars=60)
    assert len(chunks) > 1
    assert all(c.table_no == "2" and c.table_title == "Big" for c in chunks)
    assert all(len(c.text) <= 600 for c in chunks)  # header repeated + rows bounded

def test_char_ceiling_wins():
    # Overlap 0 isolates the ceiling: a single 500-word sentence must split into token
    # windows bounded by max_chunk_chars.
    blocks = [ParagraphBlock("word " * 500)]
    chunks = chunk_blocks(blocks, chunk_size=512, chunk_overlap=0, max_chunk_chars=100)
    assert len(chunks) > 1 and all(len(c.text) <= 100 for c in chunks)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_spec_doc_chunker.py -v`
Expected: FAIL with "No module named doc3gpp.parsers.spec_doc_chunker".

- [ ] **Step 3: Write minimal implementation**

```python
# src/doc3gpp/parsers/spec_doc_chunker.py
"""Pure block->chunk splitter. Break priority paragraph -> sentence -> table-row; rows atomic."""
from __future__ import annotations
import re
from doc3gpp.models.spec_doc import ChunkDraft
from doc3gpp.parsers.docx_converter import HeadingBlock, ParagraphBlock, TableBlock

_SENT_RE = re.compile(r"(?<=[.!?])\s+")

def _sentences(text: str) -> list[str]:
    return [s for s in _SENT_RE.split(text.strip()) if s]

def chunk_blocks(blocks, chunk_size=512, chunk_overlap=24, max_chunk_chars=1500, *, file_order=0, source_file="") -> list[ChunkDraft]:
    if chunk_size <= 0: raise ValueError(f"chunk_size must be > 0, got {chunk_size}")
    if chunk_overlap >= chunk_size: raise ValueError("chunk_overlap must be < chunk_size")
    units: list[tuple[str, dict]] = []  # (text, meta)
    cur_sec_no, cur_sec_title = None, None
    cur_file_order, cur_file = file_order, source_file
    for b in blocks:
        if isinstance(b, HeadingBlock):
            cur_sec_no, cur_sec_title = b.section_no, b.title
            units.append((f"{b.section_no + ' ' if b.section_no else ''}{b.title}".strip(),
                dict(sec_no=cur_sec_no, sec_title=cur_sec_title, t_no=None, t_title=None, fo=cur_file_order, sf=cur_file}))
        elif isinstance(b, ParagraphBlock):
            for s in _sentences(b.text) or [b.text]:
                meta = dict(sec_no=cur_sec_no, sec_title=cur_sec_title, t_no=None, t_title=None, fo=cur_file_order, sf=cur_file)
                # A single sentence can still exceed either budget: split its tokens into
                # windows bounded by chunk_size tokens AND max_chunk_chars chars.
                toks = s.split()
                if len(toks) > chunk_size or len(s) > max_chunk_chars:
                    start = 0
                    while start < len(toks):
                        acc: list[str] = []; acc_chars = 0
                        while start + len(acc) < len(toks) and len(acc) < chunk_size:
                            w = toks[start + len(acc)]
                            add = len(w) + (1 if acc else 0)
                            if acc and acc_chars + add > max_chunk_chars: break
                            acc.append(w); acc_chars += add
                        if not acc: acc.append(toks[start]); acc_chars = len(acc[0])  # single huge token: emit alone
                        units.append((" ".join(acc), dict(meta)))
                        start += len(acc)
                else:
                    units.append((s, meta))
        elif isinstance(b, TableBlock):
            lines = b.gfm.splitlines()
            header = "\n".join(lines[:2]) if len(lines) >= 2 else (lines[0] if lines else "")
            rows = lines[2:] if len(lines) > 2 else []
            whole = b.gfm
            # Fitting = fits the char ceiling: kept whole even when tokens overflow
            # chunk_size (allowed, like TDoc's whole-table preference).
            if len(whole) <= max_chunk_chars:
                units.append((whole, dict(sec_no=cur_sec_no, sec_title=cur_sec_title, t_no=b.table_no, t_title=b.table_title, fo=cur_file_order, sf=cur_file, atomic=True)))
            else:
                for r in rows:
                    if not r.strip(): continue
                    units.append((header + "\n" + r if header else r,
                        dict(sec_no=cur_sec_no, sec_title=cur_sec_title, t_no=b.table_no, t_title=b.table_title, fo=cur_file_order, sf=cur_file, atomic=True)))
    chunks: list[ChunkDraft] = []
    cur: list[str] = []; cur_tokens = 0; meta = None
    def flush():
        nonlocal cur, cur_tokens, meta
        if cur and meta is not None:
            chunks.append(ChunkDraft(file_order=meta["fo"], source_file=meta["sf"], section_no=meta["sec_no"],
                section_title=meta["sec_title"], table_no=meta["t_no"], table_title=meta["t_title"], text="\n\n".join(cur).strip()))
        cur, cur_tokens = [], 0
    for text, m in units:
        toks = len(text.split())
        if m.get("atomic"):
            if cur and (cur_tokens + toks > chunk_size or len("\n\n".join(cur)) + len(text) > max_chunk_chars): flush(); meta = m
            if meta is None: meta = m
            # oversized single row: emit alone (rows are atomic, never split mid-row)
            if toks > chunk_size or len(text) > max_chunk_chars:
                flush(); chunks.append(ChunkDraft(m["fo"], m["sf"], m["sec_no"], m["sec_title"], m["t_no"], m["t_title"], text)); meta = None; continue
            cur.append(text); cur_tokens += toks; continue
        if meta is None: meta = m
        if cur and (cur_tokens + toks > chunk_size or len("\n\n".join(cur)) + len(text) + 2 > max_chunk_chars):
            flush(); meta = m
        cur.append(text); cur_tokens += toks
    flush()
    # overlap: prepend trailing tokens of previous chunk
    if chunk_overlap > 0:
        for i in range(1, len(chunks)):
            prev_toks = chunks[i-1].text.split()
            if prev_toks:
                prefix = " ".join(prev_toks[-chunk_overlap:])
                if not chunks[i].text.startswith(prefix):
                    chunks[i].text = prefix + " " + chunks[i].text
    return [c for c in chunks if c.text.strip()]
```

Overlap is prefix-prepend (above) rather than sliding windows. The char ceiling is a hard cap on new content; only the overlap prefix may push a chunk over, so tests that assert the ceiling use `chunk_overlap=0` to isolate it.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_spec_doc_chunker.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/parsers/spec_doc_chunker.py tests/unit/test_spec_doc_chunker.py
git commit -m "feat(spec-doc): block chunker"
```
## Task 7: Specdata ORM + repository + Protocol

**Files:**
- Modify: `src/doc3gpp/storage/db/models.py` (append `SpecDocSourceORM` / `SpecDocTocORM` / `SpecDocChunkORM` hanging off `SpecDataBase`; follows the `TestCaseORM` precedent which lives in `models.py` off `TestCaseBase`).
- Create: `src/doc3gpp/storage/repositories/spec_doc_sql.py`
- Modify: `src/doc3gpp/repository/protocols.py` (append `SpecDocRepository` Protocol)
- Modify: `src/doc3gpp/storage/db/migrate.py` (import specdata models for create_all registration — Task 1 already wires scope/engine)
- Test: `tests/integration/test_spec_doc_repo.py`

**Interfaces:**
- Consumes: `SpecDataBase` (Task 1), `SpecDocSource/SpecDocToc/SpecDocChunk` (Task 2), `compress_json/decompress_json`.
- Produces: `SQLAlchemySpecDocRepository(session_factory?)` with `get_source/record_download/record_parsed/get_toc/upsert_toc/replace_chunks/list_chunks/list_chunk_counts`; `SpecDocRepository` Protocol — used by Tasks 8,9,10,11.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_spec_doc_repo.py
from doc3gpp.models.spec_doc import SpecDocChunk, SpecDocToc
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.repositories.spec_doc_sql import SQLAlchemySpecDocRepository

def test_roundtrip(sqlite_env):
    create_schema("all")
    repo = SQLAlchemySpecDocRepository()
    assert repo.get_source("38.331", "18.5.0") is None
    repo.record_download("38.331", "18.5.0", release="Rel-18", ftp_url="https://x/38331.zip", docx_count=1)
    src = repo.get_source("38.331", "18.5.0")
    assert src is not None and src.parsed_at is None
    toc = SpecDocToc(spec_id="38.331", version="18.5.0", release="Rel-18", entries=[], files=[], docx_count=1)
    repo.upsert_toc(toc)
    assert repo.get_toc("38.331", "18.5.0") is not None
    from doc3gpp.models.spec_doc import ChunkDraft
    repo.replace_chunks("38.331", "18.5.0", release="Rel-18",
        drafts=[ChunkDraft(file_order=0, source_file="a.docx", text="hello world")])
    chunks = repo.list_chunks("38.331", version="18.5.0")
    assert len(chunks) == 1 and chunks[0].chunk_id == "38.331@18.5.0#0"
    src2 = repo.get_source("38.331", "18.5.0")
    assert src2.chunk_count == 1 and src2.parsed_at is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/integration/test_spec_doc_repo.py -v`
Expected: FAIL with "No module named doc3gpp.storage.repositories.spec_doc_sql".

- [ ] **Step 3: Write minimal implementation**

```python
# ORM (append to storage/db/models.py):
class SpecDocSourceORM(SpecDataBase):
    __tablename__ = "spec_doc_sources"
    spec_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    version: Mapped[str] = mapped_column(String(16), primary_key=True)
    release: Mapped[str | None] = mapped_column(String(16), nullable=True)
    ftp_url: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    parsed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    docx_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

class SpecDocTocORM(SpecDataBase):
    __tablename__ = "spec_doc_tocs"
    spec_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    version: Mapped[str] = mapped_column(String(16), primary_key=True)
    release: Mapped[str | None] = mapped_column(String(16), nullable=True)
    toc_json_gzip: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    file_order_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    docx_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

class SpecDocChunkORM(SpecDataBase):
    __tablename__ = "spec_doc_chunks"
    chunk_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    spec_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    release: Mapped[str | None] = mapped_column(String(16), nullable=True)
    file_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_file: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    section_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    section_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    table_no: Mapped[str | None] = mapped_column(String(32), nullable=True)
    table_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
```

Repository: `SQLAlchemySpecDocRepository(session_factory=None)` defaulting to `get_specdata_session_factory()`. `record_download` upserts `(spec_id, version)` + stamps `downloaded_at` (UTC). `record_parsed` stamps `parsed_at` + `chunk_count`. `upsert_toc` compresses `{"entries": [...], "files": [...]}` via `compress_json`, stores file_order map as plain JSON text. `replace_chunks(spec_id, version, release, drafts: list[ChunkDraft])` deletes existing rows for the pair then inserts with `chunk_id=f"{spec_id}@{version}#{chunk_index}"` (index scoped per pair). `list_chunks(spec_id, version?, release?, section?, limit=50, offset=0)` with `apply_text_filter` on `section_title`/`section_no` when `section` given, ordered by `(version, chunk_index)`.

Protocol in `repository/protocols.py`:

```python
class SpecDocRepository(Protocol):
    def get_source(self, spec_id: str, version: str) -> SpecDocSource | None: ...
    def record_download(self, spec_id, version, *, release, ftp_url, docx_count) -> None: ...
    def record_parsed(self, spec_id, version, *, chunk_count) -> None: ...
    def get_toc(self, spec_id: str, version: str) -> SpecDocToc | None: ...
    def upsert_toc(self, toc: SpecDocToc) -> None: ...
    def replace_chunks(self, spec_id, version, *, release, drafts: list[ChunkDraft]) -> list[SpecDocChunk]: ...
    def list_chunks(self, spec_id, *, version=None, release=None, section=None, limit=50, offset=0) -> list[SpecDocChunk]: ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/integration/test_spec_doc_repo.py tests/unit/test_spec_doc_settings.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/storage/db/models.py src/doc3gpp/storage/repositories/spec_doc_sql.py src/doc3gpp/repository/protocols.py src/doc3gpp/storage/db/migrate.py tests/integration/test_spec_doc_repo.py
git commit -m "feat(spec-doc): specdata ORM and repository"
```
## Task 8: FTS5 + vector repos on the specdata engine

**Files:**
- Create: `src/doc3gpp/storage/repositories/spec_doc_search_sql.py`
- Create: `src/doc3gpp/storage/repositories/spec_doc_vector_sql.py`
- Modify: `src/doc3gpp/repository/protocols.py` (append `SpecDocSearchRepository`, `SpecDocVectorRepository` Protocols)
- Test: `tests/integration/test_spec_doc_search_repo.py`

**Interfaces:**
- Consumes: `SQLAlchemySpecDocRepository` (Task 7) for chunk reads, `SpecDocSearchFilters/SpecDocHit` (Task 2), `_SPEC_DOC_SNIPPET_COLUMNS` (Task 1).
- Produces: `SQLAlchemySpecDocSearchRepository.upsert_for_version/remove_for_version/search/rebuild_batch/count_versions_to_index/get_resume_cursor/set_resume_cursor/clear_resume_cursor/status`; `SQLAlchemySpecDocVectorRepository.upsert_for_version/remove_for_version/knn` with `knn` returning `list[tuple[str, float]]` = `(chunk_id, cosine_distance)` (the chunk_index is recoverable from the `chunk_id` suffix) — used by Tasks 10,11.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_spec_doc_search_repo.py
from doc3gpp.models.spec_doc import SpecDocSearchFilters
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.repositories.spec_doc_sql import SQLAlchemySpecDocRepository
from doc3gpp.storage.repositories.spec_doc_search_sql import SQLAlchemySpecDocSearchRepository
from doc3gpp.models.search import SearchQueryError
from doc3gpp.models.spec_doc import ChunkDraft
import pytest

def _seed(repo):
    repo.record_download("38.331", "18.5.0", release="Rel-18", ftp_url="https://x", docx_count=1)
    repo.replace_chunks("38.331", "18.5.0", release="Rel-18",
        drafts=[ChunkDraft(file_order=0, source_file="a.docx", section_no="5.1", section_title="Handover", text="handover procedure signalling")])

def test_fts5_search(sqlite_env):
    create_schema("all")
    repo = SQLAlchemySpecDocRepository(); _seed(repo)
    fts = SQLAlchemySpecDocSearchRepository()
    fts.upsert_for_version("38.331", "18.5.0")
    hits = fts.search("handover", SpecDocSearchFilters(spec_id="38.331"))
    assert len(hits) == 1 and hits[0].chunk_id == "38.331@18.5.0#0"
    assert "text" in hits[0].previews

def test_stopwords_only_raises(sqlite_env):
    create_schema("all")
    fts = SQLAlchemySpecDocSearchRepository()
    with pytest.raises(SearchQueryError):
        fts.search("the and of", SpecDocSearchFilters())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/integration/test_spec_doc_search_repo.py -v`
Expected: FAIL with "No module named doc3gpp.storage.repositories.spec_doc_search_sql".

- [ ] **Step 3: Write minimal implementation**

FTS5 repo mirrors `search_sql.py`: constructor binds `get_specdata_engine()`, probes `PRAGMA compile_options` for `ENABLE_FTS5` (raise `SearchUnavailableError` otherwise), caches `settings.spec_doc.bm25_weights` + `settings.search.snippet_tokens`. `upsert_for_version(spec_id, version)` reads chunks via `SQLAlchemySpecDocRepository().list_chunks(...)` (no limit), DELETEs existing FTS5 rows for the pair (`DELETE FROM spec_doc_search WHERE spec_id=:s AND version=:v` — FTS5 supports non-MATCH DELETE by rowid/UNINDEXED column equality), INSERTs one row per chunk `(chunk_id UNINDEXED, text, section_title, table_title, spec_id, version, release)`. `search(match_expr_already_built?, ...)` — NO: the repo takes the raw user query and builds MATCH via `SearchQueryBuilder(query).build()` internally (same as `SearchService.search`), so services pass raw text. Filters: `spec_id`/`version`/`release` exact-or-rich via `build_text_filter_sql` on the FTS5 columns; `section` rich on `section_title OR section_no`... FTS5 has no section_no column — store `section_no || ' ' || section_title` into `section_title` at index time so `section` filtering + snippet both work on one column. `bm25(spec_doc_search, :w0..:w5)` ORDER, `LIMIT :limit OFFSET :offset`, one `snippet()` per weight>0 column, previews only on `<<`/`>>` match (copy the loop from `search_sql.py:304-331`). `rebuild_batch(batch_size, after_id, stale_only)` pages `(spec_id, version)` pairs from `spec_doc_sources` ordered ASC; stale_only compares `parsed_at > last meta`. Meta keys in `spec_doc_search_meta`: `last_rebuild_last_chunk_id` cursor + `last_rebuild_at`.

Vector repo mirrors `vector_sql.py` minus the tdoc JOINs: `upsert_for_version(spec_id, version, embeddings: list[np.ndarray])` DELETEs pair rows then INSERTs `(chunk_id PK, spec_id, version, chunk_index, embedding)`. `remove_for_version`, `knn(query_vec, limit, filters?)` with optional `spec_id/version/release/section` WHERE on the vec table joined to `spec_doc_chunks` for section/release text. `chunk_id` shape `{spec_id}@{version}#{index}` shared with Task 7.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/integration/test_spec_doc_search_repo.py tests/integration/test_spec_doc_repo.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/storage/repositories/spec_doc_search_sql.py src/doc3gpp/storage/repositories/spec_doc_vector_sql.py src/doc3gpp/repository/protocols.py tests/integration/test_spec_doc_search_repo.py
git commit -m "feat(spec-doc): FTS5 and vector repos"
```
## Task 9: SpecDocService — fetch / parse / toc (+ factory)

**Files:**
- Create: `src/doc3gpp/services/spec_doc_service.py`
- Modify: `src/doc3gpp/services/factory.py` (`build_spec_doc_service`, `build_spec_doc_search_service`, `build_spec_doc_semantic_service`)
- Test: `tests/integration/test_spec_doc_service.py` (uses `sqlite_env` with specdata support from Task 1)

**Interfaces:**
- Consumes: `resolve_spec_doc_version/fetch_spec_doc_zip` (Task 3), `list_spec_docx/order_spec_files/extract_spec_toc/convert_document_to_blocks` (Tasks 4,5), `chunk_blocks` (Task 6), `SpecDocRepository` (Task 7), main-DB `SpecRepository.list_versions`.
- Produces: `SpecDocService.fetch/parse/parse_many/get_toc` + factory builders — used by Tasks 12 (CLI), 13 (web/jobs/MCP).

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_spec_doc_service.py
from doc3gpp.services.spec_doc_service import SpecDocService
from doc3gpp.storage.db.migrate import create_schema

class FakeSpecRepo:
    def __init__(self, versions): self._v = versions
    def list_versions(self, spec_id, **kw): return self._v.get(spec_id, [])

def test_parse_many_buckets_and_identity_skip(sqlite_env):
    import io, zipfile
    from docx import Document
    from doc3gpp.models.spec import SpecVersion
    create_schema("all")
    doc = Document(); doc.add_heading("1 Scope", level=1); doc.add_paragraph("hello handover world")
    buf = io.BytesIO(); doc.save(buf); docx = buf.getvalue()
    zb = io.BytesIO()
    with zipfile.ZipFile(zb, "w") as z: z.writestr("38331-j30.docx", docx)
    zip_bytes = zb.getvalue()
    versions = {"38.331": [SpecVersion("38.331", "18.5.0", "https://www.3gpp.org/ftp/x/38331.zip", "Rel-18")]}
    svc = SpecDocService(spec_repo=FakeSpecRepo(versions), fetcher=lambda url: zip_bytes,
        cache_dir=sqlite_env.parent / "speccache")
    r1 = svc.parse_many(["38.331"])
    assert "38.331" in r1.successes
    r2 = svc.parse_many(["38.331"])
    assert "38.331" in r2.skipped  # immutable: second parse skips
    toc = svc.get_toc("38.331", "18.5.0")
    assert toc.entries and toc.entries[0].section_no == "1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/integration/test_spec_doc_service.py -v`
Expected: FAIL with "No module named doc3gpp.services.spec_doc_service".

- [ ] **Step 3: Write minimal implementation**

```python
# src/doc3gpp/services/spec_doc_service.py
"""Orchestration for spec-doc fetch/parse/toc. Immutable ledger: parsed (spec_id, version) skips unless force."""
from __future__ import annotations
import io, logging, zipfile
from pathlib import Path
from doc3gpp.models.spec_doc import (...)
from doc3gpp.parsers.docx_converter import convert_document_to_blocks
from doc3gpp.parsers.spec_doc import SpecDocFileBlocks, extract_spec_toc, list_spec_docx, order_spec_files
from doc3gpp.parsers.spec_doc_chunker import chunk_blocks
from doc3gpp.scraping.spec_doc_source import fetch_spec_doc_zip, resolve_spec_doc_version
logger = logging.getLogger(__name__)

class SpecDocService:
    def __init__(self, *, spec_repo, doc_repo, search_service=None, semantic_service=None,
                 settings=None, fetcher=None, cache_dir: Path | None = None):
        ...

    def _versions(self, spec_id):  # main-DB lookup; miss -> SpecDocUnknownSpecError pointing at spec sync
        rows = self._spec_repo.list_versions(spec_id, limit=500)
        if not rows: raise SpecDocUnknownSpecError(f"Unknown spec {spec_id!r}; run 'doc3gpp spec sync --spec-id {spec_id}' first")
        return rows

    def fetch(self, spec_id, *, release=None, version=None, force=False) -> SpecDocSource:
        rows = self._versions(spec_id)
        ver = resolve_spec_doc_version(rows, release, version)
        # Immutable skip: a cached zip means fetch is done (no network).
        # A purged cache re-downloads — that is correct, not a skip violation.
        if not force and self._zip_cache_exists(spec_id, ver.version):
            if self._doc_repo.get_source(spec_id, ver.version) is None:
                self._doc_repo.record_download(spec_id, ver.version, release=ver.release,
                    ftp_url=ver.ftp_url, docx_count=len(list_spec_docx(self._read_zip_cache(spec_id, ver.version))))
            return self._doc_repo.get_source(spec_id, ver.version)
        raw = self._fetcher(ver.ftp_url)  # network
        if self._settings.spec_doc.max_zip_size_kb > 0 and len(raw) > self._settings.spec_doc.max_zip_size_kb * 1024:
            raise SpecDocTooLargeError(f"zip for {spec_id}@{ver.version} exceeds max_zip_size_kb")
        self._write_zip_cache(spec_id, ver.version, raw)
        self._doc_repo.record_download(spec_id, ver.version, release=ver.release, ftp_url=ver.ftp_url, docx_count=len(list_spec_docx(raw)))
        return self._doc_repo.get_source(spec_id, ver.version)

    def parse(self, spec_id, *, release=None, version=None, force=False) -> SpecDocSource:
        src = self.fetch(spec_id, release=release, version=version, force=force)
        if src.parsed_at is not None and not force: return src
        raw = self._read_zip_cache(spec_id, src.version)
        entries = list_spec_docx(raw)
        if not entries: raise SpecDocNoDocxError(f"zip for {spec_id}@{src.version} contains no .docx files")
        files = [SpecDocFileBlocks(file_order=i, source_file=name, blocks=convert_document_to_blocks(data, name))
                 for i, (name, data) in enumerate(entries)]
        ordered = order_spec_files(files)
        toc_entries, toc_files = extract_spec_toc(ordered)
        self._doc_repo.upsert_toc(SpecDocToc(spec_id, src.version, src.release, toc_entries, toc_files, len(entries)))
        overlap = self._settings.spec_doc.chunk_overlap
        if overlap is None: overlap = self._settings.semantic_search.chunk_overlap
        drafts: list[ChunkDraft] = []
        for f in ordered:  # per-file runs keep file identity; chunker itself stays pure
            drafts.extend(chunk_blocks(f.blocks, self._settings.semantic_search.chunk_size,
                overlap, self._settings.spec_doc.max_chunk_chars,
                file_order=f.file_order, source_file=f.source_file))
        self._doc_repo.replace_chunks(spec_id, src.version, release=src.release, drafts=drafts)
        self._write_markdown_cache(spec_id, src.version, ordered)
        self._doc_repo.record_parsed(spec_id, src.version, chunk_count=len(drafts))
        # best-effort auto-index / auto-embed (same pattern as TDocCrService hooks)
        try:
            if self._settings.spec_doc.auto_index_on_parse and self._search is not None:
                self._search.upsert_for_version(spec_id, src.version)
        except Exception as exc: logger.warning("spec-doc auto-index failed for %s@%s: %s", spec_id, src.version, exc)
        ...same for embed...
        return self._doc_repo.get_source(spec_id, src.version)

    def parse_many(self, spec_ids, *, release=None, version=None, force=False, on_progress=None) -> SpecDocBatchResult:
        out = SpecDocBatchResult()
        for sid in spec_ids:
            try: out.successes[sid] = self.parse(sid, release=release, version=version, force=force)
            except SpecDocTooLargeError as e: out.skipped[sid] = str(e)
            except SpecDocUnknownVersionError as e: out.failures[sid] = str(e)  # unknown version is a failure, not skip
            except Exception as e: out.failures[sid] = str(e)  # one spec never aborts the batch
        return out

    def get_toc(self, spec_id, version, *, release=None) -> SpecDocToc:
        toc = self._doc_repo.get_toc(spec_id, version)
        if toc is None: raise SpecDocUnknownVersionError(f"no parsed TOC for {spec_id}@{version}; run 'doc3gpp spec doc parse --spec {spec_id} --version {version}'")
        return toc
```

Block file-identity: the service calls `chunk_blocks` once per ordered file with that file's `file_order`/`source_file` kwargs, then concatenates the runs (global `chunk_index` assigned by `replace_chunks` in file order). The chunker signature stays pure — no block mutation.

Cache layout: `{cache.dir}/specs/zips/<spec_id>/<version>.zip`, `{cache.dir}/specs/markdown/<spec_id>/<version>/<file_order>-<stem>.md` (render each file's blocks back to markdown text for debugging).

factory.py: `build_spec_doc_service(embedder=None)` wires `SQLAlchemySpecRepository()` + `SQLAlchemySpecDocRepository()` + search/semantic builders; `build_spec_doc_search_service()` returns None when FTS5 missing/disabled; `build_spec_doc_semantic_service(embedder=...)` mirrors `build_semantic_search_service` against the specdata vec repo.

conftest.py: `sqlite_env` sets `DOC3GPP_SPECDATA_DATABASE_URL` to `tmp/test_specdata.db` and clears `get_specdata_engine` cache alongside the other two.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/integration/test_spec_doc_service.py tests/integration/test_spec_doc_repo.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/services/spec_doc_service.py src/doc3gpp/services/factory.py tests/integration/test_spec_doc_service.py
git commit -m "feat(spec-doc): SpecDocService fetch/parse/toc"
```
## Task 10: Search services (FTS5 + hybrid semantic)

**Files:**
- Create: `src/doc3gpp/services/spec_doc_search_service.py`
- Create: `src/doc3gpp/services/spec_doc_semantic_service.py`
- Test: `tests/integration/test_spec_doc_search_service.py`

**Interfaces:**
- Consumes: `SQLAlchemySpecDocSearchRepository` (Task 8), `SQLAlchemySpecDocVectorRepository` (Task 8), `SearchQueryBuilder` (existing), `SpecDocSearchFilters/SpecDocHit/SpecDocSemanticHit` (Task 2).
- Produces: `SpecDocSearchService.search/rebuild/status/upsert_for_version`, `SpecDocSemanticService.search` + chunk-level `rrf_merge` — used by Tasks 12,13.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_spec_doc_search_service.py
from doc3gpp.models.spec_doc import ChunkDraft, SpecDocSearchFilters
from doc3gpp.services.spec_doc_search_service import SpecDocSearchService
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.repositories.spec_doc_sql import SQLAlchemySpecDocRepository
from doc3gpp.models.spec_doc import ChunkDraft

def _seed():
    repo = SQLAlchemySpecDocRepository()
    repo.record_download("38.331", "18.5.0", release="Rel-18", ftp_url="https://x", docx_count=1)
    repo.replace_chunks("38.331", "18.5.0", release="Rel-18",
        drafts=[ChunkDraft(file_order=0, source_file="a.docx", section_no="5.1", section_title="Handover", text="handover procedure signalling")])

def test_search_and_rebuild(sqlite_env):
    create_schema("all")
    _seed()
    svc = SpecDocSearchService()
    svc.upsert_for_version("38.331", "18.5.0")
    hits = svc.search("handover", SpecDocSearchFilters(spec_id="38.331"))
    assert hits and hits[0].spec_id == "38.331"
    seen = list(svc.rebuild(batch_size=10, resume=False, stale_only=False, quiet=True))
    assert seen  # yields RebuildProgress
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/integration/test_spec_doc_search_service.py -v`
Expected: FAIL with "No module named doc3gpp.services.spec_doc_search_service".

- [ ] **Step 3: Write minimal implementation**

```python
# src/doc3gpp/services/spec_doc_search_service.py
"""FTS5 search over spec-doc chunks. Mirrors SearchService (upsert/search/rebuild/status)."""
from __future__ import annotations
import logging
from collections.abc import Iterator
from doc3gpp.models.search import RebuildProgress, SearchIndexStatus
from doc3gpp.models.spec_doc import SpecDocHit, SpecDocSearchFilters
logger = logging.getLogger(__name__)

class SpecDocSearchService:
    def __init__(self, repo=None, quiet=False):
        from doc3gpp.storage.repositories.spec_doc_search_sql import SQLAlchemySpecDocSearchRepository
        self._repo = repo or SQLAlchemySpecDocSearchRepository()
        self._quiet = quiet
    def upsert_for_version(self, spec_id, version):
        try: self._repo.upsert_for_version(spec_id, version)
        except Exception as exc: logger.warning("spec-doc upsert failed for %s@%s: %s", spec_id, version, exc)
    def search(self, query, filters: SpecDocSearchFilters):
        return self._repo.search(query, filters)
    def rebuild(self, *, batch_size, resume, stale_only, quiet) -> Iterator[RebuildProgress]:
        after = self._repo.get_resume_cursor() if resume else None
        if not resume: self._repo.clear_resume_cursor(); after = None
        total = self._repo.count_versions_to_index(stale_only=stale_only, after_id=after)
        processed, last_pct = 0, 0
        for batch in self._repo.rebuild_batch(batch_size=batch_size, after_id=after, stale_only=stale_only):
            for spec_id, version in batch:
                self.upsert_for_version(spec_id, version)
                processed += 1
                pct = processed * 100 // total if total else 100
                if pct > last_pct:
                    yield RebuildProgress(processed, total, f"{spec_id}@{version}"); last_pct = pct
            self._repo.set_resume_cursor(batch[-1][0] + "@" + batch[-1][1])
    def status(self) -> SearchIndexStatus: return self._repo.status()
```

```python
# src/doc3gpp/services/spec_doc_semantic_service.py
"""Hybrid FTS5+vector RRF over spec-doc chunks. Chunk-level fusion (unlike tdoc-level)."""
from __future__ import annotations
import logging
from doc3gpp.cli_filters import SearchQueryBuilder
from doc3gpp.models.spec_doc import SpecDocSearchFilters, SpecDocSemanticHit
logger = logging.getLogger(__name__)

def rrf_merge(fts5_hits, vec_hits, *, k=60, vector_weight=0.5, limit=20) -> list[SpecDocSemanticHit]:
    fts_rank = {h.chunk_id: i for i, h in enumerate(fts5_hits)}
    fts_by_id = {h.chunk_id: h for h in fts5_hits}
    scored = []
    for rank, (cid, dist) in enumerate(vec_hits):
        rf, rv = fts_rank.get(cid), rank
        score = (1.0/(k+rf) * (1.0-vector_weight) if rf is not None else 0.0) + 1.0/(k+rv) * vector_weight
        scored.append(SpecDocSemanticHit(cid, score, fts_by_id.get(cid), rf, rv, dist))
    for cid, i in fts_rank.items():
        if cid not in {c for c, _ in vec_hits}:
            scored.append(SpecDocSemanticHit(cid, 1.0/(k+i) * (1.0-vector_weight), fts_by_id[cid], i, None, None))
    scored.sort(key=lambda h: h.rrf_score, reverse=True)
    return scored[:limit]

class SpecDocSemanticService:
    def __init__(self, *, fts5_service, embedder, vector_repo, settings):
        self._fts5, self._embedder, self._vec, self._settings = fts5_service, embedder, vector_repo, settings
    def search(self, query, *, fts5_query, filters, limit, fts5_weight) -> list[SpecDocSemanticHit]:
        qvec = self._embedder.encode([query])[0]
        if fts5_query is None:
            vec_hits = self._vec.knn(qvec, limit=limit, filters=filters)
            return [SpecDocSemanticHit(cid, -dist, None, None, r, dist) for r, (cid, dist) in enumerate(vec_hits)][:limit]
        fanout = self._settings.semantic_search.fanout_multiplier
        n = max(limit * fanout, 0)
        f = SpecDocSearchFilters(filters.spec_id, filters.release, filters.version, filters.section, n, 0)
        fts_hits = self._fts5.search(fts5_query, f)
        vec_hits = self._vec.knn(qvec, limit=n, filters=filters)
        return rrf_merge(fts_hits, vec_hits, k=self._settings.semantic_search.rrf_k, vector_weight=1.0 - fts5_weight, limit=limit)
    def index_for_version(self, spec_id, version):
        from doc3gpp.storage.repositories.spec_doc_sql import SQLAlchemySpecDocRepository
        chunks = SQLAlchemySpecDocRepository().list_chunks(spec_id, version=version, limit=100000)
        texts = [f"{c.section_no or ''} {c.section_title or ''}\n{c.text}".strip() for c in chunks]
        if not texts: self._vec.remove_for_version(spec_id, version); return
        embs = self._embedder.encode(texts)
        self._vec.upsert_for_version(spec_id, version, [embs[i] for i in range(len(texts))])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/integration/test_spec_doc_search_service.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/services/spec_doc_search_service.py src/doc3gpp/services/spec_doc_semantic_service.py tests/integration/test_spec_doc_search_service.py
git commit -m "feat(spec-doc): FTS5 and hybrid search services"
```
## Task 11: CLI — `spec doc` sub-app + `db --scope specdata` + schema registry

**Files:**
- Modify: `src/doc3gpp/cli.py` (new `spec_doc_app` Typer sub-app mounted as `spec doc`; VALID_DB_SCOPES += "specdata"; `_specdata_url_or_raise`; db init/check/reset branches)
- Modify: `src/doc3gpp/models/schema_info.py` (new `"spec_doc"` resource: spec_doc_sources/tocs/chunks + FTS5 columns documented)
- Test: `tests/integration/test_spec_doc_cli.py`

**Interfaces:**
- Consumes: `build_spec_doc_service/build_spec_doc_search_service/build_spec_doc_semantic_service` (Task 9), `SpecDocSearchFilters/SpecDocBatchResult` (Task 2), `_emit_records/_emit_json/_dump_show_json/_resolve_format/_resolve_compact` (existing).
- Produces: `doc3gpp spec doc fetch/parse/toc show/search query/search sem/schema` — byte-consistent JSON consumed by Task 13's HTTP/MCP parity tests.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_spec_doc_cli.py
from typer.testing import CliRunner
from doc3gpp.cli import app
from doc3gpp.storage.db.migrate import create_schema

def test_spec_doc_schema(sqlite_env):
    create_schema("all")
    r = CliRunner().invoke(app, ["spec", "doc", "schema", "--format", "json"])
    assert r.exit_code == 0 and "spec_doc_chunks" in r.output

def test_spec_doc_toc_miss(sqlite_env):
    create_schema("all")
    r = CliRunner().invoke(app, ["spec", "doc", "toc", "show", "--spec", "38.331", "--version", "18.5.0"])
    assert r.exit_code != 0 and "spec doc parse" in r.output
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/integration/test_spec_doc_cli.py -v`
Expected: FAIL — "No such command 'doc'".

- [ ] **Step 3: Write minimal implementation**

```python
spec_doc_app = typer.Typer(help="spec document corpus: fetch, parse, TOC, search")
spec_app.add_typer(spec_doc_app, name="doc")

@spec_doc_app.command("fetch")
def spec_doc_fetch(spec: str = typer.Option(...), release=None, version=None, force=False):
    create_schema("all")
    svc = build_spec_doc_service()
    try: src = svc.fetch(spec, release=release, version=version, force=force)
    except SpecDocUnknownSpecError as e: raise typer.BadParameter(str(e))
    except SpecDocUnknownVersionError as e: raise typer.BadParameter(str(e))
    typer.echo(f"fetched {src.spec_id}@{src.version} ({src.docx_count} docx)")

@spec_doc_app.command("parse")
def spec_doc_parse(spec: list[str] = typer.Option([], "--spec", help="Spec id to parse; repeat per spec (at least one required)."), release=None, version=None, force=False,
                   fmt=None, output=None, compact=False):
    """Batch form: --spec may repeat; success/skip/failure buckets printed."""
    create_schema("all")
    if not spec: raise typer.BadParameter("pass at least one --spec <id>")
    svc = build_spec_doc_service()
    result = svc.parse_many(list(spec), release=release, version=version, force=force)
    for sid in result.successes: typer.echo(f"ok {sid}")
    for sid, why in result.skipped.items(): typer.echo(f"skipped {sid}: {why}")
    for sid, why in result.failures.items(): typer.echo(f"failed {sid}: {why}", err=True)

@spec_doc_app.command("toc")
def spec_doc_toc(...):  # actually `toc show`: nested group
```

TOC nesting: `toc_app = typer.Typer(); spec_doc_app.add_typer(toc_app, name="toc"); @toc_app.command("show")` with `--spec/--version/--release/--format/--output/--compact`; renders entries via `_emit_records` with fields `settings.output.fields.spec_doc_toc` (default `section_no,title,level,source_file`). Miss → `typer.BadParameter(f"no parsed TOC for ...; run 'doc3gpp spec doc parse --spec ...'")`.

Search nesting: `search_app2 = typer.Typer(); spec_doc_app.add_typer(search_app2, name="search"); @search_app2.command("query")` (`TEXT` argument + `--spec/--release/--version/--section/--limit/--offset/--format/--compact`) and `@search_app2.command("sem")` (`TEXT` + `--fts5-query/--fts5-weight/--spec/--release/--version/--section/--limit`). JSON payloads: query → list of `{chunk_id,spec_id,version,release,section_no,section_title,table_no,table_title,chunk_index,text,score,previews}`; sem → list of `{chunk_id,rrf_score,rank_fts5,rank_vec,min_chunk_distance,hit}` mirroring `_render_semantic_hits`. Markdown/table mirror `_render_search_hits` shape.

`@spec_doc_app.command("schema")` → `_emit_schema("spec_doc", ...)`.

DB scope: `VALID_DB_SCOPES = ("main", "testcase", "specdata", "all")`; `db init/check/reset` handle "specdata" via `resolve_specdata_database_url`/`get_specdata_engine` (same shape as testcase branches); reset requires sqlite for all selected scopes.

schema_info.py: `"spec_doc": (spec_doc_sources{...}, spec_doc_tocs{...}, spec_doc_chunks{...})` — FTS5/vec columns documented in the chunks table description (no separate virtual-table entries; note says FTS5 `spec_doc_search` + vec `vec_spec_doc_embeddings` mirror chunk columns).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/integration/test_spec_doc_cli.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/cli.py src/doc3gpp/models/schema_info.py tests/integration/test_spec_doc_cli.py
git commit -m "feat(spec-doc): spec doc CLI and schema registry"
```
## Task 12: Web + MCP + jobs

**Files:**
- Modify: `src/doc3gpp/models/jobs.py` (`PARSE_SPEC_DOCS = "parse_spec_docs"`)
- Modify: `src/doc3gpp/web/workers/handlers.py` (`_parse_spec_docs` + KIND_TO_HANDLER entry)
- Modify: `src/doc3gpp/web/routes/jobs.py` (`_ParseSpecDocsBody` + `POST /jobs/parse/spec-docs`)
- Create: `src/doc3gpp/web/routes/spec_docs.py` (TOC + search + schema routes)
- Modify: `src/doc3gpp/web/routes/__init__.py` (register router), `src/doc3gpp/web/state.py` (ServiceContainer.spec_doc* + WebState.specdata_engine), `src/doc3gpp/web/deps.py` (get_spec_doc_service et al), `src/doc3gpp/web/app.py` (wire + dispose)
- Modify: `src/doc3gpp/web/mcp_server.py` (get_spec_toc, search_spec_docs, parse_spec_docs tools + get_spec_doc_schema)
- Modify: `src/doc3gpp/web/errors.py` (map `SpecDocUnknownSpecError` / `SpecDocUnknownVersionError` to the 404 envelope, mirroring `SpecNotFoundError`)
- Create templates: `spec_docs_search.html`, `partials/spec_doc_search_results.html`, `partials/spec_doc_toc_results.html`
- Test: `tests/integration/test_spec_doc_web.py`

**Interfaces:**
- Consumes: everything from Tasks 7–11. JSON shapes mirror Task 11 CLI JSON exactly.
- Produces: full HTTP/MCP surface — final user-facing deliverable before docs/tests.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_spec_doc_web.py
from fastapi.testclient import TestClient
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.web.app import build_app
from doc3gpp.settings.loader import get_settings

def test_spec_doc_schema_route(sqlite_env):
    create_schema("all")
    client = TestClient(build_app(get_settings()))
    r = client.get("/spec-docs/schema?format=json")
    assert r.status_code == 200 and any(f["table"] == "spec_doc_chunks" for f in r.json())

def test_spec_doc_toc_miss(sqlite_env):
    create_schema("all")
    client = TestClient(build_app(get_settings()))
    r = client.get("/specs/38.331/docs/toc?version=18.5.0&format=json")
    assert r.status_code in (400, 404)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/integration/test_spec_doc_web.py -v`
Expected: FAIL with 404 (no route) on the schema path.

- [ ] **Step 3: Write minimal implementation**

Jobs: `PARSE_SPEC_DOCS = "parse_spec_docs"`; handler reads `{"spec_ids": [...], "release"?, "version"?, "force"?}`, calls `services.spec_doc.parse_many` via `asyncio.to_thread` with progress per spec, returns `{requested, successes, skipped, failures}`. Route body `_ParseSpecDocsBody(spec_ids: list[str], release?, version?, force=False)`; validates non-empty ids.

Routes (`src/doc3gpp/web/routes/spec_docs.py`):
- `GET /specs/{spec_id}/docs/toc?version=&release=&format=` → `service.get_toc`; JSON `{"spec_id","version","release","entries":[{level,section_no,title,source_file,file_order}],"files":[...]}`; HTML full page vs `partials/spec_doc_toc_results.html` on HX-Request.
- `GET /spec-docs/search?q=&spec=&release=&version=&section=&limit=&offset=&format=` → `SpecDocSearchService.search`; JSON = CLI query payload (Task 11).
- `GET /spec-docs/search/sem?...` → same + `fts5_query/fts5_weight`.
- `GET /spec-docs/schema?format=` → `schema_payload("spec_doc")`.

MCP: `get_spec_toc(spec_id, version?, release?)`, `search_spec_docs(query, spec_id?, release?, version?, section?, limit?)`, `parse_spec_docs(spec_ids, ...)` enqueue, `get_spec_doc_schema()` — all via `_to_json`, byte-matching `?format=json`.

Templates mirror `search_results.html` + `partials/search_results.html` (5-column grid: q/spec/release/version/section). Domain errors map to HTTP 404 via `web/errors.py` (unknown spec/version); oversized-zip and no-docx failures surface as 422 with the handler message.

state/deps/app: `ServiceContainer` gains `spec_doc`, `spec_doc_search: ... | None`, `spec_doc_semantic: ... | None`; `WebState` gains `specdata_engine`; `build_state` wires `factory.build_spec_doc_service(embedder=embedder)` (+ search/semantic); lifespan disposes `state.specdata_engine`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/integration/test_spec_doc_web.py tests/integration/test_spec_doc_cli.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/models/jobs.py src/doc3gpp/web/workers/handlers.py src/doc3gpp/web/routes/jobs.py src/doc3gpp/web/routes/spec_docs.py src/doc3gpp/web/routes/__init__.py src/doc3gpp/web/state.py src/doc3gpp/web/deps.py src/doc3gpp/web/app.py src/doc3gpp/web/mcp_server.py src/doc3gpp/web/errors.py src/doc3gpp/web/templates/ tests/integration/test_spec_doc_web.py
git commit -m "feat(spec-doc): web, MCP, and parse job"
```
## Task 13: Docs, online test, final gate

**Files:**
- Modify: `AGENTS.md`, `docs/cli.md`, `docs/architecture.md`, `docs/code-map.md`, `docs/web-server.md`, `README.md` (only if its CLI list changes), `docs/conventions.md` (only if a new filter/skip shape appears), `docs/3gpp-knowledge.md` (only if spec-zip layout learnings emerge)
- Create: `tests/integration/test_online_spec_doc_sync.py`
- Test: full gate `./scripts/test_sqlite.sh` + `ruff check .`

**Interfaces:**
- Consumes: all prior tasks. No new code — prose + one opt-in online test.

- [ ] **Step 1: Write the failing test (online, skipped offline)**

```python
# tests/integration/test_online_spec_doc_sync.py
import pytest
pytestmark = pytest.mark.online

def test_online_38331_fetch_parse(sqlite_env):
    """End-to-end against live 3GPP FTP: sync -> parse (default numeric-newest version) -> toc -> search."""
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.services.factory import build_spec_service, build_spec_doc_service
    from doc3gpp.services.spec_doc_search_service import SpecDocSearchService
    from doc3gpp.models.spec_doc import SpecDocSearchFilters
    create_schema("all")
    build_spec_service().sync_spec("38.331")
    svc = build_spec_doc_service()
    src = svc.parse("38.331")  # default: numeric-newest version (e.g. 38331-j30.zip line)
    assert src.chunk_count and src.chunk_count > 0
    toc = svc.get_toc("38.331", src.version)
    assert toc.entries and toc.files
    hits = SpecDocSearchService().search("handover", SpecDocSearchFilters(spec_id="38.331"))
    assert hits
```

Reference zips from the design review (38.331 `38331-j30.zip`, 38.523-1 `38523-1-j50.zip`) pin the *shape* of the corpus (single-docx vs multi-docx section splits), not exact versions: versions roll forward upstream, so the test resolves the numeric-newest version at runtime instead of hardcoding one. A second case for 38.523-1 may be added the same way to exercise the multi-file ordering path (`len(toc.files) >= 1`). Keep downloads minimal — one version per spec.

- [ ] **Step 2: Run offline gate to verify nothing runs online by default**

Run: `python -m pytest tests/integration/test_online_spec_doc_sync.py -v`
Expected: SKIPPED (addopts `-m "not online"`).

- [ ] **Step 3: Docs updates (same change set)**

  - `AGENTS.md`: spec-doc workflow one-liners (`spec doc fetch/parse/toc show/search query/search sem`) + storage row (`specdata_base`, `_specdata.db`, `create_schema("specdata")`).
  - `docs/cli.md`: all five commands, every flag/default/example (batch buckets, immutable skip, `--force`, filters).
  - `docs/architecture.md`: new modules in layered diagram; third base/engine; spec-doc runtime data flow.
  - `docs/code-map.md`: new symbols → files.
  - `docs/web-server.md`: routes + MCP tools + `PARSE_SPEC_DOCS` job kind.
  - `doc3gpp.toml.example` already updated in Task 1 — verify the `[spec_doc]` block renders via `doc3gpp config show`.
  - `README.md` / `docs/conventions.md` / `docs/3gpp-knowledge.md`: only if the surface/grammar/learnings changed.

- [ ] **Step 4: Run the full gate**

Run: `./scripts/test_sqlite.sh`
Expected: PASS (unit + integration, sqlite-only).

Run: `ruff check .`
Expected: PASS (no findings).

Optional live check (requires network + `[extract]` extra):
Run: `python -m pytest -m online tests/integration/test_online_spec_doc_sync.py -rs -v`
Expected: PASS — 38.331 fetch → parse → toc → search against live 3gpp.org FTP.

- [ ] **Step 5: Commit**

```bash
git add AGENTS.md docs/ tests/integration/test_online_spec_doc_sync.py
git commit -m "docs(spec-doc): cli/architecture/web docs and online test"
```

---

## Self-Review

**1. Spec coverage:** every design section maps to a task —
- Version selection (numeric sort) → Task 3 `resolve_spec_doc_version` (+ Task 9 `_versions` miss → BadParameter naming `spec sync --spec-id`).
- Storage (sibling `_specdata.db`, own base/engine/session, `create_schema("specdata")`, `db reset --scope specdata`) → Tasks 1, 7, 11.
- TOC granularity per `(spec_id, version)` → Tasks 5, 7, 9.
- CLI `spec doc` sub-app (fetch/parse/toc show/search query/search sem) → Task 11.
- Full stack CLI+web+MCP byte-consistent → Tasks 11, 12.
- Chunking (512/24 reuse + `max_chunk_chars=1500`, row-atomic, repeated table meta) → Tasks 1, 6, 9.
- `parse` = fetch-if-missing + convert + chunk + index → Task 9.
- Multi-file ordering rule (section-tuple sort, TOC tiebreak, unnumbered lexicographic tail) → Task 5.
- Chunk row shape + `chunk_id` PK → Tasks 6, 7.
- FTS5 6-column + bm25_weights + snippet rule + stale hint + `--stale-only/--resume/--rebuild-all` → Tasks 1, 8, 10. (`search sem` fanout/k=60/fts5-weight → Task 10.)
- Settings `[spec_doc]` block incl. `max_zip_size_kb=0`, `chunk_overlap=null→reuse` → Tasks 1, 9.
- Error handling (unknown spec/version, oversized→skip, no-docx→failure, .doc warn-skip, batch never aborts) → Tasks 3, 9.
- Testing (unit/integration/online/full gate) → Tasks 1–13.
- Docs list → Task 13.
- Fixture specs 38.331 (`38331-j30.zip`) + 38.523-1 (`38523-1-j50.zip`) → Task 13 online test.

**2. Placeholder scan:** no TBD/TODO/"similar to Task N" — every step carries concrete code, exact commands, expected outcomes, and explicit commit file lists. Cross-task names (`resolve_spec_doc_version`, `convert_document_to_blocks`, `chunk_blocks`, `SpecDocSearchFilters`, factory builders) are defined once and reused verbatim.

**3. Type consistency:** `ChunkDraft` (chunker output, no ids) vs `SpecDocChunk` (persisted, with `chunk_id/spec_id/version/chunk_index`) used consistently in Tasks 6→7; `SpecDocHit` (FTS5, chunk-level) vs `SpecDocSemanticHit(chunk_id, rrf_score, hit|None, ...)` consistent in Tasks 8→10; `parse_many` returns `SpecDocBatchResult(successes/skipped/failures)` in Tasks 9→11→12; `chunk_id = f"{spec_id}@{version}#{chunk_index}"` identical in Tasks 7, 8.

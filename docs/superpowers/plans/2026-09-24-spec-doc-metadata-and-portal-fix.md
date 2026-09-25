# Spec Document Metadata and Portal Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace split spec-document chunk metadata with overlap-aware combined `sections`/`tables` fields, expose both filters across every search surface, compact the web portal, and repair the local pre-deployment corpus once from cached ZIPs.

**Architecture:** Keep the existing layered design. Parser blocks carry recognized section/table context into a pure chunker; the specdata ORM, FTS5 index, vector join, DTOs, CLI, web, and MCP all use the new two-field contract. Because the application is not deployed, create the new schema through the existing `create_all` bootstrap and reset/reparse the development specdata database once; do not add a runtime migration or a public repair command.

**Tech Stack:** Python 3.10+, SQLAlchemy 2.0, Pydantic v2, Typer, FastAPI/Jinja2, python-docx, SQLite FTS5, optional sqlite-vec, pytest, Ruff.

## Global Constraints

- Use `sections` and `tables` exactly; remove the old chunk/hit/filter names `section_no`, `section_title`, `table_no`, `table_title`, and `section` rather than adding aliases.
- Keep TOC DTO/output fields `section_no` and `title`; the combined-field replacement applies to chunks and search hits only.
- Store each combined metadata value as newline-delimited text: one `<identifier> <title>` entry per line, `NULL` when empty.
- Include all section/table context represented in a chunk and merge metadata from text overlap into the following chunk.
- Recognize section identifiers such as `7.2A.3` and `7.2A.3A` without converting alphanumeric segments to integers.
- Keep the existing TDoc cache at `~/.cache/doc3gpp/tdocs`; the canonical spec-document cache is `~/.cache/doc3gpp/specs`.
- Do not add a runtime schema migration, compatibility columns, runtime legacy-cache fallback, public reparse command, or automatic reparsing during `db init`.
- Use native `<details>` elements without `open` for the TOC and every displayed chunk; preserve existing card styling and chunk anchors.
- MCP/HTTP JSON must remain byte-consistent with the CLI’s `sections`/`tables` payload shape.
- Write the failing test first for every behavior change, run it to observe the expected failure, then implement the smallest passing change.
- Prefix shell commands with `rtk`; use `rtk pytest`, `rtk ruff check .`, and `rtk git ...` in the normal workflow.
- Run `rtk pytest tests/unit/...` or the narrow integration test before each task commit; run `./scripts/test_sqlite.sh` and `rtk ruff check .` before completion.

---

## File Map

Files are grouped by responsibility so each task has a clear boundary:

- `src/doc3gpp/models/spec_doc.py` — chunk/hit/filter DTO contract; TOC DTOs remain split.
- `src/doc3gpp/parsers/docx_converter.py` — DOCX block extraction, alphanumeric section recognition, table-caption recognition.
- `src/doc3gpp/parsers/spec_doc.py` — TOC extraction and natural section/file ordering.
- `src/doc3gpp/parsers/spec_doc_chunker.py` — pure chunk formation and metadata propagation.
- `src/doc3gpp/settings/schema.py` — dedicated spec cache root, FTS column names, default output fields.
- `src/doc3gpp/storage/db/models.py` — fresh `spec_doc_chunks` ORM columns.
- `src/doc3gpp/storage/db/migrate.py` — fresh specdata FTS5 DDL only; no old-schema migration code.
- `src/doc3gpp/storage/repositories/spec_doc_sql.py` — chunk writes, reads, and `sections`/`tables` filters.
- `src/doc3gpp/storage/repositories/spec_doc_search_sql.py` — FTS5 indexing, filters, snippets, and hit construction.
- `src/doc3gpp/storage/repositories/spec_doc_vector_sql.py` — vector KNN joins and metadata filters.
- `src/doc3gpp/repository/protocols.py` — repository signatures matching the concrete implementations.
- `src/doc3gpp/services/spec_doc_service.py` — canonical cache paths and embedding text.
- `src/doc3gpp/services/spec_doc_semantic_service.py` — filter fan-out and metadata-aware vector indexing.
- `src/doc3gpp/cli.py`, `src/doc3gpp/web/render.py`, `src/doc3gpp/web/routes/spec_docs.py`, `src/doc3gpp/web/mcp_server.py` — public filter and serialization contracts.
- `src/doc3gpp/web/templates/partials/spec_doc_show_results.html` — collapsed TOC.
- `src/doc3gpp/web/templates/partials/spec_doc_chunks.html` — individually collapsed chunks with metadata summaries.
- `src/doc3gpp/web/templates/partials/spec_doc_search_form.html` — full-width Query and new filters.
- `src/doc3gpp/web/templates/partials/spec_doc_search_results_table.html` — combined metadata result columns.
- `src/doc3gpp/models/schema_info.py`, `doc3gpp.toml.example`, `AGENTS.md`, `README.md`, `docs/architecture.md`, `docs/cli.md`, `docs/code-map.md`, `docs/web-server.md` — schema/config/user-facing documentation.
- `tests/unit/test_spec_doc_*.py`, `tests/integration/test_spec_doc_*.py`, `tests/unit/test_web_routes.py`, `tests/integration/test_mcp_end_to_end.py` — regression coverage.
- `/tmp/opencode/spec_doc_metadata_repair.py` — disposable local-only repair helper; never add it to the repository.

---

### Task 1: Replace DTO, settings, and output contracts

**Files:**
- Modify: `src/doc3gpp/models/spec_doc.py:68-112`
- Modify: `src/doc3gpp/settings/schema.py:218-230,430-518`
- Modify: `src/doc3gpp/cli.py:4458-4468`
- Test: `tests/unit/test_spec_doc_models.py`
- Test: `tests/unit/test_spec_doc_settings.py`

**Interfaces:**
- Produces `ChunkDraft(file_order, source_file, sections, tables, text)`, `SpecDocChunk` with the same metadata fields, `SpecDocSearchFilters(spec_id, release, version, sections, tables, limit, offset)`, and `SpecDocHit(..., sections, tables, chunk_index, text, score, previews)`.
- Produces `SpecDocSettings.cache_dir` defaulting to `Path.home() / ".cache" / "doc3gpp" / "specs"`.
- Produces `_SPEC_DOC_SNIPPET_COLUMNS = ("text", "sections", "tables", "spec_id", "version", "release")` and default output fields using `sections` and `tables`.

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_spec_doc_models.py
from doc3gpp.models.spec_doc import ChunkDraft, SpecDocHit, SpecDocSearchFilters


def test_chunk_contract_has_combined_metadata_only():
    chunk = ChunkDraft(
        file_order=0,
        source_file="part.docx",
        sections="7.2A.3 Scope\n7.2A.3A Details",
        tables="Table 1 UE values\nTable 2 Timers",
        text="body",
    )
    assert chunk.sections == "7.2A.3 Scope\n7.2A.3A Details"
    assert chunk.tables == "Table 1 UE values\nTable 2 Timers"
    assert not hasattr(chunk, "section_no")
    assert not hasattr(chunk, "table_title")


def test_search_filters_use_plural_metadata_names():
    filters = SpecDocSearchFilters(sections="%handover%", tables="%UE%")
    assert filters.sections == "%handover%"
    assert filters.tables == "%UE%"
    assert not hasattr(filters, "section")


def test_spec_doc_hit_uses_combined_metadata():
    hit = SpecDocHit(
        "id", "38.331", "19.0.0", "Rel-19", "5 Scope", "Table 1 Values",
        0, "text", 0.1, {},
    )
    assert hit.sections == "5 Scope"
    assert hit.tables == "Table 1 Values"
```

```python
# tests/unit/test_spec_doc_settings.py
from pathlib import Path

from doc3gpp.settings.schema import SpecDocSettings, Settings


def test_spec_doc_cache_is_not_nested_under_tdoc_cache():
    settings = SpecDocSettings()
    assert settings.cache_dir == Path.home() / ".cache" / "doc3gpp" / "specs"
    assert Settings().cache.dir != settings.cache_dir


def test_spec_doc_output_fields_use_combined_metadata():
    assert Settings().output.fields.spec_doc[3:5] == ["sections", "tables"]
```

- [ ] **Step 2: Run tests to verify the contract fails**

Run: `rtk pytest tests/unit/test_spec_doc_models.py tests/unit/test_spec_doc_settings.py -v`

Expected: FAIL because the DTOs still define split metadata, `SpecDocSearchFilters` still has `section`, and `SpecDocSettings` has no dedicated cache root.

- [ ] **Step 3: Implement the contract**

Replace the split dataclass fields and update all constructor call sites discovered by the type/test failures. Add the `cache_dir` `Path` field to `SpecDocSettings`. Replace the six-column constant names and the default `OutputFieldsSettings.spec_doc` list. Do not modify `SpecDocTocEntry` or `SpecDocTocFile`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `rtk pytest tests/unit/test_spec_doc_models.py tests/unit/test_spec_doc_settings.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
rtk git add src/doc3gpp/models/spec_doc.py src/doc3gpp/settings/schema.py src/doc3gpp/cli.py tests/unit/test_spec_doc_models.py tests/unit/test_spec_doc_settings.py
rtk git commit -m "feat(spec-doc): replace split chunk metadata contract"
```

---

### Task 2: Recognize alphanumeric sections and table captions

**Files:**
- Modify: `src/doc3gpp/parsers/docx_converter.py:339-435`
- Modify: `src/doc3gpp/parsers/spec_doc.py:60-95`
- Test: `tests/unit/test_spec_doc_blocks.py`
- Test: `tests/unit/test_spec_doc_parser.py`

**Interfaces:**
- `HeadingBlock.section_no` accepts identifiers such as `7.2A.3` and `7.2A.3A`.
- `TableBlock.table_no`/`table_title` continue to support the block-level parser contract; chunk serialization happens in Task 3.
- `_first_section` returns a comparable natural sort key without calling `int()` on an alphanumeric segment.

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_spec_doc_blocks.py
def test_heading_accepts_alphanumeric_section_suffixes():
    blocks = _convert_docx_with_paragraphs(
        [
            ("Heading 1", "7.2A.3 Scope"),
            ("Heading 2", "7.2A.3A Extended details"),
        ]
    )
    headings = [block for block in blocks if isinstance(block, HeadingBlock)]
    assert [(h.section_no, h.title) for h in headings] == [
        ("7.2A.3", "Scope"),
        ("7.2A.3A", "Extended details"),
    ]


def test_table_caption_accepts_3gpp_identifier_and_separator():
    blocks = _convert_docx_with_table_caption("Table 7.2A.3A: UE capability values")
    table = next(block for block in blocks if isinstance(block, TableBlock))
    assert table.table_no == "7.2A.3A"
    assert table.table_title == "UE capability values"
```

```python
# tests/unit/test_spec_doc_parser.py
def test_order_numbered_files_handles_alphanumeric_sections():
    files = [
        _f("late.docx", ("7.2A.3A", "Later")),
        _f("early.docx", ("7.2A.3", "Earlier")),
        _f("numeric.docx", ("7.10", "Numeric")),
    ]
    ordered = order_spec_files(files)
    assert [f.source_file for f in ordered] == [
        "early.docx", "late.docx", "numeric.docx",
    ]


def test_toc_preserves_alphanumeric_section_number():
    entries, _ = extract_spec_toc(
        [_f("part.docx", ("7.2A.3A", "Extended details"))]
    )
    assert entries[0].section_no == "7.2A.3A"
```

The test helpers should build real in-memory DOCX documents using `python-docx`, as the existing block tests do; do not test only the regular expression in isolation.

- [ ] **Step 2: Run tests to verify the parser failures**

Run: `rtk pytest tests/unit/test_spec_doc_blocks.py tests/unit/test_spec_doc_parser.py -v`

Expected: FAIL because the current section regex rejects letter suffixes and `_first_section` attempts `int("2A")`.

- [ ] **Step 3: Implement section and caption parsing**

Update `_SECTION_RE` to allow each dotted component to contain letters after/beside digits, while still requiring the identifier to start with a digit and be followed by whitespace. Add a caption parser that accepts the 3GPP table identifier forms and the colon/dash/whitespace separators observed in the source, trims/collapses caption whitespace, and returns `(table_no, title)`. Keep caption paragraphs in the block stream exactly as the existing converter does.

In `spec_doc.py`, add a natural sort-key helper that tokenizes each dotted section component into numeric and alphabetic pieces, compares numeric pieces numerically and alphabetic pieces case-insensitively, and returns a tuple safe for Python tuple ordering. Use that helper in `_first_section`/`order_spec_files`; preserve the front-matter and unnumbered-file ordering rules.

- [ ] **Step 4: Run tests to verify they pass**

Run: `rtk pytest tests/unit/test_spec_doc_blocks.py tests/unit/test_spec_doc_parser.py -v`

Expected: PASS, including the existing numeric-section and TOC tests.

- [ ] **Step 5: Commit**

```bash
rtk git add src/doc3gpp/parsers/docx_converter.py src/doc3gpp/parsers/spec_doc.py tests/unit/test_spec_doc_blocks.py tests/unit/test_spec_doc_parser.py
rtk git commit -m "fix(spec-doc): recognize alphanumeric sections and tables"
```

---

### Task 3: Make chunk metadata ordered, multi-valued, and overlap-aware

**Files:**
- Modify: `src/doc3gpp/parsers/spec_doc_chunker.py`
- Modify: `src/doc3gpp/models/spec_doc.py` if constructor ordering needs adjustment after Task 1
- Test: `tests/unit/test_spec_doc_chunker.py`

**Interfaces:**
- `chunk_blocks(blocks, chunk_size=512, chunk_overlap=24, max_chunk_chars=1500, *, file_order=0, source_file="") -> list[ChunkDraft]` remains the public pure function.
- Every returned draft has `sections` and `tables` as newline-delimited strings or `None`.

- [ ] **Step 1: Write failing tests**

```python
from doc3gpp.parsers.docx_converter import HeadingBlock, ParagraphBlock, TableBlock
from doc3gpp.parsers.spec_doc_chunker import chunk_blocks


def test_chunk_collects_sections_spanning_one_chunk():
    blocks = [
        HeadingBlock(1, "7.2A.3", "Scope", "# 7.2A.3 Scope"),
        ParagraphBlock("alpha beta"),
        HeadingBlock(2, "7.2A.3A", "Details", "## 7.2A.3A Details"),
        ParagraphBlock("gamma delta"),
    ]
    chunks = chunk_blocks(blocks, chunk_size=20, chunk_overlap=0, max_chunk_chars=1500)
    assert chunks[0].sections == "7.2A.3 Scope\n7.2A.3A Details"


def test_chunk_collects_multiple_tables():
    blocks = [
        TableBlock("| A |\n| --- |\n| 1 |", "7.2A.3", "First values"),
        TableBlock("| B |\n| --- |\n| 2 |", "7.2A.3A", "Second values"),
    ]
    chunks = chunk_blocks(blocks, chunk_size=100, chunk_overlap=0, max_chunk_chars=1500)
    assert chunks[0].tables == "7.2A.3 First values\n7.2A.3A Second values"


def test_overlap_carries_previous_section_metadata():
    blocks = [
        HeadingBlock(1, "5", "Previous", "# 5 Previous"),
        ParagraphBlock("one two three four"),
        HeadingBlock(1, "6", "Current", "# 6 Current"),
        ParagraphBlock("five six seven eight"),
    ]
    chunks = chunk_blocks(blocks, chunk_size=4, chunk_overlap=2, max_chunk_chars=1500)
    assert chunks[1].text.startswith("three four")
    assert "5 Previous" in (chunks[1].sections or "")
    assert "6 Current" in (chunks[1].sections or "")


def test_empty_metadata_is_none():
    chunks = chunk_blocks([ParagraphBlock("plain text")], chunk_overlap=0)
    assert chunks[0].sections is None
    assert chunks[0].tables is None
```

Update the existing scalar-field assertions in `tests/unit/test_spec_doc_chunker.py` to assert the combined fields instead of `section_no`/`table_no`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `rtk pytest tests/unit/test_spec_doc_chunker.py -v`

Expected: FAIL because the current chunker keeps only the first unit’s scalar metadata and overlap changes text without changing metadata.

- [ ] **Step 3: Implement the chunker**

Represent each source unit internally as `(text, sections: tuple[str, ...], tables: tuple[str, ...], file_order, source_file, atomic)`. Use the current heading context for paragraphs and table blocks. A heading contributes its combined `<section_no> <title>` entry; a table contributes its combined `<table_no> <table_title>` entry and the current section. When units are grouped, maintain ordered deduplicating lists for the current chunk and serialize them with `"\n".join(...)` on flush.

Retain the existing paragraph/sentence/token/character limits and atomic-table behavior. After text overlap is prepended from chunk `i - 1` to chunk `i`, merge the previous chunk’s `sections` and `tables` entries into the next chunk in order, preserving the current chunk’s entries and removing duplicates. This explicitly retains the preceding section when overlap starts with text from that section.

- [ ] **Step 4: Run tests to verify they pass**

Run: `rtk pytest tests/unit/test_spec_doc_chunker.py -v`

Expected: PASS, including existing size, file-tag, sentence, and table-row tests.

- [ ] **Step 5: Commit**

```bash
rtk git add src/doc3gpp/parsers/spec_doc_chunker.py src/doc3gpp/models/spec_doc.py tests/unit/test_spec_doc_chunker.py
rtk git commit -m "feat(spec-doc): preserve multi-section chunk context"
```

---

### Task 4: Rebuild the fresh specdata relational schema and repository contract

**Files:**
- Modify: `src/doc3gpp/storage/db/models.py:559-575`
- Modify: `src/doc3gpp/storage/db/migrate.py:371-414`
- Modify: `src/doc3gpp/repository/protocols.py:449-467`
- Modify: `src/doc3gpp/storage/repositories/spec_doc_sql.py:166-347`
- Test: `tests/integration/test_spec_doc_repo.py`
- Test: `tests/integration/test_spec_doc_search_repo.py` fixture constructors

**Interfaces:**
- `spec_doc_chunks` has `sections TEXT` and `tables TEXT`; it has no `section_no`, `section_title`, `table_no`, or `table_title` columns.
- `SpecDocRepository.list_chunks(..., sections=None, tables=None, ...)` applies rich text filters to the combined columns.
- `replace_chunks` and `_orm_to_chunk` round-trip the new DTO fields without exposing ORM attributes.

- [ ] **Step 1: Write failing integration tests**

```python
def test_spec_doc_chunk_schema_has_only_combined_metadata(sqlite_env):
    from sqlalchemy import inspect
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.db.session import get_specdata_engine

    create_schema("specdata")
    columns = {
        column["name"]
        for column in inspect(get_specdata_engine()).get_columns("spec_doc_chunks")
    }
    assert {"sections", "tables"}.issubset(columns)
    assert not {"section_no", "section_title", "table_no", "table_title"} & columns


def test_replace_and_list_chunks_round_trip_metadata(sqlite_env):
    repo = SQLAlchemySpecDocRepository()
    repo.replace_chunks(
        "38.331", "19.0.0", release="Rel-19",
        drafts=[ChunkDraft(0, "part.docx", "5 Scope\n6 Details", "Table 1 Values", "body")],
    )
    rows = repo.list_chunks(
        "38.331", version="19.0.0", sections="%Details%", tables="%Values%",
    )
    assert rows[0].sections == "5 Scope\n6 Details"
    assert rows[0].tables == "Table 1 Values"
```

Update existing repository fixture constructors to pass `sections` and `tables`.

- [ ] **Step 2: Run tests to verify the fresh-schema failures**

Run: `rtk pytest tests/integration/test_spec_doc_repo.py tests/integration/test_spec_doc_search_repo.py -v`

Expected: FAIL because the ORM and repository still reference old columns and the test database may be created with the old DDL.

- [ ] **Step 3: Implement the fresh schema and repository**

Replace the four ORM columns with two `Text` columns. Change the specdata FTS DDL in `migrate.py` to the six indexed columns `text, sections, tables, spec_id, version, release`; do not add a migration branch for an already-existing old table. Rename the Protocol parameters and update `replace_chunks`, `list_chunks`, `_orm_to_chunk`, and result construction.

Implement one reusable repository text-filter helper for a single combined column. Preserve `null`, `not-null`, positive LIKE, and negated `!` semantics. Apply it independently to `sections` and `tables` with `AND` semantics when both filters are supplied.

- [ ] **Step 4: Run tests to verify they pass**

Run: `rtk pytest tests/integration/test_spec_doc_repo.py tests/integration/test_spec_doc_search_repo.py -v`

Expected: PASS against a fresh specdata database.

- [ ] **Step 5: Commit**

```bash
rtk git add src/doc3gpp/storage/db/models.py src/doc3gpp/storage/db/migrate.py src/doc3gpp/repository/protocols.py src/doc3gpp/storage/repositories/spec_doc_sql.py tests/integration/test_spec_doc_repo.py tests/integration/test_spec_doc_search_repo.py
rtk git commit -m "feat(spec-doc): use combined metadata columns"
```

---

### Task 5: Update FTS5, vector filtering, and semantic metadata

**Files:**
- Modify: `src/doc3gpp/storage/repositories/spec_doc_search_sql.py:148-355`
- Modify: `src/doc3gpp/storage/repositories/spec_doc_vector_sql.py:294-350`
- Modify: `src/doc3gpp/services/spec_doc_semantic_service.py:80-170`
- Test: `tests/integration/test_spec_doc_search_repo.py`
- Test: `tests/integration/test_spec_doc_search_service.py`

**Interfaces:**
- FTS5 index rows contain normalized `sections` and `tables` columns and return original combined metadata from the relational join.
- `SpecDocSearchFilters.sections` and `.tables` constrain both FTS5 and vector KNN paths.
- Semantic hybrid fan-out copies both filters into its FTS filter object; vector-only queries pass both filters directly to `knn`.

- [ ] **Step 1: Write failing tests**

```python
def test_fts_search_returns_and_filters_combined_table_metadata(sqlite_env):
    repo = SQLAlchemySpecDocRepository()
    repo.replace_chunks(
        "38.331", "19.0.0", release="Rel-19",
        drafts=[
            ChunkDraft(0, "a.docx", "5 Scope", "Table 1 UE values", "handover UE"),
            ChunkDraft(0, "a.docx", "6 Other", "Table 2 Timers", "handover timers"),
        ],
    )
    fts = SQLAlchemySpecDocSearchRepository()
    fts.upsert_for_version("38.331", "19.0.0")
    hits = fts.search(
        "handover", SpecDocSearchFilters(tables="%UE values%", limit=20)
    )
    assert [hit.tables for hit in hits] == ["Table 1 UE values"]
    assert hits[0].sections == "5 Scope"
```

```python
def test_semantic_hybrid_copies_sections_and_tables_filters():
    fts = RecordingFTSService()
    vector = RecordingVectorRepository()
    service = SpecDocSemanticService(
        fts5_service=fts, embedder=FakeEmbedder(), vector_repo=vector,
        settings=fake_settings(fanout_multiplier=2),
    )
    service.search(
        "handover", fts5_query="handover",
        filters=SpecDocSearchFilters(sections="%5 Scope%", tables="%UE%"),
        limit=4, fts5_weight=0.5,
    )
    assert fts.filters.sections == "%5 Scope%"
    assert fts.filters.tables == "%UE%"
    assert vector.filters.sections == "%5 Scope%"
    assert vector.filters.tables == "%UE%"
```

Update existing search-service fixture constructors and assertions to use the new hit fields. Add a repository-level vector test when sqlite-vec is available that filters by `tables` through the `spec_doc_chunks` join.

- [ ] **Step 2: Run tests to verify they fail**

Run: `rtk pytest tests/integration/test_spec_doc_search_repo.py tests/integration/test_spec_doc_search_service.py -v`

Expected: FAIL because FTS DDL/indexing still names `section_title`/`table_title`, the SQL join selects removed columns, and semantic filters still use `section`.

- [ ] **Step 3: Implement FTS5 and vector changes**

Use `_SPEC_DOC_SNIPPET_COLUMNS` for the FTS DDL, `bm25`, and snippet column IDs. Index `normalize_query(chunk.sections or "")` and `normalize_query(chunk.tables or "")`. Select `c.sections` and `c.tables` and construct `SpecDocHit` with those values. Apply `_fts5_text_filter` to both FTS columns using parameters `sections` and `tables`.

In `spec_doc_vector_sql.py`, join `spec_doc_chunks` whenever `release`, `sections`, or `tables` is present. Apply the existing rich text semantics to each combined column; bind separate parameter names so both filters can coexist. Keep exact `spec_id`/`version` filtering and sqlite-vec KNN ordering unchanged.

In `SpecDocSemanticService`, copy `sections` and `tables` into the FTS fan-out filter. Build vector embedding text from `sections`, `tables`, and `text` so metadata is available to the embedding model. Preserve vector-only `SpecDocSemanticHit.hit is None` behavior.

- [ ] **Step 4: Run tests to verify they pass**

Run: `rtk pytest tests/integration/test_spec_doc_search_repo.py tests/integration/test_spec_doc_search_service.py -v`

Expected: PASS; when sqlite-vec is unavailable, its tests are skipped using the repository’s existing marker/fixture convention.

- [ ] **Step 5: Commit**

```bash
rtk git add src/doc3gpp/storage/repositories/spec_doc_search_sql.py src/doc3gpp/storage/repositories/spec_doc_vector_sql.py src/doc3gpp/services/spec_doc_semantic_service.py tests/integration/test_spec_doc_search_repo.py tests/integration/test_spec_doc_search_service.py
rtk git commit -m "feat(spec-doc): filter search by sections and tables"
```

---

### Task 6: Correct the spec-document cache and parse embedding text

**Files:**
- Modify: `src/doc3gpp/services/spec_doc_service.py:93-155,181-201,359-376`
- Modify: `src/doc3gpp/repository/protocols.py:458-467` if not completed in Task 4
- Test: `tests/unit/test_spec_doc_service_reads.py`
- Test: `tests/integration/test_spec_doc_service.py`

**Interfaces:**
- `SpecDocService(cache_dir=...)` treats an explicit `cache_dir` as the spec-document root itself.
- Default paths are `<spec_doc.cache_dir>/zips/<spec>/<version>.zip` and `<spec_doc.cache_dir>/markdown/<spec>/<version>/...`.
- `SpecDocService.list_chunks(..., sections=None, tables=None, ...)` passes both filters to the repository.

- [ ] **Step 1: Write failing tests**

```python
def test_spec_doc_service_uses_dedicated_cache_root(tmp_path, fake_settings, fake_repo):
    service = SpecDocService(
        spec_repo=FakeSpecRepository(), doc_repo=fake_repo,
        settings=fake_settings(spec_doc_cache_dir=tmp_path / "specs"),
    )
    assert service._zip_path("36.508", "19.2.0") == (
        tmp_path / "specs" / "zips" / "36.508" / "19.2.0.zip"
    )
    assert "tdocs" not in str(service._zip_path("36.508", "19.2.0"))


def test_service_lists_chunks_with_plural_filters(fake_repo, fake_settings):
    service = SpecDocService(spec_repo=FakeSpecRepository(), doc_repo=fake_repo,
                             settings=fake_settings())
    service.list_chunks(
        "36.508", version="19.2.0", sections="%7.2A%", tables="%UE%",
    )
    assert fake_repo.last_list_kwargs["sections"] == "%7.2A%"
    assert fake_repo.last_list_kwargs["tables"] == "%UE%"
```

The existing test settings helper should construct `Settings(spec_doc={"cache_dir": ...})` (or assign the nested model directly); do not invent a top-level `spec_doc_cache_dir` setting.

Add an embedding-text assertion that a draft with `sections="5 Scope"` and `tables="Table 1 Values"` produces text containing both metadata values before the chunk body.

- [ ] **Step 2: Run tests to verify they fail**

Run: `rtk pytest tests/unit/test_spec_doc_service_reads.py tests/integration/test_spec_doc_service.py -v`

Expected: FAIL because the service currently appends `/specs` to `cache.dir`, accepts only `section`, and embeds only split section fields.

- [ ] **Step 3: Implement canonical cache handling**

Select `cache_dir` when explicitly injected; otherwise use `self._settings.spec_doc.cache_dir`. Remove the extra `/specs` path component from `_zip_path` and `_markdown_dir`. Rename the public list/read filter arguments to `sections` and `tables`. Update embedding text to combine non-empty `sections`, `tables`, and `text`, separated by newlines.

Do not add a production fallback to `~/.cache/doc3gpp/tdocs/specs`; the disposable repair in Task 10 copies existing files into the canonical location before parsing.

- [ ] **Step 4: Run tests to verify they pass**

Run: `rtk pytest tests/unit/test_spec_doc_service_reads.py tests/integration/test_spec_doc_service.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
rtk git add src/doc3gpp/services/spec_doc_service.py src/doc3gpp/repository/protocols.py tests/unit/test_spec_doc_service_reads.py tests/integration/test_spec_doc_service.py
rtk git commit -m "fix(spec-doc): use dedicated document cache root"
```

---

### Task 7: Rename CLI, web, MCP, and JSON filters/fields

**Files:**
- Modify: `src/doc3gpp/cli.py:4458-4595,4733-4927`
- Modify: `src/doc3gpp/web/render.py:77-116`
- Modify: `src/doc3gpp/web/routes/spec_docs.py:80-428`
- Modify: `src/doc3gpp/web/mcp_server.py:632-680`
- Test: `tests/unit/test_cli_search.py` or a new `tests/unit/test_spec_doc_cli.py`
- Test: `tests/unit/test_web_routes.py`
- Test: `tests/integration/test_mcp_end_to_end.py`

**Interfaces:**
- CLI flags are `--sections` and `--tables`; `--section` is no longer accepted.
- HTTP query parameters/forms are `sections` and `tables`; JSON emits `sections` and `tables`.
- MCP tool arguments are `sections` and `tables`.
- Detail-page chunk filtering uses `sections` and `tables` consistently with search.

- [ ] **Step 1: Write failing tests**

```python
def test_spec_doc_cli_json_uses_combined_metadata(runner, monkeypatch):
    hit = SimpleNamespace(
        chunk_id="38.331@19.0.0#0", spec_id="38.331", version="19.0.0",
        release="Rel-19", sections="5 Scope", tables="Table 1 Values",
        chunk_index=0, text="handover", score=0.1, previews={},
    )
    monkeypatch.setattr("doc3gpp.cli.build_spec_doc_search_service", lambda: FakeSearch([hit]))
    result = runner.invoke(app, ["spec", "doc", "search", "query", "handover", "--format", "json"])
    payload = json.loads(result.stdout)
    assert payload[0]["sections"] == "5 Scope"
    assert payload[0]["tables"] == "Table 1 Values"
    assert "section_no" not in payload[0]
```

```python
def test_web_spec_doc_search_accepts_sections_and_tables(client, fake_services):
    response = client.get(
        "/spec-docs/search?format=json&q=handover&sections=%255.1%25&tables=%25UE%25"
    )
    assert response.status_code == 200
    filters = fake_services.spec_doc_search.last_filters
    assert filters.sections == "%5.1%"
    assert filters.tables == "%UE%"
```

Add MCP assertions that the registered tool schema exposes `sections` and `tables`, and the returned nested hit uses those keys. Add a negative CLI test that `--section` is rejected.

- [ ] **Step 2: Run tests to verify they fail**

Run: `rtk pytest tests/unit/test_web_routes.py tests/integration/test_mcp_end_to_end.py -k 'spec_doc or spec_docs' -v`

Expected: FAIL because the current route, CLI, MCP, and serializers use the singular `section` and split output keys.

- [ ] **Step 3: Implement the public contract**

Replace `SPEC_DOC_LIST_FIELDS`, `_spec_doc_hit_to_dict`, markdown render branches, CLI `SpecDocSearchFilters` construction, and both CLI command option declarations. Rename route parameters and context keys, pass both filters into FTS5 and semantic services, and preserve pagination/query values in generated URLs.

Update `spec_doc_hit_to_json` and semantic nested rendering in `web/render.py`. Update MCP descriptions and function signatures, then construct `SpecDocSearchFilters(sections=sections, tables=tables, ...)`. Keep the TOC JSON path unchanged.

- [ ] **Step 4: Run tests to verify they pass**

Run: `rtk pytest tests/unit/test_web_routes.py tests/integration/test_mcp_end_to_end.py -k 'spec_doc or spec_docs' -v`

Expected: PASS, including existing HTTP/MCP byte-parity assertions.

- [ ] **Step 5: Commit**

```bash
rtk git add src/doc3gpp/cli.py src/doc3gpp/web/render.py src/doc3gpp/web/routes/spec_docs.py src/doc3gpp/web/mcp_server.py tests/unit/test_web_routes.py tests/integration/test_mcp_end_to_end.py tests/unit/test_spec_doc_cli.py
rtk git commit -m "feat(spec-doc): expose plural metadata filters"
```

---

### Task 8: Implement the collapsed portal and full-width search layout

**Files:**
- Modify: `src/doc3gpp/web/templates/partials/spec_doc_show_results.html:29-68`
- Modify: `src/doc3gpp/web/templates/partials/spec_doc_chunks.html:1-48`
- Modify: `src/doc3gpp/web/templates/partials/spec_doc_search_form.html:9-43`
- Modify: `src/doc3gpp/web/templates/partials/spec_doc_search_results_table.html:1-56`
- Test: `tests/unit/test_web_routes.py`
- Test: `tests/integration/test_spec_doc_web.py`

**Interfaces:**
- TOC is a card containing `<details class="..."` without `open`; clicking the summary toggles the existing TOC body.
- Every chunk is a card-shaped `<details>` without `open`, keeps `id="chunk-<index>"`, and has a summary containing chunk ID, source file, sections, and tables.
- Spec Docs Query input uses `span-5` in normal and semantic modes; semantic FTS5 query is separate.
- Search results show combined section and table metadata with preserved line breaks and link to the version chunk anchor.

- [ ] **Step 1: Write failing template tests**

```python
def test_spec_doc_detail_renders_collapsed_toc_and_each_chunk(client, parsed_spec_doc):
    response = client.get("/specs/38.331/docs?version=19.0.0")
    assert response.status_code == 200
    body = response.text
    assert '<details class="spec-doc-toc-details"' in body
    assert '<details class="card spec-doc-chunk" id="chunk-0">' in body
    assert "<summary>" in body
    assert "5 Scope" in body
    assert "Table 1 Values" in body
    assert 'id="chunk-0" open' not in body


def test_spec_doc_search_query_spans_full_row(client):
    response = client.get("/spec-docs/search")
    assert response.status_code == 200
    assert 'label class="span-5">Query' in response.text
    assert 'name="sections"' in response.text
    assert 'name="tables"' in response.text
```

Add a result-table assertion that `sections` and `tables` appear as separate metadata columns and that a multi-line value is rendered without losing its line breaks.

- [ ] **Step 2: Run tests to verify they fail**

Run: `rtk pytest tests/unit/test_web_routes.py tests/integration/test_spec_doc_web.py -k 'doc or search' -v`

Expected: FAIL because the templates currently use `<section>/<article>`, old metadata names, and a semantic Query width of `span-3`.

- [ ] **Step 3: Implement the templates**

Wrap the existing TOC heading/body in a card-preserving `<details>` structure with a summary that reads “Table of contents”. Change each chunk `<article>` to `<details class="card spec-doc-chunk" id="chunk-...">`; use a summary containing the chunk ID, source file, and conditional metadata blocks. Render metadata values with CSS `white-space: pre-line` or an equivalent `<span>` so newline-delimited entries remain readable. Keep `<pre>{{ chunk.text }}</pre>` in the details body.

Change the search form’s semantic Query class to `span-5`, place FTS5 query in a subsequent full-width row, rename Section to Sections, add Tables, and keep the existing limit/weight controls. Update pagination URLs to use `sections` and `tables`.

Add a `Tables` result column and replace reconstructed split section output with `inner.sections`/`inner.tables`. Keep vector-only semantic rows safe when `inner` is absent and preserve the existing matching-fields `<details>` behavior.

- [ ] **Step 4: Run tests to verify they pass**

Run: `rtk pytest tests/unit/test_web_routes.py tests/integration/test_spec_doc_web.py -k 'doc or search' -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
rtk git add src/doc3gpp/web/templates/partials/spec_doc_show_results.html src/doc3gpp/web/templates/partials/spec_doc_chunks.html src/doc3gpp/web/templates/partials/spec_doc_search_form.html src/doc3gpp/web/templates/partials/spec_doc_search_results_table.html tests/unit/test_web_routes.py tests/integration/test_spec_doc_web.py
rtk git commit -m "feat(web): collapse spec docs and widen search query"
```

---

### Task 9: Update static schema, configuration example, and project documentation

**Files:**
- Modify: `src/doc3gpp/models/schema_info.py:283-326`
- Modify: `src/doc3gpp/data/doc3gpp.toml.example` (`[spec_doc]` block)
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `docs/architecture.md`
- Modify: `docs/cli.md`
- Modify: `docs/code-map.md`
- Modify: `docs/web-server.md`
- Test: `tests/unit/test_schema_info.py`
- Test: `tests/unit/test_settings_config_file.py`

**Interfaces:**
- `spec_doc_chunks` schema documentation lists `sections` and `tables`, not deprecated fields.
- The example TOML documents `spec_doc.cache_dir` and the combined metadata/search filters.
- User-facing docs describe the collapsed portal, full-width Query, alphanumeric sections, one-line-per-entry metadata, and one-time pre-deployment repair.

- [ ] **Step 1: Write failing tests**

```python
def test_spec_doc_schema_lists_combined_chunk_fields():
    payload = schema_payload("spec_doc")
    fields = {row["field"] for table in payload for row in table["fields"]}
    assert "sections" in fields
    assert "tables" in fields
    assert not {"section_no", "section_title", "table_no", "table_title"} & fields
```

```python
def test_spec_doc_config_example_contains_dedicated_cache_dir():
    text = Path("src/doc3gpp/data/doc3gpp.toml.example").read_text()
    assert "cache_dir" in text
    assert "sections" in text
    assert "tables" in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `rtk pytest tests/unit/test_schema_info.py tests/unit/test_settings_config_file.py -v`

Expected: FAIL because the static registry and packaged example still document split fields and no dedicated cache root.

- [ ] **Step 3: Update documentation and static contracts**

Replace the four chunk field entries in `RESOURCE_SCHEMAS["spec_doc"]` with `sections` and `tables`, describing newline-delimited combined identifier/title values. Add the cache setting to the example TOML with the canonical default. Update command references from `--section` to `--sections`, add `--tables`, and describe the no-migration one-time local repair without documenting a public repair command.

Update architecture/code-map entries for parser metadata, search filters, cache paths, and the web templates. Keep the existing TDoc cache documentation unchanged except where it distinguishes the dedicated spec-doc cache.

- [ ] **Step 4: Run tests to verify they pass**

Run: `rtk pytest tests/unit/test_schema_info.py tests/unit/test_settings_config_file.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
rtk git add src/doc3gpp/models/schema_info.py src/doc3gpp/data/doc3gpp.toml.example README.md AGENTS.md docs/architecture.md docs/cli.md docs/code-map.md docs/web-server.md tests/unit/test_schema_info.py tests/unit/test_settings_config_file.py
rtk git commit -m "docs: document combined spec doc metadata"
```

---

### Task 10: Perform the one-time local database repair

**Files:**
- Create temporarily outside the repository: `/tmp/opencode/spec_doc_metadata_repair.py`
- Do not commit the temporary helper.
- Runtime code consumed: `src/doc3gpp/services/spec_doc_service.py`, `src/doc3gpp/storage/db/migrate.py`, `src/doc3gpp/cli.py` after Tasks 1–9.

**Interfaces:**
- Reads existing parsed pairs before reset.
- Copies legacy ZIPs from `<settings.cache.dir>/specs/zips/<spec>/<version>.zip` to `<settings.spec_doc.cache_dir>/zips/<spec>/<version>.zip` without deleting the originals.
- Recreates only the specdata database using the new schema.
- Parses every captured pair with a copied ZIP, records per-pair success/failure, and exits non-zero only after attempting all pairs if any pair failed.

- [ ] **Step 1: Stop writers and create a database backup**

Stop the local web/server worker before touching the specdata file. Use SQLite’s online backup API so WAL content is included:

```bash
rtk proxy python - <<'PY'
import sqlite3
from pathlib import Path

source = Path.home() / ".local/share/doc3gpp/doc3gpp_specdata.db"
backup = source.with_name(source.name + ".before-spec-doc-metadata.bak")
with sqlite3.connect(source) as src, sqlite3.connect(backup) as dst:
    src.backup(dst)
print(backup)
PY
```

Expected: the backup file exists and can be opened with SQLite before proceeding.

- [ ] **Step 2: Capture parsed pairs and copy legacy ZIPs**

The temporary helper must query `spec_doc_sources` for `(spec_id, version, release, ftp_url)` where `parsed_at IS NOT NULL`, then for each pair copy the old path `<settings.cache.dir>/specs/zips/<safe_spec>/<safe_version>.zip` to `<settings.spec_doc.cache_dir>/zips/<safe_spec>/<safe_version>.zip`. Use the same `_safe_part` convention as `SpecDocService`; compare source and destination paths before copying. Print `copied`, `missing`, and `already canonical` lists.

The current development database is expected to report `36.508@19.2.0`; do not invent a `38.508` pair. If the live query reports a different set, use the reported set and mention it in the repair output.

- [ ] **Step 3: Reset only specdata with the new schema**

After the pair list and ZIP copies are safely written, run:

```bash
rtk doc3gpp db reset --scope specdata --yes
```

Expected: only `/home/jerry/.local/share/doc3gpp/doc3gpp_specdata.db` is replaced. The main and testcase database files are not touched. Verify immediately with `rtk doc3gpp db check --scope specdata`.

- [ ] **Step 4: Reparse every cached pair through the existing service**

The temporary helper should instantiate the normal configured spec-document service and call `parse(spec_id, version=version, force=False)` for every captured pair. With a fresh source table and a canonical cached ZIP, the service takes its cache-hit path, records the download metadata, and performs the parse without network access; `force=True` must not be used because the current service intentionally re-downloads when forced. Catch each exception, print `ok <spec>@<version>` or `failed <spec>@<version>: <error>`, and continue to the next pair. The normal parse hooks rebuild FTS5 and, when the semantic/vector stack is configured, vector rows.

- [ ] **Step 5: Assert the repaired schema and metadata**

Run a disposable SQLite assertion script that checks:

```python
columns = {row[1] for row in conn.execute("PRAGMA table_info(spec_doc_chunks)")}
assert {"sections", "tables"} <= columns
assert not {"section_no", "section_title", "table_no", "table_title"} & columns

chunk_count, metadata_count = conn.execute(
    "SELECT COUNT(*), COUNT(*) FILTER (WHERE tables IS NOT NULL OR sections IS NOT NULL) "
    "FROM spec_doc_chunks WHERE spec_id = '36.508' AND version = '19.2.0'"
).fetchone()
assert chunk_count > 0
assert metadata_count > 0

source_count = conn.execute(
    "SELECT chunk_count FROM spec_doc_sources WHERE spec_id='36.508' AND version='19.2.0'"
).fetchone()[0]
assert source_count == chunk_count
```

Also assert the FTS row count for the pair equals `chunk_count`, and query for representative section/table metadata containing a `Table` entry. If vector support is installed and enabled, assert the vector row count equals the chunk count; otherwise record that vector indexing was unavailable rather than failing the relational/FTS repair.

- [ ] **Step 6: Remove the temporary helper and record the repair result**

Delete `/tmp/opencode/spec_doc_metadata_repair.py` after successful verification. Keep the `.before-spec-doc-metadata.bak` backup until the final full test suite and live portal check complete; do not add either artifact to Git.

---

### Task 11: Full verification and regression cleanup

**Files:**
- Modify: any test files still referring to removed names, identified by the searches below.
- No new production files unless a prior task exposes a concrete failure.

- [ ] **Step 1: Find stale split-field references**

Run:

```bash
rtk grep -n 'section_no|section_title|table_no|table_title|--section|section=' src tests docs README.md AGENTS.md
```

Remove or update every match that refers to chunk/hit/search behavior. Retain only TOC-specific `section_no` references and historical design/spec text that explicitly describes the old implementation as superseded.

- [ ] **Step 2: Run focused parser/storage/search/web tests**

```bash
rtk pytest tests/unit/test_spec_doc_*.py tests/integration/test_spec_doc_*.py tests/unit/test_web_routes.py tests/integration/test_mcp_end_to_end.py -v
```

Expected: PASS, with only the repository’s existing optional-dependency skips.

- [ ] **Step 3: Run the complete offline suite**

```bash
./scripts/test_sqlite.sh
```

Expected: all offline tests pass; record the exact pass/skip counts.

- [ ] **Step 4: Run lint and whitespace checks**

```bash
rtk ruff check .
rtk git diff --check main..HEAD
```

Expected: both commands exit successfully.

- [ ] **Step 5: Smoke-test the repaired portal**

Start the local web app using the project’s normal development command and verify:

1. `/specs/36.508/docs?version=19.2.0` shows a collapsed TOC and independently collapsed chunk cards.
2. Expanding a chunk summary shows its chunk ID, source file, all section/table lines, and chunk text.
3. `/spec-docs/search` renders Query across the full row and exposes Sections and Tables.
4. `/spec-docs/search?format=json&q=handover&tables=%25Table%25` returns `tables` metadata and no split keys.
5. `/spec-docs/search/sem` passes the same filters when the semantic stack is enabled.

- [ ] **Step 6: Inspect final repository state**

```bash
rtk git status --short --untracked-files=all
rtk git log --oneline -12
```

Expected: only intentional implementation/design/plan changes are present; no database, cache, backup, or temporary repair artifacts are tracked.

---

## Plan Self-Review

- **Spec coverage:** UI collapse and full-width Query are Task 8; parser recognition and TOC ordering are Task 2; multi-value and overlap metadata are Task 3; new ORM/DDL/repository contract is Task 4; FTS/vector filtering is Task 5; dedicated cache is Task 6; public CLI/web/MCP names and JSON are Task 7; static schema/docs are Task 9; one-time repair and `36.508@19.2.0` verification are Task 10; full regression verification is Task 11.
- **No runtime migration:** Task 4 changes fresh DDL only, and Task 10 explicitly resets the pre-deployment specdata database. No migration function or compatibility column is planned.
- **No stale compatibility names:** Tasks 1, 4, 5, 7, and 11 remove split chunk/hit/filter names while retaining TOC-only `section_no` as required.
- **Type consistency:** `sections`/`tables` are defined in Task 1, passed through repository Protocol/ORM in Task 4, used by FTS/vector in Task 5, passed by service in Task 6, and exposed by all public surfaces in Task 7.
- **Cache consistency:** Task 1 defines the setting, Task 6 consumes it as the root, and Task 10 copies the old development path before reset. TDoc cache settings remain untouched.
- **Repair safety:** Task 10 backs up the WAL-aware database, copies rather than deletes cached ZIPs, resets only specdata, continues across pair failures, and validates relational/FTS/vector counts before cleanup.
- **Placeholder scan:** The plan contains no `TBD`, `TODO`, or unspecified implementation step. Every task names files, tests, failing-test commands, implementation behavior, passing-test commands, and commit commands where a commit is appropriate.

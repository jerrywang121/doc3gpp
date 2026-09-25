# Spec-Document Chunk Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make spec-document chunks retain rendered headings/captions and expose only the section/table metadata represented by each chunk, including exact overlap content.

**Architecture:** Keep the existing `Block` types and public `sections`/`tables` DTO contract. Refactor only the pure `chunk_blocks` source-unit construction and metadata bookkeeping, add focused regressions, then rebuild the local `36.508@19.2.0` corpus through the existing `SpecDocService.parse` path.

**Tech Stack:** Python 3.10+, dataclasses, pytest, SQLAlchemy/SQLite specdata storage, python-docx fixtures, Ruff.

## Global Constraints

- Use `sections` and `tables` exactly; do not add legacy aliases.
- Preserve TOC `section_no`/`title` and the existing `HeadingBlock`/`TableBlock` public fields.
- Preserve existing Markdown heading rendering, caption paragraph text, GFM table text, separators, table atomicity, file tags, and size limits.
- Normal chunk metadata comes only from source units assigned to that chunk; no document-level historical metadata.
- Overlap metadata comes only from the exact trailing source tokens copied into the next chunk.
- Metadata remains ordered by first appearance, deduplicated, and `None` when empty.
- Rebuild only the specdata database for the local repair; retain `/home/jerry/.local/share/doc3gpp/doc3gpp_specdata.db.before-spec-doc-metadata.bak`.
- Use `rtk` for shell commands and `apply_patch`/Serena symbolic editing for code changes.

---

## Task 1: Lock Down Chunk Context Regressions

**Files:**
- Modify: `tests/unit/test_spec_doc_chunker.py`
- Test: `tests/unit/test_spec_doc_chunker.py`

**Interfaces:**
- Consumes: `chunk_blocks(blocks, chunk_size, chunk_overlap, max_chunk_chars, file_order, source_file)`.
- Produces: executable expectations for source-unit text and scoped `ChunkDraft.sections`/`tables`.

- [ ] **Step 1: Add the failing section-scope test**

Add a test with multiple heading/paragraph pairs and a small chunk size. Assert that a later chunk contains only the section labels represented by its own text and does not contain earlier headings such as `Foreword`, `Introduction`, or `3.1 Definitions`.

```python
def test_chunk_metadata_does_not_retain_historical_sections():
    blocks = [
        HeadingBlock(1, None, "Foreword", "# Foreword"),
        ParagraphBlock("foreword text"),
        HeadingBlock(1, "3.3", "Abbreviations", "# 3.3 Abbreviations"),
        ParagraphBlock("abbreviation text"),
        HeadingBlock(1, "4.1", "Environmental conditions", "# 4.1 Environmental conditions"),
        ParagraphBlock("environment text"),
        HeadingBlock(2, "4.1.1", "Temperature", "## 4.1.1 Temperature"),
        ParagraphBlock("temperature text"),
        HeadingBlock(2, "4.1.2", "Voltage", "## 4.1.2 Voltage"),
        ParagraphBlock("voltage text"),
    ]

    chunks = chunk_blocks(blocks, chunk_size=6, chunk_overlap=0, max_chunk_chars=1500)

    assert chunks[-1].sections == "4.1.2 Voltage"
    assert "Foreword" not in (chunks[-1].sections or "")
    assert "3.3 Abbreviations" not in (chunks[-1].sections or "")
```

- [ ] **Step 2: Add the failing rendered-heading test**

```python
def test_heading_markdown_is_kept_in_chunk_text():
    chunks = chunk_blocks(
        [HeadingBlock(3, "3.3", "Abbreviations", "### 3.3 Abbreviations"), ParagraphBlock("body")],
        chunk_overlap=0,
    )

    assert "### 3.3 Abbreviations" in chunks[0].text
    assert "body" in chunks[0].text
```

- [ ] **Step 3: Add the failing table-scope and caption/content tests**

Use two table blocks with distinct labels and a caption paragraph before the first table. Force separate chunks and assert each table label is present only with its table content, while the caption and GFM text remain in `ChunkDraft.text`.

```python
def test_table_metadata_and_caption_text_are_scoped_to_chunk():
    first = "| A |\n| --- |\n| 1 |"
    second = "| B |\n| --- |\n| 2 |"
    chunks = chunk_blocks(
        [
            ParagraphBlock("Table 1: First values"),
            TableBlock(first, "1", "First values"),
            TableBlock(second, "2", "Second values"),
        ],
        chunk_size=8,
        chunk_overlap=0,
        max_chunk_chars=1500,
    )

    assert chunks[0].tables == "1 First values"
    assert "Table 1: First values" in chunks[0].text
    assert first in chunks[0].text
    assert chunks[1].tables == "2 Second values"
    assert "1 First values" not in (chunks[1].tables or "")
    assert second in chunks[1].text
```

- [ ] **Step 4: Add the failing overlap-content test**

Assert that overlap copies heading/table tokens into the next text and carries only metadata attached to those copied tokens, not all metadata from the previous chunk.

```python
def test_overlap_carries_only_metadata_for_copied_tokens():
    chunks = chunk_blocks(
        [
            HeadingBlock(1, "5", "Earlier", "# 5 Earlier"),
            ParagraphBlock("one two"),
            HeadingBlock(1, "6", "Later", "# 6 Later"),
            ParagraphBlock("three four"),
            ParagraphBlock("five six"),
        ],
        chunk_size=4,
        chunk_overlap=2,
        max_chunk_chars=1500,
    )

    assert chunks[1].text.startswith("three four")
    assert chunks[1].sections == "6 Later"
```

- [ ] **Step 5: Run the focused tests and confirm the expected failures**

Run: `rtk pytest tests/unit/test_spec_doc_chunker.py -q`

Expected: the new assertions fail because headings are currently omitted and metadata is accumulated across a chunk/history.

- [ ] **Step 6: Commit the red regression tests**

```bash
rtk git add tests/unit/test_spec_doc_chunker.py
rtk git commit -m "test: define spec doc chunk context rules"
```

## Task 2: Refactor Source Units and Metadata Bookkeeping

**Files:**
- Modify: `src/doc3gpp/parsers/spec_doc_chunker.py:14-158`
- Test: `tests/unit/test_spec_doc_chunker.py`

**Interfaces:**
- Consumes: existing `HeadingBlock`, `ParagraphBlock`, `TableBlock` values.
- Produces: `list[ChunkDraft]` from the unchanged `chunk_blocks` signature.

- [ ] **Step 1: Represent headings as rendered source units**

In the unit-building loop, append `HeadingBlock.raw` as a non-atomic rendered unit. Give it the current section label as its own metadata, so the heading text and its section context are present in the chunk that receives the heading.

- [ ] **Step 2: Preserve paragraph text and associate adjacent table captions**

Keep paragraph sentence/token splitting unchanged. When a paragraph is the caption associated with an adjacent `TableBlock`, retain its text as a normal source unit and attach the table label to that unit. The following table units retain the same table label. Do not remove or rewrite caption text.

- [ ] **Step 3: Keep table units and row splitting behavior unchanged**

Continue emitting fitting tables as atomic GFM units and oversized tables as header-plus-row atomic units. Preserve section metadata on table units and table metadata on every table unit.

- [ ] **Step 4: Reset metadata on normal flushes**

Keep ordered deduplication, but ensure `cur_sections` and `cur_tables` are populated only from units currently in `cur`. After `flush()`, clear all current metadata and token metadata before processing the next unit.

- [ ] **Step 5: Track per-token source metadata for all rendered units**

For each rendered unit, append one `(sections, tables)` tuple per `text.split()` token. For oversized table rows, retain the existing repeated per-token metadata. This is the source of truth for exact overlap propagation.

- [ ] **Step 6: Apply overlap only from the exact copied trailing tokens**

When prepending `chunk_overlap` tokens from the previous chunk, prepend the matching trailing token metadata to the next chunk’s token metadata and merge only those entries into the next chunk’s fields. Do not merge the previous chunk’s already-serialized full metadata.

- [ ] **Step 7: Run focused tests and preserve existing behavior**

Run: `rtk pytest tests/unit/test_spec_doc_chunker.py -q`

Expected: all chunker tests pass, including the new context/content tests and existing paragraph/table/limit tests.

- [ ] **Step 8: Commit the implementation**

```bash
rtk git add src/doc3gpp/parsers/spec_doc_chunker.py tests/unit/test_spec_doc_chunker.py
rtk git commit -m "fix: scope spec doc chunk context"
```

## Task 3: Add Service-Level Content and Corpus Regression Coverage

**Files:**
- Modify: `tests/integration/test_spec_doc_service.py` or the existing spec-doc service test file containing parse fixtures
- Modify: `tests/unit/test_spec_doc_service_reads.py` only if the service-level cache/content contract is tested there

**Interfaces:**
- Consumes: `SpecDocService.parse`, `convert_document_to_blocks`, `chunk_blocks`, and the existing test repository/settings fixtures.
- Produces: a regression that verifies parsed spec-document rows retain heading/caption text and corrected metadata.

- [ ] **Step 1: Locate the existing service parse fixture and add a minimal DOCX-based regression**

Use the existing in-memory or fixture ZIP setup. Build a document containing a heading, a paragraph, a caption paragraph, and a table; parse it through `SpecDocService.parse`; read the stored chunk and assert:

```python
assert "# 1 Scope" in chunk.text
assert "Table 1: Values" in chunk.text
assert "| A |" in chunk.text
assert chunk.sections == "1 Scope"
assert chunk.tables == "1 Values"
```

- [ ] **Step 2: Run the service regression**

Run: `rtk pytest tests/integration/test_spec_doc_service.py -q`

Expected: the regression passes and existing service tests remain green.

- [ ] **Step 3: Commit service coverage**

```bash
rtk git add tests/integration/test_spec_doc_service.py tests/unit/test_spec_doc_service_reads.py
rtk git commit -m "test: cover rendered spec doc chunk content"
```

## Task 4: Repair the Local `36.508@19.2.0` Corpus

**Files:**
- Modify externally: `/home/jerry/.local/share/doc3gpp/doc3gpp_specdata.db`
- Preserve externally: `/home/jerry/.local/share/doc3gpp/doc3gpp_specdata.db.before-spec-doc-metadata.bak`
- Use existing cache: `/home/jerry/.cache/doc3gpp/specs/zips/36.508/19.2.0.zip`

**Interfaces:**
- Consumes: normal `SpecDocService.parse(..., force=True)` path and current canonical ZIP cache.
- Produces: repaired specdata rows with rendered headings/captions and scoped metadata.

- [ ] **Step 1: Verify no application process holds the specdata DB**

Run the repository’s existing process check before reset. If a process holds the DB, stop only the known application process after confirming its command/port; do not delete the backup.

- [ ] **Step 2: Confirm the backup and canonical ZIP**

Run:

```bash
rtk test -f "/home/jerry/.local/share/doc3gpp/doc3gpp_specdata.db.before-spec-doc-metadata.bak"
rtk test -f "/home/jerry/.cache/doc3gpp/specs/zips/36.508/19.2.0.zip"
```

Expected: both files exist.

- [ ] **Step 3: Reset only specdata**

Use the existing CLI command with `--scope specdata --yes`; do not reset main or testcase databases.

- [ ] **Step 4: Parse through the normal service**

Run the existing project repair helper or equivalent `.venv/bin/doc3gpp` invocation with the current settings and `force=True`, targeting `36.508@19.2.0`. Do not introduce a repair command or runtime migration.

- [ ] **Step 5: Inspect representative rows**

Query the repaired DB and verify:

- `sections`/`tables` do not contain unrelated historical earlier headings;
- heading Markdown appears in `text`;
- table caption and GFM table text appear in `text`;
- relational, FTS, and TOC counts remain coherent;
- vector rows remain zero when `embedding_base_url` is unset.

## Task 5: Full Verification and Documentation Review

**Files:**
- Modify: none unless verification identifies a stale test/documentation assertion

**Interfaces:**
- Consumes: completed parser, tests, and repaired local corpus.
- Produces: verified branch state ready for integration choice.

- [ ] **Step 1: Run focused parser/service tests**

Run: `rtk pytest tests/unit/test_spec_doc_chunker.py tests/unit/test_spec_doc_blocks.py tests/integration/test_spec_doc_service.py -q`

Expected: all pass.

- [ ] **Step 2: Run the full SQLite suite**

Run: `rtk test ./scripts/test_sqlite.sh`

Expected: zero failures.

- [ ] **Step 3: Run lint and whitespace checks**

Run:

```bash
rtk ruff check .
rtk git diff --check main..HEAD
```

Expected: both commands exit successfully.

- [ ] **Step 4: Inspect final status and report exact evidence**

Run: `rtk git status --short --untracked-files=all`

Report test count, skipped tests/warnings, lint status, corpus repair evidence, and any intentional untracked plan/spec files. Do not claim completion without fresh command output.

# Presentation ZIP Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILLS: Use superpowers:subagent-driven-development and superpowers:test-driven-development. Steps use checkbox (`- [ ]`) syntax for tracking. Execute tasks in order; each has a separate implementation and review gate.

**Goal:** Convert presentation members of supported TDoc and spec-document ZIPs to searchable, SQL-stored Markdown and expose their complete content in detail views.

**Architecture:** Add one lazy, shared presentation converter and dispatch through the existing document pool. Reuse TDoc content storage and indexes; add a specdata full-file table whose replacement is atomic with spec chunks, then expose SQL-only spec file details through CLI, web, HTTP, and MCP.

**Tech Stack:** Python 3.10+, python-pptx, optional LibreOffice Impress, SQLAlchemy, SQLite gzip-JSON, existing FTS5/vector search, Typer, FastAPI, MCP, pytest.

**Spec:** `docs/superpowers/specs/2026-10-30-presentation-zip-design.md`.

## Global Constraints

- Scope is `.pptx` and `.ppt` **inside ZIP packages** for already eligible TDoc families and spec documents; do not broaden standalone parsing or TDoc eligibility.
- Extract slide text, titles, grouped text, tables, and existing speaker notes only; no OCR, graphical inference, or domain metadata extraction.
- Preserve existing Word behavior, member identity/order, TDoc ZIP validation/limits, spec ZIP size limit, parsed-skip rules, and the historical `docx_count` name.
- `.ppt` requires isolated LibreOffice Impress conversion using the existing `word_conversion.timeout_seconds`; missing optional dependencies and conversion failures must be actionable.
- Spec presentation detail reads never access network or caches. Selected TDoc presentation content from complete extractions is SQL-first even if the derived cache is missing; retain existing TDoc auto-sync semantics. Existing databases need additive `doc3gpp db init --scope specdata` and a forced re-parse.
- Project policy in `docs/conventions.md` forbids automatic commits. Do not commit any task, plan, or spec without a separate user request; preserve the pre-existing modification to `.superpowers/sdd/.gitignore`.
- Before implementation install/verify the required test extras in the active development environment: `python -m pip install -e '.[dev,extract,semantic]'`. Run the real conversion and semantic tests, not only mocked or skipped substitutes.

## Review Focus

1. A deck containing `Contents`, numbered headings, and a table caption must not manufacture spec TOC/section/table metadata (Task 4).
2. Empty decks and exact-path duplicates must not disappear or ambiguously return another member (Tasks 1, 3, 4).
3. A DB exception after deleting old rows must roll back both old chunks and old full-file records (Task 2).
4. An incomplete extraction or missing selected TDoc presentation must still use the existing extraction-first retry path (Task 3).
5. A source path containing quotes, ampersands, Unicode, or markup must survive exact lookup while remaining inert in HTML and encoded in links (Task 5).

## File Map

- `parsers/presentation_converter.py`: lazy PowerPoint parsing, deterministic Markdown, and isolated legacy conversion.
- `parsers/docx_converter.py`, `services/docx_convert_pool.py`: retain the pool interface and route each supported extension to the right converter.
- `parsers/tdoc_zip.py`, `tdoc_zip_convert.py`, `tdoc_family_main.py`, `direct_extractor.py`, `services/tdoc_content_service.py`, `cli.py`: TDoc selection, ingestion, retry behavior, and SQL-first selected presentation reads.
- `models/spec_doc.py`, `repository/protocols.py`, `storage/db/models.py`, `storage/repositories/spec_doc_sql.py`, `models/schema_info.py`: full-presentation DTO/table, repository contract, transactional replacement, and schema registry.
- `parsers/spec_doc.py`, `services/spec_doc_service.py`: enumeration, presentation-only/mixed conversion, metadata-free chunks, storage and reads.
- `cli.py`, `web/routes/spec_docs.py`, `web/mcp_server.py`, `web/templates/`: spec full-file detail adapters and safe version-page links.
- Existing and new unit/integration tests cover each layer; `README.md`, `AGENTS.md`, `docs/cli.md`, `docs/architecture.md`, and `docs/web-server.md` document the shipped behavior.

---

### Task 1: Shared conversion and pool dispatch

**Files:** Create `src/doc3gpp/parsers/presentation_converter.py`, `tests/fixtures/presentation_builders.py`, `tests/unit/test_presentation_converter.py`; modify `pyproject.toml`, `src/doc3gpp/parsers/docx_converter.py`, `tests/unit/test_docx_converter_pool_protocol.py`, `tests/integration/test_spec_doc_docx_pool_equivalence.py`.

**Interfaces:** Produce `convert_presentation_to_markdown(payload: bytes, filename: str) -> str` and `PythonPptxNotInstalledError(ImportError)` in the new parser. Retain `DocxMemberPool.convert_members(kind, members) -> list[object]`; for presentation `kind="markdown"` return the exact Markdown, and for `kind="blocks"` return `[ParagraphBlock(markdown)]` or `[]` for an empty deck. Word dispatch and per-member exception outcomes remain unchanged.

- [ ] **Write failing tests.** Provide generated PPTX fixture builders using python-pptx, including a zero-slide deck, two slides with Unicode/text frames, a grouped shape, a table containing `a|b` and `line1\nline2`, and existing speaker notes. Test the public converter, not python-pptx itself. Pin slide headings to `## Slide 1`, `## Slide 2`, and the notes label to `**Speaker notes:**`:

  ```python
  def test_slide_shapes_tables_and_notes_are_ordered(deck_with_content):
      md = convert_presentation_to_markdown(deck_with_content, "folder/slides.pptx")
      assert md.index("## Slide 1") < md.index("日本語") < md.index("## Slide 2")
      assert "group text" in md and r"a\|b" in md and "line1<br>line2" in md
      assert "**Speaker notes:**" in md and "speaker secret" in md

  def test_empty_deck_is_known_content(empty_deck):
      assert convert_presentation_to_markdown(empty_deck, "empty.pptx") == ""
  ```

  Add tests for malformed PPTX; no creation of absent notes slides; lazy import and install hint when `pptx` is unavailable; invalid extensions; `.PPTX` case handling; no image/chart inference; and `.ppt` success, missing executable, zero-output, timeout, and child cleanup using a mocked subprocess. Assert fixed `input.ppt` and private profile rather than a ZIP member path. Test both pool kinds on mixed Word/PPTX input and serial/spawned-pool result and exception-order equivalence.
- [ ] Run `python -m pytest tests/unit/test_presentation_converter.py tests/unit/test_docx_converter_pool_protocol.py tests/integration/test_spec_doc_docx_pool_equivalence.py -q`; confirm the new tests fail for the missing converter/dispatch.
- [ ] Add `python-pptx>=1.0.2` to `[extract]`. Implement shape-order traversal, recursive groups, GFM tables with escaped `|` and `<br>` cell line breaks, speaker notes only when `slide.has_notes_slide`, and lazy imports. For `.ppt`, follow `word_converter.py`'s isolated profile, timeout, fixed filenames, output checks, and process-group cleanup, targeting PPTX via Impress; preserve source paths only for diagnostics.
- [ ] Dispatch by suffix in `convert_document_to_markdown`/`convert_document_to_blocks` and keep existing Word functions, pool workers, and per-member error contracts compatible. Run the command above and `python -m pytest tests/unit/test_legacy_doc.py tests/unit/test_docx_converter_golden.py tests/unit/test_docx_convert_pool.py -q`; all pass.

### Task 2: Compressed spec presentation repository

**Files:** Modify `src/doc3gpp/models/spec_doc.py`, `src/doc3gpp/repository/protocols.py`, `src/doc3gpp/storage/db/models.py`, `src/doc3gpp/storage/repositories/spec_doc_sql.py`, `src/doc3gpp/models/schema_info.py`, `src/doc3gpp/storage/db/migrate.py`; create `tests/integration/test_spec_presentation_repository.py`; update `tests/unit/test_schema_info.py` and relevant schema-count tests.

**Interfaces:** Add `SpecDocPresentation(spec_id: str, version: str, source_file: str, file_order: int, markdown: str)` as a slots DTO. Add to `SpecDocRepository` and its SQL implementation:

```python
def list_presentations(self, spec_id: str, version: str) -> list[SpecDocPresentation]: ...
def get_presentation(self, spec_id: str, version: str, source_file: str) -> SpecDocPresentation | None: ...
def replace_version_content(self, spec_id: str, version: str, *, release: str | None,
                            drafts: list[ChunkDraft], presentations: list[SpecDocPresentation]) -> list[SpecDocChunk]: ...
```

`replace_chunks` remains available to existing callers; `replace_version_content` is the atomic interface used by Task 4.

- [ ] **Write failing tests** with a temporary specdata database and the SQL repository. Test the following values and query the raw `content` blob, not only the DTO:

  ```python
  def test_roundtrip_is_gzip_json_and_exact(repo, raw_specdata_connection):
      row = SpecDocPresentation("38.331", "18.5.0", "slides/a.pptx", 1, "## Slide 1\n日本語")
      repo.replace_version_content("38.331", "18.5.0", release="Rel-18", drafts=[], presentations=[row])
      assert repo.get_presentation("38.331", "18.5.0", "slides/a.pptx") == row
      assert repo.get_presentation("38.331", "18.5.0", "a.pptx") is None
      blob = raw_specdata_connection.execute(text("SELECT content FROM spec_doc_presentations")).scalar_one()
      assert blob.startswith(b"\x1f\x8b") and decompress_json(blob) == row.markdown
  ```

  Also assert deterministic `file_order` listing; empty Markdown survives; replacement with `presentations=[]` removes prior files; version isolation; duplicate key rejection; rejection of a presentation whose `(spec_id, version)` differs from the replacement target; additive `create_schema("specdata")` twice preserves old rows; registry columns equal ORM columns. Inject a DB failure after the delete and assert both the old chunk and old presentation remain unchanged after rollback.
- [ ] Run `python -m pytest tests/integration/test_spec_presentation_repository.py tests/unit/test_schema_info.py -q`; confirm the new DTO/table/transaction tests fail.
- [ ] Add `SpecDocPresentationORM` on `SpecDataBase`, table `spec_doc_presentations`, composite PK `(spec_id, version, source_file)`, non-null integer `file_order`, and non-null gzip-JSON binary `content`. Use the existing compression helpers. Create the table through the normal additive specdata bootstrap and register all five fields. Implement all methods above; factor the current chunk insert so chunk deletion/insertion and presentation deletion/insertion share **one** session transaction.
- [ ] Run the command above plus `python -m pytest tests/integration/test_spec_doc_repo.py tests/integration/test_spec_doc_chunk_text_blob_sqlite.py -q`; all pass.

### Task 3: TDoc ZIP ingestion and SQL-first selected details

**Files:** Modify `src/doc3gpp/parsers/tdoc_zip.py`, `tdoc_zip_convert.py`, `tdoc_family_main.py`, `direct_extractor.py`, `src/doc3gpp/services/tdoc_content_service.py`, `src/doc3gpp/cli.py`; create `tests/integration/test_tdoc_presentation_sqlite.py`; extend `tests/unit/test_tdoc_family_main.py`, `tests/unit/test_tdoc_zip_convert.py`, `tests/unit/test_tdoc_content_surfaces.py`.

**Interfaces:** Retain `enumerate_docx_members(...)` and `select_main_member(tdoc_id, docx_members, other_members=())` signatures; their historical names now cover four supported member extensions. The existing `TDocContentService.get(tdoc_id=..., source_file=...)` remains the SQL source; the CLI bypasses extraction only when this exact presentation exists in a complete extraction.

- [ ] **Write failing tests** using generated ZIPs and the builders from Task 1. Include these selector expectations:

  ```python
  def test_presentation_main_and_exact_ambiguity():
      assert select_main_member("R5-260001", ["R5-260001.pptx"]) == "R5-260001.pptx"
      with pytest.raises(TDocMainDocumentError, match="ambiguous"):
          select_main_member("R5-260001", ["R5-260001.pptx", "R5-260001.docx"])
  ```

  Assert mixed and presentation-only eligible family ZIPs persist compressed content and exact document identities; zero-slide presentation is stored; CR Word cover remains main even when a PPTX contains CR-looking text; known-family direct ZIP parsing works but standalone `.pptx` remains unsupported. Assert presentations count toward both ZIP limits, unsafe/normalized duplicate names fail, and archive order is stable. A failed presentation main fails; a failed annex appears in `skipped_sources` and a subsequent normal parse retries. Test existing FTS5 and enabled semantic searches find slide/notes text.
- [ ] Add CLI tests for a complete stored CR presentation after deleting only the derived Markdown archive: exact `--source-file` emits SQL Markdown without `extract` or download. For an incomplete extraction, missing source, non-presentation source, or no source selector, assert the existing extract-first behavior remains.
- [ ] Run `python -m pytest tests/unit/test_tdoc_family_main.py tests/unit/test_tdoc_zip_convert.py tests/unit/test_tdoc_content_surfaces.py tests/integration/test_tdoc_presentation_sqlite.py -q`; confirm new tests fail.
- [ ] Extend existing enumeration, extension checks, main-name selection, and pool dispatch. Limit CR structural-cover candidates to Word results while converting/storing presentations as annexes; preserve existing exception classification and retry completeness. Implement the narrowly gated SQL-first CLI path without changing URL-anchored reads or other raw reads.
- [ ] Re-run the command above plus `python -m pytest tests/unit/test_tdoc_parse_direct.py tests/unit/test_tdoc_cr_service.py tests/integration/test_tdoc_content_search_sqlite.py tests/integration/test_tdoc_content_semantic_sqlite.py -q`; all pass.

### Task 4: Spec ZIP parsing, chunks, and full-file persistence

**Files:** Modify `src/doc3gpp/parsers/spec_doc.py`, `src/doc3gpp/models/spec_doc.py`, `src/doc3gpp/services/spec_doc_service.py`; extend `tests/unit/test_spec_doc_parser.py`, `tests/unit/test_spec_doc_docx_pool.py`, `tests/integration/test_spec_doc_service.py`; create `tests/integration/test_spec_presentation_parse.py`.

**Interfaces:** Keep `list_spec_docx(zip_bytes: bytes) -> list[tuple[str, bytes]]` and `prepare_parse(...)` signatures; add `presentations: tuple[SpecDocPresentation, ...] = ()` to `PreparedSpecDocParse`. Add `SpecDocService.list_presentations(spec_id: str, version: str) -> list[SpecDocPresentation]` and `get_presentation(spec_id: str, version: str, source_file: str) -> SpecDocPresentation`, raising `SpecDocError` with the exact selectors on a miss. Persist through Task 2's `replace_version_content`.

- [ ] **Write failing tests** for mixed and presentation-only ZIPs using generated real documents. A slide contains `Contents`, `1 Scope`, `Table 1: Equipment`, and `speaker secret`, and a separate Word file contains a valid section. Assert:

  ```python
  assert source.docx_count == 2  # Word + PPTX, historical field name
  assert all(c.sections is None and c.tables is None for c in presentation_chunks)
  assert all(e.source_file != "slides.pptx" for e in toc.entries)
  assert repo.get_presentation("38.331", "18.5.0", "slides.pptx").markdown == expected_markdown
  assert any("speaker secret" in hit.text for hit in fts_hits)
  ```

  Assert presentation text is also offered to enabled semantic indexing; chunks use configured limits and file order; zero-slide decks persist an empty full file with no empty search chunk; overlapping chunks never supply the full-file detail. Duplicate presentation paths must fail clearly while raw inventory still retains both entries. A bad presentation fails the version parse without replacing previously stored chunks or presentation records; force re-parse to a Word-only ZIP deletes old presentation rows. A parsed version without `force` neither downloads nor silently backfills.
- [ ] Run `python -m pytest tests/unit/test_spec_doc_parser.py tests/unit/test_spec_doc_docx_pool.py tests/integration/test_spec_presentation_parse.py -q`; confirm new tests fail.
- [ ] Extend ZIP enumeration, reading by `ZipInfo` and rejecting repeated exact presentation paths. Route presentation block outcomes through `ParagraphBlock` so the chunker has no heading/table metadata; exclude presentation files entirely from TOC extraction. Preserve file ordering, full Markdown, original source paths, existing inventory/failure behavior, and aggregate `docx_count`. Complete all conversion before invoking the atomic repository replacement; keep parsed-state and indexing lifecycle.
- [ ] Re-run the command above plus `python -m pytest tests/integration/test_spec_doc_service.py tests/integration/test_spec_doc_search_repo.py tests/integration/test_spec_doc_docx_pool_equivalence.py -q`; all pass.

### Task 5: Spec full-file CLI, web, HTTP, and MCP details

**Files:** Modify `src/doc3gpp/cli.py`, `src/doc3gpp/web/routes/spec_docs.py`, `src/doc3gpp/web/mcp_server.py`, the existing spec-doc version template; create `src/doc3gpp/web/templates/spec_doc_content.html`, `tests/integration/test_spec_presentation_surfaces.py`; extend `tests/unit/test_web_routes.py` as needed.

**Interfaces:** Add `doc3gpp spec doc show --spec <id> --version <version> --source-file <exact-path> [--format raw|json] [--compact] [--output FILE]` (raw default). Add `GET /specs/{spec_id}/docs/content?version=...&source_file=...` (HTML default, `format=json` for JSON) and MCP `get_spec_doc_content(spec_id, version, source_file)`. All call Task 4's SQL-backed service. JSON is the ordered object `{"spec_id": ..., "version": ..., "source_file": ..., "markdown": ...}`; compact CLI, HTTP and MCP output have identical UTF-8 bytes.

- [ ] **Write failing cross-surface tests** against a persisted presentation, with cache directories removed and download/extraction mocked to raise if called. Use exact source `slides/<script>&"日本語.pptx` and Markdown containing `<script>alert(1)</script>`:

  ```python
  assert cli_raw == markdown
  assert json.loads(cli_compact) == {"spec_id": "38.331", "version": "18.5.0",
                                    "source_file": source_file, "markdown": markdown}
  assert cli_compact == http_json == mcp_json
  assert "<script>alert(1)</script>" not in html
  assert "&lt;script&gt;" in html
  ```

  Assert the version page lists the stored file in file order with a safely encoded link; following it retrieves the same file. Assert exact-name lookup, a missing version/source produces a clear CLI error and HTTP 404/MCP error, JSON preserves Unicode, and output-to-file works.
- [ ] Run `python -m pytest tests/integration/test_spec_presentation_surfaces.py tests/integration/test_spec_doc_web.py -q`; confirm new command/route/tool tests fail.
- [ ] Implement the adapters and one shared JSON payload builder using the fixed fields above. Render Markdown with the existing HTML-safe Markdown renderer and autoescaped templates; construct query links with URL encoding. Never use ZIP or Markdown caches, network, or parse services to answer these reads. Preserve existing `spec doc` commands and web routes.
- [ ] Re-run the command above plus `python -m pytest tests/integration/test_spec_doc_cli.py tests/unit/test_web_routes.py tests/integration/test_mcp_end_to_end.py -q`; all pass.

### Task 6: Cross-pipeline acceptance and documentation

**Files:** Extend `tests/integration/test_tdoc_presentation_sqlite.py`, `tests/integration/test_spec_presentation_parse.py`, `tests/integration/test_spec_presentation_surfaces.py`; modify `README.md`, `AGENTS.md`, `docs/cli.md`, `docs/architecture.md`, `docs/web-server.md`, and relevant `src/doc3gpp/data/doc3gpp.toml.example` comments only if they describe supported formats.

**Interfaces:** No new public interfaces. Exercise the interfaces and contracts of Tasks 1–5 end to end.

- [ ] **Write acceptance tests** that parse a real mixed ZIP into each SQLite corpus, search for both slide and speaker-note text through the existing FTS5 and enabled semantic services, evict caches, and retrieve exact full Markdown via all requested detail surfaces. Mark the new vector tests `@pytest.mark.semantic`, use the real sqlite-vec repositories with a deterministic local fake embedder (no external service), and assert the resulting presentation hits. Cover legacy `.ppt` with an actual LibreOffice conversion when installed and the deterministic mocked legacy converter otherwise; verify `--force` refresh and unchanged Word/CR behavior. Run each new acceptance test first and fix any cross-layer failure using the TDD cycle.
- [ ] Update user docs with supported formats, optional `[extract]` dependency and Impress requirement, limits, no image extraction or presentation-derived metadata, `docx_count` compatibility, SQL-backed detail commands/route/tool, cache independence, and `db init --scope specdata` plus `--force` backfill. Keep `README.md`, `AGENTS.md`, and the listed docs synchronized.
- [ ] Run `ruff check .`; expect no violations. Run `./scripts/test_sqlite.sh`; expect the entire offline SQLite profile to pass. Run `python -m pytest -m semantic tests/integration/test_tdoc_presentation_sqlite.py tests/integration/test_spec_presentation_parse.py -q`; expect new TDoc **and** spec presentation semantic tests to execute and pass rather than being skipped. Review `git diff --check` and `git status --short --branch`; preserve the pre-existing `.superpowers/sdd/.gitignore` change.
- [ ] Request a fresh whole-branch review after all task reviews, address findings, and repeat affected tests. Do not commit unless the user separately requests it.

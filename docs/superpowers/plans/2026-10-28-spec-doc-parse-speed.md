# Spec-Doc Parse Speed-Up Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `doc3gpp spec doc parse` (and TDoc ZIP extraction) faster on multi-DOCX archives by cutting per-DOCX conversion cost and converting DOCX members in a bounded process pool, with byte-identical output.

**Architecture:** Hot-path fixes in `parsers/docx_converter.py` (single thread, same output). A `DocxConvertPool` in `services/docx_convert_pool.py` runs per-member conversion in spawn-context worker processes, behind a `DocxMemberPool` Protocol that parsers depend on. `InlineDocxPool` is the in-process default. Spec-doc and TDoc callers take an optional pool; web and CLI wire one bounded pool per process.

**Tech Stack:** Python 3.10+ (no 3.11-only syntax), python-docx 1.2, `concurrent.futures.ProcessPoolExecutor` (spawn), Pydantic v2 settings, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-10-28-spec-doc-parse-speed-design.md`. Read it before starting; this plan argues from it.

## Global Constraints

- Converted output (blocks, markdown, TOC, chunks, stored rows) must stay byte-identical to the current code for every input.
- `parsers/` stays free of network and of service imports. The pool lives in `services/`; parsers see only the `DocxMemberPool` Protocol.
- Worker entry points must be top-level, importable, picklable functions. Use the `spawn` multiprocessing context. Never fork a threaded process.
- `docx_workers` bounds: `ge=0`, `le=32`; default `min(4, os.cpu_count() or 1)`. `0` and `1` mean in-process.
- TDD: write the failing test first, see it fail, then implement.
- Verification before completion: `./scripts/test_sqlite.sh` and `ruff check .` must pass.
- Docs change in the same branch as behaviour (AGENTS.md convention).

## Review Focus

1. ZIP with one DOCX, or with more members than `docx_workers`: output identical, no hang, no extra processes beyond the bound.
2. A DOCX member that fails to convert in spec-doc parse: the parse fails with the same exception type and message as sequential today (not a silent skip).
3. A TDoc annex that fails: recorded in `skipped_sources`; a failing main document still fails the TDoc; an empty-text document still yields `""`.
4. Worker process dies (`BrokenProcessPool`): the affected parse fails with `DocxConvertPoolError` and the pool recovers for the next call.
5. Tables with merged cells (`gridSpan`, `vMerge`) and tracked insertions/deletions render exactly as before.

---

### Task 1: Characterization baseline (golden tests on the current converter)

**Files:**
- Create: `tests/fixtures/docx_builders.py`
- Create: `tests/unit/test_docx_converter_golden.py`
- Create: `tests/fixtures/docx_golden/expected.md`
- Create: `tests/fixtures/docx_golden/expected_blocks.json`

**Interfaces:**
- Consumes: `convert_document_to_markdown(bytes, filename) -> str`, `convert_document_to_blocks(bytes, filename) -> list[HeadingBlock | ParagraphBlock | TableBlock]` from `doc3gpp.parsers.docx_converter`.
- Produces: `build_golden_docx() -> bytes` in `tests/fixtures/docx_builders.py`. Later tests import it as `from tests.fixtures.docx_builders import build_golden_docx`, matching the repo's shared-fixture pattern (`tests.fixtures.search_corpus`).

This task runs on the unchanged converter. It is a characterization test: it must pass before any converter edit.

- [ ] **Step 1: Write `build_golden_docx()` and the golden test**

`build_golden_docx()` lives in `tests/fixtures/docx_builders.py`. It builds one `.docx` with python-docx `Document()` and returns its bytes. It must include:
- headings: "Heading 1" and "Heading 2" paragraphs, plus a "Title" paragraph;
- a paragraph with bold, italic, and bold-italic runs;
- a "List Bullet" paragraph;
- a table caption paragraph "Table 5.1-1: Example caption" immediately before a 3x3 table, with one `cell.merge()` horizontal merge (`gridSpan`) and one vertical merge (`vMerge`), and a cell containing `|`;
- a paragraph containing a tracked insertion and a tracked deletion, built by appending `w:ins` / `w:del` elements with `w:r`/`w:t`/`w:delText` children via lxml;
- a paragraph with no explicit style.

Test shape:

```python
def test_golden_markdown_matches_expected():
    assert convert_document_to_markdown(build_golden_docx(), "golden.docx") == EXPECTED_MD

def test_golden_blocks_match_expected():
    blocks = convert_document_to_blocks(build_golden_docx(), "golden.docx")
    assert [_as_json(b) for b in blocks] == EXPECTED_BLOCKS
```

`EXPECTED_MD` and `EXPECTED_BLOCKS` are loaded from the two fixture files. `_as_json` maps each dataclass block to `{"type": ..., **dataclasses.asdict(b)}`; confirm the block classes are dataclasses first.

- [ ] **Step 2: Generate the expected fixtures from the unchanged converter**

Run a one-off script from `/tmp/opencode` (do not commit it) that imports `build_golden_docx` and writes `expected.md` (the markdown string) and `expected_blocks.json` (the `_as_json` list, `indent=2`, `ensure_ascii=False`). Read both files and check by eye that the merged cells, tracked changes, caption attachment, and heading levels look right.

- [ ] **Step 3: Run the golden tests and verify they pass on current code**

Run: `.venv/bin/python -m pytest tests/unit/test_docx_converter_golden.py -v`
Expected: 2 passed.

- [ ] **Step 4: Commit**

```bash
git add tests/unit/test_docx_converter_golden.py tests/fixtures/docx_golden/
git commit -m "test(docx): add golden characterization for DOCX converter"
```

---

### Task 2: Behaviour-preserving hot-path fixes in the converter

**Files:**
- Modify: `src/doc3gpp/parsers/docx_converter.py` (`_table_to_markdown` ~line 223, `_paragraph_to_markdown` ~line 183, `_style_name` ~line 117, `_render_children` ~line 150, `convert_document_to_blocks` ~line 389, `_document_to_markdown` ~line 258)

**Interfaces:**
- Consumes: Task 1 golden tests.
- Produces: unchanged public signatures. `_paragraph_to_markdown(paragraph, style_name: str | None = None) -> str` (optional precomputed style name; `None` keeps current lookup). `_style_name` is unchanged.

- [ ] **Step 1: Add a failing test for the single `row.cells` evaluation**

In `tests/unit/test_docx_converter_golden.py`, add a test that wraps `docx.table._Row` cells access with a counter (monkeypatch the `cells` property on `docx.table._Row` with a counting property delegating to the original) and converts `build_golden_docx()`. Assert the count of `cells` evaluations equals the number of rows (one per row, not one per cell index).

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_docx_converter_golden.py -k cells -v`
Expected: FAIL (count is greater than the row count).

- [ ] **Step 3: Implement the three fixes**

- `_table_to_markdown`: `row_cells = row.cells` once per row; `total_cells = len(row_cells)`; `cell = row_cells[ci]`.
- Style: in `_document_to_markdown` and `convert_document_to_blocks`, keep a per-conversion `style_cache: dict[str | None, str]` keyed by `paragraph.style_id`. Compute the lowered style name once per paragraph, pass it to `_paragraph_to_markdown(paragraph, style_name)`, and reuse it for the block style check. `_style_name` itself is unchanged.
- Hoist the `qn("w:...")` tags used in `_render_children`, `_run_formatting`, and `_is_paragraph`/`_is_table` to module-level constants (e.g. `_W_T = qn("w:t")`).

- [ ] **Step 4: Run the golden tests and the existing converter tests**

Run: `.venv/bin/python -m pytest tests/unit/test_docx_converter_golden.py tests/unit/test_spec_doc_blocks.py tests/unit/test_spec_doc_parser.py -v`
Expected: all pass; the counting test now passes.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/parsers/docx_converter.py tests/unit/test_docx_converter_golden.py
git commit -m "perf(docx): cache row cells and style lookups in converter"
```

---

### Task 3: `DocxMemberPool` Protocol and `InlineDocxPool`

**Files:**
- Modify: `src/doc3gpp/parsers/docx_converter.py` (add to module, before `__all__`)
- Test: `tests/unit/test_docx_converter_pool_protocol.py`

**Interfaces:**
- Produces:
  - `MemberKind = Literal["blocks", "markdown"]`
  - `class DocxMemberPool(Protocol)`: `def convert_members(self, kind: MemberKind, members: Sequence[tuple[str, bytes]]) -> list[object]`. Returns one outcome per member, in input order. An outcome is the converted value, or the exception instance raised for that member.
  - `class InlineDocxPool`: no-arg constructor; `convert_members` runs the matching worker in-process for each member and captures `Exception` as the outcome. `kind="blocks"` uses `convert_document_to_blocks`; `kind="markdown"` uses `convert_document_to_markdown`.
  - `DOCX_WORKERS: dict[MemberKind, Callable[[bytes, str], object]]` mapping kinds to the two worker functions; the process pool (Task 4) imports it.

- [ ] **Step 1: Write failing tests**

Tests (in `test_docx_converter_pool_protocol.py`):
- `InlineDocxPool().convert_members("markdown", [("a.docx", build_golden_docx())])[0]` equals `convert_document_to_markdown(...)` for the same bytes.
- `InlineDocxPool().convert_members("blocks", [("a.docx", build_golden_docx())])[0]` equals `convert_document_to_blocks(...)`.
- A member with invalid bytes (`b"not a zip"`) and name `x.docx` yields an exception instance as its outcome, and does not raise.
- A member named `legacy.doc` yields a `ValueError` outcome.
- Order: with three members, outcomes are in input order.

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_docx_converter_pool_protocol.py -v`
Expected: FAIL (`ImportError` on `InlineDocxPool`).

- [ ] **Step 3: Implement**

Add the `Protocol`, `InlineDocxPool`, and `DOCX_WORKERS` to `docx_converter.py`. Add `Literal`, `Protocol`, and `Sequence` imports. `InlineDocxPool.convert_members` loops in order, `try: outcomes.append(DOCX_WORKERS[kind](payload, name)) except Exception as exc: outcomes.append(exc)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_docx_converter_pool_protocol.py tests/unit/test_docx_converter_golden.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/parsers/docx_converter.py tests/unit/test_docx_converter_pool_protocol.py
git commit -m "feat(docx): add DocxMemberPool protocol and inline implementation"
```

---

### Task 4: Process pool `DocxConvertPool`

**Files:**
- Create: `src/doc3gpp/services/docx_convert_pool.py`
- Test: `tests/unit/test_docx_convert_pool.py`

**Interfaces:**
- Consumes: `DOCX_WORKERS`, `InlineDocxPool`, `MemberKind`, `DocxMemberPool` from `doc3gpp.parsers.docx_converter` (Task 3).
- Produces:
  - `class DocxConvertPoolError(RuntimeError)`
  - `class DocxConvertPool` with `__init__(self, workers: int)` (raise `ValueError` if `workers < 0`), `convert_members(kind, members) -> list[object]` (same contract as the Protocol), `close() -> None` (idempotent), `__enter__`/`__exit__` calling `close()`.
  - Module-level `_run_member(kind: MemberKind, name: str, payload: bytes) -> object` (picklable worker entry point, calls `DOCX_WORKERS[kind](payload, name)`).

Behaviour:
- `workers <= 1`: delegate every call to an internal `InlineDocxPool`; no executor is created.
- `workers >= 2`: a `ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"))` is created on first use, under a `threading.Lock`. Each member is submitted as `_run_member(kind, name, payload)`. Results are collected in input order: a successful `result()` is the outcome; an `Exception` raised by `result()` is the outcome (the member's own exception).
- `concurrent.futures.process.BrokenProcessPool` (from `submit` or `result`): the affected member's outcome is `DocxConvertPoolError(f"DOCX conversion pool broke while converting {name}: {exc}")`; the executor is shut down and set to `None` so the next call recreates it.

- [ ] **Step 1: Write failing tests**

- `DocxConvertPool(0)` and `DocxConvertPool(1)` produce the same outcomes as `InlineDocxPool()` for three generated docx members (compare `convert_members` output lists; for `blocks`, compare the dataclass lists).
- `DocxConvertPool(2)` produces the same outcomes as `InlineDocxPool()` for five generated docx members of different sizes, in input order (use `build_golden_docx()` and two variants with extra paragraphs, built in the test). Mark the test as using real processes; it may take a few seconds.
- A bad member (`b"junk"`, name `bad.docx`) among good members: the bad member's outcome is an exception instance, the rest are values, and the call does not raise.
- Exception round-trip: `ValueError` from `legacy.doc` and `RuntimeError` from an empty document both come back as the same exception type with the same message.
- `DocxConvertPool(-1)` raises `ValueError`.
- `close()` twice does not raise.
- `BrokenProcessPool`: monkeypatch `DocxConvertPool._executor_for_submit` (or the internal executor factory method you define) so `submit` raises `BrokenProcessPool`; assert the outcome is a `DocxConvertPoolError` and that a second call creates a fresh executor.

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_docx_convert_pool.py -v`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement**

Implement per the Behaviour section. Use a `_get_executor()` method (under the lock) so the broken-pool test has a seam. Keep the file focused: the class, `_run_member`, and the error class.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_docx_convert_pool.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/services/docx_convert_pool.py tests/unit/test_docx_convert_pool.py
git commit -m "feat(docx): add bounded spawn-context conversion pool"
```

---

### Task 5: `[pipeline] docx_workers` setting

**Files:**
- Modify: `src/doc3gpp/settings/schema.py` (`PipelineSettings`, line ~722)
- Modify: `src/doc3gpp/data/doc3gpp.toml.example` (the `[pipeline]` block)
- Test: `tests/unit/test_settings.py`, `tests/unit/test_server_settings.py`

**Interfaces:**
- Produces: `PipelineSettings.docx_workers: int`, `default_factory=lambda: min(4, os.cpu_count() or 1)`, `ge=0`, `le=32`, with a description stating that `0` and `1` run conversion in-process.

- [ ] **Step 1: Write failing tests**

Mirror the existing `prepare_workers` tests:
- `PipelineSettings().docx_workers` equals `min(4, os.cpu_count() or 1)` (patch `os.cpu_count` to 2 and to 64 in two tests: expect 2 and 4).
- `PipelineSettings(docx_workers=-1)` raises `ValidationError`; `docx_workers=33` raises.
- `docx_workers=0` is accepted.
- TOML override: `[pipeline]\ndocx_workers = 7` yields `get_settings().pipeline.docx_workers == 7` (follow `test_pipeline_prepare_workers_toml_override`).

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_settings.py tests/unit/test_server_settings.py -k docx_workers -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Add the field to `PipelineSettings` with the same style as `prepare_workers`. Update the class docstring to mention the conversion pool. Add the commented example key under `[pipeline]` in `doc3gpp.toml.example`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_settings.py tests/unit/test_server_settings.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/settings/schema.py src/doc3gpp/data/doc3gpp.toml.example tests/unit/test_settings.py tests/unit/test_server_settings.py
git commit -m "feat(settings): add pipeline.docx_workers"
```

---

### Task 6: Spec-doc service uses the pool

**Files:**
- Modify: `src/doc3gpp/services/spec_doc_service.py` (`__init__` line ~94; `prepare_parse` lines ~305-315)
- Test: `tests/unit/test_spec_doc_docx_pool.py`

**Interfaces:**
- Consumes: `DocxMemberPool`, `InlineDocxPool` (Task 3); `DocxConvertPool` (Task 4) for the integration test.
- Produces: `SpecDocService.__init__(..., docx_pool: DocxMemberPool | None = None)`. `None` is replaced by `InlineDocxPool()`.

`prepare_parse` replaces the list comprehension over `entries` with: `outcomes = self._docx_pool.convert_members("blocks", entries)`; then for each `(i, (name, _), outcome)`, `raise outcome` if it is an exception instance, else append `SpecDocFileBlocks(file_order=i, source_file=name, blocks=outcome)`. Everything after (ordering, TOC, chunks, return value) is unchanged.

- [ ] **Step 1: Write failing tests**

Build a small multi-DOCX ZIP in the test (use `Document()` as the existing `tests/unit/test_spec_doc_pipeline.py` does; put the docx members into a `zipfile` archive; create the service with `fetcher`, `cache_dir=tmp_path`, and a repo stub like the existing tests use).
- With `docx_pool=DocxConvertPool(2)` and with the default (no pool), `prepare_parse(...)` returns equal `toc`, `drafts`, and `ordered_files` (compare dataclass values).
- With a fake pool returning an `RuntimeError("boom")` outcome for the second member, `prepare_parse` raises `RuntimeError` with message `"boom"`.
- The fake pool receives `kind == "blocks"` and the members in ZIP order.

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_spec_doc_docx_pool.py -v`
Expected: FAIL (`docx_pool` keyword unknown).

- [ ] **Step 3: Implement**

Add the constructor keyword and the `prepare_parse` change per the Interfaces block. Import `InlineDocxPool` and `DocxMemberPool` from `doc3gpp.parsers.docx_converter`. Keep the `convert_document_to_blocks` import only if still used elsewhere in the file; otherwise remove it to keep ruff clean.

- [ ] **Step 4: Run tests and the existing spec-doc suites**

Run: `.venv/bin/python -m pytest tests/unit/test_spec_doc_docx_pool.py tests/unit/test_spec_doc_pipeline.py tests/unit/test_spec_doc_service_reads.py tests/integration/test_spec_doc_service.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/services/spec_doc_service.py tests/unit/test_spec_doc_docx_pool.py
git commit -m "feat(spec-doc): convert version DOCX members through docx pool"
```

---

### Task 7: TDoc ZIP conversion, family service, and CR service use the pool

**Files:**
- Modify: `src/doc3gpp/parsers/tdoc_zip_convert.py` (`convert_tdoc_zip` line ~97, `convert_direct_payload` line ~166, `convert_direct_main_markdown` line ~200)
- Modify: `src/doc3gpp/services/tdoc_family_service.py` (`__init__` line ~61; `prepare_extract` line ~129)
- Modify: `src/doc3gpp/services/tdoc_cr_service.py` (`__init__` line ~425; family construction line ~470; direct-payload calls at lines ~1332 and ~1359)
- Test: `tests/unit/test_tdoc_zip_convert.py` (extend), `tests/unit/test_tdoc_cr_service.py` (one test)

**Interfaces:**
- Consumes: `DocxMemberPool`, `InlineDocxPool`, `is_empty_text_error` (existing in `tdoc_zip_convert.py`).
- Produces:
  - `convert_tdoc_zip(zip_bytes, *, tdoc_id, identity, max_documents, max_uncompressed_bytes, convert=convert_document_to_markdown, docx_pool: DocxMemberPool | None = None) -> TDocZipConversion`
  - `convert_direct_payload(..., docx_pool: DocxMemberPool | None = None)` and `convert_direct_main_markdown(..., docx_pool: DocxMemberPool | None = None)` with the same keyword.
  - `TDocFamilyService.__init__(..., docx_pool: DocxMemberPool | None = None)`
  - `TDocCrService.__init__(..., docx_pool: DocxMemberPool | None = None)`; it passes the pool to the `TDocFamilyService` it builds, and to both direct-payload calls.

Outcome semantics for `convert_tdoc_zip`, preserving today's behaviour exactly:
- Members are `[(name, payloads[name]) for name in ordered_sources(tuple(payloads), main)]`, main first.
- Outcomes come from `docx_pool.convert_members("markdown", members)` when `docx_pool is not None and convert is convert_document_to_markdown`. Otherwise they come from an in-process loop calling `convert(payload, name)` and capturing `Exception` as the outcome (this keeps the injected-`convert` test seam).
- Each outcome is mapped the way `_convert_member` maps exceptions: an exception instance that satisfies `is_empty_text_error` becomes `""`.
- Main (index 0): any other exception instance is re-raised.
- Annexes (index >= 1): any other exception instance logs the same warning as today, appends the name to `skipped`, and continues.

- [ ] **Step 1: Write failing tests**

In `test_tdoc_zip_convert.py`:
- With a fake pool that returns `[markdown_main, exc_for_annex]`, the result has the main document and the annex name in `skipped_sources`.
- With a fake pool that returns `[RuntimeError(...)]` for the main, `convert_tdoc_zip` raises.
- A fake pool returning `[RuntimeError("python-docx returned empty result for annex.docx"), "text"]` yields an annex document with empty markdown and `skipped_sources == ()`.
- The fake pool receives kind `"markdown"` and members in main-first order.
- With `convert=` a custom function and a fake pool, the pool is not called (the injected `convert` is used).

In `test_tdoc_cr_service.py`, add one test: a `TDocCrService` built with `docx_pool=<fake>` passes the pool into the family service it constructs (assert via the fake being called for a family TDoc extract, or assert `service._family_service._docx_pool is fake`).

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_tdoc_zip_convert.py tests/unit/test_tdoc_cr_service.py -k docx_pool -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Implement the outcome semantics above inside `convert_tdoc_zip` and thread the `docx_pool` keyword through `convert_direct_payload`, `convert_direct_main_markdown`, `TDocFamilyService`, and `TDocCrService`. In `TDocFamilyService.prepare_extract`, pass `docx_pool=self._docx_pool` to `convert_tdoc_zip`. Keep the standalone `.docx` path (a single member) in-process: no pool is used for it. Check each direct-payload call site in `tdoc_cr_service.py` and pass the pool keyword.

- [ ] **Step 4: Run tests and the existing TDoc suites**

Run: `.venv/bin/python -m pytest tests/unit/test_tdoc_zip_convert.py tests/unit/test_tdoc_cr_service.py tests/unit/test_tdoc_parse_direct.py tests/unit/test_tdoc_pipeline.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/parsers/tdoc_zip_convert.py src/doc3gpp/services/tdoc_family_service.py src/doc3gpp/services/tdoc_cr_service.py tests/unit/test_tdoc_zip_convert.py tests/unit/test_tdoc_cr_service.py
git commit -m "feat(tdoc): convert ZIP members through docx pool"
```

---

### Task 8: Factory, web, and CLI wiring with pool lifecycle

**Files:**
- Modify: `src/doc3gpp/services/factory.py` (`build_spec_doc_service` line ~508; `build_tdoc_cr_service` line ~597)
- Modify: `src/doc3gpp/web/state.py` (`WebState` field next to `close_embedder`, line ~196)
- Modify: `src/doc3gpp/web/app.py` (`build_state`, and `lifespan` finally block line ~231)
- Modify: `src/doc3gpp/cli.py` (every `build_spec_doc_service(` call that parses, ~line 5469; every `build_tdoc_cr_service(` call that parses, lines ~2352, 2570, 2882, 3784, 4127, 4298; verify each by reading the command)
- Test: `tests/unit/test_web_app.py` (lifespan closes the pool)

**Interfaces:**
- Produces:
  - `build_spec_doc_service(..., docx_pool: DocxMemberPool | None = None)` and `build_tdoc_cr_service(..., docx_pool: DocxMemberPool | None = None)`, both forwarding the keyword to the service. Default `None` keeps every existing caller in-process.
  - `WebState.close_docx_pool: Callable[[], None] | None = None`.

Wiring:
- `build_state(settings)`: `docx_pool = DocxConvertPool(settings.pipeline.docx_workers)`; pass `docx_pool=docx_pool` to `factory.build_tdoc_cr_service(...)` and `factory.build_spec_doc_service(...)`; set `close_docx_pool=docx_pool.close` on `WebState`.
- `lifespan` `finally`: after `await handle.shutdown()`, call `state.close_docx_pool()` if it is not `None`, before the engine disposals.
- CLI: for each parsing command (`spec doc parse`, and the `tdoc parse` commands that call `build_tdoc_cr_service`), wrap the call in `with DocxConvertPool(get_settings().pipeline.docx_workers) as docx_pool:` and pass `docx_pool=docx_pool` to the builder. Non-parsing commands (`list`, `show`, `sync`, `index`, `schema`, and so on) are not changed.

- [ ] **Step 1: Write failing test**

In `tests/unit/test_web_app.py`, build the app with a `WebState` whose `close_docx_pool` is a recording fake (follow how the existing tests construct the app or patch `build_state`); run the lifespan to exit; assert the fake was called once. Also assert `build_state` returns a state whose `close_docx_pool` is set.

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_web_app.py -k docx -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Apply the wiring above. Read each CLI call site before editing; do not wrap commands that only read data. Keep `CLI` changes minimal: one `with` block per command body that parses, and pass the pool keyword.

- [ ] **Step 4: Run tests and the CLI / web suites**

Run: `.venv/bin/python -m pytest tests/unit/test_web_app.py tests/integration/test_spec_doc_cli.py tests/integration/test_spec_doc_web.py tests/unit/test_tdoc_parse_direct.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/services/factory.py src/doc3gpp/web/state.py src/doc3gpp/web/app.py src/doc3gpp/cli.py tests/unit/test_web_app.py
git commit -m "feat(wiring): share one docx pool per web and CLI process"
```

---

### Task 9: End-to-end equivalence, docs, benchmark, and verification

**Files:**
- Test: `tests/integration/test_spec_doc_docx_pool_equivalence.py`
- Modify: `docs/architecture.md` (pipeline section: conversion pool and server lifespan)
- Modify: `docs/cli.md` only if the `spec doc parse` or `tdoc parse` entries describe conversion as sequential
- Scratch only (not committed): benchmark run under `/tmp/opencode/spec_profile/`

- [ ] **Step 1: Write the end-to-end equivalence test**

Build a 4-member ZIP of generated docx files (reuse the golden builder and two extra variants). Run `SpecDocService.prepare_parse` once with no pool and once with `DocxConvertPool(2)`, using identical settings and repo stubs. Assert the TOC, the drafts, and the ordered files are equal. Add a TDoc equivalence case: `convert_tdoc_zip` on the same ZIP with `docx_pool=None` and `docx_pool=DocxConvertPool(2)` gives equal `TDocZipConversion` values.

- [ ] **Step 2: Run it**

Run: `.venv/bin/python -m pytest tests/integration/test_spec_doc_docx_pool_equivalence.py -v`
Expected: PASS.

- [ ] **Step 3: Update the docs**

- `docs/architecture.md`: describe `DocxConvertPool`, `InlineDocxPool`, and the single per-process pool created by `build_state` and by parsing CLI commands; state that `[pipeline] docx_workers` bounds conversion processes independently of `prepare_workers`.
- `docs/cli.md`: adjust only if a parse entry says conversion is sequential.

- [ ] **Step 4: Run the benchmark (scratch, not committed)**

Time `spec doc parse` on cached specs before and after (checkout `07e20ad` into a `git worktree` under `/tmp/opencode` for the "before" run, or rely on the earlier baseline numbers: 38.523-1/19.6.0 sequential about 379 s on four docx; 38.903/19.4.0 about 19.3 s conversion). Record before/after times for `38.523-1/19.6.0`, `38.533/19.4.0`, and `38.903/19.4.0` with `docx_workers` at 1, 2, and 4. Report the numbers to the user; do not commit them.

- [ ] **Step 5: Full verification**

Run: `./scripts/test_sqlite.sh`
Expected: all pass.
Run: `ruff check .`
Expected: no findings.

- [ ] **Step 6: Commit**

```bash
git add tests/integration/test_spec_doc_docx_pool_equivalence.py docs/architecture.md docs/cli.md
git commit -m "test(docs): add pool equivalence test and document conversion pool"
```

---

## Self-Review

**Spec coverage:**
- Hot-path fixes (spec §1): Task 2.
- Conversion pool and Protocol (spec §2): Tasks 3 and 4.
- Spec-doc integration (spec §3): Task 6. TDoc integration: Task 7.
- Settings and lifecycle (spec §4): Task 5 (setting), Task 8 (web, CLI, factory).
- Errors and memory (spec §5): Task 4 (broken-pool and error round-trip tests), Task 7 (annex and main semantics).
- Testing list (spec Testing section): golden (Tasks 1-2), pool unit (Task 4), equivalence (Tasks 4, 9), integration (Tasks 6, 7, 9), settings (Task 5), benchmark (Task 9).
- Docs (spec Documentation section): Task 5 (TOML example), Task 9 (architecture, CLI).

**Type consistency:** `MemberKind`, `DocxMemberPool.convert_members`, `DOCX_WORKERS`, `InlineDocxPool`, `DocxConvertPool(workers)`, `docx_pool` keyword, and `close_docx_pool` are used with identical names across Tasks 3-8.

**Gaps found and fixed in-plan:** the empty-text `RuntimeError` mapping in `_convert_member` is applied to pool outcomes in Task 7; the injected `convert` seam keeps the in-process path in Task 7.

**Scope check:** embedding batch parallelism and single-DOCX splitting stay out of scope, as in the spec.

## Execution Handoff

Plan saved to `docs/superpowers/plans/2026-10-28-spec-doc-parse-speed.md`. Please review the plan.

Which execution approach would you prefer?

- **Subagent-driven**: a fresh subagent implements each task, and a fresh reviewer checks each one before the next starts, then a whole-branch review at the end. Most thorough; costs a fresh context per task and per review.
- **Native**: I implement every task myself in this session, then one fresh reviewer checks the whole branch. Cheapest and fastest; no independent review until the end.

For this plan I recommend **subagent-driven**, because the tasks depend on each other's interfaces (the Protocol, the pool, and the `docx_pool` keyword threaded through five modules), and a mistake in the outcome semantics would silently change TDoc skip behaviour. Does the plan capture what you want, and which approach should we use?

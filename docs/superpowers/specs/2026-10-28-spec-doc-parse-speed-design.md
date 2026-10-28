# Spec-Doc Parse Speed-Up (DOCX Conversion)

**Status:** Design approved in conversation; spec pending user review.

## Problem

`doc3gpp spec doc parse` is slow on large specifications. A version ZIP can
hold 10-17 `.docx` files of 1-7 MB each, and the parse converts them one after
another. Measurements on the local cache:

- One 3 MB docx (38.903 / 19.4.0): conversion 19.3 s, list 0.02 s, chunking
  0.2 s. Conversion is about 99% of compute.
- Four docx of 38.523-1 / 19.6.0: sequential 379 s, `ThreadPoolExecutor(4)`
  293 s (GIL-bound), `ProcessPoolExecutor(4)` 178 s. Wall-clock is bounded by
  the largest single file.
- Profiling the converter shows avoidable work on the hot path: `row.cells` is
  recomputed for every table cell, each paragraph's style is looked up twice,
  and `qn(...)` tags are rebuilt for every node.

Chunking is about 1% of parse time, so parallelising chunks alone is not worth
the complexity. The cost is DOCX conversion (python-docx), which is CPU-bound
Python and therefore needs processes rather than threads.

The same conversion loop exists in TDoc ZIP extraction, so the fix is shared.

## Goals

1. Reduce wall-clock time of spec-doc parse on multi-docx ZIPs.
2. Reduce single-thread conversion cost for every DOCX, spec and TDoc alike.
3. Keep outputs byte-identical: same blocks, markdown, TOC, chunks, skip and
   failure semantics, and result ordering.
4. Bound total conversion processes per server or CLI process, regardless of how
   many prepare tasks run concurrently.

## Scope

### Included

- Behaviour-preserving hot-path fixes in `src/doc3gpp/parsers/docx_converter.py`.
- A shared process pool for per-DOCX conversion, used by:
  - `SpecDocService.prepare_parse` (spec-doc parse);
  - `convert_tdoc_zip` callers (TDoc family extraction and CR extraction).
- A new setting `[pipeline] docx_workers`.
- Tests, a before/after benchmark (not committed), and documentation updates.

### Not included

- Parallelising the embedding batches in `OpenAICompatibleEmbedder.encode`.
- Splitting a single large DOCX across processes.
- Changing the chunker, the TOC extraction, or the persistence lane.
- Changing public CLI commands or HTTP/MCP contracts.

## Design

### 1. Hot-path fixes (`parsers/docx_converter.py`)

All changes are internal and keep output identical:

- `_table_to_markdown`: call `row.cells` once per row and index into the
  resulting tuple, instead of calling it for every cell index.
- Paragraph style: `_style_name(p)` is evaluated once per paragraph and the
  lower-cased result is reused. Style names are memoised by `w:pStyle` id for
  the duration of one conversion call, so the style-table scan runs once per
  style rather than once per paragraph.
- `qn("w:...")` tags used in rendering are module-level constants.

These fixes benefit `convert_document_to_blocks` and
`convert_document_to_markdown`, which share `_paragraph_to_markdown`,
`_table_to_markdown`, and `_render_children`.

### 2. Conversion pool (`services/docx_convert_pool.py`, new)

A small class `DocxConvertPool` wraps a lazily created
`concurrent.futures.ProcessPoolExecutor`:

```text
DocxConvertPool(workers: int)
    convert_members(kind, members) -> list[Outcome]
    close() -> None
```

- `kind` is `"blocks"` or `"markdown"`. It selects one of two top-level,
  picklable worker functions: `convert_document_to_blocks` and
  `convert_document_to_markdown` in `parsers/docx_converter.py`. These are pure
  functions with no I/O, so they respect the `parsers/` rule.
- `members` is a sequence of `(filename, payload_bytes)`.
- The result list is in input order. Each outcome is either the converted value
  or the exception raised for that member, so each caller decides the policy.
- The executor uses the `spawn` multiprocessing context. Forking from a
  threaded process is unsafe, and the web server runs prepares on threads.
- The executor is created on first use. After `BrokenProcessPool`, the affected
  members get a `DocxConvertPoolError` outcome and the executor is reset so the
  next call creates a fresh one.
- `workers <= 1` means no executor at all. `convert_members` runs in-process,
  sequentially, exactly like today.

The pool is an optional constructor dependency (`docx_pool: DocxConvertPool |
None = None`) of `SpecDocService`, `TDocFamilyService`, and the TDoc CR service.
`None` means in-process conversion, so existing call sites and tests keep their
behaviour.

### 3. Integration points

**Spec-doc (`SpecDocService.prepare_parse`).** Each `.docx` member of the
version ZIP is converted with `kind="blocks"` through `convert_members`. Any
member error is re-raised, which preserves the current failure behaviour. The
converted `SpecDocFileBlocks` list keeps `file_order` as produced by
`order_spec_files`, so TOC and chunk output are unchanged.

**TDoc ZIP conversion (`convert_tdoc_zip`).** The main document is converted
first with `kind="markdown"`, and its error propagates as today. The annexes
(`ordered_sources(...)[1:]`) are converted through the pool. An annex error is
logged, recorded in `skipped_sources`, and does not fail the archive, as today.
When a caller injects a non-default `convert` callable, which tests do, the
conversion runs in-process. That keeps the existing injection seam working
without requiring picklable callables.

**Direct-mode TDoc parse.** `convert_direct_payload` and
`convert_direct_main_markdown` receive the same optional pool through their
callers. A standalone `.docx` is a single member and runs in-process.

### 4. Settings and lifecycle

- `PipelineSettings` (`settings/schema.py`, `[pipeline]` block) gains
  `docx_workers: int`, default `min(4, os.cpu_count())`, bounds `0..32`.
  `0` and `1` disable the pool. This field is independent of `prepare_workers`
  and `server.max_concurrent_jobs`.
- Web server: `build_state` (`web/app.py`) builds one `DocxConvertPool` and
  injects it into the services it builds. The pool is closed during lifespan
  shutdown, after the `PipelineRuntime` has drained and before database engines
  are disposed.
- CLI: `spec doc parse`, `tdoc parse` (database and direct modes) create a pool
  per invocation and close it in a `finally` block. Commands that never convert
  DOCX (`list`, `show`, `sync`, `index`, and so on) create no pool.
- Service factories (`services/factory.py`: `build_spec_doc_service`, the TDoc
  family and CR builders) accept an optional `docx_pool` and pass it through.

Because one pool exists per process and is shared by all concurrent prepares,
the total number of conversion processes is `docx_workers`, regardless of
`prepare_workers` or the number of queued jobs.

### 5. Errors and memory

- Exceptions raised in a worker are pickled back and re-raised in the parent as
  the member's outcome, with the original type and message preserved.
- A dead worker (`BrokenProcessPool`) fails the members outstanding in the window
  of each `convert_members` call (at most `2 × docx_workers` members per call),
  plus the member whose own submit hit the break, with a clear
  `DocxConvertPoolError`. Callers share one executor, so the same break can also
  fail other concurrent callers' unfinished members. Members not yet submitted
  run on a fresh executor. The next call recreates the pool.
- Memory: each member payload is already in memory from the ZIP read. Pickling
  adds one copy per in-flight member. Peak extra memory is roughly
  `2 × docx_workers` × the largest uncompressed member, plus the returned blocks.
  This is acceptable for the observed 1-7 MB members and bounded by the knob.

## Testing

Written test-first. The test list is:

- **Pool unit tests** (`tests/unit/test_docx_convert_pool.py`): input order is
  preserved; per-member exceptions come back as outcomes; `workers <= 1` runs
  in-process; `close()` is idempotent; a `BrokenProcessPool` maps to
  `DocxConvertPoolError` and the pool recovers.
- **Equivalence** (pool vs sequential): for a set of generated DOCX files,
  `kind="blocks"` and `kind="markdown"` outputs are identical in both modes.
- **Hot-path golden tests** (`tests/unit/test_docx_converter_golden.py`):
  expected markdown and blocks are captured from the current converter before
  any edit and committed as fixtures. The fixture DOCX files are built with
  python-docx and cover headings, styled paragraphs, bold and italic runs,
  tracked insertions and deletions, and merged table cells (`gridSpan` and
  `vMerge`). The changed converter must reproduce them byte-for-byte.
- **Integration**: spec-doc parse end-to-end on a generated multi-docx ZIP
  (pool on and off give identical stored TOC and chunks); TDoc ZIP extraction
  with an annex failure still records `skipped_sources`; a main failure still
  fails the TDoc.
- **Settings**: `docx_workers` defaults, bounds, and TOML override, following
  the existing `prepare_workers` tests in `tests/unit/test_settings.py` and
  `tests/unit/test_server_settings.py`.
- **Benchmark (manual, not committed)**: before/after timings on cached ZIPs
  38.523-1/19.6.0, 38.533/19.4.0, and 38.903/19.4.0, recorded in the PR
  description.

Full suite: `./scripts/test_sqlite.sh`. Lint: `ruff check .`.

## Documentation

Per `AGENTS.md`, update in the same change:

- `src/doc3gpp/data/doc3gpp.toml.example`: the `[pipeline] docx_workers` key.
- `docs/architecture.md`: the conversion pool in the pipeline section, and its
  place in the server lifespan.
- `docs/cli.md`: only if the `spec doc parse` or `tdoc parse` entries describe
  per-document conversion as sequential.

## Risks

- **Spawn start-up cost.** Each worker imports `doc3gpp.parsers.docx_converter`
  once. This is a one-time cost per process and small compared with a
  multi-minute parse.
- **Process pool on macOS and Windows.** `spawn` works on all platforms, but
  the worker entry point must stay a top-level function in an importable module.
- **Largest-file bound.** Wall-clock is still limited by the largest single
  DOCX. This is accepted; splitting one document is out of scope.
- **Behaviour drift.** The golden fixtures and pool-vs-sequential equivalence
  tests are the guard against this.

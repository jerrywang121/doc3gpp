# TDoc Type Contents Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Store the Word documents of WID, LS, and discussion TDocs (main and annexes), and the annexes of CR TDocs, in four per-type SQL content tables. Make that content searchable through FTS5 and chunked semantic search, and return it as raw Markdown on every surface. The CR main Markdown stays in the derived cache archive only; its cover, change, and TTCN fields remain in SQL.

**Architecture:** An eligibility registry maps `tdocs.type` to one content store: `tdoc_cr_annexes` (CR), `tdoc_wid_contents`, `tdoc_ls_contents`, or `tdoc_discussion_contents`. Each TDoc ZIP URL has one `tdoc_extracts` parent (`extraction_version = 2`, `document_count`, `skipped_sources`). The four content tables share one row shape. A pure ZIP conversion keeps every converted member in the archive and returns the SQL set: annexes only for CR, main plus annexes for families. Search reads the union of the four tables. Semantic search adds labelled chunks for every stored document. Content retrieval reads SQL, plus the CR main from the archive.

**Tech Stack:** Python 3.10+, SQLAlchemy 2.0, SQLite FTS5 and sqlite-vec, Pydantic v2, Typer, FastAPI, FastMCP, python-docx (optional `extract` extra), pytest.

**Spec:** `docs/superpowers/specs/2026-10-22-tdoc-type-contents-design.md` (approved 2026-10-23).

---

## Global Constraints

- **Eligible `tdocs.type`** (case-insensitive exact match after stripping whitespace): `CR` → `tdoc_cr_annexes` (annexes only); `WID new`, `WID revised` → `tdoc_wid_contents`; `LS in`, `LS out` → `tdoc_ls_contents`; `discussion` → `tdoc_discussion_contents`. Every other type, including `other`, is never downloaded and never stored.
- **Shared content row shape** (all four tables): `ftp_url` String(1024) (PK part, FK `tdoc_extracts.ftp_url` ON DELETE CASCADE); `doc_filename` String(1024) (PK part, full case-preserving member path); `document_id` String(64) = `document_id_for(ftp_url, doc_filename)`; `tdoc_id` String(64) (FK `tdocs.tdoc_id` ON DELETE CASCADE, indexed); `content` LargeBinary = `compress_json(markdown)`; `content_hash` String(64) = SHA-256 of the UTF-8 Markdown. **No** `file_order`, `markdown_file`, `markdown_text`, or `tdoc_documents` column anywhere.
- **Derived on read, not stored:** order is main first, then the rest by `source_sort_key`. `markdown_file` is `documents/<document_id>/<sanitized_source_stem>.md`.
- **Naming** for every type, including the CR main in the archive: `sanitized_source_stem` (basename without `.docx`; characters outside `[A-Za-z0-9._-]` become `_`; empty result becomes `document`).
- **Parent** `tdoc_extracts`: one row per immutable ZIP URL. `doc_filename` is the main path for every type. `extraction_version = 2` for new bundles. `document_count` = number of stored content rows (CR: annex count; family: main plus annexes). `skipped_sources` nullable JSON list (`NULL` and `[]` both mean none).
- **CR main** is never written to SQL. The CR cover, TTCN, and change sidecars are written as today.
- **Archive** `markdown/<cache_file>` is a versioned multi-member ZIP with a manifest. It holds every converted `.docx` member, including the CR main. Extracted members never touch the filesystem.
- **Eligibility of ZIP members:** only `.docx` is converted. `.doc`, PDF, spreadsheets, and nested ZIPs are never converted. Limits reuse `tdoc_parse.max_documents_per_zip` (default `100`), `max_uncompressed_size_kb` (default `102400`), `max_tdoc_size_kb` (default `1000`); `0` disables each.
- **Chunking:** `semantic_search.chunk_size = 512`, `chunk_overlap = 24`. Metadata chunks keep `max_chunks_per_tdoc = 8`. Content chunks use the new TOML-only `semantic_search.max_content_chunks_per_tdoc` (default `16`, `ge=1`).
- **FTS:** column `content_text` (renamed from `document_text`). The nine positional BM25 weights are unchanged. Changing the layout recreates `tdoc_search`; run `tdoc index --rebuild` afterwards.
- **Layering:** ORM-free dataclasses in `models/`; protocols in `repository/protocols.py`; SQL in `storage/repositories/`; services never handle ORM objects; the pure conversion lives in `parsers/` and does no network I/O.
- **Pre-branch baseline:** `main` at `59273b5`. The intermediate branch schema (`tdoc_documents`, `document_text`) was never deployed and is not a migration source.
- **TDD in every task:** write the failing test, run it and see it fail, implement the minimum, rerun the focused tests, then rerun the listed regression tests.
- **Test command:** `.venv/bin/python -m pytest -q -p no:cacheprovider <paths>`. **Full suite:** `.venv/bin/python -m pytest -q -m "not online" -p no:cacheprovider`. **Lint:** `.venv/bin/ruff check .`.
- **No commit steps.** Repository policy requires an explicit user request. Leave `package-lock.json` untouched.
- **Rename audit** (Task 16 final check): `grep -rn "tdoc_documents\|document_text\|TDocDocumentOrm" src/ tests/ docs/ README.md AGENTS.md` returns no hits except in historical `docs/superpowers/specs/2026-10-22-tdoc-multi-file-zip-design.md` and `docs/superpowers/plans/2026-10-22-tdoc-multifile-markdown.md`.

## Review Focus

Seven behaviours that the spec implies and that a test must pin. Each names the owning task and test.

1. A TDoc whose only main-named ZIP member is `.doc` fails with a reason naming that member. Owned by Task 5 (`test_doc_only_main_reports_not_docx`) and Task 6 (`test_doc_only_main_fails_archive_with_docx_reason`).
2. A zero-text main or zero-text annex is stored as empty content, adds no content chunks, and does not break FTS or semantic indexing. Owned by Task 4 (`test_empty_markdown_roundtrips`), Task 11 (`test_zero_text_document_indexes_without_error`), and Task 12 (`test_zero_text_document_adds_no_content_chunks`).
3. Member names with spaces, non-ASCII characters, or `>` round-trip through SQL. Raw headers are one-line JSON with `\u003e`. Owned by Task 4 (`test_replace_family_bundle_roundtrips_special_names`) and Task 14 (`test_cli_raw_multi_document_named_blocks`).
4. Content over `max_content_chunks_per_tdoc` is truncated for semantic indexing without error, and FTS still finds text beyond the cap. Owned by Task 12 (`test_content_chunks_capped`) and Task 11 (`test_fts_finds_text_beyond_semantic_cap`).
5. A main inside a folder (`R5-123456/R5-123456.docx`) keeps its full member path as `doc_filename`. Selection and archive paths still work. Owned by Task 5 (`test_main_inside_folder_uses_basename`) and Task 7 (`test_family_main_in_folder_stored_with_full_path`).
6. A term only in the CR main body is not found by FTS, while CR cover, change, and TTCN terms are found. This is the documented consequence of spec §8. Owned by Task 11 (`test_cr_main_only_term_not_indexed`).
7. After a cache purge, raw CR main output fails with the cache-miss re-parse hint. Annexes are not returned as a partial envelope. Owned by Task 13 (`test_cr_raw_without_archive_is_cache_miss`).

## Decisions (resolutions of spec gaps; each is a choice the implementer must not re-decide)

- **P1 Document identity.** `document_id = document_id_for(ftp_url, doc_filename)` is stored on every content row (`models/tdoc_identity.py`). It is the join key for FTS, chunk IDs, and matched documents.
- **P2 Chunk IDs.** Content chunks (CR annexes and family documents) use `<tdoc_id>#<document_id>#<content_hash>#<i>`, where `i` is the position within its own document. Metadata chunks keep `<tdoc_id>#<i>`. CR main gets no content chunks. Parsing splits on `#`.
- **P3 Skipped sources.** `TDocExtractMeta.skipped_sources: tuple[str, ...] = ()` maps to nullable `tdoc_extracts.skipped_sources` (JSON array). Empty is stored as `NULL`; `'[]'` reads as empty.
- **P4 Result types.** CR results stay `ExtractResult` (gains a `tdoc_id` property). Family results are `FamilyExtractResult`. Batch success keys and every completeness check use `tdoc_id` and accept both classes.
- **P5 Explicit ID selection.** `tdoc parse --tdoc <exact id>` with no `--type` skips the eligibility filter, so an ineligible TDoc reaches the pipeline. Its type check raises `TDocTypeUnsupportedError` before any download, and the pipeline classifies it as skipped. Pattern selectors, web filters, and MCP filters apply the eligibility filter, so ineligible rows are never selected.
- **P6 Completeness.** One Python rule per store: `is_complete_cr_extraction` and `is_complete_family_extraction` (Task 1). Each has a SQL mirror (Task 9). `parsed`, `exclude_parsed`, and `--parsed` use the rule for the TDoc's type.
- **P7 Direct modes.** `--from-path` never persists, matching the documented local-file rule. Family and CR results from local files are in memory only. `--from-url` persists when a `tdocs` row exists. Steps 2–4 of the direct-mode procedure run through `trigger_auto_sync` in both direct modes when `sync.auto_sync` is on. This changes `--from-path` behaviour, so AGENTS.md and `docs/cli.md` are corrected in Task 16. Approved by the user.
- **P8 Default list fields.** `parsed` joins the default `tdoc list` fields. `document_count` is available through `--fields` only.
- **P9 CR main not in SQL.** A CR cache hit requires the v2 parent, the cover row, and the archive file `markdown/<cache_file>`. Without the archive, normal parse re-downloads and rewrites it.
- **P10 `document_count` and completeness.** `document_count` is the number of stored content rows. `TDocExtractMeta.complete` (`models/tdoc_cr.py:306`) changes from `extraction_version == 2 and document_count > 0` to `extraction_version == 2 and not skipped_sources`, because a CR with zero annexes is complete.
- **P11 Renames.** `tdoc_documents` (branch only) is replaced by the four content tables and has no migration. `document_text` becomes `content_text` in the FTS DDL, `_SNIPPET_COLUMN_NAMES`, the `previews` key, docs, and tests.
- **P12 Replacement scope.** `replace_cr_bundle` and `replace_family_bundle` delete rows for the `ftp_url` from all four content tables, then insert. The spec says "this type's table(s)". Deleting from all four is a superset that yields the same result when the type is unchanged, and it also clears rows left by an earlier type. Approved by the user.
- **P13 Tolerant annexes for CR (approved by the user).** Today `_direct_parse_payload` (`parsers/direct_extractor.py:537-546`) converts members in order and raises `ValueError` on the first failure, so one bad annex fails the whole CR TDoc. The new rule converts every member and records each failure instead of raising. The CR main is then found structurally (`is_cr_header_present`) among the successful members. If exactly one main is found, each failed member becomes a `skipped_sources` entry with a logged warning, and the TDoc succeeds with `complete` false. A failed member whose basename starts with the TDoc number is treated as a possible main and fails the TDoc, because its failure would otherwise leave the main unidentified. No main, or several mains, fails the TDoc as today. Consequence: an annex that always fails is retried by every normal `tdoc parse` until it converts, and each run reports it as a warning under success.
- **P14 Pure helpers.** `sanitized_source_stem`, `source_sort_key`, and `markdown_member_name` live in `models/tdoc_document.py`. Storage derives `markdown_file` and ordering without importing `parsers/`. `parsers/tdoc_zip.py` imports them and drops its private copies.

---

## File Map and Dependency Order

1. Registry and domain: Tasks 1, 2 (`models/tdoc_content_families.py`, `models/tdoc_document.py`, `models/tdoc_cr.py`).
2. Durable storage: Tasks 3, 4 (`storage/db/models.py`, `storage/db/migrate.py`, `storage/repositories/tdoc_content_sql.py` (new), `storage/repositories/tdoc_cr_sql.py`, `storage/repositories/tdoc_extraction_sql.py` (replaced), `repository/protocols.py`, `services/factory.py`).
3. Extraction: Tasks 5, 6, 7, 8 (`parsers/tdoc_family_main.py` (new), `parsers/tdoc_zip_convert.py` (new), `services/tdoc_family_service.py` (new), `services/tdoc_cr_service.py`, `parsers/direct_extractor.py`, `services/tdoc_pipeline.py`).
4. Selection: Tasks 9, 10 (`storage/repositories/tdoc_sql.py`, `services/tdoc_parse_selection.py` (new), `cli.py`, `web/workers/handlers.py`, `web/routes/tdocs.py`, `web/mcp_server.py`).
5. Search: Tasks 11, 12 (`storage/repositories/tdoc_document_sources.py` (new), `storage/repositories/search_sql.py`, `storage/repositories/vector_sql.py`, `services/semantic_search_service.py`, `services/tdoc_search_facade.py`, `settings/schema.py`).
6. Content: Tasks 13, 14 (`services/tdoc_content_service.py`, `cli.py`, `web/routes/tdocs.py`, `web/mcp_server.py`).
7. Direct mode: Task 15 (`services/tdoc_type_detection.py` (new), `services/tdoc_cr_service.py`, `cli.py`).
8. Schema, settings, docs: Task 16. Verification: Task 17.

Dependencies: 3 needs 1; 4 needs 2 and 3; 6 needs 5; 7 needs 4 and 6; 8 needs 4 and 6; 9 needs 3; 11 needs 3 and 9; 12 needs 11; 13 needs 4 and 2; 14 needs 13; 15 needs 6, 7, and 8.

---

## Task 1: Content Registry, Eligibility, and Completeness Rules

**Files:**
- Create: `src/doc3gpp/models/tdoc_content_families.py`
- Test: `tests/unit/test_tdoc_content_families.py` (new)

**Interfaces:**
- Consumes: `TDOC_TYPES` in `src/doc3gpp/models/schema_info.py` (test only); `TDocExtractMeta` and `TDocDocument` (Task 2 and Task 3 add fields).
- Produces:
  - `@dataclass(frozen=True, slots=True) class TDocContentStore`: `key: str` (`"cr"`, `"wid"`, `"ls"`, `"discussion"`), `table_name: str`, `type_values: tuple[str, ...]`, `includes_main: bool` (`False` for `cr`, `True` for the three families).
  - `TDOC_CONTENT_STORES: tuple[TDocContentStore, ...]` in the order cr, wid, ls, discussion.
  - `FAMILY_STORES` = the three family entries.
  - `store_for_type(tdoc_type: str | None) -> TDocContentStore | None` (casefold and strip; `None`, blank, or unmatched returns `None`).
  - `is_eligible_type(tdoc_type: str | None) -> bool`.
  - `eligible_type_values() -> tuple[str, ...]` returns exactly `("CR", "WID new", "WID revised", "LS in", "LS out", "discussion")`.
  - `is_complete_cr_extraction(meta, annexes, *, has_cover: bool) -> bool`: `meta.extraction_version == 2`; `not meta.skipped_sources`; `has_cover`; `meta.document_count == len(annexes)`; no annex `source_file` equals `meta.doc_filename`; `annexes` may be empty.
  - `is_complete_family_extraction(meta, documents) -> bool`: `meta.extraction_version == 2`; `not meta.skipped_sources`; `documents` non-empty; `meta.document_count == len(documents)`; sources unique; exactly one source equals `meta.doc_filename`; that source is first.

- [ ] **Step 1: Write the failing tests**

```python
from types import SimpleNamespace
import pytest
from doc3gpp.models.schema_info import TDOC_TYPES
from doc3gpp.models.tdoc_content_families import (
    TDOC_CONTENT_STORES, eligible_type_values, is_complete_cr_extraction,
    is_complete_family_extraction, is_eligible_type, store_for_type,
)

def _meta(**kw):
    base = dict(ftp_url="x", tdoc_id="R5-000001", doc_filename="main.docx",
                extraction_version=2, document_count=0, skipped_sources=())
    base.update(kw)
    return SimpleNamespace(**base)

def _doc(source):
    return SimpleNamespace(source_file=source)

def test_store_lookup_is_case_insensitive_and_strips():
    assert store_for_type("WID new").table_name == "tdoc_wid_contents"
    assert store_for_type(" wid revised ").key == "wid"
    assert store_for_type("LS out").table_name == "tdoc_ls_contents"
    assert store_for_type("Discussion").table_name == "tdoc_discussion_contents"
    assert store_for_type("cr").table_name == "tdoc_cr_annexes"
    assert store_for_type("cr").includes_main is False

def test_unmatched_types_have_no_store():
    for value in ("other", "pCR", "", None):
        assert store_for_type(value) is None

def test_eligibility_and_exact_value_tuple():
    assert is_eligible_type("cr") and is_eligible_type("LS in")
    assert not is_eligible_type("other")
    assert eligible_type_values() == ("CR", "WID new", "WID revised", "LS in", "LS out", "discussion")

def test_store_values_are_known_tdoc_types():
    known = {t.casefold() for t in TDOC_TYPES}
    for store in TDOC_CONTENT_STORES:
        assert {v.casefold() for v in store.type_values} <= known

@pytest.mark.parametrize("kwargs, expected", [
    ({}, True),
    ({"skipped_sources": ("a.docx",)}, False),
    ({"extraction_version": 1}, False),
    ({"document_count": 2}, False),
])
def test_family_rule_parametrized(kwargs, expected):
    docs = [_doc("main.docx")]
    meta = _meta(document_count=1, **kwargs)
    assert is_complete_family_extraction(meta, docs) is expected

def test_family_rule_rejects_missing_or_unordered_main():
    assert not is_complete_family_extraction(_meta(document_count=2), [_doc("a.docx"), _doc("main.docx")])
    assert not is_complete_family_extraction(_meta(document_count=0), [])

def test_cr_rule_zero_annexes_complete_with_cover():
    assert is_complete_cr_extraction(_meta(document_count=0), [], has_cover=True)
    assert not is_complete_cr_extraction(_meta(document_count=0), [], has_cover=False)

def test_cr_rule_rejects_main_among_annexes_and_count_mismatch():
    assert not is_complete_cr_extraction(_meta(document_count=1), [_doc("main.docx")], has_cover=True)
    assert not is_complete_cr_extraction(_meta(document_count=2), [_doc("a.docx")], has_cover=True)
    assert not is_complete_cr_extraction(_meta(document_count=1, skipped_sources=("b.docx",)), [_doc("a.docx")], has_cover=True)
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_tdoc_content_families.py -q -p no:cacheprovider`
Expected: FAIL with `ModuleNotFoundError: doc3gpp.models.tdoc_content_families`.

- [ ] **Step 3: Implement the module**

Create the module with the interfaces above. Matching is `value.strip().casefold()` against each store's `type_values` after casefolding. The module imports nothing from ORM or services. The completeness functions use attribute access only, so they accept `TDocExtractMeta` and `TDocDocument` values.

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_tdoc_content_families.py -q -p no:cacheprovider`
Expected: PASS.

---

## Task 2: Naming and Ordering Helpers

**Files:**
- Modify: `src/doc3gpp/models/tdoc_document.py` (`_source_file_sort_key` at line 40; add public helpers)
- Modify: `src/doc3gpp/parsers/tdoc_zip.py` (`_path_sort_key` at line 59; `_sanitized_source_stem` at line 234; `_markdown_member_for` at line 241; `_ordered_documents` at line 248)
- Modify: `src/doc3gpp/parsers/direct_extractor.py` (stem logic near lines 590-591)
- Test: `tests/unit/test_tdoc_naming.py` (new)

**Interfaces:**
- Produces in `models/tdoc_document.py`:
  - `sanitized_source_stem(source_file: str) -> str`: basename without a case-insensitive `.docx` suffix; characters outside `[A-Za-z0-9._-]` (runs) become `_`; strip `._-`; empty result becomes `"document"`.
  - `source_sort_key(source_file: str) -> tuple[str, str]`: casefold of the path, then the original path. This replaces `_source_file_sort_key` and `_path_sort_key`.
  - `markdown_member_name(document_id: str, source_file: str) -> str`: `f"documents/{document_id}/{sanitized_source_stem(source_file)}.md"`.
  - `ordered_sources(sources: Sequence[str], main: str | None) -> tuple[str, ...]`: main first when present, then the rest by `source_sort_key`.
- `parsers/tdoc_zip.py` imports these and deletes its private copies. Its behaviour does not change.

- [ ] **Step 1: Write the failing tests**

```python
from doc3gpp.models.tdoc_document import (
    markdown_member_name, ordered_sources, sanitized_source_stem, source_sort_key,
)
from doc3gpp.parsers.tdoc_zip import _markdown_member_for  # regression anchor

def test_stem_strips_docx_and_sanitizes():
    assert sanitized_source_stem("R5-123456.docx") == "R5-123456"
    assert sanitized_source_stem("R5-123456.DOCX") == "R5-123456"
    assert sanitized_source_stem("annex/with space.docx") == "with_space"
    assert sanitized_source_stem("annex/Ünïcode > x.docx") == "n_code_x"

def test_empty_stem_falls_back_to_document():
    assert sanitized_source_stem("..docx") == "document"

def test_ordering_is_main_first_then_casefold_path():
    sources = ["annex/b.docx", "Annex/A.docx", "main.docx"]
    assert ordered_sources(sources, "main.docx") == ("main.docx", "Annex/A.docx", "annex/b.docx")

def test_member_name_uses_document_id_and_stem():
    assert markdown_member_name("ab" * 32, "CRs/R5-1.docx") == f"documents/{'ab' * 32}/R5-1.md"

def test_parser_member_name_matches_shared_helper():
    # _markdown_member_for must produce the same stem as the shared helper
    ...  # build a TDocDocument for "annex/with space.docx" and compare the .md member suffix
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_tdoc_naming.py -q -p no:cacheprovider`
Expected: FAIL with `ImportError` on the new names.

- [ ] **Step 3: Implement and redirect the parser**

Move the stem regex and fallback into `sanitized_source_stem`. Replace `_path_sort_key` and `_source_file_sort_key` with `source_sort_key`. Update `parsers/tdoc_zip.py` and `parsers/direct_extractor.py` to import the shared names. Run `grep -rn "_path_sort_key\|_source_file_sort_key\|_sanitized_source_stem" src/ tests/` and update every reference.

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_tdoc_naming.py tests/unit/test_tdoc_zip.py -q -p no:cacheprovider`
Expected: PASS.

---

## Task 3: Durable Schema, Migration, and Parent Fields

**Files:**
- Modify: `src/doc3gpp/storage/db/models.py` (`TDocDocumentOrm` at line 346 is removed; module docstring at line 311 updated; add four content ORM classes; `TDocExtractOrm` at line 322 gains `skipped_sources`)
- Modify: `src/doc3gpp/storage/db/migrate.py` (line 16 import of `TDocDocumentOrm` replaced; `_migrate_tdoc_extract_completeness` at lines 488-514 additions tuple extended)
- Modify: `src/doc3gpp/models/tdoc_cr.py` (`TDocExtractMeta` at line 226; add `skipped_sources`; `complete` at line 306)
- Modify: `src/doc3gpp/storage/repositories/tdoc_cr_sql.py` (`_meta_to_orm` and the read counterpart map `skipped_sources`)
- Test: `tests/integration/test_tdoc_content_schema_sqlite.py` (new), `tests/unit/test_tdoc_extract_meta.py` (new)

**Interfaces:**
- Produces ORM classes sharing one column mixin: `TDocCrAnnexOrm` (`tdoc_cr_annexes`), `TDocWidContentOrm` (`tdoc_wid_contents`), `TDocLsContentOrm` (`tdoc_ls_contents`), `TDocDiscussionContentOrm` (`tdoc_discussion_contents`). Each has columns from the shared row shape, a composite primary key `(ftp_url, doc_filename)`, a foreign key `ftp_url` to `tdoc_extracts.ftp_url` (`ondelete="CASCADE"`), and a foreign key `tdoc_id` to `tdocs.tdoc_id` (`ondelete="CASCADE"`, indexed).
- `TDocExtractOrm.skipped_sources: Mapped[str | None]` (`Text`, nullable). `extraction_version` and `document_count` already exist from the branch, with defaults 1 and 0.
- `TDocExtractMeta.skipped_sources: tuple[str, ...] = ()`. Each entry must be a non-empty string. `complete` returns `self.extraction_version == 2 and not self.skipped_sources`.
- Migration: `_migrate_tdoc_extract_completeness` adds `("skipped_sources", "TEXT")` to its additions tuple. It keeps the probe-then-`ALTER TABLE ... ADD COLUMN` pattern, and the nullable column has no default.

- [ ] **Step 1: Write the failing tests**

- `test_create_schema_creates_four_content_tables(sqlite_env)`: each table exists; its column set is `{ftp_url, doc_filename, document_id, tdoc_id, content, content_hash}`; its primary key is `("ftp_url", "doc_filename")`; `tdoc_id` has a foreign key to `tdocs.tdoc_id` with `ondelete == "CASCADE"`.
- `test_no_tdoc_documents_table(sqlite_env)`: `tdoc_documents` does not exist.
- `test_tdoc_extracts_has_nullable_skipped_sources(sqlite_env)`.
- `test_create_schema_is_idempotent(sqlite_env)`.
- `test_deleting_tdoc_cascades_to_content(sqlite_env)`: insert a tdocs row, a tdoc_extracts row, and one `tdoc_wid_contents` row; delete the tdocs row; the content row is gone.
- `test_pre_branch_upgrade_adds_new_columns(sqlite_env)`: after `create_schema`, `ALTER TABLE tdoc_extracts DROP COLUMN` the three new columns and drop the four content tables; insert one parent row; rerun `create_schema`. Assert the three columns exist, the pre-existing row reads `extraction_version == 1`, `document_count == 0`, `skipped_sources IS NULL`, and `TDocExtractMeta.complete` is `False`. A second run is a no-op.
- `test_extract_meta_skipped_sources_validation()`: `()` by default; `("annex/bad.docx",)` is accepted; `("",)` raises `ValueError`.
- `test_extract_meta_complete_rule()`: v2 with no skipped sources is complete even with `document_count == 0`; v2 with skipped sources is not complete; v1 is not complete.
- `test_skipped_sources_roundtrip(sqlite_env)`: upsert with `("annex/bad.docx",)`, read it back; upsert with `()` and check the raw column is `NULL`.

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `.venv/bin/python -m pytest tests/integration/test_tdoc_content_schema_sqlite.py tests/unit/test_tdoc_extract_meta.py -q -p no:cacheprovider`
Expected: FAIL.

- [ ] **Step 3: Implement the ORM, migration, and domain field**

Remove `TDocDocumentOrm` and its unique constraints. Add the mixin and the four classes. Add the migration tuple entry. Add the `TDocExtractMeta` field and the `complete` rule. Map `skipped_sources` in `_meta_to_orm` and the reader (empty tuple stored as `NULL`; `NULL` or `'[]'` read as `()`). Update the import in `migrate.py` line 16.

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `.venv/bin/python -m pytest tests/integration/test_tdoc_content_schema_sqlite.py tests/unit/test_tdoc_extract_meta.py -q -p no:cacheprovider`
Expected: PASS. Existing CR meta tests may need `complete` expectations updated (P10); fix them in this task.

---

## Task 4: Content Repository and Atomic Replacement

**Files:**
- Modify: `src/doc3gpp/repository/protocols.py` (`TDocDocumentRepository` at line 668 and `TDocExtractionRepository` at line 688 are replaced by `TDocContentRepository`)
- Create: `src/doc3gpp/storage/repositories/tdoc_content_sql.py` (replaces `tdoc_extraction_sql.py`, whose writer and readers at lines 47-218 are rewritten)
- Modify: `src/doc3gpp/models/tdoc_content_families.py` (add `validate_content_bundle`)
- Modify: `src/doc3gpp/services/factory.py` (add `build_tdoc_content_repository()`)
- Test: `tests/integration/test_tdoc_content_sqlite.py` (new)

**Interfaces:**
- Produces protocol `TDocContentRepository` with:
  - `get_extract_meta(ftp_url: str) -> TDocExtractMeta | None`
  - `replace_cr_bundle(details, ttcn, changes, meta, annexes: Sequence[TDocDocument]) -> None`: atomically writes the CR sidecars (as today), the parent `tdoc_extracts` row, and the annex rows. CR main is not an argument.
  - `replace_family_bundle(store: TDocContentStore, meta, documents: Sequence[TDocDocument]) -> None`: atomically writes the parent and the rows. `documents[0]` is the main.
  - `list_documents(store: TDocContentStore, ftp_url: str) -> tuple[TDocDocument, ...]`: decompresses `content`; order is `ordered_sources`; `file_order` is the index; `markdown_file = markdown_member_name(document_id, source_file)`; `content_hash` is read from the column.
  - `get_document(store, ftp_url, source_file) -> TDocDocument | None`.
- Behaviour of both replace methods:
  - Validation runs before any SQL: `validate_content_bundle(store, meta, documents)` raises `ValueError` for: empty documents on a family store; a family store whose `documents[0].source_file != meta.doc_filename`; a CR store where any annex equals `meta.doc_filename`; duplicate `source_file`; `document_id != document_id_for(meta.ftp_url, source_file)`; `meta.document_count != len(documents)`.
  - The transaction upserts the `tdoc_extracts` row, deletes rows for `meta.ftp_url` from all four content tables (P12), then inserts the new rows with `content = compress_json(markdown_text)` and `content_hash = sha256(utf-8 markdown)`.
  - `build_tdoc_content_repository()` returns the SQLAlchemy implementation with the default session factory.

- [ ] **Step 1: Write the failing tests**

- `test_replace_family_bundle_roundtrips_special_names(sqlite_env)`: main `CRs/main/R5-000003.docx`; annexes `annex/Ünïcode > x.docx` and `annex/with space.docx`. `list_documents` returns the same `source_file` strings, main first, with `file_order` 0, 1, 2 and the same markdown.
- `test_empty_markdown_roundtrips(sqlite_env)`: empty markdown reads back as `""`.
- `test_replace_cr_bundle_writes_annexes_only(sqlite_env)`: a CR bundle with main `R5-1.docx` and annex `a.docx`; `list_documents(store_cr, url)` returns only `a.docx`; the cover, TTCN, and change sidecars are persisted; `document_count == 1`.
- `test_cr_zero_annexes_bundle(sqlite_env)`: `replace_cr_bundle` with no annexes; `document_count == 0`; `list_documents` returns `()`.
- `test_validation_failure_writes_nothing(sqlite_env)`: duplicate `source_file`, a family bundle whose first document is not the main, and a CR bundle containing the main path each raise `ValueError`; the previous bundle is unchanged.
- `test_replace_removes_members_no_longer_present(sqlite_env)`: a 3-document family bundle is replaced by a 1-document bundle; 1 row remains; `document_count == 1`.
- `test_one_parent_per_url_across_replacements(sqlite_env)`: two replacements for one `ftp_url` leave exactly one `tdoc_extracts` row.
- `test_type_change_clears_previous_store(sqlite_env)`: a WID bundle followed by an LS bundle for the same URL leaves no WID rows (P12).
- `test_document_id_must_match_identity(sqlite_env)`: a wrong `document_id` raises `ValueError`.
- `test_content_hash_is_sha256_of_markdown(sqlite_env)`.

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `.venv/bin/python -m pytest tests/integration/test_tdoc_content_sqlite.py -q -p no:cacheprovider`
Expected: FAIL.

- [ ] **Step 3: Implement the repository, validation, protocol, and factory**

Share the CR sidecar mapping with the existing `tdoc_cr_sql.py` helpers. Keep the single-transaction behaviour that `replace_bundle` has today. Update `services/factory.py` to return the new repository.

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `.venv/bin/python -m pytest tests/integration/test_tdoc_content_sqlite.py -q -p no:cacheprovider`
Expected: PASS.

---

## Task 5: Main Member Selection

**Files:**
- Create: `src/doc3gpp/parsers/tdoc_family_main.py`
- Test: `tests/unit/test_tdoc_family_main.py` (new)

**Interfaces:**
- Produces `class TDocMainDocumentError(ValueError)`; `select_main_member(tdoc_id: str, docx_members: Sequence[str], other_members: Sequence[str] = ()) -> str`; `member_names(zip_bytes: bytes) -> tuple[str, ...]` (all non-directory entries, excluding `__MACOSX/`).
- Basenames (text after the last `/`) are compared casefolded.
- Rules:
  1. Candidates are the docx members whose basename starts with `tdoc_id` (casefolded).
  2. One candidate: it is the main.
  3. Several candidates: if exactly one basename equals `f"{tdoc_id}.docx"` (casefolded), it is the main. Otherwise raise with a message beginning `ambiguous main document for <tdoc_id>`.
  4. No candidate: if any `other_members` basename starts with the TDoc number, raise with a message containing `is not .docx` and that member path. Otherwise raise with a message containing `no main .docx`.

- [ ] **Step 1: Write the failing tests**

- `test_single_prefixed_docx_is_main`: `["R5-123456.docx", "annex/x.docx"]` → `"R5-123456.docx"`.
- `test_prefix_match_is_case_insensitive`: `["r5-123456.DOCX"]` → `"r5-123456.DOCX"`.
- `test_exact_name_breaks_tie`: `["R5-123456_rev2.docx", "R5-123456.docx"]` → `"R5-123456.docx"`.
- `test_longer_id_sibling_does_not_win`: `["R5-1234567.docx", "R5-123456.docx"]` → `"R5-123456.docx"`.
- `test_ambiguous_without_exact_name_fails`: `["R5-123456a.docx", "R5-123456b.docx"]` → `TDocMainDocumentError` with `ambiguous main document`.
- `test_main_inside_folder_uses_basename`: `["R5-123456/R5-123456.docx", "annex/x.docx"]` → `"R5-123456/R5-123456.docx"`.
- `test_doc_only_main_reports_not_docx`: `docx_members=()`, `other_members=("R5-123456.doc",)` → message contains `is not .docx` and `R5-123456.doc`.
- `test_no_main_at_all_reports_no_main`: message contains `no main .docx`.
- `test_member_names_skips_directories_and_macosx(tmp_path)`.

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_tdoc_family_main.py -q -p no:cacheprovider`
Expected: FAIL (ImportError).

- [ ] **Step 3: Implement the selection module**

Use `str.casefold()` comparisons. Build zips in the test with `zipfile.ZipFile`, following the `_make_zip` pattern in `tests/unit/test_tdoc_zip.py`.

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_tdoc_family_main.py -q -p no:cacheprovider`
Expected: PASS (9 tests).

---

## Task 6: Shared ZIP Conversion with Tolerant Annexes

**Files:**
- Create: `src/doc3gpp/parsers/tdoc_zip_convert.py`
- Test: `tests/unit/test_tdoc_zip_convert.py` (new)

**Interfaces:**
- Consumes: `select_main_member`, `member_names` (Task 5); `enumerate_docx_members(zip_bytes, *, max_documents, max_uncompressed_bytes)` (`parsers/tdoc_zip.py:181`); `convert_document_to_markdown(doc_bytes, filename) -> str` (`parsers/docx_converter.py:280`); `document_id_for` (`models/tdoc_identity.py`); `ordered_sources`, `sanitized_source_stem` (Task 2); `TDocDocument` (`models/tdoc_document.py`).
- Produces:
  - `@dataclass(frozen=True, slots=True) class TDocZipConversion`: `main_source_file: str`, `documents: tuple[TDocDocument, ...]` (every converted member, main first, then the rest in `source_sort_key` order), `skipped_sources: tuple[str, ...]`.
  - `convert_tdoc_zip(zip_bytes: bytes, *, tdoc_id: str, identity: str, max_documents: int, max_uncompressed_bytes: int, convert: Callable[[bytes, str], str] = convert_document_to_markdown) -> TDocZipConversion`. `identity` is the `ftp_url` for stored results, or a local identity string for in-memory results (Task 15).
- Rules:
  - `members = enumerate_docx_members(...)`; `other` = non-docx member names; `main = select_main_member(tdoc_id, [name for name, _ in members], other)`.
  - Main conversion: exceptions propagate (the archive fails, nothing is stored).
  - Annex conversion: each failure logs `logger.warning` naming the source and adds it to `skipped_sources`. Its document is not returned.
  - Each `TDocDocument`: `document_id = document_id_for(identity, source_file)`; `markdown_file = markdown_member_name(document_id, source_file)`; `content_hash = sha256(markdown_text.encode("utf-8")).hexdigest()`; `file_order` is the position in the returned list.

- [ ] **Step 1: Write the failing tests**

Use python-docx-built fixtures, guarded by the `_docx_available()` skip pattern (`tests/integration/test_tdoc_parse_direct_sqlite.py:43`). Use a stub `convert` callable where a specific member must fail.

- `test_stores_main_then_annexes_in_order`: 3 documents, main first, annexes in path order, `skipped_sources == ()`.
- `test_failing_annex_is_skipped_and_not_fatal`: `annex/bad.docx` raises; `skipped_sources == ("annex/bad.docx",)`; other annexes present; a warning naming the source appears in `caplog`.
- `test_failing_main_raises`: the main conversion raises and the exception propagates.
- `test_doc_only_main_fails_archive_with_docx_reason`: only `R5-123456.doc` → `TDocMainDocumentError` with `is not .docx`.
- `test_zero_text_documents_are_kept`: main and an annex convert to `""`; both are returned; `content_hash` is the hash of empty bytes.
- `test_zip_document_limit_raises`: `max_documents=1` on two docx members → `TDocZipLimitError`.
- `test_traversing_member_rejected`: `../evil.docx` → `TDocZipValidationError`.
- `test_main_inside_folder_keeps_full_path`: `R5-123456/R5-123456.docx` is `main_source_file`.

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_tdoc_zip_convert.py -q -p no:cacheprovider`
Expected: FAIL (ImportError).

- [ ] **Step 3: Implement the conversion**

Keep it pure: no SQL, no cache, no network. Task 7 (families) and Task 8 (CR) both call it.

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_tdoc_zip_convert.py -q -p no:cacheprovider`
Expected: PASS (8 tests). Skip docx-dependent tests when python-docx is absent.

---

## Task 7: Family Extraction Service (WID, LS, Discussion)

**Files:**
- Create: `src/doc3gpp/services/tdoc_family_service.py`
- Test: `tests/unit/test_tdoc_family_service.py` (new)

**Interfaces:**
- Consumes: `convert_tdoc_zip`, `TDocZipConversion` (Task 6); `TDocContentRepository` (Task 4); `TDocContentStore` and `is_complete_family_extraction` (Task 1); `download_tdoc_zip`, `DownloadedZip` (`scraping/tdoc_zip_source.py:268`, `:42`); `build_markdown_archive(documents, *, ftp_url, main_source_file)` (`parsers/tdoc_zip.py:287`); `build_ftp_url`, `normalize_ftp_path`, `derive_cache_file` (as imported in `services/tdoc_cr_service.py`); `TDoc` (`models/tdoc.py`).
- Produces:
  - `@dataclass(frozen=True, slots=True) class PreparedFamilyExtract`: `tdoc_id`, `store: TDocContentStore`, `extract_meta: TDocExtractMeta`, `documents: tuple[TDocDocument, ...]`, `skipped_sources`.
  - `@dataclass(frozen=True, slots=True) class FamilyExtractResult`: `tdoc_id`, `store`, `extract_meta | None`, `documents`, `skipped_sources`, `from_cache: bool`, `persisted: bool = True`. `complete` is `is_complete_family_extraction(extract_meta, documents)` when `extract_meta` is set, else `False`.
  - `class TDocFamilyService` with `__init__(self, *, scraper, cache, content_repo, max_tdoc_size_bytes, max_documents_per_zip, max_uncompressed_size_bytes)`, `prepare_extract(tdoc, store, *, force=False) -> PreparedFamilyExtract | FamilyExtractResult`, and `persist_prepared_extract(prepared) -> FamilyExtractResult`.
- Behaviour:
  - Prepare (no SQL writes): `canonical = normalize_ftp_path(build_ftp_url(tdoc.ftp_url))`; `cache_file = derive_cache_file(tdoc.ftp_url)`. Unless `force`: read `get_extract_meta(canonical)` and `list_documents(store, canonical)`; a complete result returns `FamilyExtractResult(from_cache=True)` with no download.
  - Otherwise download with `download_tdoc_zip(tdoc.tdoc_id, scraper, cache, primary_url=build_ftp_url(tdoc.ftp_url), ftp_url=tdoc.ftp_url, max_bytes=max_tdoc_size_bytes)`, read the bytes, and call `convert_tdoc_zip(bytes, tdoc_id=..., identity=canonical, ...)`.
  - Build `TDocExtractMeta(ftp_url=canonical, tdoc_id, cache_file, doc_filename=main, extraction_version=2, document_count=len(documents)-0, skipped_sources=...)`. Here `document_count` counts stored content rows, which are the main plus the converted annexes.
  - Write the archive with `cache.put_bytes(cache_file, build_markdown_archive(conversion.documents, ftp_url=canonical, main_source_file=main), "markdown")`. The archive holds every converted member.
  - Persist: `content_repo.replace_family_bundle(store, meta, documents)`, then return `FamilyExtractResult(from_cache=False)`. A failed prepare never reaches persist, so earlier SQL content survives.
- Does not import `tdoc_cr_service.py` (avoids an import cycle).

- [ ] **Step 1: Write the failing tests**

Build docx bytes with a `_make_docx` helper; monkeypatch `doc3gpp.services.tdoc_family_service.download_tdoc_zip` to return a `DownloadedZip` for a temp file; use a fake cache recording `put_bytes`; use the Task 4 repository on `sqlite_env`.

- `test_stores_main_and_annexes_in_order`: `document_count == 3`, main first, `skipped_sources == ()`.
- `test_failing_annex_is_skipped_recorded_and_not_fatal`: `skipped_sources == ("annex/bad.docx",)`; `complete` is False; other annexes stored; a warning in `caplog`.
- `test_failing_main_fails_and_keeps_previous_content`: a forced prepare whose main fails raises; the previous bundle is still listed.
- `test_doc_only_main_fails_with_docx_reason`: only `R5-123456.doc` → `TDocMainDocumentError` with `is not .docx`.
- `test_family_main_in_folder_stored_with_full_path`: `doc_filename == "R5-123456/R5-123456.docx"` and it is listed first.
- `test_zero_text_main_is_stored_and_complete`.
- `test_cache_hit_skips_download_when_complete`: after a persist, `prepare_extract(force=False)` returns `from_cache=True`. A download stub that raises fails the test if it is called.
- `test_force_redownloads_even_when_complete`.
- `test_incomplete_extraction_is_retried_by_next_normal_parse`: persist with `skipped_sources=("annex/bad.docx",)`; the next normal prepare calls the download once.
- `test_archive_contains_every_converted_member`: `put_bytes` is called once with `cache_file == derive_cache_file(ftp_url)`; `read_markdown_archive` returns one markdown entry per converted document.
- `test_zip_document_limit_raises_limit_error`.

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_tdoc_family_service.py -q -p no:cacheprovider`
Expected: FAIL (ImportError).

- [ ] **Step 3: Implement the service**

Follow the behaviour above. Keep the archive write in this service. The CR path (Task 8) keeps its own archive write and reuses `build_markdown_archive`.

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_tdoc_family_service.py -q -p no:cacheprovider`
Expected: PASS (11 tests).

---

## Task 8: CR Extraction Changes and Type Dispatch

**Files:**
- Modify: `src/doc3gpp/services/tdoc_cr_service.py` (`TDocTypeUnsupportedError` at line 217; `ExtractResult` at line 301; `_bind_documents_to_ftp_url` at lines 511-527; `_is_complete_bundle` at lines 530-545; `_write_markdown_archive` at line 554; `prepare_extract` cache-hit at lines 640-690; `persist_prepared_extract` at line 758; `persist_prepared_url` at line 1059; `_load_tdoc` at lines 1431-1445; `extract_many` at line 792)
- Modify: `src/doc3gpp/parsers/direct_extractor.py` (`_direct_parse_payload` at line 477 and `direct_parse_bytes` at line 605: annex conversion failures become `skipped_sources`, P13)
- Modify: `src/doc3gpp/services/tdoc_pipeline.py` (`classify_tdoc_exception` at lines 69-85)
- Test: `tests/unit/test_tdoc_family_dispatch.py` (new); extend `tests/unit/test_tdoc_cr_service.py` and `tests/unit/test_tdoc_pipeline.py`

**Interfaces:**
- `TDocCrService.__init__(..., family_service: TDocFamilyService | None = None)`. When `None`, it builds one from its own scraper, cache, limits, and `build_tdoc_content_repository()`.
- `ExtractResult.tdoc_id -> str` property returns `self.details.tdoc_id`.
- `prepare_extract(tdoc_id, *, force=False, full=False) -> PreparedTDocExtract | PreparedFamilyExtract | ExtractResult | FamilyExtractResult`.
- `persist_prepared_extract(prepared) -> ExtractResult | FamilyExtractResult`.
- `_load_tdoc`: a missing row raises `TDocNotFoundError`. A row whose type is not `is_eligible_type` raises `TDocTypeUnsupportedError(tdoc_id, tdoc.type)` before any network call. The message names the type: `"TDoc X has type Y; only CR, WID, LS, and discussion TDocs are parsed"`.
- Dispatch in `prepare_extract`: after `_validate_tdoc_id` and `_load_tdoc`, require a non-empty `ftp_url` (else `TDocNotYetOnFTPError`). Then `store = store_for_type(tdoc.type)`. A family store goes to `self._family_service.prepare_extract(tdoc, store, force=force)`. A CR store runs the CR path below.
- **CR cache hit (P9):** `ExtractResult(from_cache=True)` only when the v2 parent and the cover row exist, `is_complete_cr_extraction` holds, and `markdown/<cache_file>` exists in the cache.
- **CR bundle:** `_parse_bundle` produces every converted document (CR main and annexes), with tolerant annexes (P13). The archive holds all of them. `persist_prepared_extract` and `persist_prepared_url` pass the annexes only to `replace_cr_bundle` (the main is never in SQL). `document_count` is the annex count.
- `persist_prepared_extract` routes `PreparedFamilyExtract` to the family service, then calls `_index_after_parse(tdoc_id)` and `_embed_after_parse(tdoc_id)`.
- `extract_many`: the pipeline `is_complete` accepts `(ExtractResult, FamilyExtractResult)`. Success keys use `result.tdoc_id`.
- `classify_tdoc_exception`: `TDocTypeUnsupportedError` returns `"skipped"`.
- `family_service` and `tdoc_cr_service` have no import cycle: the family service does not import the CR service.

- [ ] **Step 1: Write the failing tests**

- `test_ineligible_type_is_skipped_without_download(monkeypatch)`: an `other` row with a non-empty `ftp_url`; a scraper `get` that raises if called. `extract_many(["R5-000001"])` puts it in `skipped` with a reason beginning `TDocTypeUnsupportedError`.
- `test_family_tdoc_routes_to_family_service(monkeypatch)`: the fake family service returns `FamilyExtractResult`; `successes` is keyed by the family TDoc id.
- `test_cr_tdoc_never_uses_family_service(monkeypatch)`: a raising stub on the family service; the CR path runs.
- `test_cr_persist_writes_annexes_only(sqlite_env)`: a CR bundle with main and one annex; `tdoc_cr_annexes` contains only the annex; the archive contains both.
- `test_cr_cache_hit_requires_archive(sqlite_env, tmp_path)`: with the cover, v2 parent, and archive present, `prepare_extract(force=False)` returns `from_cache=True`. Deleting the archive makes the next prepare download again.
- `test_cr_zero_annex_bundle_is_complete_with_cover()`.
- `test_cr_annex_failure_is_tolerant(...)`: an annex conversion error leaves `skipped_sources` set, `complete` False, and the other annexes stored (P13).
- `test_family_persist_runs_index_and_embed_hooks(monkeypatch)`: recorders for both hooks.
- `test_family_without_ftp_url_is_skipped`: empty `ftp_url` → `TDocNotYetOnFTPError` → skipped.
- `test_classify_marks_unsupported_type_as_skipped()`.

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_tdoc_family_dispatch.py tests/unit/test_tdoc_pipeline.py tests/unit/test_tdoc_cr_service.py -q -p no:cacheprovider`
Expected: FAIL.

- [ ] **Step 3: Implement the dispatch, CR changes, and tolerant annexes**

Read `_direct_parse_payload` (`parsers/direct_extractor.py:477`) before editing, because the CR annex conversion happens there. Keep the CR cover, TTCN, and change parsers unchanged.

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_tdoc_family_dispatch.py tests/unit/test_tdoc_pipeline.py tests/unit/test_tdoc_cr_service.py -q -p no:cacheprovider`
Expected: PASS. Existing CR tests that assert the archive or SQL document content change to the new contract (annexes in SQL, main in archive only); update them here.

---

## Task 9: Repository Selection and Parsed Predicate

**Files:**
- Modify: `src/doc3gpp/storage/repositories/tdoc_sql.py` (`list` and `list_with_meeting`; the `exclude_parsed` block at lines 212-283 is replaced by the predicate below)
- Modify: `src/doc3gpp/repository/protocols.py` (`TDocRepository.list` and `list_with_meeting`)
- Modify: `src/doc3gpp/models/tdoc.py` (add `parsed: bool | None = None` and `document_count: int | None = None` to `TDoc`)
- Test: `tests/integration/test_tdoc_selection_sqlite.py` (new)

**Interfaces:**
- `list(..., eligible_types: Sequence[str] | None = None, parsed: bool | None = None, ...)` and `list_with_meeting(...)` with the same arguments.
  - `eligible_types`: keep rows where `func.lower(TDocORM.type)` is in the casefolded values. `None` means no filter.
  - `parsed=True` keeps complete rows; `parsed=False` keeps incomplete rows; `None` means no filter. `exclude_parsed=True` is equivalent to `parsed=False`.
- `complete_extraction_predicate()` returns `or_(cr_complete, family_complete)`, correlated to `TDocORM`:
  - `cr_complete`: a `TDocExtractOrm` row with `tdoc_id` matching, `extraction_version == 2`, `skipped_sources IS NULL OR skipped_sources = '[]'`, a `TDocCrDetailOrm` cover row for the same `ftp_url`, the count of `TDocCrAnnexOrm` rows for `ftp_url` equal to `document_count`, and no annex row whose `doc_filename == TDocExtractOrm.doc_filename`.
  - `family_complete`: an OR over the three family ORM classes. Each branch matches the same parent conditions, `document_count > 0`, the count of that table's rows for `ftp_url` equal to `document_count`, and an EXISTS row with `doc_filename == TDocExtractOrm.doc_filename`.
- After the main query, `TDoc.parsed` and `TDoc.document_count` are filled with one extra query. `document_count` is the stored content-row count across the four tables. Non-complete rows get `parsed=False` and `document_count=None`. Use `dataclasses.replace`.

- [ ] **Step 1: Write the failing tests**

- `test_default_eligibility_excludes_other`: seed CR, WID new, LS out, discussion, and other rows. `eligible_types=eligible_type_values()` returns exactly the four eligible ids.
- `test_explicit_type_intersects_with_eligibility`: `tdoc_type="pCR"` with eligible types → `[]`; `tdoc_type="WID%"` → WID rows only.
- `test_parsed_filter_for_family_and_cr`: a complete family TDoc (two documents) has `parsed=True`; a family TDoc with skipped sources has `parsed=False`; `parsed=True`/`False` split the rows.
- `test_exclude_parsed_drops_complete_family_and_cr`.
- `test_cr_with_cover_and_no_v2_is_not_parsed`: pre-branch shape (cover row, v1, count 0) → `parsed=False`.
- `test_cr_with_annexes_is_parsed_with_count`: `document_count` equals the annex count.
- `test_zero_annex_cr_is_parsed`: v2, zero annexes, cover → `parsed=True`, `document_count == 0`.
- `test_parity_with_python_rules`: for each state, the SQL predicate equals `is_complete_cr_extraction` or `is_complete_family_extraction` on the same stored state.

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `.venv/bin/python -m pytest tests/integration/test_tdoc_selection_sqlite.py -q -p no:cacheprovider`
Expected: FAIL.

- [ ] **Step 3: Implement the predicate and filters**

Keep one predicate function that the list filter and the `parsed` fields both use. Map store key to ORM class in `storage/db/models.py`.

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `.venv/bin/python -m pytest tests/integration/test_tdoc_selection_sqlite.py -q -p no:cacheprovider`
Then run the existing `exclude_parsed` tests: find them with `grep -rln "exclude_parsed" tests/` and rerun those files.
Expected: PASS after the test expectations for the new CR rule are updated.

---

## Task 10: Selection Surfaces (CLI, Web, MCP) and List Output

**Files:**
- Create: `src/doc3gpp/services/tdoc_parse_selection.py`
- Modify: `src/doc3gpp/cli.py` (the `--type` help near line 2124; default selection near line 2446; the `--tdoc` handling before `list_with_meeting` near lines 2440-2467; the success summary near lines 2575-2579; `tdoc_list` at line 1439 for `--parsed` and the output fields)
- Modify: `src/doc3gpp/web/workers/handlers.py` (`_parse_tdocs` list kwargs near lines 364-374; runner `is_complete` near lines 408-418)
- Modify: `src/doc3gpp/web/routes/tdocs.py` (`list_tdocs` near line 144: `parsed` query parameter)
- Modify: `src/doc3gpp/web/mcp_server.py` (`list_tdocs` near lines 306-360: `parsed` parameter; `parse_tdocs` near lines 917-931)
- Test: `tests/unit/test_tdoc_selection_surfaces.py` (new)

**Interfaces:**
- `exact_tdoc_id(value: str) -> bool`: true when the value has no `%`, `_`, or `*` and does not start with `!`.
- `parse_selection_kwargs(tdoc_type: str | None, *, exact_tdoc_id: bool = False) -> dict[str, object]`: returns `{"tdoc_type": tdoc_type, "eligible_types": ...}`. `eligible_types` is `None` when `tdoc_type is None and exact_tdoc_id`, and `eligible_type_values()` otherwise.
- CLI `tdoc parse`: `list_kwargs` uses `parse_selection_kwargs(tdoc_type, exact_tdoc_id=exact_tdoc_id(tdoc))` in place of `"tdoc_type": tdoc_type or "CR"`. Help: `Defaults to CR, WID, LS, and discussion TDocs; an explicit value is intersected with those types.`
- CLI success summary: a `FamilyExtractResult` prints `TDoc <id>: <family key>, <n> documents, extraction_version 2`. Each skipped source prints `warning: skipped <source>`.
- CLI `tdoc list`: `--parsed` (`typer.Option(None, "--parsed", help="true|false: complete extraction state")`) parsed with `_parse_bool_option`. Output fields include `parsed` (bool) and `document_count` (int or null). `parsed` joins the default fields (P8).
- Web `_parse_tdocs` uses `parse_selection_kwargs(filters.get("tdoc_type"))`. The runner `is_complete` accepts `FamilyExtractResult`. Web list accepts `parsed`. MCP `list_tdocs` accepts `parsed`; MCP `parse_tdocs` uses the same helper.

- [ ] **Step 1: Write the failing tests**

- `test_exact_tdoc_id_classifier()`: `"R5-123456"` → True; `"R5-12%"`, `"R5_1"`, `"!R5-1"` → False.
- `test_parse_selection_default_and_intersection()`.
- `test_cli_default_parse_selects_cr_and_families(sqlite_env, monkeypatch)`: `TDocCrService.extract_many` is recorded; CR and family ids are selected; `other` is not.
- `test_cli_explicit_type_intersection_reports_no_match(sqlite_env)`: `--type pCR` exits 1 with `No TDoc matched`.
- `test_cli_exact_ineligible_tdoc_reaches_skipped(sqlite_env, monkeypatch)`: `--tdoc` for an `other` row lists it under skipped with `has type 'other'`.
- `test_cli_tdoc_list_parsed_filter_and_fields(sqlite_env)`: `tdoc list --parsed true --format json` returns complete rows with `parsed: true` and `document_count`.
- `test_web_parse_selection_excludes_other(...)`: the fake repository records `eligible_types`; follow `tests/unit/test_web_jobs_routes.py`.
- `test_mcp_parse_and_list_defaults(...)`: follow `tests/integration/test_mcp_end_to_end.py`.

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_tdoc_selection_surfaces.py -q -p no:cacheprovider`
Expected: FAIL.

- [ ] **Step 3: Implement the helper and surface wiring**

Run `grep -rn 'tdoc_type or "CR"' src/` to confirm no CR-only default remains. Update existing tests whose expected `tdoc list` default columns change (P8).

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_tdoc_selection_surfaces.py tests/unit/test_tdoc_parse_cli.py tests/unit/test_web_jobs_routes.py tests/integration/test_mcp_end_to_end.py -q -p no:cacheprovider`
Expected: PASS.

---

## Task 11: FTS5 Content Rows, `content_text` Column, and Freshness

**Files:**
- Create: `src/doc3gpp/storage/repositories/tdoc_document_sources.py`
- Modify: `src/doc3gpp/storage/db/migrate.py` (`_create_search_schema` `expected_layout` near line 229: `("document_text", False)` → `("content_text", False)`; DDL column near line 283)
- Modify: `src/doc3gpp/settings/schema.py` (`_SNIPPET_COLUMN_NAMES` near lines 436-446: `"document_text"` → `"content_text"`)
- Modify: `src/doc3gpp/storage/repositories/search_sql.py` (`_TDOC_FRESHNESS_CTES_SQL` joins at lines 122 and 130; insert column list and bind near lines 214, 219, 250, 258; matched-document join at line 436; `_build_index_text` join at line 720)
- Modify: `src/doc3gpp/web/...` and `src/doc3gpp/services/tdoc_search_facade.py` only where a preview key is read (`previews.document_text` → `previews.content_text`); find with `grep -rn "document_text" src/`
- Test: `tests/integration/test_tdoc_content_search_sqlite.py` (new)

**Interfaces:**
- `tdoc_document_sources_sql() -> str`: a `UNION ALL` over `tdoc_cr_annexes` and the three family tables (names from `TDOC_CONTENT_STORES`). Each branch selects `ftp_url, document_id, doc_filename AS source_file, content_hash`. Only the freshness CTE, the matched-document join, and `_build_index_text` use it.
- `_build_index_text(tdoc_id)` keeps its return shape. It reads the TDoc's rows from the four tables, decompresses `content`, and returns `(document_id, source_file, extraction_version, content_hash, normalize_query(markdown))`. `extraction_version` comes from the parent. The CR main is not included.
- The FTS column is `content_text`. Nine positional BM25 weights stay the same. The layout change recreates `tdoc_search` on the next run; `tdoc index --rebuild` is required after upgrade.

**Behaviour:**
- Freshness, `count_tdocs_to_index`, `status`, and `rebuild_batch` keep their signatures. A changed content hash or a removed member makes the TDoc stale until `upsert(tdoc_id)` runs.
- `upsert(tdoc_id)` reads rows from SQL only. It never reads the markdown cache.
- CR cover, change, and TTCN text stays indexed through `_cover_text`, `_change_text`, and `_ttcn_text`.

- [ ] **Step 1: Write the failing tests**

- `test_annex_only_term_finds_parent_with_matched_document(sqlite_env)`: a WID TDoc whose term `zyxqvunique` appears only in an annex. After `upsert`, `search` returns the parent once; `matched_documents` lists that annex's `source_file`.
- `test_main_term_reports_main_document`.
- `test_metadata_only_tdoc_remains_searchable_by_title`.
- `test_zero_text_document_indexes_without_error`: empty markdown; `upsert` succeeds; title search still works.
- `test_fts_finds_text_beyond_semantic_cap`: a 5,000-word family document with a unique term at the end is found.
- `test_cr_annex_term_is_indexed(sqlite_env)`: a CR annex term finds the CR parent.
- `test_cr_main_only_term_not_indexed(sqlite_env)`: a term only in the CR main body is not found; a CR cover-field term is found (Review Focus 6).
- `test_changed_content_hash_reports_stale`: `count_tdocs_to_index(stale_only=True)` includes the TDoc before `upsert` and excludes it after.
- `test_content_text_column_and_preview_key(sqlite_env)`: the FTS layout has `content_text` and no `document_text`; the preview key is `content_text`.
- `test_rebuild_after_cache_purge_uses_sql_only(sqlite_env, tmp_path)`.

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `.venv/bin/python -m pytest tests/integration/test_tdoc_content_search_sqlite.py -q -p no:cacheprovider`
Expected: FAIL.

- [ ] **Step 3: Implement the union, the rename, and the family rows**

Build the union string from the store table names. Do not change the FTS table shape other than the column rename, and do not change the BM25 weights. Rename `document_text` at every site from the grep in Global Constraints.

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `.venv/bin/python -m pytest tests/integration/test_tdoc_content_search_sqlite.py tests/integration/test_search_schema.py tests/integration/test_search_previews_per_column.py tests/integration/test_search_snippet_column.py -q -p no:cacheprovider`
Expected: PASS after the `document_text` expectations are changed to `content_text` in those files.

---

## Task 12: Semantic Content Chunks (CR Annexes and Families)

**Files:**
- Modify: `src/doc3gpp/models/semantic_search.py` (add `EmbeddedChunk`, `SemanticDocumentHit`; add `document_hits: tuple[SemanticDocumentHit, ...] = ()` to `SemanticSearchHit`)
- Modify: `src/doc3gpp/repository/protocols.py` (`VectorIndexRepository.upsert_chunks`, `content_signatures`)
- Modify: `src/doc3gpp/storage/repositories/vector_sql.py` (`upsert_chunks` at lines 208-229; `content_signatures`; freshness in `count_tdocs_to_index`, `rebuild_batch`, `status`)
- Modify: `src/doc3gpp/services/semantic_search_service.py` (`index_for_tdoc` at lines 226-247; `search` at lines 132-195)
- Modify: `src/doc3gpp/services/tdoc_search_facade.py` (chunk hits populate `matched_documents`)
- Modify: `src/doc3gpp/settings/schema.py` (`SemanticSearchSettings`: `max_content_chunks_per_tdoc: int = Field(default=16, ge=1)`, TOML-only)
- Test: `tests/unit/test_tdoc_content_semantic.py` (new), `tests/integration/test_tdoc_content_semantic_sqlite.py` (new)

**Interfaces:**
- `EmbeddedChunk(chunk_id: str, chunk_index: int, embedding: Any)` (frozen slots).
- `SemanticDocumentHit(document_id: str, chunk_id: str, distance: float)` (frozen slots).
- `VectorIndexRepository.upsert_chunks(tdoc_id: str, chunks: Sequence[EmbeddedChunk]) -> None`: deletes all rows for the TDoc, then inserts one row per chunk (`chunk_id`, `tdoc_id`, `chunk_index`, float32 bytes).
- `VectorIndexRepository.content_signatures() -> dict[str, frozenset[tuple[str, str]]]`: parses `(document_id, content_hash)` from chunk IDs with three `#` separators.
- `SemanticSearchService.index_for_tdoc(tdoc_id) -> None`.

**Indexing:**
- Metadata chunks: `_build_embed_text` → `_chunks(text, chunk_size, chunk_overlap)` → the first `max_chunks_per_tdoc` chunks, IDs `f"{tdoc_id}#{i}"`.
- Content chunks for every stored document: CR annexes in path order, then family main plus annexes (main first). Each document is chunked from its decompressed Markdown with `_chunks`. Chunks are taken until the total reaches `max_content_chunks_per_tdoc`. IDs `f"{tdoc_id}#{document_id}#{content_hash}#{i}"`, with `i` the position within the document. CR main is not in SQL and gets none.
- One `encode` call over all chunks, then one `upsert_chunks`. With no chunks at all, call `remove_for_tdoc`.

**Search:**
- Group knn rows by `tdoc_id`. The first row (smallest distance) gives `best_chunk_id`. Semantic-only returns one result row per TDoc. Each content document's best chunk becomes a `SemanticDocumentHit`, and `document_hits` holds them. Hybrid fuses metadata and chunk hits per TDoc with RRF and carries the same `document_hits`. Facade `matched_documents` for chunk hits resolve `source_file`, `ftp_url`, `extraction_version`, and `content_hash` through `tdoc_document_sources_sql()`. A metadata-only match leaves `matched_documents` empty.

**Freshness:**
- `content_signatures()` is compared with the SQL signatures of each TDoc. A differing TDoc is content-stale. `status().is_stale` is true when any TDoc is content-stale. `rebuild_batch(stale_only=True)` yields the union of uploaded-date-stale and content-stale TDocs in `tdoc_id` order.

- [ ] **Step 1: Write the failing tests**

Use a deterministic fake embedder (a fixed-size hash of the text, or an explicit map for a "needle" text) with `dim()` and `model_name`.

- `test_content_chunk_ids_embed_document_id_and_hash`.
- `test_metadata_chunk_ids_unchanged`: `<tdoc>#0`.
- `test_content_chunks_capped`: with `chunk_size=5`, `chunk_overlap=1`, a long multi-document TDoc yields exactly 16 content chunks.
- `test_zero_text_document_adds_no_content_chunks`.
- `test_cr_main_gets_no_content_chunks_and_cr_annex_does`: the CR main has no content IDs; an annex has them.
- `test_annex_text_found_by_semantic_search`: the annex chunk matches; the parent appears once with `document_hits` containing the annex `document_id`.
- `test_semantic_only_returns_one_hit_per_tdoc`.
- `test_hybrid_carries_document_hits`.
- `test_rebuild_embeddings_with_cache_purged_uses_sql(tmp_path)`.
- `test_changed_content_hash_marks_status_stale`: stale before rebuild, not stale after.

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_tdoc_content_semantic.py tests/integration/test_tdoc_content_semantic_sqlite.py -q -p no:cacheprovider`
Expected: FAIL.

- [ ] **Step 3: Implement the chunk index, grouping, and freshness**

Find every caller of `upsert_chunks` with `grep -rn "upsert_chunks(" src/ tests/` and update each to pass `EmbeddedChunk`. Keep `remove_for_tdoc` and `knn` signatures.

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_tdoc_content_semantic.py tests/integration/test_tdoc_content_semantic_sqlite.py tests/integration/test_embed_after_parse.py -q -p no:cacheprovider`
Expected: PASS. Update existing tests that assert `upsert_chunks` raw embeddings or the old CR no-content-chunk behaviour (`test_cr_tdocs_get_no_content_chunks` is replaced by `test_cr_main_gets_no_content_chunks_and_cr_annex_does`).

---

## Task 13: Content Retrieval Service

**Files:**
- Modify: `src/doc3gpp/services/tdoc_content_service.py` (`__init__` near line 65; `get` near lines 78-161; `_content_from_sql` at line 232; error classes near lines 26-58; `tdoc_content_to_dict` at line 419)
- Modify: `src/doc3gpp/models/tdoc_content.py` (`TDocContent` gains `skipped_sources: tuple[str, ...] = ()`)
- Modify: `src/doc3gpp/services/factory.py` (`build_tdoc_content_service` near line 299 passes the content repository)
- Test: `tests/unit/test_tdoc_content_service_families.py` (new)

**Interfaces:**
- `TDocContentService.__init__(*, tdoc_repository, cr_repository, content_repository, cache_root)`. `get(*, tdoc_id=None, ftp_url=None, source_file=None) -> TDocContent` (signature unchanged).
- `class TDocContentIneligibleError(TDocContentError)` with `tdoc_id: str`, `observed_type: str | None`. Message: `"TDoc {tdoc_id!r} has type {observed_type!r}; raw Markdown is available for CR, WID, LS, and discussion TDocs"`.
- `tdoc_content_to_dict` adds the `skipped_sources` key.

**Behaviour:**
- Parent resolution is unchanged. Then branch on the parent's `tdocs.type`:
  - **CR:** the parent must be v2 (otherwise the existing legacy v1 path runs, reported incomplete). Read the main from the archive: the `markdown/<cache_file>` archive, the member whose source is `meta.doc_filename`. Read annexes from `list_documents(store_cr, ftp_url)`. Documents are main first, then annexes in path order, with `is_main` set on the main. If the archive is missing or unreadable, raise the existing cache-miss error with the re-parse hint (`tdoc parse --tdoc <id> --force`). Never return a partial envelope.
  - **Family:** `meta = get_extract_meta(ftp_url)`. A missing meta raises the same not-found error the CR path uses. `documents = list_documents(store, ftp_url)`. Empty documents is incomplete.
  - **Other:** raise `TDocContentIneligibleError`.
- `complete` for families and CR is the Python rule from Task 1, so `skipped_sources` makes it false. `source_file` selects one document; an unknown `source_file` is a not-found error with no fallback. Family reads never touch the cache.

- [ ] **Step 1: Write the failing tests**

- `test_family_content_main_first_with_is_main`.
- `test_source_file_selects_one_member_and_unknown_is_not_found`.
- `test_skipped_sources_makes_content_incomplete`.
- `test_ineligible_type_raises_named_error`: the message contains `'other'` and lists `WID, LS, and discussion`.
- `test_tdoc_content_to_dict_has_skipped_sources_key`: keys are exactly `{tdoc_id, ftp_url, extraction_version, complete, skipped_sources, documents}`.
- `test_cr_content_is_archive_main_plus_annexes(sqlite_env, tmp_path)`: with a valid archive, the envelope lists the main from the archive first, then the annexes.
- `test_cr_raw_without_archive_is_cache_miss(sqlite_env, tmp_path)`: no archive → the cache-miss error with the re-parse hint; no envelope (Review Focus 7).
- `test_family_content_never_reads_cache(tmp_path)`.
- `test_v1_cr_uses_legacy_path_and_is_incomplete()`.

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_tdoc_content_service_families.py -q -p no:cacheprovider`
Expected: FAIL.

- [ ] **Step 3: Implement the routing, archive read, and error**

Reuse `_content_from_sql`, `_cache_not_found`, `_content_not_found`, `_incomplete`, and `_legacy_document`. Read the archive with `read_markdown_archive` (`parsers/tdoc_zip.py:469`) and select the member by source name.

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_tdoc_content_service_families.py tests/unit/test_tdoc_content_service.py -q -p no:cacheprovider`
Expected: PASS after the existing CR content tests are updated for the archive-main contract.

---

## Task 14: Raw Output Surfaces (CLI, HTTP, MCP)

**Files:**
- Modify: `src/doc3gpp/cli.py` (`_render_tdoc_show_raw` near lines 3676-3713: the `TDocTypeUnsupportedError` branch near lines 3694-3698 is replaced by the `TDocContentIneligibleError` mapping; the incomplete warning lists skipped sources)
- Modify: `src/doc3gpp/web/routes/tdocs.py` (`/{tdoc_id}/content` near lines 505-527; `/by-url/content` near lines 481-504; error mapping for the ineligible error and cache miss)
- Modify: `src/doc3gpp/web/mcp_server.py` (`get_tdoc_content` near lines 453-515; the incomplete hint near lines 502-512 lists skipped sources)
- Test: `tests/unit/test_tdoc_content_surfaces.py` (new)

**Interfaces:**
- Consumes `TDocContentService.get`, `TDocContentIneligibleError`, `tdoc_content_to_dict`, and `render_tdoc_documents_markdown(documents, *, include_source_headers=True)` (`services/tdoc_content_service.py:438`).
- Output contract (renderer unchanged):
  - One document: body unchanged.
  - Several documents: main first, then path order. Each block starts with `<!-- doc3gpp-source: <JSON string of source_file> -->` on its own line, then the body. Blocks are joined by line breaks. `>` in the source name is written as `\u003e` after `json.dumps(ensure_ascii=False)`.
- CLI `tdoc show --tdoc <id> --format raw [--source-file <path>]`: stdout or `--output`. Incomplete content writes a stderr warning naming the skipped sources. Extraction may run first, as for CR today. The URL path never downloads.
- HTTP `GET /tdocs/{tdoc_id}/content` and `GET /tdocs/by-url/content?ftp_url=...`: `text/markdown; charset=utf-8`; incomplete sets `X-Doc3gpp-Extraction-Complete: false`; `format=json` returns the shared envelope. An ineligible type returns 404 with the message. A cache miss returns the existing cache-miss response.
- MCP `get_tdoc_content`: `format=markdown` (default) returns the same text; incomplete content and cache misses raise with the re-parse hint; `format=json` returns the envelope with `complete` and `skipped_sources`.
- CLI maps the ineligible error to `typer.BadParameter` with the same message.

- [ ] **Step 1: Write the failing tests**

- `test_cli_raw_multi_document_named_blocks(sqlite_env)`: a complete WID TDoc with main `CRs/R5-000003.docx` and annex `annex/Ünïcode name > x.docx`. The output starts with `<!-- doc3gpp-source: "CRs/R5-000003.docx" -->` followed by the main body. The annex header is `<!-- doc3gpp-source: "annex/Ünïcode name \u003e x.docx" -->` (Review Focus 3).
- `test_cli_raw_source_file_selects_one_document(sqlite_env)`.
- `test_cli_raw_ineligible_type_error_names_type(sqlite_env)`.
- `test_cli_raw_incomplete_warns_with_skipped_sources(sqlite_env)`.
- `test_cr_raw_uses_archive_main(sqlite_env, tmp_path)`: the CLI output begins with the archive main block.
- `test_cr_raw_without_archive_is_cache_miss_on_all_surfaces(...)`: CLI, HTTP, and MCP each report the re-parse hint (Review Focus 7).
- `test_http_markdown_sets_incomplete_header(client)`.
- `test_http_json_matches_mcp_json_bytes(client, mcp)`.
- `test_mcp_incomplete_markdown_raises_with_hint(mcp)`.
- `test_by_url_content_never_downloads(client, scraper_spy)`.

Use the fixtures from `tests/unit/test_web_routes.py` and `tests/integration/test_mcp_end_to_end.py`.

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_tdoc_content_surfaces.py -q -p no:cacheprovider`
Expected: FAIL.

- [ ] **Step 3: Implement the surface changes**

Replace the CR-only `BadParameter` text in `cli.py` with the named-type message. Keep the existing error mapping shapes.

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_tdoc_content_surfaces.py tests/unit/test_tdoc_parse_cli.py tests/unit/test_web_routes.py tests/integration/test_mcp_end_to_end.py -q -p no:cacheprovider -k "content or raw"`
Expected: PASS.

---

## Task 15: Direct-Mode Type Detection and Persistence

**Files:**
- Create: `src/doc3gpp/services/tdoc_type_detection.py`
- Modify: `src/doc3gpp/services/tdoc_cr_service.py` (direct entry points `prepare_from_url`, `prepare_from_bytes`, `_prepare_from_3gpp_url`: route by decided type; CR parsing unchanged except annexes-only persistence)
- Modify: `src/doc3gpp/cli.py` (`_tdoc_parse_direct` near line 2788, `_tdoc_parse_local_batch` near line 3969, `_tdoc_parse_url_batch` near line 4155: `--from-path` and `--from-url` run the sync steps when `sync.auto_sync` is on; family and CR results print the summary)
- Test: `tests/unit/test_tdoc_type_detection.py` (new), `tests/integration/test_tdoc_family_direct_sqlite.py` (new)

**Interfaces:**
- Consumes `store_for_type` (Task 1); `convert_tdoc_zip` (Task 6); `convert_document_to_markdown` (`parsers/docx_converter.py:280`); `is_cr_header_present(markdown) -> tuple[bool, str]` (`parsers/cr/header.py:81`); `trigger_auto_sync` (`cli_auto_sync.py:264`); `extract_tsg_from_tdoc_id_or_pattern` (`cli_auto_sync.py:55`); `resolve_meeting_id_for_tdoc_id` (`cli_auto_sync.py:81`); `sync_tsg_internal` (`cli_auto_sync.py:115`); `sync_meeting_internal` (`cli_auto_sync.py:145`).
- Produces:
  - `sniff_content_family(markdown: str) -> TDocContentStore | None`:
    - WID: a line containing `3GPP™ Work Item Description` within the first 20 non-empty lines.
    - LS: a line matching `^\s*Title:\s+(Reply\s+){0,1}LS\s+on\s+` within the first 10 non-empty lines.
    - Discussion: a line matching `^\s*Title:\s+Discussion\s+on\s+` within the first 10 non-empty lines.
  - `@dataclass(frozen=True, slots=True) class DirectTypeDecision`: `kind: Literal["cr", "family", "skipped"]`, `store: TDocContentStore | None`, `reason: str | None`.
  - `decide_direct_type(tdoc_id, *, tdoc_type, main_markdown: Callable[[], str]) -> DirectTypeDecision`:
    - With a known `tdoc_type`: CR → `cr`; a family → `family`; anything else → `skipped` with reason `f"type {tdoc_type!r} is not parsed"`. `main_markdown` is not called.
    - With `tdoc_type is None`: call `main_markdown()`. A sniffed family → `family`. `is_cr_header_present` → `cr`. Otherwise `skipped` with reason `type could not be determined`.
- The seven-step procedure (spec §6): (1) TDoc ID from the file name; (2) sync the TSG; (3) resolve the meeting by `start_doc`/`end_doc`; (4) sync that meeting's TDoc list; (5) read `tdocs.type`; (6) convert the main and sniff; (7) skip with the reason. Steps 2–4 run only when `sync.auto_sync` is on, under the existing skip rules (P7).
- Persistence: CR and family results from `--from-url` persist when a `tdocs` row exists, using the canonical URL. Otherwise they stay in memory (`persisted=False`). `--from-path` never persists (P7). The sniffing conversion of a standalone `.docx` uses `convert_document_to_markdown`; a ZIP uses `convert_tdoc_zip` with `identity=f"local:{tdoc_id}:{filename}"`.

- [ ] **Step 1: Write the failing tests**

- `test_sniff_wid_within_first_20_non_empty_lines`: the marker on line 20 matches; line 21 does not.
- `test_sniff_ls_title_patterns`: `Title: LS on X` and `Title: Reply LS on X` match; `Title: LS on` after line 10 does not.
- `test_sniff_discussion_title`.
- `test_decide_uses_tdocs_type_without_converting`: the `main_markdown` lambda is never called.
- `test_decide_sniffs_when_no_row`.
- `test_decide_cr_header_fallback(monkeypatch)`: `is_cr_header_present` patched → `cr`.
- `test_decide_unknown_is_skipped_with_reason`.
- `test_from_path_family_is_not_persisted(sqlite_env)`: `persisted=False`; no `tdoc_extracts` row.
- `test_from_url_family_with_row_persists(sqlite_env)`: `persisted=True`; rows in the family table.
- `test_from_url_cr_persists_annexes_only(sqlite_env)`: the annex table holds annexes only.
- `test_auto_sync_on_triggers_sync_for_from_path(monkeypatch)`: `trigger_auto_sync` recorded once.
- `test_auto_sync_off_skips_sync(monkeypatch)`.

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_tdoc_type_detection.py tests/integration/test_tdoc_family_direct_sqlite.py -q -p no:cacheprovider`
Expected: FAIL.

- [ ] **Step 3: Implement detection and wiring**

Read the direct entry points in `services/tdoc_cr_service.py` and the CLI direct handlers before editing. Keep the CR parse path unchanged except for annexes-only persistence.

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_tdoc_type_detection.py tests/integration/test_tdoc_family_direct_sqlite.py tests/integration/test_tdoc_parse_direct_sqlite.py tests/unit/test_cli_auto_sync.py -q -p no:cacheprovider`
Expected: PASS for new tests. Regressions in existing direct-mode and auto-sync tests are real and must be fixed in this task. Note that `docs/cli.md:1439` and AGENTS.md contradict each other on `--from-url` auto-sync; Task 16 resolves the docs.

---

## Task 16: Schema Registry, Settings Example, and Documentation

**Files:**
- Modify: `src/doc3gpp/models/schema_info.py` (remove the `tdoc_documents` table entry near line 240; add entries for `tdoc_cr_annexes`, `tdoc_wid_contents`, `tdoc_ls_contents`, `tdoc_discussion_contents`; `tdoc_extracts.skipped_sources` near line 227; `tdoc.parsed` and `tdoc.document_count`)
- Modify: `src/doc3gpp/data/doc3gpp.toml.example` (`[semantic_search] max_content_chunks_per_tdoc = 16`; `parsed` in the default tdoc output fields, per P8; `content_text` at lines 177-178)
- Modify: `README.md` (lines 239, 648, 650), `AGENTS.md` (lines 85, 277, 285, 338, 398), `docs/architecture.md` (lines 311, 476, 499, 872), `docs/cli.md` (lines 674, 734, 965, 1430, 1439, 1945-1946), `docs/web-server.md` (line 273), `docs/conventions.md` (lines 340, 356), `docs/code-map.md` (lines 59, 154, 178), `docs/known-constraints.md` (lines 20, 38, 167)
- Test: the existing schema registry tests (find with `grep -rln "schema_payload" tests/`), updated for the new totals

**Interfaces:**
- Registry rows for the four content tables (columns `ftp_url`, `doc_filename`, `document_id`, `tdoc_id`, `content`, `content_hash`), `tdoc_extracts.skipped_sources`, and `tdoc.parsed` and `tdoc.document_count`.

**Documentation requirements** (each sentence must match the behaviour from Tasks 1–15):
- Rewrite every "`tdoc_documents` owns inner-file content" sentence to: `tdoc_cr_annexes` owns CR annex content; `tdoc_wid_contents`, `tdoc_ls_contents`, and `tdoc_discussion_contents` own family content (main and annexes); the CR main Markdown stays in the derived cache archive only.
- `document_text` references become `content_text`.
- `AGENTS.md`: correct the direct-mode auto-sync sentence. Both `--from-path` and `--from-url` run steps 2–4 when `sync.auto_sync` is on (P7). Add "Where to look" rows for the content registry, the family extraction service, and the content tables.
- `docs/cli.md`: `tdoc parse --type` default (CR plus families; an explicit value is intersected); `--tdoc` exact-ID skipped behaviour; `tdoc list --parsed` and the `parsed` and `document_count` fields; `tdoc show --format raw` for families and CR (archive main), and the named-type error; the `.doc` main-document limitation; tolerant annexes and `skipped_sources`.
- `docs/known-constraints.md`: remove "TDoc types other than CR (LS, DRAFT, BB, …) are not handled"; state that only `.docx` is converted and that a `.doc`-only main fails; state that CR main text is not full-text indexed (spec §8).
- `docs/architecture.md`, `docs/conventions.md`, `docs/code-map.md`, `docs/web-server.md`, `README.md`: add the new modules, tables, routes, and settings, following the existing style.

- [ ] **Step 1: Write the failing tests**

- `test_schema_payload_lists_content_tables()`: the four content tables with the exact column names.
- `test_schema_payload_has_no_tdoc_documents()`.
- `test_schema_payload_tdoc_has_parsed_and_document_count()`: `parsed` boolean; `document_count` integer, nullable.
- `test_default_config_example_has_content_chunk_cap()`: the TOML example contains `max_content_chunks_per_tdoc = 16`.

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `.venv/bin/python -m pytest <schema test file> -q -p no:cacheprovider`
Expected: FAIL on the new assertions; existing count assertions also change and must be updated to the new totals.

- [ ] **Step 3: Implement the registry, example file, and docs**

Run `grep -rn "tdoc_documents\|document_text\|CR-type\|non-CR\|only CR" README.md AGENTS.md docs/ src/` and update every hit that describes the old behaviour. Leave the two historical files under `docs/superpowers/` (`2026-10-22-tdoc-multi-file-zip-design.md` and `2026-10-22-tdoc-multifile-markdown.md`) unchanged.

- [ ] **Step 4: Run the tests and lint, and confirm they pass**

Run: `.venv/bin/python -m pytest <schema test file> -q -p no:cacheprovider && .venv/bin/ruff check .`
Expected: PASS and `All checks passed!`.

---

## Task 17: Verification and Handoff

**Files:** none modified unless a verification failure requires a fix in its owning task.

- [ ] **Step 1: Rename audit**

Run: `grep -rn "tdoc_documents\|document_text\|TDocDocumentOrm\|file_order" src/ tests/ README.md AGENTS.md docs/ | grep -v "docs/superpowers/specs/2026-10-22-tdoc-multi-file-zip-design.md\|docs/superpowers/plans/2026-10-22-tdoc-multifile-markdown.md"`
Expected: no hits. `file_order` may appear only as the derived index in `TDocDocument` (Task 4 `list_documents`); if it appears elsewhere, fix it in its owning task.

- [ ] **Step 2: Lint**

Run: `.venv/bin/ruff check .`
Expected: `All checks passed!`

- [ ] **Step 3: Full SQLite suite**

Run: `.venv/bin/python -m pytest -q -m "not online" -p no:cacheprovider > /tmp/opencode/type-contents-full.log 2>&1; tail -n 3 /tmp/opencode/type-contents-full.log`
Expected: `0 failed`. The previous baseline was `2787 passed, 1 skipped, 10 deselected`. The new total must be higher and the failure count zero.

- [ ] **Step 4: Spec coverage check**

Walk spec sections 1–13 and name the task and test that covers each requirement. Confirm there is no `TODO` or `TBD` in changed files: `grep -rn "TODO\|TBD" src/doc3gpp/ | grep -i "family\|content"`. Review `git diff -- src/doc3gpp/services/tdoc_cr_service.py` to confirm CR cover, TTCN, and body-change parsing are unchanged, and that the changes are limited to the dispatch points, annex-only persistence, the archive-based cache hit, and tolerant annexes (P13).

- [ ] **Step 5: Report**

Report the changed files, the test totals, and the spec coverage map. Do not commit. Ask the user whether to commit, and whether to merge or push the branch.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-10-22-tdoc-type-contents.md`. P7, P12, and P13 are approved by the user. Execution needs the choice below.

- **P7** (direct modes run the sync steps for `--from-path`; `--from-path` stays in memory) changes documented behaviour and is approved.
- **P12** (replacement deletes from all four content tables) and **P13** (tolerant CR annexes) are approved.

Which execution approach would you prefer?

- **Subagent-driven:** a fresh subagent implements each task, a fresh reviewer checks it before the next one starts, and a whole-branch review runs at the end. This is the most thorough, and it costs a fresh context per task and per review.
- **Native:** I implement every task in this session, then one fresh reviewer checks the whole branch. This is cheaper and faster, with no independent review until the end.

**Recommendation: subagent-driven.** The plan has 17 tasks with many cross-task interfaces (the content repository, the conversion, the selection predicate, and chunk IDs). Interface drift between tasks is the likeliest defect, and a reviewer per task catches it early.

- **REQUIRED SUB-SKILL (if chosen):** superpowers:subagent-driven-development, or superpowers:executing-plans for native execution.

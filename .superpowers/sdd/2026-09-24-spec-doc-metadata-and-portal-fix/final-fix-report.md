# Final Review Fix Report

## Status

Implemented and committed as `c37f292` (`fix(spec-doc): close final metadata review gaps`).

The approved untracked implementation plan,
`docs/superpowers/plans/2026-09-24-spec-doc-metadata-and-portal-fix.md`, was not
modified or staged. Pre-existing worktree changes outside this final fix wave
were also left untouched.

## Findings Addressed

### FTS5 nullable metadata

`SQLAlchemySpecDocSearchRepository.upsert_for_version()` now preserves
`None` for nullable `sections` and `tables` instead of converting them to empty
strings. FTS5 `null` and `not-null` predicates therefore match the relational
chunk contract.

Added explicit integration coverage for both fields and all related rich-filter
branches:

- `null`
- `not-null`
- positive LIKE
- negated `!pattern`
- simultaneous `sections` and `tables` filters with AND semantics

### Overlap metadata scope

`chunk_blocks()` now tracks metadata at source-token granularity. When overlap
tokens are prepended, only metadata attached to the trailing token range is
merged into the following chunk. Ordered deduplication and current-chunk
metadata remain unchanged.

Regression coverage includes:

- a previous chunk with earlier and later section metadata where only the later
  section overlaps;
- the equivalent table-overlap case;
- repeated section/table metadata deduplication.

Existing atomic-table, chunk-boundary, character-ceiling, file-tag, and empty
metadata behavior remains covered.

### Stable stale-only rebuild cutoff

`rebuild_batch(stale_only=True)` captures `last_indexed_parsed_at` once before
iterating batches and binds that stable cutoff for every page. Per-version
`upsert_for_version()` watermark updates can no longer remove older stale rows
from later pages.

The regression seeds two stale versions and rebuilds with `batch_size=1`; both
versions must be indexed.

### Configured cache path expansion

`SpecDocSettings.cache_dir` now applies `Path.expanduser()` after Pydantic
validation. TOML-style `~/.cache/doc3gpp/specs` values resolve under the user
home, while absolute explicit paths and the absolute default remain unchanged.

### Contract coverage

The final wave pins:

- exact `_SPEC_DOC_SNIPPET_COLUMNS` ordering;
- removal of all split chunk/hit/filter DTO fields;
- nullable FTS metadata behavior;
- complete rich-filter semantics for both combined metadata fields;
- overlap deduplication and table overlap;
- configured cache path behavior.

### Vector-side `release` semantics

The existing semantic implementation intentionally uses exact equality for
vector-side `spec_id`, `version`, and `release`, while vector-side `sections`
and `tables` use plain LIKE. The FTS side remains the rich-filter path.

This is now explicit and consistent in:

- `src/doc3gpp/storage/repositories/spec_doc_vector_sql.py`;
- `src/doc3gpp/services/spec_doc_semantic_service.py`;
- `docs/architecture.md`;
- `docs/cli.md`;
- `AGENTS.md`.

An optional sqlite-vec regression asserts that `release="Rel-18"` matches and
`release="Rel-1%"` does not, preserving the intentional exact contract.

## TDD Evidence

The new regression tests were written before production changes.

Initial red run:

```text
rtk pytest tests/unit/test_spec_doc_models.py tests/unit/test_spec_doc_settings.py tests/unit/test_spec_doc_chunker.py tests/integration/test_spec_doc_search_repo.py -q
Pytest: 33 passed, 7 failed
```

The failures were the expected reviewer defects:

- configured `~` cache path stayed literal;
- section overlap copied the earlier metadata;
- table overlap copied the earlier metadata;
- nullable FTS filters returned no matching rows;
- stale-only batch size 1 indexed only one of two stale versions.

Green focused run after the minimal fixes:

```text
rtk pytest tests/unit/test_spec_doc_models.py tests/unit/test_spec_doc_settings.py tests/unit/test_spec_doc_chunker.py tests/integration/test_spec_doc_search_repo.py -q
40 passed
```

The covering spec-document/web/MCP run then passed:

```text
rtk pytest tests/unit/test_spec_doc_*.py tests/integration/test_spec_doc_*.py tests/unit/test_web_routes.py tests/integration/test_mcp_end_to_end.py -q
330 passed
```

## Verification

Full offline SQLite suite:

```text
rtk ./scripts/test_sqlite.sh
2405 passed, 1 skipped, 109 warnings in 103.45s
```

The skip is the existing optional-dependency skip. Existing collection and
datetime deprecation warnings remain; the stale-rebuild path also exercises the
project's existing `datetime.utcnow()` warning in
`spec_doc_search_service.py`.

Lint:

```text
rtk ruff check .
clean, exit 0
```

Whitespace:

```text
rtk git diff --check
clean, exit 0
```

The focused fix files also passed their targeted Ruff check before the full
gate.

## Files In Commit

- `AGENTS.md`
- `docs/architecture.md`
- `docs/cli.md`
- `src/doc3gpp/parsers/spec_doc_chunker.py`
- `src/doc3gpp/settings/schema.py`
- `src/doc3gpp/storage/repositories/spec_doc_search_sql.py`
- `tests/integration/test_spec_doc_search_repo.py`
- `tests/unit/test_spec_doc_chunker.py`
- `tests/unit/test_spec_doc_models.py`
- `tests/unit/test_spec_doc_settings.py`

No migration, public repair command, unrelated TDoc behavior, or generated
artifact was added.

## Concerns

- Vector-side semantic behavior was not changed from exact `release` matching;
  the docs now state that contract instead of claiming release LIKE behavior.
- sqlite-vec-specific assertions are skipped when the optional extension is not
  installed; the core FTS, chunker, cache, and stale-rebuild coverage is
  extension-independent.
- The repository retains its pre-existing warning set, including UTC datetime
  deprecations.

# Final Review Fix Report

## Context

This fix wave addresses the final review findings for the spec-document web
portal branch at `9e44230`. The pre-existing untracked approved plan,
`docs/superpowers/plans/2026-09-24-spec-doc-web-portal.md`, was not modified or
staged.

## Files Changed

- `src/doc3gpp/storage/repositories/spec_doc_sql.py`
  - Removed the `parsed_at` and `chunk_count` success-state mutation from
    `replace_chunks()`.
  - `record_parsed()` is now the only repository operation that marks a source
    parsed or records its final chunk count.
- `tests/integration/test_spec_doc_service.py`
  - Added a regression test that fails the markdown-cache write after chunks
    are stored, verifies `parsed_at` remains unset, and verifies a subsequent
    non-force parse succeeds rather than being skipped.
- `tests/integration/test_spec_doc_repo.py`
  - Updated the repository round-trip to assert that chunk replacement alone
    leaves the source unparsed, then explicitly records the successful parse.
  - Added coverage that direct chunk replacement still creates an explicitly
    unparsed source row when no source row exists.
- `src/doc3gpp/web/templates/spec_doc_show.html`
  - Hides the force checkbox unless the source is parsed.
  - Adds an explicit parsed-state data attribute for defensive client logic.
- `src/doc3gpp/web/static/js/spec_doc_parse.js`
  - Forces the submitted `force` value to `false` unless the form declares a
    parsed source, even if a stale or injected checkbox is present.
- `src/doc3gpp/web/templates/partials/spec_doc_show_results.html`
  - Renders the stored TOC file summary, including source filename, file order,
    and first section.
- `src/doc3gpp/web/templates/partials/spec_doc_chunks.html`
  - Distinguishes a parsed zero-chunk source from a non-matching section
    filter.
- `tests/unit/test_web_routes.py`
  - Covers force-control availability for unparsed and parsed states, the TOC
    file summary, the zero-chunk message, and the JS parsed-state guard.
- `docs/web-server.md`
  - Clarifies that the endpoint accepts optional `release`, while the version
    page form submits `spec_ids`, the selected exact `version`, and `force`
    without a release selector.

## Tests And Commands

- `rtk pytest tests/integration/test_spec_doc_service.py -k post_chunk_cache_failure -q`
  - Initial TDD red run: failed because `replace_chunks()` had already set
    `parsed_at`; this confirmed the regression exercised the reported bug.
- `rtk pytest tests/integration/test_spec_doc_service.py tests/integration/test_spec_doc_repo.py -q`
  - `10 passed`.
- `rtk pytest tests/unit/test_web_routes.py -k 'spec_doc_show or spec_doc_parse_js' -q`
  - `8 passed`.
- `rtk pytest tests/unit/test_spec_doc_service_reads.py tests/unit/test_spec_doc_source.py tests/integration/test_spec_doc_service.py tests/integration/test_spec_doc_repo.py tests/integration/test_spec_doc_search_repo.py tests/integration/test_spec_doc_search_service.py tests/integration/test_spec_doc_web.py tests/unit/test_web_routes.py tests/unit/web/test_landing_version.py -q`
  - `225 passed`.
- `rtk pytest tests/integration/test_spec_doc_cli.py tests/unit/test_spec_doc_settings.py tests/unit/test_spec_doc_models.py tests/unit/test_spec_doc_parser.py tests/unit/test_spec_doc_chunker.py -q`
  - `23 passed`.
- `rtk ./scripts/test_sqlite.sh`
  - `2356 passed, 1 skipped, 108 warnings`.
  - Warnings are the repository's existing pytest collection and deprecation
    warnings; no test failures occurred.
- `rtk ruff check .`
  - Clean.
- `rtk git diff --check`
  - Clean.

## Self-Review

- A failed parse after chunk replacement and before markdown-cache completion
  no longer creates a false successful parse marker. The retry path uses the
  existing ZIP cache and reaches `record_parsed()` normally.
- Existing force re-parse behavior remains available for parsed sources. The
  browser checkbox is absent for unparsed/downloaded sources, and the client
  independently clamps `force` to `false` in that state.
- The version page's TOC now includes the complete stored file summary using
  the same fields as the existing standalone TOC page.
- A parsed source with `chunk_count == 0` gets an incomplete/corrupt/zero-chunk
  message. A parsed source with chunks but no rows after filtering retains the
  filter-empty message.
- The documentation now separates endpoint query support from the form's
  exact-version job payload.
- No CLI, MCP, JSON, schema, or job contract was changed.
- No direct semantic nested-hit route test was added; this remains the
  explicitly accepted test-only residual from the prior review.

## Concerns

- TOC persistence, chunk replacement, filesystem cache writes, and the parsed
  ledger remain separate operations because the existing repository architecture
  does not provide one cross-storage transaction. A failed attempt can leave
  intermediate TOC/chunk/cache data, but `parsed_at` remains unset and the
  next parse replaces those intermediates before recording success.
- The full suite retains the existing 108 warnings noted above; they are not
  introduced by this fix wave.

## Fix Round 2

### Finding Addressed

A forced re-parse of an already parsed version could leave the old non-null
`parsed_at` marker in place when post-chunk/cache work failed. A later
non-force parse then treated the failed re-parse as an immutable success and
skipped it.

### Files Changed

- `src/doc3gpp/storage/repositories/spec_doc_sql.py`
  - `record_download()` now clears `parsed_at` and resets `chunk_count` when
    refreshing an existing source row. This invalidates the previous success
    marker at the force-download boundary while preserving the existing method
    signature and repository flow.
- `tests/integration/test_spec_doc_service.py`
  - Added coverage that parses a version successfully, forces a re-parse that
    fails during markdown-cache writing, verifies the old marker is cleared,
    and verifies a subsequent non-force parse succeeds.
- `.superpowers/sdd/2026-09-24-spec-doc-web-portal/final-fix-report.md`
  - Appended this fix-round record. The approved untracked plan remains
    untouched and unstaged.

### Tests And Commands

- `rtk pytest tests/integration/test_spec_doc_service.py -k 'failed_force_reparse_invalidates_old_marker_for_retry or post_chunk_cache_failure or force_reparses' -q`
  - `3 passed`.
- `rtk pytest tests/integration/test_spec_doc_repo.py -q`
  - `2 passed`.
- `rtk pytest tests/integration/test_spec_doc_service.py tests/integration/test_spec_doc_repo.py tests/integration/test_spec_doc_search_repo.py tests/integration/test_spec_doc_search_service.py tests/integration/test_spec_doc_web.py tests/unit/test_spec_doc_service_reads.py tests/unit/test_web_routes.py tests/unit/web/test_landing_version.py -q`
  - `220 passed`.
- `rtk ./scripts/test_sqlite.sh`
  - `2357 passed, 1 skipped, 108 warnings`.
- `rtk ruff check .`
  - Clean.
- `rtk git diff --check`
  - Clean.

### Self-Review

- A successful force re-parse still reaches `record_parsed()` and restores a
  non-null marker with the new chunk count.
- A non-force immutable skip does not call `record_download()` when the ZIP
  cache and parsed source already exist, so existing skip behavior is
  unchanged.
- If a forced download or a cache-miss download replaces an existing source,
  its previous success marker is invalidated before parse work begins; any
  later failure therefore remains retryable.
- `record_parsed()` remains the only operation that marks a source as
  successfully parsed.
- The fix changes no schema, service signature, CLI, HTTP, MCP, or job
  contract.

### Concerns

- As in the first fix round, TOC/chunk/cache writes and the source ledger use
  separate repository/filesystem transactions. A failed force re-parse may
  leave intermediate replacement data, but the source remains unparsed and a
  subsequent retry replaces those intermediates before recording success.
- The full suite retains the existing 108 warnings; no new warning category
  was introduced by this round.

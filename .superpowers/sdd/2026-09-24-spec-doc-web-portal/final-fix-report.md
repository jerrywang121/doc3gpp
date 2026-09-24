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

## Fix Round 3

### Finding Addressed

The round-2 placement was still too late for a forced network failure:
`SpecDocService.fetch(force=True)` called `_fetcher()` before
`record_download()`, so a failed download left an already parsed source's old
`parsed_at` marker intact. The final fix invalidates parse state before the
forced fetch, without recording a download that did not succeed.

### TDD Failing-Test Evidence

Added `test_failed_force_download_invalidates_old_marker_for_retry` to
`tests/integration/test_spec_doc_service.py`. The first focused run was:

```text
rtk pytest tests/integration/test_spec_doc_service.py -k failed_force_download_invalidates_old_marker_for_retry -q
Pytest: 0 passed, 1 failed
AssertionError: source.parsed_at is None
```

The failure reproduced the confirmed root cause: the source remained parsed
after the forced `_fetcher()` raised `OSError("upstream unavailable")`.

### Files Changed

- `src/doc3gpp/repository/protocols.py`
  - Added the clearly named `SpecDocRepository.invalidate_parse_state()`
    operation.
- `src/doc3gpp/storage/repositories/spec_doc_sql.py`
  - Implemented `invalidate_parse_state()` as a committed update of only
    `parsed_at` and `chunk_count` for an existing source row.
  - Kept `record_download()` responsible for successful-download metadata;
    it no longer doubles as parse-state invalidation.
- `src/doc3gpp/services/spec_doc_service.py`
  - On `force=True`, resolves the existing source and invalidates its parse
    state before invoking `_fetcher()`.
  - Does nothing for a missing source, preserving the first-download path.
- `tests/integration/test_spec_doc_service.py`
  - Added the required already-parsed, forced-network-failure, then
    non-force-retry regression.
- `tests/integration/test_spec_doc_repo.py`
  - Verifies explicit invalidation clears only parse state while preserving
    release, FTP URL, download timestamp, and DOCX count.
- `.superpowers/sdd/2026-09-24-spec-doc-web-portal/final-fix-report.md`
  - Appended this round-3 record. The approved untracked plan remains
    untouched and unstaged.

### Tests And Commands

- `rtk pytest tests/integration/test_spec_doc_service.py -k failed_force_download_invalidates_old_marker_for_retry -q`
  - Initial TDD red run: `0 passed, 1 failed`, with the expected stale
    `parsed_at` assertion failure.
- `rtk pytest tests/integration/test_spec_doc_service.py -k 'failed_force_download_invalidates_old_marker_for_retry or failed_force_reparse_invalidates_old_marker_for_retry or post_chunk_cache_failure or force_reparses' -q`
  - `4 passed` after the fix.
- `rtk pytest tests/integration/test_spec_doc_repo.py -q`
  - `2 passed`.
- `rtk pytest tests/integration/test_spec_doc_service.py tests/integration/test_spec_doc_repo.py tests/integration/test_spec_doc_search_repo.py tests/integration/test_spec_doc_search_service.py tests/integration/test_spec_doc_web.py tests/unit/test_spec_doc_service_reads.py tests/unit/test_web_routes.py tests/unit/web/test_landing_version.py -q`
  - `221 passed`.
- `rtk ./scripts/test_sqlite.sh`
  - `2358 passed, 1 skipped, 108 warnings`.
- `rtk ruff check .`
  - Clean.
- `rtk git diff --check`
  - Clean.

### Self-Review

- A forced re-parse with an existing source clears the success marker before
  any network, ZIP-size, cache-write, or parse operation can fail.
- The invalidation operation preserves truthful download metadata and does not
  create a source row or claim that a forced download succeeded.
- Successful force re-parse still runs `record_download()` after bytes arrive,
  then `record_parsed()` after all required parse/cache work completes.
- A non-force immutable skip does not call the invalidation operation because
  it returns through the existing cache/parsed check without entering the
  forced path.
- First-parse failures and the prior post-chunk/cache failure regression remain
  covered and retryable.
- The round-2 reset inside `record_download()` was removed; round 3 replaces
  that late invalidation with the explicit pre-fetch operation requested by the
  review.
- No web portal behavior, schema, job payload, CLI contract, or public service
  signature changed.

### Concerns

- Parse-state invalidation and the later network/cache/parse work remain
  separate transactions under the existing repository architecture. After a
  forced network failure, prior download metadata and cached bytes remain, but
  `parsed_at` is intentionally null so the source is not treated as a
  successful parse and the next non-force run retries.
- A failure after TOC/chunk replacement can still leave intermediate data; the
  existing retry path replaces it before `record_parsed()` marks success.
- The full suite retains the existing 108 warnings; no new warning category
  was introduced by this round.

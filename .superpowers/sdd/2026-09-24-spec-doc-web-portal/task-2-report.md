# Task 2 Report: Add Spec Document Version Read Page

## Files Changed

- `src/doc3gpp/web/routes/spec_docs.py`
  - Added `GET /specs/{spec_id}/docs` before the existing TOC route.
  - Resolves the parent spec and exact requested version through the injected `SpecService`.
  - Reads source, TOC, and chunks only through `SpecDocService.get_source`, `get_toc`, and `list_chunks`.
  - Supports release/section filters, bounded chunk pagination, one-row probe fetching, and HTMX `#results` fragments.
  - Preserves the existing error mapping for missing specs, missing versions, disabled spec-doc services, and invalid query values.
- `src/doc3gpp/web/templates/spec_doc_show.html`
  - Added the full version document page with spec/version identity, parent-spec link, source links, and metadata.
- `src/doc3gpp/web/templates/partials/spec_doc_show_results.html`
  - Added the `#results` fragment with source state, parsed metadata, TOC, and chunk composition.
- `src/doc3gpp/web/templates/partials/spec_doc_chunks.html`
  - Added chunk cards, section/table metadata, source filenames, preformatted text, and filter-preserving pagination.
- `src/doc3gpp/web/static/style.css`
  - Added responsive source/chunk page styles while reusing existing portal primitives.
- `tests/unit/test_web_routes.py`
  - Added fake public spec-document service data and focused tests for unparsed state, parsed TOC/chunks, probe pagination, HTMX fragments, and unknown versions.
- `.superpowers/sdd/2026-09-24-spec-doc-web-portal/task-2-report.md`
  - Added this report.

The pre-existing untracked `docs/superpowers/plans/2026-09-24-spec-doc-web-portal.md` was not modified or staged.

## Tests And Commands

- `rtk pytest tests/unit/test_web_routes.py -k "spec_doc_show" -q`
  - Initial TDD red run: `0 passed, 5 failed`; the expected failure was HTTP 404 because the new route did not yet exist.
- `rtk pytest tests/unit/test_web_routes.py -k "spec_doc_show" -q`
  - Final result: `5 passed`.
- `rtk pytest tests/integration/test_spec_doc_web.py -q`
  - Final result: `3 passed`.
- `rtk pytest tests/unit/test_web_routes.py -q`
  - Final result: `178 passed`.
- `rtk ruff check src/doc3gpp/web/routes/spec_docs.py tests/unit/test_web_routes.py`
  - Final result: clean (`[]`).
- `rtk git diff --check`
  - Final result: clean.
- `rtk pytest`
  - Collection did not start because three existing integration/unit modules import `tests.integration.test_web_end_to_end` and the plain executable entry point did not expose the repository root as the `tests` package (`3 ModuleNotFoundError` collection errors).
- `rtk python -m pytest -q`
  - Final result: `2346 passed, 1 skipped, 10 deselected, 18 warnings` in `401.75s`.
- `rtk grep '_doc_repo|fetch|parse form|spec-doc-parse-form|jobs/parse' src/doc3gpp/web/routes/spec_docs.py`
  - Final result: no route/private-repository/fetch/parse-control matches.

## Self-Review

- The route is registered before `/specs/{spec_id}/docs/toc`, so the static TOC path remains reachable.
- Exact version identity is verified after `SpecService.list_versions(..., version=version)`; unknown versions include available version strings in the normal 404 envelope.
- `parsed_at` is the authoritative gate: unparsed or missing source rows do not trigger TOC/chunk reads.
- A missing TOC for an otherwise parsed source is treated as absent while chunks continue to render, as required.
- Pagination requests `limit + 1`, renders only `limit`, and preserves `version`, `release`, and `section` in next/previous links.
- HTMX responses render only `partials/spec_doc_show_results.html` with the required outer `#results` element.
- No JSON contract, storage schema, CLI, MCP, fetch route, version-table link, or synchronous parse behavior was added or changed.
- The direct task instruction defers parse controls to Task 3, so the page intentionally renders read/source state without a parse form or parse JavaScript despite the broader plan's future parse-form design.
- Tests use the public `SpecDocService` surface and do not access `_doc_repo`.

## Concerns

- The page currently has no parse action by design; Task 3 must add the parse form/job poller integration.
- The pre-existing untracked plan file remains in the worktree and was intentionally left untouched.

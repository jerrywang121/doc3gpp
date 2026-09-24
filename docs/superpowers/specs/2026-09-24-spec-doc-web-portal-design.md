# Spec Document Web Portal — Design

**Date:** 2026-09-24  
**Branch:** `feature/spec-doc-parse`  
**Status:** approved, pending implementation plan

## Goal

Make the existing spec-document corpus visible and usable from the web portal:
from a spec's version list, a user can open a version-specific document page,
parse or re-parse the document, inspect its TOC, and browse paginated parsed
chunks. Add a separate Spec Docs search tab alongside the existing TDoc search.

## Root Cause

The spec-document backend is already wired into the FastAPI app, but its human
workflow is orphaned:

- `spec_show.html` has no per-version document link.
- `base.html` and `landing.py` have no Spec Docs entry.
- `GET /specs/{spec_id}/docs/toc` requires a manually constructed URL and only
  renders an already-stored TOC.
- `POST /jobs/parse/spec-docs` exists, but no portal page presents a parse form.
- `SpecDocRepository.list_chunks()` exists, but no web-facing service method or
  route exposes parsed chunks.
- `/spec-docs/search` and `/spec-docs/search/sem` exist, but the top-level
  Search page only links to TDoc search, so users cannot discover them.

## Scope

### In scope

- A version-first document page linked from every row of the existing spec
  detail version table.
- Parse and force re-parse controls using the existing background job contract.
- Parsed-source status, complete TOC display, and paginated chunk display.
- Search tabs for TDoc Search and Spec Docs Search.
- Links from Spec Docs search hits to the version document page.
- Web route, service, template, JavaScript, test, and current web-documentation
  updates required by the workflow.

### Out of scope

- A standalone batch parse-management page.
- A public standalone fetch action or fetch route. Parsing remains the visible
  action and retains its existing fetch-if-missing behavior.
- Changes to CLI, MCP, or existing JSON payload contracts.
- Changes to spec version selection, chunking, indexing, or storage schema.
- A new document download or raw-markdown viewer.

## User Flow

1. User opens `/specs` and selects a spec.
2. On `/specs/{spec_id}`, each version row has a `show` link in a Docs
   column.
3. The link opens:

   ```text
   GET /specs/{spec_id}/docs?version=<version>
   ```

4. The document page displays the spec/version identity and source state.
5. If the version is not parsed, the user selects **Parse document**. If it is
   already parsed, the user may select **Force re-parse**.
6. The form enqueues:

   ```json
   {
     "spec_ids": ["38.331"],
     "version": "18.5.0",
     "force": false
   }
   ```

   to `POST /jobs/parse/spec-docs`.
7. The shared job poller renders inline status and reloads the page after a
   terminal job state so the source, TOC, and chunks reflect the database.
8. Once parsed, the page shows the TOC and the first chunk page. Previous and
   next controls navigate the chunk pages.
9. A user can open `/tdocs/search` and switch to **Spec Docs Search**, or open
   `/spec-docs/search` directly. Search hits link back to the version document
   page.

## Architecture

### Version document route

Add a human-facing route in `src/doc3gpp/web/routes/spec_docs.py`:

```text
GET /specs/{spec_id}/docs?version=<version>&release=&section=&limit=20&offset=0
```

The route will:

- Resolve the parent `Spec` and requested `SpecVersion` through the existing
  `SpecService` dependency. A missing parent spec uses the existing
  `SpecNotFoundError` behavior; a missing version is a 404 with the normal web
  error envelope.
- Require `version` for the HTML page. The existing TOC/API route remains
  available for compatibility and JSON clients.
- Read the `(spec_id, version)` source ledger from `SpecDocService`.
- Read the stored TOC only when the source is parsed, using the existing
  `get_toc` path.
- Read chunks only when the source is parsed, using a new public service method.
- Fetch one extra chunk beyond the requested page size. Render only the
  requested page and use the extra row to determine `next_offset`.
- Render the full page on ordinary requests and a dedicated results fragment
  for `HX-Request` when chunk pagination or section filtering is enhanced with
  HTMX. The fragment's outer element is `#results`, matching existing portal
  conventions.

The canonical human-facing document page is `/specs/{spec_id}/docs`; the
existing `/specs/{spec_id}/docs/toc` route remains a TOC-specific compatibility
and machine-readable surface. The new page does not add a fetch route.

### Service boundary

Extend `SpecDocService` with public read methods that delegate to the injected
`SpecDocRepository`; routes must not access the private `_doc_repo` field:

```python
def get_source(self, spec_id: str, version: str) -> SpecDocSource | None: ...

def list_chunks(
    self,
    spec_id: str,
    *,
    version: str,
    release: str | None = None,
    section: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[SpecDocChunk]: ...
```

The repository already supports the required `(spec_id, version, release,
section, limit, offset)` chunk query. No schema or repository SQL change is
required unless implementation testing reveals a contract gap.

### Parse job integration

Add a version-specific parse form to the document page. It uses the existing
`POST /jobs/parse/spec-docs` endpoint and `JobKind.PARSE_SPEC_DOCS`; no new job
kind or handler is needed.

Add a focused JavaScript wrapper, following `spec_sync.js` and `tdoc_parse.js`,
that calls `bindJobPolling` with:

- `contentType: "application/json"`;
- `queuedSelector` for the document page's queued message;
- `targetSelector` for the inline job-status target;
- a body containing the current spec ID, version, and force checkbox;
- the default terminal reload behavior.

The form must preserve the existing immutable parse semantics:

- An unparsed or downloaded-but-unparsed version submits `force: false`.
- A parsed version can submit `force: true` when the user explicitly checks
  **Force re-parse**.
- There is no separate download button.

### Search tabs

Keep the single top-level `Search` navigation item pointing to TDoc search.
Inside both search page families, render a shared tab partial:

```text
[TDoc Search] [Spec Docs Search]
```

Tab behavior:

- `/tdocs/search` and `/tdocs/search/sem` remain TDoc Search.
- `/spec-docs/search` and `/spec-docs/search/sem` are Spec Docs Search.
- Each resource keeps its existing FTS5 and semantic/hybrid submode links.
- Spec Docs filters remain Query, Spec, Release, Version, Section, and Limit,
  plus FTS5 query/weight in semantic mode.
- HTMX forms continue to swap only `#results`.
- Spec Docs results link their spec/version identity to
  `/specs/{spec_id}/docs?version={version}`. The page may accept an optional
  `chunk` anchor so a result can focus the matching chunk when that chunk is
  rendered.
- Existing JSON routes and payloads do not change.

The shared tab partial avoids duplicating tab markup while leaving each search
resource's form and result table independent.

### Navigation and landing page

The Specs page remains the primary document entry point. Add discoverability
without introducing an unnecessary top-level nav item:

- Add `Spec Docs` to the landing page's static sections, pointing to
  `/spec-docs/search`.
- Add the shared search tabs so the feature is visible from the existing Search
  navigation.
- Keep `active_nav="search"` on Spec Docs search pages.
- Keep `active_nav="specs"` on the version document page so users retain the
  Specs context.

## Page Layout and Data States

### Header and source card

The document page header shows:

- Spec type, spec ID, title, and version.
- Release, upload date, and links to the existing ZIP/PDF version resources
  when present.
- A link back to the parent spec detail page.

The source card distinguishes:

- **Not parsed:** no source ledger row. Show a parse action and explain that
  parsing downloads and converts the version documents.
- **Downloaded, not parsed:** source row has `downloaded_at` but no
  `parsed_at`. Show a parse action.
- **Parsed:** show parsed timestamp, DOCX count, and chunk count, plus a force
  re-parse action.

The page must not infer parsed state from cache files; `SpecDocSource.parsed_at`
is the authoritative state.

### TOC

When a parsed TOC exists, render the complete TOC using the existing TOC
fields: section number, title, level, and source file. Preserve the existing
source-file summary. When no TOC exists yet, show a concise “Parse this
version to view its TOC” message.

### Chunks

Render chunks in repository `chunk_index` order. Each chunk card includes:

- Stable `chunk_id` and human-readable chunk number.
- Section number/title when present.
- Table number/title when present.
- Source file.
- Text in a whitespace-preserving block.

Chunk pagination defaults to `20` and caps at `100`. Query parameters are
`limit`, `offset`, and optional `section`. The route fetches `limit + 1`,
renders at most `limit`, and supplies `next_offset` only when another chunk
exists. Pagination links preserve `version`, `section`, and `limit`.

If a parsed source reports zero chunks, render a clear empty/corrupt-state
message rather than an empty area with no explanation.

## Errors and Availability

- Unknown spec: existing `SpecNotFoundError` response.
- Known spec with unknown version: HTTP 404 using the existing web error
  envelope and a message naming the spec/version.
- Known version with no specdata source row: normal unparsed state, not an
  error.
- Parse job failure: the shared job-status component shows the failure; the
  document page is not refreshed into a false parsed state.
- Disabled Spec Docs FTS5 service: preserve current 503 behavior.
- Disabled semantic service or missing embedder/vector support: preserve the
  current disabled-settings behavior.
- Invalid `limit`, `offset`, or section filter: use the existing
  `InvalidFilterError` mapping and bounds.
- No standalone fetch control or route is introduced.

## Testing

### Route and service tests

- `SpecDocService.get_source` delegates and returns source state.
- `SpecDocService.list_chunks` delegates the version and pagination/filter
  arguments.
- Version document route renders the unparsed state without requiring a TOC or
  chunk row.
- Version document route renders parsed source metadata, TOC, and chunks.
- Unknown spec/version responses use the expected status and error envelope.
- Chunk pagination uses the extra-row probe, renders the correct slice, and
  preserves query parameters in previous/next links.
- Section filtering is passed to the repository.
- `show` links appear for every version in `spec_show.html`.

### Parse form and job tests

- The document page includes the parse endpoint, spec ID, version, force input,
  and job target.
- Static JavaScript coverage verifies the JSON body contains `spec_ids`, the
  current `version`, and the force value.
- Existing parse job route behavior and no-public-fetch-route regression remain
  covered.

### Search and discoverability tests

- TDoc search renders the TDoc Search and Spec Docs Search tabs.
- Spec Docs search renders the same tabs with Spec Docs active.
- Spec Docs FTS5 and semantic mode links remain discoverable.
- Spec Docs result rows link to the version document page.
- Landing-page JSON/HTML includes the Spec Docs search section.
- Existing TDoc search routes and JSON payloads remain unchanged.

### Verification

Run focused web/service tests, then the full project gates:

```bash
python -m pytest tests/integration/test_spec_doc_web.py tests/unit/test_web_routes.py tests/unit/test_web_jobs_routes.py
./scripts/test_sqlite.sh
ruff check .
```

## Files and Documentation

Expected implementation files:

- Modify `src/doc3gpp/services/spec_doc_service.py` with public source/chunk
  reads.
- Modify `src/doc3gpp/web/routes/spec_docs.py` with the version document route
  and page context.
- Modify `src/doc3gpp/web/routes/specs.py` or its version result template only
  as needed for route validation and link context.
- Modify `src/doc3gpp/web/templates/spec_show.html` with the Docs column and
  per-version `show` links in the spec detail version table.
- Add or modify document-page templates and chunk partials under
  `src/doc3gpp/web/templates/`.
- Add a shared search-tab partial and include it from TDoc and Spec Docs search
  pages.
- Modify `src/doc3gpp/web/templates/landing.html` only if the static landing
  section requires a presentation change; the section data lives in
  `src/doc3gpp/web/routes/landing.py`.
- Add a document parse JavaScript wrapper under
  `src/doc3gpp/web/static/js/`.
- Add focused route, service, template, and static-JavaScript tests under
  `tests/`.

Update current documentation in `docs/web-server.md`, `AGENTS.md`, and any
affected architecture/code-map entries. Historical design and plan documents
are not rewritten to change their historical scope.

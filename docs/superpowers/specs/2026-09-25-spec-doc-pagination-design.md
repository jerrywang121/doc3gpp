# Spec Document Chunk Pagination — Design

**Date:** 2026-09-25  
**Status:** approved, pending implementation plan

## Goal

Improve the pagination footer on the spec-document chunk list. Replace the
current `prev` / `next` controls with a compact paginator that shows the
current range, first/last controls, and a rolling window of up to ten page
numbers. Pagination must remain accurate when Sections or Tables filters are
active.

Example target shape:

```text
Showing 321–340 | ‹‹ first ‹ prev 1 2 3 4 5 6 [7] 8 9 ... next › last ››
```

## Scope

### In scope

- Count matching chunks using the active version, release, Sections, and
  Tables predicates.
- Render a page range and a rolling page-number window with at most ten
  numeric page links.
- Add first, previous, next, and last navigation links where applicable.
- Preserve all current document-page query parameters in every pagination URL.
- Clamp stale offsets beyond the final page to the last valid page.
- Keep behavior consistent for normal full-page responses and HTMX result
  fragments.
- Add repository, service, route, and template tests for the new behavior.

### Out of scope

- Changes to the spec-document schema, chunk ordering, chunk size, or search
  contracts.
- Changes to CLI or MCP pagination output.
- Changes to the existing page-size limit or offset query syntax.
- A separate pagination component used by unrelated resources.

## Existing Behavior

`GET /specs/{spec_id}/docs` currently fetches `limit + 1` chunks from
`SpecDocService.list_chunks`. The extra row indicates whether a next page
exists, while the source ledger's `chunk_count` is displayed as an overall
total. The template renders only `‹ prev` and `next ›` links and derives the
display range from the requested offset and returned page length.

The source ledger total is not sufficient for filtered pagination: Sections
and Tables predicates can reduce the result set. The new implementation will
therefore use a filtered count query rather than treating the source
ledger's unfiltered count as authoritative.

## Design

### Repository and service boundary

Add a `count_chunks` operation to `SpecDocRepository`:

```python
def count_chunks(
    self,
    spec_id: str,
    *,
    version: str | None = None,
    release: str | None = None,
    sections: str | None = None,
    tables: str | None = None,
) -> int: ...
```

Implement it in `SQLAlchemySpecDocRepository` with `SELECT COUNT(*)` and the
same predicates as `list_chunks`: `spec_id`, optional exact `version`,
optional `release`, and the existing text-filter handling for `sections` and
`tables`.

Add the matching public delegation method to `SpecDocService`:

```python
def count_chunks(
    self,
    spec_id: str,
    *,
    version: str,
    release: str | None = None,
    sections: str | None = None,
    tables: str | None = None,
) -> int: ...
```

The web route must use this service method rather than accessing the private
repository field. Existing `list_chunks` behavior remains unchanged and is
still used to fetch the visible page plus one probe row.

### Route pagination calculation

For a parsed source, `spec_doc_show` will:

1. Count matching chunks with the active version, release, Sections, and
   Tables filters.
2. Compute `total_pages = ceil(total_chunks / limit)`.
3. If the requested offset is beyond the final page, replace it with the
   offset of the final page before fetching chunks.
4. Fetch `limit + 1` chunks at the effective offset and render at most `limit`.
5. Derive the displayed range from the effective offset and visible chunk
   count.

When no chunks match, no pagination controls or range are rendered. When a
parsed source reports zero chunks, the existing incomplete/corrupt-state
message remains unchanged.

The route will pass pagination metadata to the templates, including:

- `total_chunks`
- `current_page`
- `total_pages`
- `page_items`
- `first_offset`
- `previous_offset`
- `next_offset`
- `last_offset`

The page item sequence contains numeric page descriptors and ellipsis
markers. It shows at most ten numeric pages, keeps the current page visible,
and inserts an ellipsis only where a hidden page range exists. For a short
document it shows every page. First and last controls remain available when
the current page is not already at that boundary.

### Page-window behavior

The page window is rolling rather than fixed to pages 1–10. It should expose
the current page and nearby pages while keeping the total number of numeric
links at or below ten. The deterministic rule is:

- If `total_pages <= 10`, show every page number.
- Otherwise, choose a contiguous ten-page window.
- Keep the window at pages 1–10 while the current page is in its first six
  pages.
- Keep the window at the final ten pages while the current page is in its
  final five pages.
- Otherwise, center the current page in the window with four pages before it
  and five pages after it.

Add an ellipsis before the window when its first page is greater than 1 and
after the window when its last page is less than `total_pages`. The explicit
first/last controls provide boundary navigation even when those boundary
pages are outside the numeric window.

The exact window calculation belongs in the route module as a small pure
helper or equivalent route-local logic, so it can be unit tested without
rendering templates. It must handle:

- one-page and zero-page result sets;
- first, middle, and final pages;
- fewer than ten total pages;
- more than ten total pages;
- current pages near either boundary.

### Template and links

Update `src/doc3gpp/web/templates/partials/spec_doc_chunks.html` to render:

- `Showing start–end` when the current page contains chunks;
- `‹‹ first` when a later page exists;
- `‹ prev` when a previous page exists;
- numeric page links for non-current pages;
- a highlighted, non-link current-page marker;
- `...` markers for hidden page ranges;
- `next ›` when a later page exists;
- `last ››` when a later page exists.

All generated URLs must preserve `version`, `release`, `sections`, `tables`,
and `limit`. The HTMX fragment will continue to use the same partial, so
pagination links retain the existing full-page navigation behavior unless the
current page's existing request flow explicitly enhances them through HTMX.

Use accessible link text or title attributes for the symbol-based first,
previous, next, and last controls without changing the requested compact
visible labels.

## Error and Boundary Handling

- Invalid `limit` and `offset` values continue to use the existing query
  parsing and `InvalidFilterError` behavior.
- A negative offset remains invalid through the existing parser.
- An offset beyond the final page is clamped to the final valid page, rather
  than producing an empty page with an incorrect range.
- A zero-count filtered result has no page links and keeps the existing
  “No document chunks match these filters.” message.
- Unparsed and downloaded-but-unparsed states do not perform a chunk count.
- No schema migration is required.

## Testing

### Repository and service tests

- `count_chunks` applies version, release, Sections, and Tables predicates in
  the SQL repository.
- `SpecDocService.count_chunks` delegates all arguments to the repository.

### Web route and template tests

- A first-page result renders the range, numeric pages, and applicable next
  and last links.
- A middle-page result renders a rolling window with ellipses and keeps the
  current page highlighted.
- A final-page result omits next/last links and renders the correct range.
- Filtered totals drive page counts and ranges instead of the source ledger's
  unfiltered `chunk_count`.
- First, previous, next, and last links preserve version, release, Sections,
  Tables, and limit query parameters.
- An out-of-range offset is clamped to the final page.
- A zero-match filtered result has no pagination controls.
- HTMX responses render the same paginator in the results fragment.

Focused tests should run before the full project gates:

```bash
python -m pytest tests/unit/test_spec_doc_service_reads.py tests/unit/test_web_routes.py tests/integration/test_spec_doc_repo.py
ruff check .
./scripts/test_sqlite.sh
```

## Files Expected To Change

- `src/doc3gpp/repository/protocols.py`
- `src/doc3gpp/storage/repositories/spec_doc_sql.py`
- `src/doc3gpp/services/spec_doc_service.py`
- `src/doc3gpp/web/routes/spec_docs.py`
- `src/doc3gpp/web/templates/partials/spec_doc_chunks.html`
- Relevant unit and integration tests for the repository, service, route, and
  template behavior.

# Spec Document Chunk Pagination Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the spec-document chunk list's simple previous/next footer with accurate filtered pagination that shows a range, first/last controls, and a rolling window of at most ten page numbers.

**Architecture:** Add a filtered `count_chunks` read to the existing repository Protocol, SQL repository, and `SpecDocService`. Keep the existing `limit + 1` chunk query for page data, but let the web route calculate the effective page, range, and page window from the filtered count. Keep all rendering in the existing `spec_doc_chunks.html` partial so full-page and HTMX responses share identical pagination markup.

**Tech Stack:** Python 3.10+, FastAPI, Jinja2, SQLAlchemy 2.0, SQLite, pytest, Ruff.

## Global Constraints

- Use the existing `SpecDocRepository` → `SpecDocService` → web route layering; the route must not access `SpecDocService._doc_repo`.
- Count with the same `version`, `release`, `sections`, and `tables` predicates used by `list_chunks`; filtered totals are authoritative over `SpecDocSource.chunk_count`.
- Preserve the existing `limit` default of `20`, maximum of `100`, offset syntax, chunk ordering, chunk anchors, and `limit + 1` probe query.
- Numeric pagination must contain at most ten page numbers and use the deterministic rolling-window rule from the approved design.
- Every pagination URL must preserve `version`, `release`, `sections`, `tables`, and `limit`.
- Out-of-range offsets must render the final valid page; zero matching chunks must render no pagination controls.
- Unparsed and downloaded-but-unparsed sources must not issue a chunk count query.
- Full-page and `HX-Request: true` responses must render the same chunk partial and paginator.
- Keep the CLI, MCP, database schema, and JSON contracts unchanged.
- Write the failing test first for each behavior change, run it to observe the failure, then implement the smallest passing change.
- Prefix shell commands with `rtk`; use `apply_patch` or Serena symbolic editing for source changes.
- Do not create a git commit unless the user explicitly requests one.

## File Map

- `src/doc3gpp/repository/protocols.py` — add the `SpecDocRepository.count_chunks` contract.
- `src/doc3gpp/storage/repositories/spec_doc_sql.py` — implement filtered `COUNT(*)` over `spec_doc_chunks`.
- `src/doc3gpp/services/spec_doc_service.py` — expose the count through a public service method.
- `src/doc3gpp/web/routes/spec_docs.py` — add the pure page-window helper and calculate effective pagination metadata in `spec_doc_show`.
- `src/doc3gpp/web/templates/partials/spec_doc_chunks.html` — render range text, numeric pages, ellipses, and boundary controls.
- `tests/unit/test_spec_doc_service_reads.py` — verify service count delegation.
- `tests/integration/test_spec_doc_repo.py` — verify filtered SQL counts against SQLite.
- `tests/unit/test_web_routes.py` — extend the fake service and cover page-window and route/template behavior.
- `docs/web-server.md` — document the filtered count and expanded chunk paginator.
- `AGENTS.md` — update the spec-document portal behavior summary if the current wording no longer describes the paginator accurately.

---

### Task 1: Add Filtered Chunk Count Contract

**Files:**
- Modify: `src/doc3gpp/repository/protocols.py:434-478`
- Modify: `src/doc3gpp/storage/repositories/spec_doc_sql.py:1-281`
- Modify: `src/doc3gpp/services/spec_doc_service.py:355-377`
- Test: `tests/unit/test_spec_doc_service_reads.py`
- Test: `tests/integration/test_spec_doc_repo.py`

**Interfaces:**
- Produces `SpecDocRepository.count_chunks(spec_id, *, version=None, release=None, sections=None, tables=None) -> int`.
- Produces `SpecDocService.count_chunks(spec_id, *, version, release=None, sections=None, tables=None) -> int`.
- Does not change the existing `list_chunks` arguments or return type.

- [ ] **Step 1: Add the failing service delegation test**

Append a unit test beside `test_list_chunks_forwards_version_filters_and_paging`:

```python
def test_count_chunks_forwards_version_filters() -> None:
    repo = MagicMock()
    repo.count_chunks.return_value = 37
    service = SpecDocService(spec_repo=MagicMock(), doc_repo=repo)

    assert service.count_chunks(
        "38.331",
        version="18.5.0",
        release="Rel-18",
        sections="%5.1%",
        tables="%UE%",
    ) == 37
    repo.count_chunks.assert_called_once_with(
        "38.331",
        version="18.5.0",
        release="Rel-18",
        sections="%5.1%",
        tables="%UE%",
    )
```

- [ ] **Step 2: Run the service test and verify it fails**

Run: `rtk pytest tests/unit/test_spec_doc_service_reads.py::test_count_chunks_forwards_version_filters -q`

Expected: FAIL because `SpecDocService.count_chunks` does not exist.

- [ ] **Step 3: Add the failing SQLite count test**

Extend `test_replace_and_list_chunks_round_trip_metadata` or add a neighboring integration test that writes rows with distinct versions, releases, section metadata, and table metadata, then asserts every predicate combination:

```python
def test_count_chunks_applies_all_filters(sqlite_env):
    create_schema("specdata")
    repo = SQLAlchemySpecDocRepository()
    repo.replace_chunks(
        "38.331", "19.0.0", release="Rel-19",
        drafts=[
            ChunkDraft(0, "a.docx", "5 Scope", "Table 1 Values", "a"),
            ChunkDraft(0, "a.docx", "6 Details", "Table 2 Timers", "b"),
        ],
    )
    repo.replace_chunks(
        "38.331", "18.5.0", release="Rel-18",
        drafts=[ChunkDraft(0, "b.docx", "5 Scope", "Table 1 Values", "c")],
    )

    assert repo.count_chunks("38.331") == 3
    assert repo.count_chunks("38.331", version="19.0.0") == 2
    assert repo.count_chunks("38.331", release="Rel-18") == 1
    assert repo.count_chunks("38.331", sections="%Details%") == 1
    assert repo.count_chunks("38.331", tables="%Timers%") == 1
    assert repo.count_chunks(
        "38.331", version="19.0.0", sections="%Scope%", tables="%Values%"
    ) == 1
```

- [ ] **Step 4: Run the repository test and verify it fails**

Run: `rtk pytest tests/integration/test_spec_doc_repo.py::test_count_chunks_applies_all_filters -q`

Expected: FAIL because the concrete repository has no `count_chunks` method.

- [ ] **Step 5: Add the Protocol method**

Add this method after `list_chunks` in `SpecDocRepository`:

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

- [ ] **Step 6: Implement the SQL count query**

Import `func` in `spec_doc_sql.py` and add `count_chunks` after `list_chunks`. Apply exactly the same predicates as `list_chunks`, including `apply_text_filter` for both metadata columns:

```python
def count_chunks(
    self,
    spec_id: str,
    *,
    version: str | None = None,
    release: str | None = None,
    sections: str | None = None,
    tables: str | None = None,
) -> int:
    with self._session_factory() as session:
        stmt = select(func.count()).select_from(SpecDocChunkORM).where(
            SpecDocChunkORM.spec_id == spec_id
        )
        if version is not None:
            stmt = stmt.where(SpecDocChunkORM.version == version)
        if release is not None:
            stmt = stmt.where(SpecDocChunkORM.release == release)
        stmt = apply_text_filter(stmt, SpecDocChunkORM.sections, sections)
        stmt = apply_text_filter(stmt, SpecDocChunkORM.tables, tables)
        return int(session.scalar(stmt) or 0)
```

- [ ] **Step 7: Add the service delegation method**

Add this method immediately before `list_chunks` in `SpecDocService`:

```python
def count_chunks(
    self,
    spec_id: str,
    *,
    version: str,
    release: str | None = None,
    sections: str | None = None,
    tables: str | None = None,
) -> int:
    return self._doc_repo.count_chunks(
        spec_id,
        version=version,
        release=release,
        sections=sections,
        tables=tables,
    )
```

- [ ] **Step 8: Run the focused service and repository tests**

Run: `rtk pytest tests/unit/test_spec_doc_service_reads.py tests/integration/test_spec_doc_repo.py -q`

Expected: PASS, including existing list-chunk and metadata round-trip coverage.

---

### Task 2: Add Deterministic Page-Window Calculation

**Files:**
- Modify: `src/doc3gpp/web/routes/spec_docs.py:67-174`
- Test: `tests/unit/test_web_routes.py`

**Interfaces:**
- Produces `_spec_doc_page_items(current_page: int, total_pages: int) -> list[int | None]`.
- `None` in the returned list represents an ellipsis; integers are one-based page numbers.
- Returns no page items for zero pages and never returns more than ten integers.

- [ ] **Step 1: Add failing pure-helper tests**

Import the private helper in `tests/unit/test_web_routes.py` and add exact boundary cases:

```python
from doc3gpp.web.routes.spec_docs import _spec_doc_page_items


@pytest.mark.parametrize(
    "current,total,expected",
    [
        (1, 0, []),
        (1, 1, [1]),
        (3, 8, list(range(1, 9))),
        (1, 20, list(range(1, 11)) + [None]),
        (6, 20, list(range(1, 11)) + [None]),
        (7, 20, [None, *range(3, 13), None]),
        (16, 20, [None, *range(11, 21)]),
        (20, 20, [None, *range(11, 21)]),
    ],
)
def test_spec_doc_page_items_uses_rolling_ten_page_window(
    current: int, total: int, expected: list[int | None]
) -> None:
    assert _spec_doc_page_items(current, total) == expected
```

- [ ] **Step 2: Run the helper tests and verify they fail**

Run: `rtk pytest tests/unit/test_web_routes.py -k spec_doc_page_items -q`

Expected: FAIL because the helper is not defined.

- [ ] **Step 3: Implement the page-window helper**

Add the helper after the pagination constants in `spec_docs.py`:

```python
def _spec_doc_page_items(current_page: int, total_pages: int) -> list[int | None]:
    if total_pages <= 0:
        return []
    if total_pages <= 10:
        return list(range(1, total_pages + 1))

    if current_page <= 6:
        first_page = 1
    elif current_page >= total_pages - 4:
        first_page = total_pages - 9
    else:
        first_page = current_page - 4

    last_page = first_page + 9
    items: list[int | None] = []
    if first_page > 1:
        items.append(None)
    items.extend(range(first_page, last_page + 1))
    if last_page < total_pages:
        items.append(None)
    return items
```

- [ ] **Step 4: Run the helper tests**

Run: `rtk pytest tests/unit/test_web_routes.py -k spec_doc_page_items -q`

Expected: PASS for zero, short, first-boundary, middle, and final-boundary windows.

---

### Task 3: Wire Count, Clamping, and Page Metadata Into the Route

**Files:**
- Modify: `src/doc3gpp/web/routes/spec_docs.py:80-174`
- Modify: `tests/unit/test_web_routes.py:2698-2899`

**Interfaces:**
- `FakeSpecDocService` gains `count_chunks(...) -> int` and records count calls.
- `spec_doc_show` passes `total_chunks`, `current_page`, `total_pages`, `page_items`, `first_offset`, `previous_offset`, `next_offset`, `last_offset`, and the effective `offset` to the template.
- Existing `display_chunks`, source, TOC, filter strings, and parse form context remain available.

- [ ] **Step 1: Extend the web fake before changing the route**

Add an optional `total_chunks` constructor argument and this method to `FakeSpecDocService`:

```python
def __init__(self, source=None, toc=None, chunks=None, total_chunks=None) -> None:
    self.source = source
    self.toc = toc
    self.chunks = list(chunks or [])
    self.total_chunks = len(self.chunks) if total_chunks is None else total_chunks
    self.calls = []

def count_chunks(
    self,
    spec_id: str,
    *,
    version: str,
    release: str | None = None,
    sections: str | None = None,
    tables: str | None = None,
) -> int:
    self.calls.append(("count", spec_id, version, release, sections, tables))
    return self.total_chunks
```

Keep the existing fake `list_chunks` slice behavior. This lets tests model a filtered count independently from the fixture list while still verifying the route passes the active predicates to both methods.

- [ ] **Step 2: Add failing route tests for first, middle, final, and filtered pages**

Add a test with 25 fake chunks, `limit=2`, `offset=12`, and `total_chunks=25`. Assert that the response contains the `Showing 13–14` range, the current page marker for page 7, ellipsis markup, `first`, `prev`, `next`, and `last`, and preserved encoded filters. Assert the count and list calls received the same parsed filters.

Add a final-page test with `total_chunks=25`, `limit=10`, and `offset=20`; assert `Showing 21–25`, no `next` or `last` link, and a `prev` link.

Add a filtered-total test with `source.chunk_count=200` but `total_chunks=3`; request `limit=2` and assert only two pages and `Showing 1–2`, proving the source ledger count is not used for pagination.

Use assertions shaped like:

```python
assert "Showing 13–14" in response.text
assert "first" in response.text
assert "prev" in response.text
assert "next" in response.text
assert "last" in response.text
assert "offset=0" in response.text
assert "offset=10" in response.text
assert "version=18.0.0" in response.text
assert "sections=%255.1%25" in response.text
assert service.calls[2] == (
    "count", "36.579-5", "18.0.0", None, "%5.1%", "%UE%"
)
```

- [ ] **Step 3: Add the failing out-of-range and zero-result tests**

For `total_chunks=25`, request `limit=10&offset=999` and assert the response renders `Showing 21–25` and the fake receives `offset=20` for the chunk query.

For `total_chunks=0`, use a parsed source and assert `No document chunks match these filters.` appears without any `pagination` navigation. Keep the existing separate zero-source test asserting `Parsed source contains zero chunks`.

- [ ] **Step 4: Add the failing HTMX parity assertion**

Request the same middle-page URL with `headers={"HX-Request": "true"}`. Assert the response is a fragment, contains `id="results"`, contains the same `Showing` range and page controls, and does not contain `<!DOCTYPE` or `<html`.

- [ ] **Step 5: Run the new route tests and verify they fail**

Run: `rtk pytest tests/unit/test_web_routes.py -k 'spec_doc_show and (pagination or page_items or zero_chunk)' -q`

Expected: FAIL because the route neither calls `count_chunks` nor supplies page metadata to the template.

- [ ] **Step 6: Implement route-side count and clamping**

Inside the existing parsed-source branch, count before querying chunks. Compute the effective offset from the filtered total:

```python
total_chunks = 0
total_pages = 0
effective_offset = 0
chunks: list[Any] = []

if source is not None and source.parsed_at is not None:
    total_chunks = doc_service.count_chunks(
        spec_id,
        version=version,
        release=parsed_release,
        sections=parsed_sections,
        tables=parsed_tables,
    )
    total_pages = (total_chunks + parsed_limit - 1) // parsed_limit
    if total_pages:
        last_offset = (total_pages - 1) * parsed_limit
        effective_offset = min(parsed_offset, last_offset)
        chunks = doc_service.list_chunks(
            spec_id,
            version=version,
            release=parsed_release,
            sections=parsed_sections,
            tables=parsed_tables,
            limit=parsed_limit + 1,
            offset=effective_offset,
        )
```

Retain the existing TOC lookup and its `SpecDocUnknownVersionError` fallback. Do not count or query chunks for unparsed sources.

- [ ] **Step 7: Build the route pagination context**

After `display_chunks = chunks[:parsed_limit]`, derive the one-based page and navigation offsets from `effective_offset` and `total_pages`:

```python
current_page = effective_offset // parsed_limit + 1 if total_pages else 0
page_items = _spec_doc_page_items(current_page, total_pages)
first_offset = 0 if current_page > 1 else None
previous_offset = effective_offset - parsed_limit if current_page > 1 else None
next_offset = effective_offset + parsed_limit if current_page < total_pages else None
last_offset = (total_pages - 1) * parsed_limit if current_page < total_pages else None
```

Add `total_chunks`, `current_page`, `total_pages`, `page_items`, `first_offset`, `previous_offset`, `next_offset`, and `last_offset` to the existing context. Set `offset` to `effective_offset`, not the stale requested offset, so every rendered link and range points at the clamped page.

Use the filtered total to determine `next_offset`; retain the extra-row probe as a defensive check by requiring both `len(chunks) > parsed_limit` and `current_page < total_pages` before exposing a next page.

- [ ] **Step 8: Run all spec-document web route tests**

Run: `rtk pytest tests/unit/test_web_routes.py -k spec_doc -q`

Expected: PASS, including the existing unparsed, parsed, pagination, zero-chunk, HTMX, search, and parse-form tests.

---

### Task 4: Render the Expanded Paginator

**Files:**
- Modify: `src/doc3gpp/web/templates/partials/spec_doc_chunks.html:29-41`
- Test: `tests/unit/test_web_routes.py`

**Interfaces:**
- Consumes the route context keys `page_items`, `current_page`, `total_pages`, `first_offset`, `previous_offset`, `next_offset`, `last_offset`, `total_chunks`, `offset`, `limit`, `display_chunks`, `version_row`, `version`, `release`, `sections`, and `tables`.
- Produces the visible shape `Showing start–end | ‹‹ first ‹ prev ... next › last ››` with applicable controls omitted.

- [ ] **Step 1: Replace the simple footer with the page-window markup**

Keep the chunk cards and empty-state branches unchanged. Replace only the existing pagination block with a block equivalent to:

```jinja2
  {% if total_chunks and display_chunks %}
    <nav class="pagination" aria-label="Spec document chunks">
      <span>Showing {{ offset + 1 }}–{{ offset + display_chunks|length }}</span>
      <span aria-hidden="true">|</span>
      {% if first_offset is not none %}
        <a href="{{ request.url.include_query_params(version=version_row.version, release=release, sections=sections, tables=tables, limit=limit, offset=first_offset) }}" title="First page">‹‹ first</a>
      {% endif %}
      {% if previous_offset is not none %}
        <a href="{{ request.url.include_query_params(version=version_row.version, release=release, sections=sections, tables=tables, limit=limit, offset=previous_offset) }}" title="Previous page">‹ prev</a>
      {% endif %}
      {% for page in page_items %}
        {% if page is none %}
          <span class="pagination-ellipsis" aria-hidden="true">...</span>
        {% elif page == current_page %}
          <span class="current" aria-current="page">[{{ page }}]</span>
        {% else %}
          <a href="{{ request.url.include_query_params(version=version_row.version, release=release, sections=sections, tables=tables, limit=limit, offset=((page - 1) * limit)) }}">{{ page }}</a>
        {% endif %}
      {% endfor %}
      {% if next_offset is not none %}
        <a href="{{ request.url.include_query_params(version=version_row.version, release=release, sections=sections, tables=tables, limit=limit, offset=next_offset) }}" title="Next page">next ›</a>
      {% endif %}
      {% if last_offset is not none %}
        <a href="{{ request.url.include_query_params(version=version_row.version, release=release, sections=sections, tables=tables, limit=limit, offset=last_offset) }}" title="Last page">last ››</a>
      {% endif %}
    </nav>
  {% endif %}
```

Retain the existing `pagination` class and add the `current` class to the
non-link current-page marker. Do not add CSS or alter unrelated styling; the
current page is deliberately non-clickable.

- [ ] **Step 2: Run the focused paginator tests**

Run: `rtk pytest tests/unit/test_web_routes.py -k 'spec_doc_show or spec_doc_page_items' -q`

Expected: PASS with exact range text, current-page marker, ellipses, applicable boundary links, preserved filters, no controls for zero matches, and HTMX fragment parity.

---

### Task 5: Update Web Documentation and Run Project Gates

**Files:**
- Modify: `docs/web-server.md` at the spec-document portal section.
- Modify: `AGENTS.md` at the spec-document web-surface summary.

**Interfaces:**
- Documentation describes the current portal behavior without changing CLI, MCP, or API payload contracts.

- [ ] **Step 1: Update the web-server guide**

In the spec-document portal section, state that chunk pagination uses the filtered matching-chunk count and preserves `version`, `release`, `sections`, `tables`, and `limit`. State that the footer shows the range, first/previous/next/last controls, and up to ten rolling numeric page links with ellipses. State that out-of-range offsets clamp to the last page and zero filtered matches omit pagination controls.

- [ ] **Step 2: Update the architecture/agent pointer**

Update the existing spec-document web-surface sentence in `AGENTS.md` to say
that the version page renders filtered, paginated chunks with a range, up to
ten rolling page numbers, ellipses, and first/previous/next/last controls.
Keep the route list and all unrelated workflow descriptions unchanged.

- [ ] **Step 3: Run focused tests and lint**

Run:

```bash
rtk pytest tests/unit/test_spec_doc_service_reads.py tests/unit/test_web_routes.py tests/integration/test_spec_doc_repo.py -q
rtk ruff check src/doc3gpp/repository/protocols.py src/doc3gpp/storage/repositories/spec_doc_sql.py src/doc3gpp/services/spec_doc_service.py src/doc3gpp/web/routes/spec_docs.py tests/unit/test_spec_doc_service_reads.py tests/unit/test_web_routes.py tests/integration/test_spec_doc_repo.py
```

Expected: all focused tests pass and Ruff reports no errors.

- [ ] **Step 4: Run the complete offline SQLite suite**

Run: `rtk ./scripts/test_sqlite.sh`

Expected: the full offline unit and integration suite passes.

- [ ] **Step 5: Inspect the final diff**

Run: `rtk git status --short` and `rtk git diff --check`.

Confirm only the planned source, test, and documentation files changed; do not commit unless the user requests it.

## Verification Checklist

- `count_chunks` and `list_chunks` apply identical filters.
- Unparsed pages do not call `count_chunks`.
- The source ledger’s unfiltered `chunk_count` does not control page totals.
- The numeric window never contains more than ten page numbers.
- Page windows match the approved rules at first, middle, and final pages.
- First/previous/next/last links have correct offsets and preserve all filters.
- Stale offsets render the final page.
- Zero filtered matches have no paginator.
- Full-page and HTMX responses share the same paginator.
- Existing spec-document search, parse form, TOC, and chunk rendering tests remain green.
- Focused tests, Ruff, and `./scripts/test_sqlite.sh` pass before completion.

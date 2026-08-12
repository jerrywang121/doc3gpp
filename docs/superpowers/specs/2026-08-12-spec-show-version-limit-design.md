# Design: `spec show` version limiting

**Date:** 2026-08-12

## Problem

`doc3gpp spec show <id>` renders every stored version row for a spec. Specs
can accumulate dozens of versions, and the web detail page lists them all in
one table. There is no way to limit which versions are shown, filter by
release line (e.g. only `19.x.x`), or paginate.

## Goal

Add `limit`, `offset`, and `version` controls to the `spec show` surface so
callers can page through and filter the version list. The same controls must
be exposed consistently across the CLI, web, and MCP surfaces. The web spec
detail page paginates the version table.

## Non-goals

- No change to `spec list` (already has `limit`/`offset`).
- No change to the sync/backfill path — it must keep seeing all versions.
- No new DB schema.

## Design

### 1. Repository layer

`SpecRepository.list_versions` (protocol) and
`SQLAlchemySpecRepository.list_versions` (impl) gain a `version` filter
parameter:

```python
def list_versions(
    self,
    spec_id: str,
    limit: int = 200,
    offset: int = 0,
    version: str | None = None,
) -> list[SpecVersion]:
```

The impl applies the filter via the existing rich-grammar helper
`apply_text_filter(stmt, SpecVersionORM.version, version)` before the
numeric-DESC sort and the `offset/limit` slice. This means `19.%`,
`!19.%`, `null`, and `not-null` all work, consistent with every other
text filter in the codebase. Filtering happens before ordering and
pagination, so the version-DESC ordering and `limit`/`offset` still apply
to the filtered set.

Default `limit` stays `200` so the sync backfill
(`SpecService._backfill_pdf_urls`) continues to receive all versions.

### 2. Service layer

`SpecService.list_versions` gains the same `version` parameter and passes
it through to the repository. Default `limit` stays `200`.

### 3. CLI (`spec show`)

Add three options to `spec_show`:

- `--limit` — default `10`, `min=1, max=500`.
- `--offset` — default `0`, `min=0`.
- `--version` — rich filter pattern on the version string (e.g. `19.%`).

All three are passed to `service.list_versions(spec_id, limit=limit,
offset=offset, version=version)`. The header row is unaffected; only the
version rows are limited/filtered.

### 4. Web route + template

`show_spec` gains `limit` (default `10`), `offset` (default `0`), and
`version` query params. It computes `next_offset` following the meetings
route pattern:

```python
next_offset = parsed_offset + len(versions) if len(versions) == parsed_limit else None
```

The template context gains `limit`, `offset`, `next_offset`, and `version`.
`spec_show.html` renders the existing `partials/pagination.html` below the
versions table. The pagination partial uses
`request.url.include_query_params(...)`, so it preserves the `version`
filter (and any other query params) in the prev/next links automatically.

JSON output (`?format=json`) honors the same `limit`/`offset`/`version`
params.

### 5. MCP (`get_spec` tool)

Add `limit` (default `10`), `offset` (default `0`), and `version` params
to the `get_spec` tool, passed to `services.spec.list_versions`. The tool
result shape is unchanged — only the number of version rows returned is
controlled.

### 6. Tests

- `tests/integration/test_spec_sql.py`: `version` filter, and
  `limit`/`offset` applied to the filtered, DESC-ordered set.
- `tests/integration/test_spec_cli.py`: `--limit`/`--offset`/`--version`
  are forwarded to the service.
- `tests/unit/test_web_routes.py`: web pagination (`next_offset`) and
  `version` filter forwarding.
- `tests/integration/test_mcp_end_to_end.py`: MCP `get_spec` accepts the
  new params.

### 7. Docs

Update `docs/cli.md` (spec show flags), `AGENTS.md` (spec-show workflow
line), and `docs/code-map.md` if the symbol table changes.

## Open questions

None.

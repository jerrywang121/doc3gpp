# Spec list: rich-text `rapporteurs` filter + display consistency

**Status:** Approved
**Date:** 2026-08-11
**Branch:** feat/spec-sync-list-show

## Problem

The `specs.rapporteurs` column (comma-joined company names, added in the
previous spec-rapporteurs plan) is currently surfaced as a *display* field on
the CLI `spec list` / `spec show`, the web `spec show` detail page, the web +
MCP JSON payloads, and the `_SPEC_FIELDS` / `_SPEC_DEFAULT_FIELDS` column
sets. However:

- **No filter exists for it** — `spec list` cannot restrict by rapporteurs on
  any surface (repo / service / CLI / web route / MCP tool).
- **The web HTML results table** (`partials/spec_results.html`) does not render
  a Rapporteurs column, so the list page is display-inconsistent with the
  detail page and with the JSON/CLI surfaces that already include it.

## Goal

Add a rich-text `rapporteurs` filter to `spec list` end-to-end and mirror it
across the CLI, web, and MCP surfaces; also add a visible Rapporteurs column
to the web results table so all surfaces are consistent.

## Non-goals

- No schema change (column already exists).
- No change to the `rapporteurs` extraction or normalization logic.
- No change to `spec show` (already surfaces rapporteurs in its header).
- No change to the filter grammar itself — reuse the existing shared rich
  grammar (`null` / `not-null` / `!pattern` / plain LIKE with `%`/`_`).

## Design

The work is a vertical slice following the existing `wis` filter precedent.
Each layer adds a `rapporteurs: str | None = None` filter parameter and
threads it through to the layer below.

### 1. Repository

`src/doc3gpp/repository/protocols.py` — `SpecRepository.list` gains
`rapporteurs: str | None = None`. Its docstring already states text columns
use the rich-filter grammar; no prose change required beyond the signature.

`src/doc3gpp/storage/repositories/spec_sql.py` — `SQLAlchemySpecRepository.list`
gains the param and applies it exactly like the existing text filters:

```python
if rapporteurs:
    stmt = apply_text_filter(stmt, SpecORM.rapporteurs, rapporteurs)
```

### 2. Service

`src/doc3gpp/services/spec_service.py` — `SpecService.list_recent` gains the
`rapporteurs` passthrough param, forwarded to `_repository.list(...)`.

### 3. CLI

`src/doc3gpp/cli.py` — `spec_list` gains a `--rapporteurs` option:

```python
rapporteurs: str | None = typer.Option(
    None, "--rapporteurs", help="Rich filter on rapporteurs (comma-joined company names)."
)
```

passed through to `service.list_recent`. The `spec_list` docstring is corrected
to include `rapporteurs` in the listed output columns (currently stale).

### 4. Web route

`src/doc3gpp/web/routes/specs.py` — `list_specs` gains
`rapporteurs: str | None = Query(default=None)`, is passed through as
`rapporteurs=parse_text_query(rapporteurs)`, and is added to the `filters`
context dict as `"rapporteurs": rapporteurs or ""`.

### 5. Web templates

- `src/doc3gpp/web/templates/partials/spec_filters.html` — add a
  `Rapporteurs` text input (mirroring the `WIs` input).
- `src/doc3gpp/web/templates/partials/spec_results.html` — add a `Rapporteurs`
  `<th>` header and `<td>{{ spec.rapporteurs or '-' }}</td>` cell (matching the
  other columns).

### 6. MCP

`src/doc3gpp/web/mcp_server.py` — `list_specs` gains
`rapporteurs: Annotated[str | None, Field(description="Rich filter pattern on rapporteurs.")] = None`,
is forwarded to `list_recent`, and the tool `description` is updated to list
`rapporteurs` among the rich-filter fields.

## Testing

- **Repo (integration, sqlite):** `tests/integration/test_spec_sql.py` — filter
  by rapporteurs via LIKE, negated `!`, `not-null`, and `null`.
- **Web (unit):** `tests/unit/test_web_routes.py` — extend `FakeSpecService` to
  record the `rapporteurs` filter; assert the route forwards it and that the
  rendered HTML contains a `Rapporteurs` column + cell.
- **CLI (integration):** `tests/integration/test_spec_cli.py` — assert
  `spec list --rapporteurs ...` forwards the value to the service.
- **MCP (integration):** `tests/integration/test_mcp_end_to_end.py` — assert
  the `list_specs` tool accepts and applies the `rapporteurs` filter.

## Documentation

Update `docs/cli.md` (spec list flag) and `docs/code-map.md` (symbol surface)
in the same change set, per the repo's documentation-sync convention.

# Spec Parsed Output Design

## Goal

Expose derived spec-document parse status on every spec list/show surface:

- `spec list` includes `parsed`, containing the comma-separated versions whose
  spec documents have been parsed.
- `spec show` includes `parsed` on every listed version as a boolean.
- `spec list --parsed true|false` filters specs by whether any version has
  been parsed.
- REST, web HTML, MCP, and CLI expose the same status semantics.

The status is output-only. No `parsed` column is added to the main `specs` or
`spec_versions` tables, and no schema migration is required.

## Source Of Truth

Parsed status comes from the separate specdata database's
`spec_doc_sources.parsed_at` column:

- A version is parsed when its `(spec_id, version)` source row has a non-null
  `parsed_at`.
- A spec is parsed when at least one of its stored versions is parsed.
- A missing source row or a null `parsed_at` means the version is not parsed.
- Parsed version strings are sorted newest-first using the existing numeric
  version ordering (`18.10.1` is newer than `18.2.1`).

The main specification repository remains responsible for spec and version
rows. A small parsed-status repository contract is added to the service layer
so the two database boundaries remain explicit.

## Output Contract

### Spec List

The default `spec` output field list gains `parsed` after `rapporteurs`.
Custom configured output fields continue to control whether the field is
shown.

The value is:

- A comma-separated, numeric-newest-first version string, for example
  `19.2.0,19.1.0`.
- `null` in structured JSON output when no version is parsed.
- The existing display placeholder (`-`) in tabular and Markdown output when
  the value is null.

The same value is returned by REST JSON and MCP list output. Web HTML displays
the list or `-` in its parsed column.

### Spec Show

Every returned version row gains `parsed`:

- JSON, REST JSON, and MCP emit a native boolean `true` or `false`.
- CLI table/Markdown output renders the boolean using the existing text
  formatting rules.
- Web HTML displays the corresponding parsed/not-parsed state in the version
  table.

The spec header in `spec show` does not gain a second aggregate `parsed`
field; the per-version values are the authoritative detail output.

Existing `--no-wis-crs` behavior remains unchanged.

## Filtering

`spec list --parsed` accepts exactly `true` or `false`, case-insensitively;
other values produce the normal CLI parameter error.

The REST/web list route accepts `?parsed=true` or `?parsed=false` and applies
the same strict validation. The web filter form exposes an Any/true/false
control and preserves the selection through pagination and HTMX refreshes.

The MCP `list_specs` tool accepts an optional boolean `parsed` argument.

Filtering occurs before main-database pagination:

- `true` restricts the main spec query to spec IDs present in the parsed
  status map.
- `false` excludes those spec IDs.
- omitted leaves the existing spec filters and pagination unchanged.

## Architecture

`Spec` and `SpecVersion` receive transient, non-persisted output attributes:

- `Spec.parsed: str | None`, defaulting to `None`.
- `SpecVersion.parsed: bool`, defaulting to `False`.

The SQL repositories ignore these attributes when writing rows. `SpecService`
populates them after reading main-database rows, using an injected
specdata parsed-status repository. The service also performs the parsed
filter before applying `limit`/`offset`, ensuring all adapters share one
implementation.

The parsed-status repository exposes a batch lookup returning
`dict[str, list[str]]` keyed by spec ID. It reads only rows with non-null
`parsed_at`, optionally restricted to a set of spec IDs. The SQL implementation
uses the existing specdata session factory and does not alter schema creation.

CLI, REST/web, and MCP call the enriched `SpecService` methods. Shared render
helpers preserve native `None` for the spec-list `parsed` field and native
`bool` for the spec-show version `parsed` field while retaining existing string
coercion for legacy list cells.

## Tests

Add coverage for:

1. Parsed-status repository lookup, including parsed and unparsed versions,
   numeric version ordering, and missing rows.
2. `SpecService.list_recent` enrichment and true/false filtering before
   pagination.
3. `SpecService.list_versions` boolean enrichment.
4. CLI list default JSON/table behavior, `--parsed true`, `--parsed false`,
   and invalid values.
5. CLI show JSON version booleans and no-WI/CR compatibility.
6. REST JSON list/show output and strict `parsed` query filtering.
7. Web parsed filter controls and parsed columns in list/show HTML.
8. MCP list filtering and native parsed output values.

Run focused spec/service/CLI/web/MCP tests, then the full SQLite suite, Ruff,
and `git diff --check`.

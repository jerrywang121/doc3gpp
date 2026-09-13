# Resource `schema` surfaces — Design

**Date:** 2026-09-13
**Status:** proposed, awaiting user review
**Scope:** `tsg`, `meeting`, `tdoc`, `wi`, `spec`, `testcase` × CLI / REST+HTML / MCP

## Goal

Add a `schema` subcommand to each of the six resource groups
(`tsg`, `meeting`, `tdoc`, `wi`, `spec`, `testcase`) that returns the
list of fields of the corresponding database table(s) plus a
description per field covering meaning, format, and possible values
for categorical dimensions (e.g. TSG short names, testcase groups).
Cover all four surfaces: CLI, REST (`?format=json`), web HTML, MCP.

## Non-goals

- No live DB introspection (`PRAGMA`, `SELECT DISTINCT`) — output is
  deterministic on an empty DB.
- No filters, pagination, or `--fields` selection on `schema` — the
  whole descriptor fits in one response.
- No DB reads/writes on the `schema` path (no `create_schema`, no
  auto-sync, no service/repo calls).
- No change to `sync` / `list` / `show` semantics.

## Context

- Each resource already has `list`/`show` on CLI (`src/doc3gpp/cli.py`),
  a REST router (`src/doc3gpp/web/routes/`), HTML templates, and MCP
  tools (`src/doc3gpp/web/mcp_server.py`) with byte-parity between
  `list --format json` and `?format=json`.
- Domain dataclasses (`src/doc3gpp/models/`) carry the field names;
  ORM tables (`src/doc3gpp/storage/db/models.py`, testcase tables on
  `TestCaseBase`) carry the column types. Descriptions today live only
  in docstrings / `docs/3gpp-knowledge.md`.
- Canonical categorical sets already exist in code: 19 seeded TSG
  short names (`services/tsg_service.py::_DEFAULT_TSGS`), testcase
  groups `5G/LTE/IMS/UTRA/POS/MCX` (`cli.py::VALID_TESTCASE_GROUPS`,
  `routes/testcases.py::_VALID_GROUPS`, `mcp_server.py::_TESTCASE_GROUPS`),
  testcase paths (`repositories/testcase_sql.py::_PATH_RANK`), spec
  type `TS/TR`, CR category `F/B/A/C/D` (`models/tdoc_cr.py`),
  TDoc file types `revision/review/support` (`models/tdoc_file.py`).

## Design

### 1. Static curated registry (single source of truth)

New pure-data module `src/doc3gpp/models/schema_info.py`:

- `FieldInfo(name, type, nullable, description, values)` —
  `type` uses the fixed vocabulary `str | int | date | datetime`
  (plus `gzip-json` for the two compressed blobs and `text` for
  long-form text); `nullable` is a bool; `values: tuple[str, ...] |
  None`, `None` = free-form.
- `TableSchema(table, fields)` + `RESOURCE_SCHEMAS: dict[str, list[TableSchema]]`
  keyed `tsg | meeting | tdoc | wi | spec | testcase`.
- `description` is one human sentence covering meaning + format
  (e.g. "ISO-8601 date", "FK into `tsgs.short_name`", "path relative
  to `https://www.3gpp.org/ftp/`"); machine-readable enum members
  live in `values`, rendered comma-joined in text formats.
- `models/` cannot import from `services/`/`storage/` (layering), so
  the registry duplicates the categorical literals with a comment
  pointing at each canonical constant, plus a unit test asserting
  equality (TSG 19, groups 6, paths 10, `TS/TR`, `F/B/A/C/D`,
  `revision/review/support`).

### 2. Registry content (main + related tables)

- `tsg` → `tsgs`: `tsg_name` (str, e.g. `RAN WG5`), `short_name`
  (str PK, values = all 19: `RP,R1,R2,R3,R4,R5,RT,SP,S1,S2,S3,S4,S5,S6,CP,C1,C3,C4,C6`),
  `description` (str), `url` (str|null), `meeting_last_sync`
  (datetime|null, ISO-8601 UTC).
- `meeting` → `meetings`: `meeting_id` int PK, `name`/`title`/
  `location` str, `start_date`/`end_date` date|null, `ftp_url`
  str|null (relative path), `start_doc`/`end_doc` str|null
  (e.g. `R5-200001`), `tsg` str|null FK with TSG values,
  `tdoc_list_last_sync` datetime|null.
- `tdoc` → `tdocs` (23 cols: `tdoc_id` PK with shapes
  `R5-260013`/`R5s260009`/`R5w260013`/`R4-2607922`; `title`,
  `meeting_id` FK, `ftp_url`, `source`, `type` (free-form,
  e.g. `CR`, `Discussion Paper`), `status` (free-form,
  e.g. `Agreed`, `Noted`, `Approved`), `reservation_date`/
  `uploaded_date` date, `cr_cat`, `is_revision_of`, `revised_to`,
  `release` e.g. `Rel-17`, `spec` e.g. `38.523-1`, `version`
  e.g. `18.4.0`, `related_wis`, `cr_num`, `cr_pack`, `tdoc_for`,
  `abstract`, `secretary_remarks`, `ls_to`, `ls_cc`,
  `original_ls`) + `tdoc_cr_cover_page` (19 cols incl. `rev`
  digit-string, `date` date, `cr_cat` values `F/B/A/C/D`,
  long-text reason/consequences/summary/clauses/comments/history,
  `extracted_tdoc_id`) + `tdoc_cr_ttcn_details` (`testcase`, `ue`,
  `ss`, `ats_version`, `ttcn_release`, `test_suite`,
  `required_changes` gzip-JSON, `changed_functions`
  newline-delimited `<module>.<function>` with trailing/leading-dot
  sentinels) + `tdoc_cr_change_details` (`clauses`, `changes`
  gzip-JSON `{clauses,text}` blocks) + `tdoc_files` (`id`,
  `tdoc_id` FK, `type` values `revision/review/support`, `file`,
  `ftp_url` unique, `uploaded_date`) + `tdoc_extracts`
  (`ftp_url` PK, `tdoc_id` FK, `cache_file`, `doc_filename`,
  `extracted_at`, `parser_version` e.g. `1.0.0`).
- `wi` → `wis`: composite PK (`wi_id` int, `tsg_short` FK with TSG
  values), `acronym`, `release` e.g. `Rel-19`, `name` (title).
- `spec` → `specs` (`spec_id` PK dotted e.g. `36.579-5`, `type`
  values `TS/TR`, `title`, `status` e.g. `Under change control`,
  `radio_tech` comma-joined e.g. `LTE,NR`, `initial_release`
  e.g. `Rel-20`/`R99`, `tsg` FK, `wis` snapshot, `rapporteurs`,
  `last_synced_at`) + `spec_versions` (composite PK
  (`spec_id` FK, `version` e.g. `18.3.0`), `ftp_url` absolute URL,
  `release` (`draft`/`pre-release`/`Rel-N`), `meeting_id`,
  `meeting_name` e.g. `RAN#108`, `upload_date`, `version_id`,
  `pdf_url`, `crs`; transient `wki_id` explicitly excluded).
- `testcase` → `testcases` (composite PK (`testcase_id`, `group`
  values `5G/LTE/IMS/UTRA/POS/MCX`), `title`, `ats`, `feature`,
  `release`, `wis`, `spec` — `5G` fixed `38.523-1`, `LTE` fixed
  `36.523-1`, else row `part of` verbatim) +
  `testcase_status` (composite PK (`testcase_id`, `group`, `path`;
  `path` values `FR1/FR2/FR1+FR2/FDD/TDD/IPCAN-4G/EUTRA/IPCAN-5G/NR5GC/default`,
  single-path groups store literal `'default'`), `gcf_ptcrb`/
  `ttcn_status` e.g. `Approved`) + `testcase_sources` ledger
  (`filename` PK e.g. `TTCN CR Agreement Status 2024-wk32.zip`,
  `year`, `week`, `revision`, `downloaded_at`, `parsed_at`,
  `testcase_count`, `status_count`). Note the separate sqlite file
  (`<main-stem>_testcase.db`).
- Free-form vs categorical rule: `values` is set only where code
  fixes the set (above); everything scraped verbatim from XLSX/HTML
  (`status`, `type`, WI `release`, spec `status`, TTCN overview
  strings) is free-form with representative examples in
  `description`, never presented as exhaustive.

### 3. CLI — six `schema` commands

`doc3gpp <resource> schema` on all six Typer apps, mirroring `list`
flags exactly: `--format table|json|markdown` (default table),
`--output/-o`, `--compact`. No filter flags. Pure registry read;
renders via existing `_emit_records` / `_resolve_format` /
`_resolve_compact`. Flat row shape shared by all formats:
`{table, field, type, nullable, description, values}` (`type` from
the fixed vocabulary above; `nullable` renders `yes`/`no` in
table/markdown and a JSON bool; `values`
comma-joined string, `"-"` when none — same null-coercion as `list`
JSON). Table/markdown show the same six columns.

### 4. REST + HTML

One route per router: `GET /tsgs/schema`, `/meetings/schema`,
`/tdocs/schema`, `/wis/schema`, `/specs/schema`,
`/testcases/schema`. `?format=json` returns the CLI JSON array
verbatim (same field order, same coercion). Default renders HTML via
one shared template (e.g. `schema.html` + `partials/schema_results.html`,
following the list/detail HTMX pattern) grouped by DB table. No
filters, no pagination.

### 5. MCP — six tools

`get_tsg_schema`, `get_meeting_schema`, `get_tdoc_schema`,
`get_wi_schema`, `get_spec_schema`, `get_testcase_schema`: no params,
thin wrappers returning `_to_json(registry payload)` under the
existing error guard — byte-identical to REST `?format=json`.

## Alternatives considered

- **Runtime dataclass/ORM introspection:** auto-syncs names but
  descriptions/values still need curation; drift detection becomes
  implicit. Rejected — explicit registry + equality test is clearer.
- **Live `PRAGMA` + `DISTINCT` scans:** always matches the DB file,
  but empty-DB returns nothing and output is non-deterministic;
  breaks the parity contract. Rejected.
- **Nested JSON (`{table, fields:[...]}`) vs flat rows:** nested is
  richer; flat reuses `_emit_records` unchanged and keeps all four
  surfaces trivially identical. Chosen flat for v1.

## Testing

- Unit: registry completeness (every dataclass/ORM field covered,
  no extras), categorical equality vs canonical constants,
  CLI `schema` table/json for all six resources.
- Parity: REST `?format=json` ≡ CLI json ≡ MCP payload (extend the
  existing `-k mcp_end_to_end` style check).
- Offline only; no network.

## Docs

Same change set updates `docs/cli.md`, `docs/web-server.md`,
`docs/3gpp-knowledge.md` (if any value set is refined),
`docs/code-map.md`, `README.md`/`AGENTS.md` inventory lines per
`docs/conventions.md` §"Documentation sync".

## Open decisions for the plan

Shared-vs-per-resource HTML templates, and the exact six CLI column
widths (table truncation rules).

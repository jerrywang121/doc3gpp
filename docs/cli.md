# CLI Reference

This document describes the currently implemented command surface in src/doc3gpp/cli.py.

## Installation

The CLI requires the `[cli]` extra on top of the core SDK:

```bash
# via pip
pip install "doc3gpp[cli]"

# via pipx (isolated environment)
pipx install "doc3gpp[cli]"

# development (editable install)
pip install -e ".[dev]"
```

> **Note:** `pip install doc3gpp` (without `[cli]`) installs the SDK only —
> the `doc3gpp` CLI command will not be available.

## Command Entry

- Command: doc3gpp
- Python entrypoint: doc3gpp.cli:main

## db Commands

### doc3gpp db check

Purpose:

- Validate connectivity to the configured database backend.

Behavior:

- Creates an engine from DOC3GPP_DATABASE_URL.
- Executes SELECT 1.
- Prints the active database URL when successful.

### doc3gpp db init

Purpose:

- Initialize schema for current backend and seed the TSG reference table.

Behavior:

- Calls create_schema.
- Creates currently defined ORM tables if they do not exist.
- Seeds the `tsgs` table with the canonical 3GPP TSG list (16 rows). Existing
  rows are refreshed in place, so re-running this command is safe.

### doc3gpp db reset

Purpose:

- Recover from schema drift by wiping the SQLite database file and
  recreating it from scratch. Use this after an ORM change has left the
  live schema out of sync — Alembic is not wired up in this project, so
  manual migrations are the norm and a mismatched schema can leave the
  DB unusable. **Destructive: every row in every table is wiped.**

Options:

- `--yes`, `-y`: skip the interactive confirmation prompt.

Behavior:

- Refuses to run on MySQL or PostgreSQL URLs (use the backend-native
  `DROP DATABASE` / `CREATE DATABASE` workflow instead).
- For file-based SQLite URLs (`sqlite:///...` /
  `sqlite+pysqlite:///...`): deletes the on-disk `.db` file plus any
  WAL / SHM / journal sidecars, then re-runs `create_schema` +
  `seed_defaults`.
- For in-memory SQLite (`sqlite:///:memory:`): skips the delete step
  (there is nothing to delete) and re-runs `create_schema` +
  `seed_defaults`.
- Clears the cached SQLAlchemy engine so the subsequent `create_schema`
  opens a fresh connection to the (now empty) file.

## meeting Commands

### doc3gpp meeting sync

Purpose:

- Scrape and persist meeting records from 3gpp DynaReport.

Options:

- --tsg: TSG short name.
  - default: r5
  - validated against the `tsgs` reference table; unknown values raise an
    error listing the known short names and pointing to `doc3gpp tsg list`.
  - if the `tsgs` table is empty (fresh install), it is auto-seeded before
    validation runs.
- --closed-years: number of historical years to keep.
  - default: 2
- --future-years: number of future years to keep.
  - default: 1

Behavior:

- Builds the 3GPP meeting report URL from the TSG short name.
- Fetches HTML page.
- Parses meeting rows.
- Filters by date window.
- Stamps the canonical (`--tsg` upper-cased) short name onto every
  parsed `Meeting` so the persisted `meetings.tsg` FK column is
  populated. The parent row in `tsgs` must exist (auto-seeded on a
  fresh install); sync without a matching `tsgs` row will fail the FK
  constraint.
- Upserts records into meetings table.
- Prints inserted/updated row count.

### doc3gpp meeting list

Purpose:

- List recent meeting rows from database.

Options:

- --limit: number of rows.
  - default: 20
- --tsg: only list meetings for the given TSG short name.
  - default: none
  - exact-match on the `meetings.tsg` FK (case-insensitive on input;
    stored canonicalised to upper case by `meeting sync`). Rows whose
    `tsg` is `NULL` (e.g. imported before the column was added) are
    excluded.

Additional options:

- --name: SQL LIKE pattern to filter meeting `name` (supports `%` and `_`).
- --name: SQL LIKE pattern to filter meeting `name` (supports `%` and `_`).
- --location: SQL LIKE pattern to filter meeting `location` (supports `%` and `_`).
- --year: filter meetings by the year of the `end_date`.
- --fields: comma-separated list of fields to include in output, or `all`.
- --format: see "Common list output options" below (table | json | markdown).
- -o, --output: write the result to a file instead of stdout.

Default output fields:

- `meeting_id`, `name`, `location`, `start_date`, `end_date`, `start_doc`, `end_doc`

Note: by default `title`, `updated_at`, `ftp_url`, and `tsg` are excluded
to keep the listing compact; use `--fields all` to include every
available column (or `--fields tsg` to add just the owning TSG).

Examples:

- List recent R5 meetings (default fields):

```bash
doc3gpp meeting list --tsg r5
```

- Match names containing "TTCN" using SQL LIKE (% wildcard):

```bash
doc3gpp meeting list --name '%TTCN%'
```

- List meetings ending in 2026:

```bash
doc3gpp meeting list --year 2026
```

- Output only the meeting id and name columns:

```bash
doc3gpp meeting list --fields meeting_id,name
```

- Include the owning TSG (FK column populated by `meeting sync --tsg`):

```bash
doc3gpp meeting list --fields meeting_id,name,tsg --tsg r5
```

- Output every available field (including `ftp_url`, `title`, `updated_at`):

```bash
doc3gpp meeting list --fields all
```

## tdoc Commands

### doc3gpp tdoc sync

Purpose:

- Discover and persist TDoc records by looking up a stored meeting's FTP path.

Options:

- --meeting-id: numeric meeting ID from the meetings database (see `doc3gpp meeting sync`).
- --meeting: exact meeting name from the meetings database (see `doc3gpp meeting sync`).

Notes:

- Exactly one of `--meeting-id` or `--meeting` must be provided.

Behavior:

- Loads the meeting record from storage.
- Resolves the stored FTP URL from that meeting.
- Discovers the matching `TDoc_List_Meeting_*.xlsx` file on 3GPP FTP.
- Parses and persists TDoc rows.

### doc3gpp tdoc list

Purpose:

- List recent stored TDoc records with optional filters.

Options:

- --limit: maximum number of rows.
  - default: 20
- --tsg: filter TDoc IDs by TSG prefix (e.g. R5, S2).
- --year: filter by the two-digit year code embedded in the TDoc identifier.
- --meeting: SQL LIKE pattern to filter by meeting name (supports % and _).
- --meeting-id: exact match on the parent meeting's numeric ID (see
  `doc3gpp meeting list`). Combinable with `--meeting`; rows must satisfy
  both predicates.
- --source: filter by TDoc source/contributor.
- --spec: filter by technical specification.
- --wi: filter by related work items.
- --title: filter by TDoc title.
- --cat: filter by CR category (`cr_cat`).
- --status: filter by TDoc status.
- --type: filter by TDoc type.
- --revision-of: filter by `is_revision_of`.
- --revised-to: filter by `revised_to`.
- --ftp-url: filter by `ftp_url`.
- --uploaded-date: filter by `uploaded_date`. See
  [Filter syntax](#filter-syntax-for-text-and-date-filters) below for the
  accepted forms (including date comparisons).
- --fields: comma-separated list of fields to include in output, or `all`.
- --format: see "Common list output options" below (table | json | markdown).
- -o, --output: write the result to a file instead of stdout.

Default output fields:

- `tdoc_id`, `meeting_name`, `title`, `source`, `type`, `status`, `cr_cat`, `spec`, `version`, `related_wis`

Examples:

- List TDocs from Qualcomm:

```bash
doc3gpp tdoc list --source "Qualcomm%"
```

- Find TDocs related to Spec 38.331:

```bash
doc3gpp tdoc list --spec "38.331%"
```

- Filter by Work Item containing "NR_ext":

```bash
doc3gpp tdoc list --wi "%NR_ext%"
```

- Find TDocs with "RedCap" in the title:

```bash
doc3gpp tdoc list --title "%RedCap%"
```

- Exclude titles containing "Sidelink" (NOT LIKE):

```bash
doc3gpp tdoc list --title "!%Sidelink%"
```

- List all TDocs from a single meeting by its numeric ID:

```bash
doc3gpp tdoc list --meeting-id 85434
```

- Output only ID, title and status:

```bash
doc3gpp tdoc list --fields tdoc_id,title,status
```

### doc3gpp tdoc show

Purpose:

- Print every :class:`TDoc` field for a single TDoc plus the parsed CR
  cover-page fields (if `tdoc parse` has been run for this id).

Options:

- `--tdoc TDOC`: canonical TDoc identifier (e.g. `R5s260009`). Required.

Behavior:

- Looks up the row in the `tdocs` table via a PK lookup.
- On miss: raises `BadParameter` listing the requested id and pointing
  to `doc3gpp tdoc sync` / `doc3gpp tdoc list`.
- On hit: prints a `[TDoc]` section (every `TDoc` field) followed by
  one `[Extracted Details]` block **per revision** when one or more
  matching `tdoc_cr_details` rows exist (a single `tdoc_id` may have
  multiple revisions at distinct URLs; the CLI renders one block per
  URL with `extracted_at` newest first). The `corrections` list of
  every block is rendered as pretty-printed JSON.
- Long free-text fields (`reason_for_change`,
  `consequences_if_not_approved`) are truncated to 200 characters with
  an ellipsis.

Examples:

```bash
# Show the TDoc + any extracted CR cover-page fields.
doc3gpp tdoc show --tdoc R5s260009
```

### doc3gpp tdoc parse

Purpose:

- Download a TDoc zip from the 3GPP FTP, render its `.docx` body to
  markdown, parse the cover-page fields, and persist the result to
  `tdoc_cr_details` + `tdoc_extracts`. Wraps the Phase 6
  `TDocCrService.extract_many` for batch CLI use.

Options:

- `--tdoc TDOC`: canonical TDoc identifier (e.g. `R5s260009`).
  Repeatable for batch extraction.
- `--tdoc-id N`: integer form of a TDoc id; resolved against the
  `tdocs` table (PK lookup) before extraction. Repeatable. An unknown
  id prints a warning and is skipped — the rest of the batch still runs.
- `--meeting-id N`: batch selector that fetches every CR-type TDoc
  stored under meeting `N` (see `doc3gpp meeting list`) and runs them
  through the same pipeline. Without `--force` only TDocs that have
  not yet been parsed (no row in `tdoc_cr_details`) are processed;
  `--force` re-parses every CR-type TDoc under the meeting.
  Mutually exclusive with `--tdoc` and `--tdoc-id`. Combinable with
  the field filters below (only active when `--meeting-id` is used).
- `--status PATTERN`: filter meeting TDocs by `status` (SQL `LIKE`).
- `--cat PATTERN`: filter meeting TDocs by `cr_cat`.
- `--spec PATTERN`: filter meeting TDocs by technical specification
  (`spec`).
- `--wi PATTERN`: filter meeting TDocs by `related_wis`.
- `--revision-of PATTERN`: filter meeting TDocs by `is_revision_of`.
- `--revised-to PATTERN`: filter meeting TDocs by `revised_to`.
- `--title PATTERN`: filter meeting TDocs by `title`.
- `--ftp-url PATTERN`: filter meeting TDocs by `ftp_url`.
- `--source PATTERN`: filter meeting TDocs by source / contributor.
- `--type PATTERN`: filter meeting TDocs by document `type`.
- `--uploaded-date EXPR`: filter meeting TDocs by `uploaded_date`.
  See [Filter syntax](#filter-syntax-for-meeting-id-batch) below for
  the accepted forms (including date comparisons).
- `--force`: skip both the on-disk zip/markdown cache and the
  persisted `tdoc_cr_details` row so every id is re-fetched and
  re-parsed.
- `--full`: reserved for the parser's `full=True` mode (pulls in
  `before_change` / `after_change` per correction). The current
  service does not yet wire this through; accepted silently so existing
  scripts keep parsing.

#### Filter syntax for `--meeting-id` batch

The ten text filters above (`--status`, `--cat`, `--spec`, `--wi`,
`--revision-of`, `--revised-to`, `--title`, `--ftp-url`, `--source`,
`--type`) accept the same value grammar:

| Value              | Effect                                                          |
| ------------------ | --------------------------------------------------------------- |
| `null`             | match rows whose column is `NULL`                               |
| `not-null`         | match rows whose column is not `NULL`                           |
| `!<pattern>`       | match rows whose column does NOT LIKE `<pattern>` — the `!` is consumed and the rest is bound as the LIKE pattern (e.g. `!%Sidelink%` excludes titles containing `Sidelink`) |
| any other text     | applied as a SQL `LIKE` pattern (use `%` / `_`)                 |

`--uploaded-date` accepts the same `null` / `not-null` tokens plus a
parameterised SQL comparison of the form ` "<op> 'YYYY-MM-DD'"` where
`<op>` is one of `=`, `!=`, `<`, `<=`, `>`, `>=`. The operator and the
date literal are bound as SQLAlchemy parameters — the date string is
never string-interpolated into the SQL, so the surface is safe to
expose to operator input. Anything else is rejected at the CLI
boundary with a clear error before the database is touched:

```
Invalid date filter 'yesterday'. Expected 'null', 'not-null',
or an expression like ">= 'YYYY-MM-DD'" with one of =, !=, <, <=, >, >=.
```

The filters compose: combining several filters narrows the batch with
`AND`. They are only active with `--meeting-id`; passing them with
`--tdoc` or `--tdoc-id` is silently ignored (the per-id selectors do
not need them).

Behavior:

- Calls `TDocCrService.extract_many(tdoc_ids, force=force)`, which
  returns a `BatchExtractResult` bundling successes with a per-id
  failure reason. The service catches `TDocZipDownloadError`,
  `TDocTypeUnsupportedError`, `TDocNotFoundError`, `CRHeaderMissingError`,
  plus the `ValueError` raised by the tdoc_id shape guard, and
  records the failure reason (formatted as
  `"{ExceptionClassName}: {exc}"`) so the CLI can surface it inline.
- When `python-docx` is not installed the entire batch fails before any
  per-id work happens — the CLI prints an install hint and exits 1.
- Output per id: `<tdoc_id>: spec=<spec> cr_num=<cr_num> title=<title>`
  on success; `<tdoc_id>: FAILED - {ExceptionClassName}: {exc}` on
  failure (e.g. `R5s260010: FAILED - TDocNotFoundError: TDoc 'R5s260010'
  is not stored in the tdocs table; run \`doc3gpp tdoc sync\` first`).
  The class name tells the operator *which* step failed (type guard,
  DB lookup, network, shape check) without tailing the log file; a
  full traceback is still written to the logs for debugging.
- Final summary line: `Extracted N/M TDocs (K failures)`.
- `--meeting-id` first validates the meeting row exists (otherwise
  prints `Unknown meeting_id N` and exits non-zero), then asks the
  TDoc repository for CR-type rows under it, and finally checks each
  row's parsed status against `tdoc_cr_details` unless `--force`
  bypasses the check. When every row is already parsed, the CLI
  prints a "use --force to re-parse" hint and exits 0.

Exit codes:

- `0` — at least one TDoc extracted successfully (cache hits count),
  or `--meeting-id` without `--force` had nothing new to parse.
- `1` — every TDoc failed, **or** `python-docx` is missing and the
  batch could not even start, **or** `--meeting-id` resolved to a
  meeting that has no CR-type TDocs (after filters), **or** an invalid
  `--uploaded-date` value was supplied.

Install the optional dependency before first use:

```bash
pip install "doc3gpp[extract]"
```

Examples:

```bash
# Extract a single CR.
doc3gpp tdoc parse --tdoc R5s260009

# Batch extract three CRs, bypassing the on-disk cache.
doc3gpp tdoc parse --tdoc R5s260009 --tdoc R5s260051 --tdoc R5s260135 --force

# Mix string and integer selectors.
doc3gpp tdoc parse --tdoc R5s260009 --tdoc-id 1234

# Parse every not-yet-parsed CR-type TDoc under meeting 85434.
doc3gpp tdoc parse --meeting-id 85434

# Re-parse every CR-type TDoc under the meeting (cache + DB row bypassed).
doc3gpp tdoc parse --meeting-id 85434 --force

# Narrow the batch: only 38.331 CRs sourced from Qualcomm, uploaded in Q1.
doc3gpp tdoc parse --meeting-id 85434 \
    --spec '38.331%' \
    --source 'Qualcomm%' \
    --uploaded-date ">= '2026-01-01'"

# Re-parse CRs whose `cr_cat` is currently NULL (i.e. not yet classified).
doc3gpp tdoc parse --meeting-id 85434 --cat null --force

# Find revisions of a known TDoc id under the meeting.
doc3gpp tdoc parse --meeting-id 85434 --revision-of 'R5-260050'

# Exclude Sidelink titles from the batch (NOT LIKE).
doc3gpp tdoc parse --meeting-id 85434 --title '!%Sidelink%'
```

## cache Commands

The `cache` sub-app exposes the on-disk cache that backs the TDoc
extraction pipeline (Phase 1 `TDocCache`). The cache lives under
`settings.cache.dir` (default `~/.cache/doc3gpp/tdocs`) with two
subtrees: `zips/` (raw 3GPP zip downloads) and `markdown/` (python-docx
output keyed by content hash). Both commands are pure file-system
operations — they do **not** touch the database.

### doc3gpp cache status

Purpose:

- Print the current cache footprint and configured ceiling.

Output (plain text, no `--format` flag for this initial cut):

```text
file_count:  3
total_bytes: 128 B
limit_bytes: 1.0 GB
zips:        2
markdown:    1
```

`limit_bytes: 0` renders as `limit_bytes: unlimited` so an unset cap
is unambiguous. `status()` is a pure read — it does **not** trigger FIFO
eviction, even when the cache is over the configured ceiling.

Examples:

```bash
doc3gpp cache status
```

### doc3gpp cache purge

Purpose:

- Delete every cached zip and markdown file, recreating the subtrees
  empty so subsequent `tdoc parse` calls still work.

Options:

- `--yes`, `-y`: skip the confirmation prompt.

Behavior:

- When `settings.cache.purge_confirm` is `True` (the default) and
  `--yes` is **not** passed, the command prompts for confirmation
  (`typer.confirm(..., abort=True)`). In a non-interactive environment
  the prompt raises `Abort` and no files are deleted.
- Set `DOC3GPP_CACHE__PURGE_CONFIRM=false` (env var) or `purge_confirm
  = false` in the TOML config file to skip the prompt globally
  (CI / scripted use).
- The on-disk artefacts referenced from
  `tdoc_extracts.markdown_path` and `tdoc_extracts.zip_path` become
  stale — the next `tdoc parse` will repopulate them.

Examples:

```bash
# Interactive confirmation.
doc3gpp cache purge

# Skip the prompt (scripted).
doc3gpp cache purge --yes
```

## tsg Commands

The `tsg` sub-app exposes the canonical 3GPP TSG reference table. The table
is created and seeded automatically by `doc3gpp db init`, and the canonical
short names (R1..R5, RT, S1..S6, C1, C3, C4, C6) are used to validate the
`--tsg` option on `doc3gpp meeting sync`.

### doc3gpp tsg list

Purpose:

- List TSG reference records.

Options:

- --fields: comma-separated list of fields, or `all`.
  - default: `tsg_name,short_name,description`
- --format: see "Common list output options" below (table | json | markdown).
- -o, --output: write the result to a file instead of stdout.

Default output fields:

- `tsg_name`, `short_name`, `description`

Use `--fields all` to also include `url`.

Examples:

```bash
# Default compact listing
doc3gpp tsg list

# Include the URL column
doc3gpp tsg list --fields all

# Only the short codes and full names
doc3gpp tsg list --fields short_name,tsg_name

# Dump the full table to a JSON file
doc3gpp tsg list --format json --output tsg_reference.json
```

### doc3gpp tsg show

Purpose:

- Show a single TSG record by its short name or full `tsg_name`.

Options:

- --tsg: short name (e.g. `R5`) or full tsg_name (e.g. `RAN WG5`). Required.
  - matching is case-insensitive on both forms.

Behavior:

- Looks up by short name first, then by `tsg_name`.
- On miss, raises a `BadParameter` listing the known short names.

Examples:

```bash
doc3gpp tsg show --tsg r5
doc3gpp tsg show --tsg "RAN AH1"
```

### doc3gpp tsg seed

Purpose:

- Insert or refresh the canonical 3GPP TSG reference list.

Behavior:

- Creates the schema (idempotent).
- Upserts the 16 canonical rows; existing rows are updated in place rather
  than duplicated. URLs are composed from the project URL pattern.

Examples:

```bash
doc3gpp tsg seed
```

## wi Commands

The `wi` sub-app exposes the 3GPP Work Item (WI) reference table. WI rows
are scraped from the per-TSG DynaReport page at
`https://www.3gpp.org/dynareport?code=TSG-WG--<tsg>--wis.htm` and stored
with their owning TSG short name as a foreign key into the `tsgs` table.

### doc3gpp wi sync

Purpose:

- Fetch and store the active WIs for a single TSG from 3gpp.org.

Options:

- --tsg: TSG short name (e.g. `R5`).
  - default: `r5`
  - validated against the `tsgs` reference table; unknown values raise an
    error listing the known short names and pointing to `doc3gpp tsg list`.
  - if the `tsgs` table is empty (fresh install), it is auto-seeded before
    validation runs.

Behavior:

- Composes the DynaReport URL from the uppercased TSG short name.
- Fetches the HTML body via `ScraperClient`.
- Parses the `dsp-tsgwgxwis` table into `Wi` records
  (`wi_id`, `acronym`, `release`, `name`).
- Upserts into the `wis` table keyed by `(wi_id, tsg_short)`. Existing
  rows for the same composite key are refreshed in place, so re-running
  this command updates acronym, release, name and `updated_at` without
  duplicating rows.
- Prints the count of rows written for the TSG.

Examples:

```bash
# Sync WIs for RAN WG5 (default TSG).
doc3gpp wi sync --tsg r5

# Sync WIs for SA WG2.
doc3gpp wi sync --tsg S2
```

### doc3gpp wi list

Purpose:

- List stored WI records with optional SQL `LIKE` filters.

Options:

- --limit: number of rows to return.
  - default: 20
  - range: 1..500
- --tsg: only list WIs belonging to the given TSG short name.
  - case-insensitive; normalised to the canonical uppercase form.
  - default: none (results span all TSGs).
- --name: SQL `LIKE` pattern to filter the WI title (supports `%` and `_`).
- --acronym: SQL `LIKE` pattern to filter the WI acronym
  (supports `%` and `_`).
- --release: SQL `LIKE` pattern to filter the release marker
  (supports `%` and `_`).
- --format: see "Common list output options" below (table | json | markdown).
- -o, --output: write the result to a file instead of stdout.

Default output fields (tab-separated):

- `wi_id`, `acronym`, `release`, `name`

Examples:

```bash
# 10 most recent WIs across all TSGs.
doc3gpp wi list --limit 10

# All Rel-19 WIs for RAN WG5.
doc3gpp wi list --tsg R5 --release "Rel-19" --limit 100

# WIs whose acronym contains "UEConTest".
doc3gpp wi list --acronym "%UEConTest%" --limit 50

# Markdown export of all RAN WG5 WIs.
doc3gpp wi list --tsg R5 --format markdown -o r5_wis.md
```

## Common list output options

The `meeting list`, `tdoc list`, `tsg list`, and `wi list` commands all
accept the same two output-routing flags in addition to their
command-specific filters:

- `-o, --output PATH`: write the result to `PATH` instead of stdout. The
  file is opened in `w` mode and truncated if it already exists; pass
  `-o -` to force stdout.
- `--format FMT`: choose the output format.
  - `table` (default) — the legacy tab-separated rendering. With zero
    records, a friendly "No X found" line is written to stdout (or
    suppressed when stdout is redirected to a file).
  - `json` — UTF-8 JSON array of objects keyed by the selected field
    names. With zero records, the file contains `[]` so consumers always
    see a parseable payload.
  - `markdown` — a GitHub-flavored markdown table (`| col | col |` header
    plus `|---|` separator). With zero records, only the header and
    separator lines are emitted. Pipe characters in cell values are
    backslash-escaped.

Both flags are orthogonal: `--format` without `--output` re-prints to
stdout in the chosen format, and `--output` without an explicit
`--format` keeps the legacy tab-separated output. Values for
`--format` are case-insensitive and an unknown value raises a
`BadParameter` listing the accepted tokens.

The defaults for `--format` and the implicit column list can be
overridden for every list command via a TOML config file — see the
`config` commands below.

## config Commands

### doc3gpp config path

Print the config file currently in effect, or `(no config file found)`
when none of the search locations resolve. Search order:

1. `$DOC3GPP_CONFIG` (file or directory).
2. `./doc3gpp.toml` (project-local).
3. `$XDG_CONFIG_HOME/doc3gpp/config.toml`, falling back to
   `~/.config/doc3gpp/config.toml`.

### doc3gpp config show

Print the fully-resolved settings as JSON. The first line is a comment
identifying the config source that contributed the file-derived portion
of the result, followed by the merged view of every
`doc3gpp.settings.schema.Settings` field after applying the precedence
chain (CLI flags > environment variables > config file > defaults).

Use this command to verify which file is in effect and to diff your
TOML overrides against the built-in defaults.

## Examples

```bash
doc3gpp db init
doc3gpp db check
doc3gpp db reset --yes           # destructive: wipe + recreate SQLite schema
doc3gpp tsg list
doc3gpp meeting sync --tsg r5
doc3gpp meeting list --limit 20
doc3gpp tdoc sync --meeting-id 85434 --meeting "R5#74"
doc3gpp tdoc list --limit 10
doc3gpp tdoc parse --tdoc R5s260009
doc3gpp tdoc show --tdoc R5s260009
doc3gpp cache status
doc3gpp cache purge --yes
doc3gpp wi sync --tsg r5
doc3gpp wi list --limit 10
doc3gpp wi list --tsg r5 --release "Rel-19" --limit 100

# Common output variants
doc3gpp tdoc list --format json --output tdocs.json
doc3gpp meeting list --format markdown -o meetings.md
doc3gpp tsg list --format json
doc3gpp wi list --format markdown -o wis.md

# Inspect the resolved configuration
doc3gpp config path
doc3gpp config show | jq '.meeting_sync, .output'
```

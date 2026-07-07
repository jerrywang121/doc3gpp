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

Note: by default `title`, `updated_at`, and `ftp_url` are excluded to keep
the listing compact; use `--fields all` to include every available column.

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
- --source: SQL LIKE pattern to filter by TDoc source/contributor.
- --spec: SQL LIKE pattern to filter by technical specification.
- --wi: SQL LIKE pattern to filter by related work items.
- --title: SQL LIKE pattern to filter by TDoc title.
- --cat: SQL LIKE pattern to filter by CR category.
- --status: SQL LIKE pattern to filter by TDoc status.
- --type: SQL LIKE pattern to filter by TDoc type.
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

- Output only ID, title and status:

```bash
doc3gpp tdoc list --fields tdoc_id,title,status
```

### doc3gpp tdoc show

Purpose:

- Print every :class:`TDoc` field for a single TDoc plus the parsed CR
  cover-page fields (if `tdoc extract` has been run for this id).

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

### doc3gpp tdoc extract

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
- `--force`: skip both the on-disk zip/markdown cache and the
  persisted `tdoc_cr_details` row so every id is re-fetched and
  re-parsed.
- `--full`: reserved for the parser's `full=True` mode (pulls in
  `before_change` / `after_change` per correction). The current
  service does not yet wire this through; accepted silently so existing
  scripts keep parsing.

Behavior:

- Calls `TDocCrService.extract_many(tdoc_ids, force=force)`. The service
  catches `TDocZipDownloadError`, `PythonDocxNotInstalledError`,
  `TDocTypeUnsupportedError`, `TDocNotFoundError`, and
  `CRHeaderMissingError` per-id and skips the broken entry; the CLI
  computes the failure set as `input - successful_keys` and prints
  one `FAILED` line per skipped id.
- When `python-docx` is not installed the entire batch fails before any
  per-id work happens — the CLI prints an install hint and exits 1.
- Output per id: `<tdoc_id>: spec=<spec> cr_num=<cr_num> title=<title>`
  on success, `<tdoc_id>: FAILED - extract error (see logs)` on failure.
- Final summary line: `Extracted N/M TDocs (K failures)`.

Exit codes:

- `0` — at least one TDoc extracted successfully (cache hits count).
- `1` — every TDoc failed, **or** `python-docx` is missing and the
  batch could not even start.

Install the optional dependency before first use:

```bash
pip install "doc3gpp[extract]"
```

Examples:

```bash
# Extract a single CR.
doc3gpp tdoc extract --tdoc R5s260009

# Batch extract three CRs, bypassing the on-disk cache.
doc3gpp tdoc extract --tdoc R5s260009 --tdoc R5s260051 --tdoc R5s260135 --force

# Mix string and integer selectors.
doc3gpp tdoc extract --tdoc R5s260009 --tdoc-id 1234
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
  empty so subsequent `tdoc extract` calls still work.

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
  stale — the next `tdoc extract` will repopulate them.

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
doc3gpp tsg list
doc3gpp meeting sync --tsg r5
doc3gpp meeting list --limit 20
doc3gpp tdoc sync --meeting-id 85434 --meeting "R5#74"
doc3gpp tdoc list --limit 10
doc3gpp tdoc extract --tdoc R5s260009
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

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
```

## Examples

```bash
doc3gpp db init
doc3gpp db check
doc3gpp tsg list
doc3gpp meeting sync --tsg r5
doc3gpp meeting list --limit 20
doc3gpp tdoc sync --meeting-id 85434 --meeting "R5#74"
doc3gpp tdoc list --limit 10
doc3gpp wi sync --tsg r5
doc3gpp wi list --limit 10
doc3gpp wi list --tsg r5 --release "Rel-19" --limit 100
```

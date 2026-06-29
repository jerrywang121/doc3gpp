# CLI Reference

This document describes the currently implemented command surface in src/doc3gpp/cli.py.

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

- Initialize schema for current backend.

Behavior:

- Calls create_schema.
- Creates currently defined ORM tables if they do not exist.

## meetings Commands

### doc3gpp meetings sync

Purpose:

- Scrape and persist meeting records from 3gpp DynaReport.

Options:

- --tsg: TSG short name.
  - default: r5
- --closed-years: number of historical years to keep.
  - default: 2
- --future-years: number of future years to keep.
  - default: 1

Behavior:

- Builds the 3GPP meetings report URL from the TSG short name.
- Fetches HTML page.
- Parses meeting rows.
- Filters by date window.
- Upserts records into meetings table.
- Prints inserted/updated row count.

### doc3gpp meetings list

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
- --year: filter meetings by the year of the `start_date`.
- --fields: comma-separated list of fields to include in output, or `all`.

Default output fields:

- `meeting_id`, `name`, `location`, `start_date`, `end_date`, `start_doc`, `end_doc`

Note: by default `title`, `updated_at`, and `ftp_url` are excluded to keep
the listing compact; use `--fields all` to include every available column.

Examples:

- List recent R5 meetings (default fields):

```bash
doc3gpp meetings list --tsg r5
```

- Match names containing "TTCN" using SQL LIKE (% wildcard):

```bash
doc3gpp meetings list --name '%TTCN%'
```

- List meetings starting in 2026:

```bash
doc3gpp meetings list --year 2026
```

- Output only the meeting id and name columns:

```bash
doc3gpp meetings list --fields meeting_id,name
```

- Output every available field (including `ftp_url`, `title`, `updated_at`):

```bash
doc3gpp meetings list --fields all
```

## tdoc Commands

### doc3gpp tdoc sync

Purpose:

- Discover and persist TDoc records by looking up a stored meeting's FTP path.

Options:

- --meeting-id: numeric meeting ID from the meetings database.
- --meeting: exact meeting name from the meetings database.

Notes:

- Exactly one of `--meeting-id` or `--meeting` must be provided.

Behavior:

- Loads the meeting record from storage.
- Resolves the stored FTP URL from that meeting.
- Discovers the matching `TDoc_List_Meeting_*.xlsx` file on 3GPP FTP.
- Parses and persists TDoc rows.

### doc3gpp tdoc list

Purpose:

- List recent tdoc records.

Options:

- --limit: number of rows.
  - default: 20
- --tsg: filter TDoc IDs by TSG prefix (e.g. R5).
- --year: filter TDocs by the two-digit year code embedded in the TDoc ID.
- --meeting: SQL LIKE pattern to filter meeting name; supports % and _.

## Examples

```bash
doc3gpp db init
doc3gpp db check
doc3gpp meetings sync --tsg r5
doc3gpp meetings list --limit 20
doc3gpp tdoc sync --meeting-id 85434 --meeting "R5#74"
doc3gpp tdoc list --limit 10
```

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

## calendar Commands

### doc3gpp calendar sync

Purpose:

- Scrape and persist meeting calendar entries from 3gpp DynaReport.

Options:

- --url: calendar page URL.
  - default: https://www.3gpp.org/dynareport?code=Meetings-R5.htm
- --closed-years: number of historical years to keep.
  - default: 2
- --future-years: number of future years to keep.
  - default: 1

Behavior:

- Fetches HTML page.
- Parses meeting rows.
- Filters by date window.
- Upserts records into meetings table.
- Prints inserted/updated row count.

### doc3gpp calendar list

Purpose:

- List recent meeting rows from database.

Options:

- --limit: number of rows.
  - default: 20

Output format:

- meeting_id, name, start_date, end_date, ftp_url, updated_at

## tdoc Commands

### doc3gpp tdoc add

Purpose:

- Insert or update one tdoc record.

Options:

- --tdoc-id
- --title
- --meeting (optional)
- --url (optional)

### doc3gpp tdoc list

Purpose:

- List recent tdoc records.

Options:

- --limit: number of rows.
  - default: 20

## Examples

```bash
doc3gpp db init
doc3gpp db check
doc3gpp calendar sync --url "https://www.3gpp.org/dynareport?code=Meetings-R5.htm"
doc3gpp calendar list --limit 20
doc3gpp tdoc add --tdoc-id R1-000001 --title "Example"
doc3gpp tdoc list --limit 10
```

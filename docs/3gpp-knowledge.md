# 3GPP Knowledge Reference

This document summarizes the 3GPP source URLs, naming conventions, and extraction fields used by this project.

## Meeting Source URLs

### Meetings report URL

Meeting calendars are fetched from the 3GPP DynaReport meeting report pages.

Pattern:

```text
https://www.3gpp.org/dynareport?code=Meetings-{TSG}.htm
```

Where `{TSG}` is the TSG short name, such as `R5` for RAN5.

Plenary TSGs use the same convention, for example:

- `RP` for RAN Plenary
- `CP` for CT Plenary
- `SP` for SA Plenary

Example:

```text
https://www.3gpp.org/dynareport?code=Meetings-RP.htm
```

### CLI URL construction

The CLI currently constructs the meeting URL from the `--tsg` value using the rule:

```python
f"https://www.3gpp.org/dynareport?code=Meetings-{tsg.upper()}.htm"
```

That means `--tsg r5`, `--tsg R5`, and `--tsg r5` all map to the same page.

## Extracted Meeting Fields

Meeting rows are parsed from the HTML table on the meeting report page.

The current implementation extracts these fields into the `Meeting` domain model:

- `meeting_id` (int)
  - Extracted from the meeting entry link query parameter `MtgId`.
  - Example: `https://www.3gpp.org/ftp/tsg_ran/TSG_RAN5/Meetings/2026/85434/Meetings-R5-20260702.htm` may contain `MtgId=85434`.

- `name` (str)
  - The meeting short name shown in the first table cell.
  - Example: `R5--TTCN Workshop#74`.

- `title` (str)
  - The meeting full title shown in the second table cell.
  - Example: `3GPPRAN5-TTCN Workshop#74`.

- `location` (str)
  - The location value shown in the third table cell.
  - Example: `Online`.

- `start_date` (date)
  - The meeting start date shown in the fourth table cell.
  - Parsed from ISO-like text with support for hyphen variants.

- `end_date` (date)
  - The meeting end date shown in the fifth table cell.
  - Parsed like `start_date`.

- `ftp_url` (str | None)
  - The FTP path discovered in one of the row’s hyperlinks.
  - The parser accepts both `/` and `\` separators and returns a normalized path without repeated slashes.

- `start_doc` (str | None)
  - The first document identifier extracted from the meeting row’s document range text.
  - Example: `R5-200001`.

- `end_doc` (str | None)
  - The second document identifier from the document range text.
  - Example: `R5-200050`.

- `tsg` (str | None)
  - Canonical TSG short name stamped by `meeting sync --tsg` and stored as
    a nullable foreign key into `tsgs.short_name`.

### Parsing rules

The meeting parser implementation is in `src/doc3gpp/parsers/calendar_parser.py`.

Important behavior:

- It skips rows with fewer than 6 cells.
- It skips entries where the meeting title ends with `CANCELLED`.
- It looks for an FTP link in the 6th table cell and, if absent, tries the 9th cell.
- It normalizes Unicode hyphen characters to `-` before parsing dates.

## TDoc and Document Source URLs

### TDoc records

TDocs are represented by the `TDoc` model with these fields:

- `tdoc_id` (str)
  - The 3GPP document identifier, typically formatted like `R1-000001`,
    `R5s260009`, or `R5w260045`.

- `title` (str | None)
  - The document title.

- `meeting_id` (int | None)
  - Optional foreign key into `meetings.meeting_id`. The CLI joins this to
    `meetings.name` when it needs the presentation-only
    `TDocWithMeeting.meeting_name` field.

- `ftp_url` (str | None)
  - Optional relative URL for the document, stored as a path relative
    to the canonical 3GPP FTP root (`https://www.3gpp.org/ftp/`).

- `source`, `type`, `status`, `cr_cat`, `is_revision_of`, `revised_to`,
  `release`, `spec`, `version`, `related_wis`, `cr_num`, `cr_pack`
  (str | None)
  - Metadata extracted from the TDoc list XLSX.

- `reservation_date`, `uploaded_date` (date | None)
  - Dates parsed from the TDoc list XLSX when present.

### TDocFile records (auxiliary attachments)

The `TDocFile` model captures auxiliary files attached to a TDoc: revisions, reviews, and support documents. These are populated automatically as part of `tdoc sync` and stored in the `tdoc_files` table.

- `id` (int)
  - Auto-incrementing primary key.

- `tdoc_id` (str)
  - Foreign key into `tdocs.tdoc_id`. The owning TDoc must already be persisted before the auxiliary file is recorded.

- `type` (str)
  - One of three values: `revision`, `review`, or `support`.
  - The set of allowed values is fixed; see `models/tdoc_file.py` for the canonical constants.

- `file` (str)
  - Bare filename of the attachment, e.g. `R5s260001_MCC160Comments.zip`.

- `ftp_url` (str)
  - Relative download URL on `https://www.3gpp.org/ftp/`, stored as a
    path relative to the FTP root. Unique across the table; serves as
    the upsert key.

- `uploaded_date` (date | None)
  - Date the attachment was uploaded to the 3GPP FTP, parsed from the
    directory listing when available.

### Where auxiliary TDoc files live

Auxiliary TDoc files are stored in meeting-specific subfolders, scanned in this order during `tdoc sync`:

- `Inbox/`
  - Carries intermediate revision TDoc ZIPs for most meetings.
  - File pattern: `{tdoc_id}r#.zip` (e.g. `R5s260001r1.zip`).
  - Type: `revision`.

- `Docs/` and `Tdocs/` (mutually exclusive)
  - Stores the base TDoc ZIPs (`{tdoc_id}.zip`).
  - For R5 TTCN Workshop meetings, also stores revision ZIPs (the workshop keeps revisions under `Docs/` rather than `Inbox/`).
  - Type: `revision`.

- `Review/`
  - Present only in R5 TTCN email meetings.
  - File patterns:
    - `{tdoc_id}_MCC160Comments[_r#]?.zip` — TTCN CR review documents.
      Type: `review`.
    - `{tdoc_id}_*.zip` (other suffixes) — supporting draft prose CR documents.
      Type: `support`.

### Classification rules

The filename parser in `src/doc3gpp/parsers/tdoc_file_parser.py` classifies each candidate file by stripping the longest matching `{tdoc_id}` prefix and inspecting the remainder:

| Suffix after the TDoc ID   | Type       |
|----------------------------|------------|
| `r` followed by digits     | `revision` |
| `_MCC160Comments` (optionally `_r#`) | `review`   |
| `_` + anything else        | `support`  |
| (no suffix)                | skipped (base TDoc) |
| anything else              | skipped    |

A file whose TDoc ID is not in the local `tdocs` table is silently dropped — auxiliary-file sync runs after the TDoc sync so the parser can match against the freshly-persisted TDoc IDs.

### Additional TDoc sources

The implemented TDoc sync path downloads each meeting's TDoc-list XLSX
from the 3GPP portal via `GenerateDocumentList.aspx?meetingId={meeting_id}`.
The sheet format is identical to the legacy FTP `TDoc_List_Meeting_*.xlsx`
files, so the same parser produces the `TDoc` rows.

Auxiliary TDoc files (revisions, review packs, supporting documents) are
still discovered by scanning the meeting's FTP subfolders after the TDoc
list sync, because those artifacts are not exposed through the portal
XLSX endpoint.

Future sources still planned:

- Expanded metadata beyond the current Excel-list columns.

## Work Item (WI) Source URLs

### WIs report URL

Active Work Items per TSG are fetched from the 3GPP DynaReport WI pages.

Pattern:

```text
https://www.3gpp.org/dynareport?code=TSG-WG--{TSG}--wis.htm
```

Where `{TSG}` is the TSG short name, such as `R5` for RAN5.

The page lists only those active (i.e. not yet completed) WIs where the named
TSG is the **sole responsible group**. WIs for which the group holds joint
responsibility — particularly hierarchically higher WIs that comprise WIs from
several groups — are intentionally not shown on this page.

Example:

```text
https://www.3gpp.org/dynareport?code=TSG-WG--R5--wis.htm
```

### CLI URL construction

The CLI constructs the WI URL from the `--tsg` value using the rule:

```python
f"https://www.3gpp.org/dynareport?code=TSG-WG--{tsg.upper()}--wis.htm"
```

That means `--tsg r5`, `--tsg R5`, and `--tsg R5` all map to the same page.

## Extracted WI Fields

WI rows are parsed from the HTML table on the WI report page. The data lives
in a single `<table>` carrying the `dsp-tsgwgxwis` CSS class; each data row
has three `<td>` cells.

The current implementation extracts these fields into the `Wi` domain model:

- `wi_id` (int)
  - The canonical numeric WI identifier from the 3GPP portal, extracted
    from the row anchor's `workitemId=` URL parameter.
  - Example: `1031076` from
    `https://portal.3gpp.org/desktopmodules/WorkItem/WorkItemDetails.aspx?workitemId=1031076`.

- `acronym` (str)
  - The WI acronym shown in the second table cell.
  - Example: `LTE_TN_NR_NTN_mob-Core`.

- `release` (str)
  - The release marker shown in the third table cell.
  - Example: `Rel-19`.

- `name` (str)
  - The full WI title shown in the first table cell (anchor text).
  - Example: `Building Block: Core part: Inter-RAT mode mobility support
    from E-UTRAN TN to NR NTN`.

- `tsg_short` (str)
  - The owning TSG short name, uppercased and stored on every row as a
    foreign key into `tsgs.short_name`. The `tsgs` table is auto-seeded
    on first sync so this FK is always satisfiable.

### Parsing rules

The WI parser implementation is in `src/doc3gpp/parsers/wi_parser.py`.

Important behavior:

- It locates the table by CSS class `dsp-tsgwgxwis` and returns an empty
  list when the table is missing (e.g. on pages with no active WIs).
- It skips rows with fewer than three `<td>` cells.
- It skips rows whose first cell has no `<a>` element or whose anchor has
  no `workitemId=` query parameter.
- It collapses internal whitespace (newlines, tabs) in the WI title and
  acronym to a single space.
- It uppercases the supplied `tsg_short` before stamping it onto each row.

## Meeting FTP directory structure

From a meeting `ftp_url`, the following directory layout is common:

1. `Agenda/`
   - Stores meeting agenda documents.
   - There may be multiple versions as the agenda is updated before and during the meeting.

2. `Docs/` or `Tdocs/` (older meetings)
   - Stores all TDoc ZIP files for the meeting.
   - TDoc ZIP files are named as `{tdoc_id}.zip`.
   - A metadata spreadsheet named `TDoc_List_Meeting_xxx.xlsx` lists all TDocs and their status.
   - ZIP files are immutable; once uploaded they are never changed.
   - Any update to a TDoc is stored as a revision file in `Inbox/` with the pattern `{tdoc_id}r#.zip`, where `#` is the reversion number.
   - (R5 TTCN Workshop meetings only) the `Docs/` only exists for meeting on and after 2016. There are multiple `TDoc_List_Meeting_TTCN Workshop{#xx}.xlsx` files, where the `Workshop{#xx}` matches the same part of the meeting name from meeting calendar. Moreover, the TDoc revision files named `{tdoc_id}r#.zip` are also stored under the same `Docs/` directory rather than `Inbox/`.
   - A revision TDoc eventually becomes the new official TDoc, with new TDoc IDs and is uploaded to `Docs/` or `Tdocs/`.
   - The TDoc list XLSX is updated over time to capture new TDocs and statuses such as revised, agreed, withdrawn, etc.
   - Files matching `{tdoc_id}r#.zip` here are surfaced in the `tdoc_files` table with `type='revision'`.

3. `Review/` (R5 TTCN email meetings only)
   - Present in R5 TTCN email meetings with an extra review folder under the meeting FTP tree.
   - Stores TTCN CR review documents associated with a base `{tdoc_id}`.
   - Review files are named `{tdoc_id}_MCC160Comments.zip`.
   - Updated review revisions are named `{tdoc_id}_MCC160Comments_r#.zip`, where `#` is the revision number.
   - The folder can also contain supporting draft prose CR documents for the same TTCN CR, typically named `{tdoc_id}_xxxxx.zip`.
   - Files matching the `_MCC160Comments[_r#]?` pattern are surfaced as `type='review'`; the rest of `{tdoc_id}_*.zip` files in this folder are surfaced as `type='support'`.

4. `Report/`
   - Stores meeting minutes and reports.
   - Draft and Final versions may be present.

5. `Inbox/`
   - If present, stores temporary documents during the meeting.
   - This often includes intermediate revision TDocs, new draft TDocs, liaison statements (LSs), and discussion papers created during the meeting.
   - Files matching `{tdoc_id}r#.zip` are surfaced as `type='revision'`. Files with other TDoc prefixes or unrelated documents are ignored.

6. `LSin/` and `LSout/`
   - Store incoming and outgoing liaison statements, respectively.

## Naming Conventions

### TSG short names

The CLI uses TSG short names such as:

- `R5` for RAN5
- `R1` for RAN1
- `C1` for CT1
- `S1` for SA1

TSG short names are normally derived from the 3GPP groups page:

```text
https://www.3gpp.org/3gpp-groups
```

This page lists TSGs and working groups, and the short names are typically used in 3GPP report codes.

Common examples:

- `S1` for SA WG1
- `C6` for CT WG6
- `R5` for RAN WG5

The report page URL is constructed by uppercasing the given short name.

### TDoc IDs

A TDoc ID generally follows the pattern:

```text
{TSG}-{YY}{####}
```

Where:

- `{TSG}` is the TSG short name, such as `C6`, `R5`, or `RP`.
- `{YY}` is the two-digit year of the associated meeting, e.g. `26` for 2026.
- `{####}` is a four-digit sequence number allocated by that TSG during the year, starting from `0001`.

Some R5 TTCN-related meetings use a special naming convention:

- `R5s{YY}{####}` for R5 TTCN email meetings, where TDocs are TTCN CRs for updating the TTCN test scripts codebase maintained by RAN5.
- `R5w{YY}{####}` for R5 TTCN workshop meetings, where TDocs are CRs for TTCN models updates.

Example:

```text
R5-260001
R5s260123
R5w260045
C6-260045
RP-260123
```

### 3GPP document path conventions

Meeting pages and document listings may use FTP-style links containing `ftp/` or `\ftp\` paths. The parser normalizes these to forward slashes.

## Source file references

- `src/doc3gpp/cli.py`
  - Builds the meeting URL from `--tsg`.
  - Exposes the `wi` Typer group (`wi sync`, `wi list`).

- `src/doc3gpp/services/meetings_service.py`
  - Syncs meeting rows and filters by date range.

- `src/doc3gpp/scraping/calendar_source.py`
  - Fetches raw meeting HTML via `ScraperClient`.

- `src/doc3gpp/parsers/calendar_parser.py`
  - Parses meeting table rows and extracts meeting fields.

- `src/doc3gpp/models/meeting.py`
  - Meeting domain fields.

- `src/doc3gpp/models/tdoc.py`
  - TDoc domain fields.

- `src/doc3gpp/scraping/wi_source.py`
  - Builds the per-TSG DynaReport URL and fetches the raw WI page HTML
    via `ScraperClient`.

- `src/doc3gpp/parsers/wi_parser.py`
  - Parses the `dsp-tsgwgxwis` table and extracts WI rows into `Wi`
    dataclasses.

- `src/doc3gpp/services/wi_service.py`
  - Orchestrates WI sync (fetch + parse + upsert) and exposes the
    SQL-`LIKE`-filtered list query used by the CLI and SDK.

- `src/doc3gpp/storage/repositories/wi_sql.py`
  - SQLAlchemy implementation that upserts into `wis` keyed by
    `(wi_id, tsg_short)` and lists rows with `LIKE` filters.

- `src/doc3gpp/models/wi.py`
  - WI domain fields (`wi_id`, `acronym`, `release`, `name`,
    `tsg_short`, `updated_at`).

## Extracted field summary

### Meetings

- `meeting_id`
- `name`
- `title`
- `location`
- `start_date`
- `end_date`
- `ftp_url`
- `start_doc`
- `end_doc`
- `tsg`

### TDocs

- `tdoc_id`
- `title`
- `meeting_id`
- `ftp_url`
- `source`
- `type`
- `status`
- `reservation_date`
- `uploaded_date`
- `cr_cat`
- `is_revision_of`
- `revised_to`
- `release`
- `spec`
- `version`
- `related_wis`
- `cr_num`
- `cr_pack`

### TDocFiles

- `id`
- `tdoc_id`
- `type`
- `file`
- `ftp_url`
- `uploaded_date`

### WIs

- `wi_id`
- `acronym`
- `release`
- `name`
- `tsg_short`

## Notes

- The current implementation supports meeting report scraping, TDoc list sync
  from the 3GPP portal `GenerateDocumentList.aspx` endpoint, auxiliary TDoc
  file discovery on the meeting FTP folders, CR cover-page extraction for
  synced CR TDocs, and WI DynaReport sync.
- `doc3gpp tdoc sync` uses the stored `Meeting.meeting_id` as the portal
  `meetingId` parameter and falls back to the configured
  `sync.tdoc_list_url_template` when the default endpoint changes.
- Work Item extraction is fully automated: each TSG's `WI DynaReport` page
  is fetched on demand by `doc3gpp wi sync --tsg <short>` and persisted
  to the `wis` table with `(wi_id, tsg_short)` as the upsert key.
- The meeting URL is derived from the TSG short name and not entered as a full URL.

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

- `updated_at` (datetime | None)
  - Set by persistence when the row is inserted or updated.

### Parsing rules

The meeting parser implementation is in `src/doc3gpp/parsers/calendar_parser.py`.

Important behavior:

- It skips rows with fewer than 6 cells.
- It skips entries where the meeting title ends with `CANCELLED`.
- It looks for an FTP link in the 6th table cell and, if absent, tries the 9th cell.
- It normalizes Unicode hyphen characters to `-` before parsing dates.

## TDoc and Document Source URLs

### TDoc records

TDocs are represented by the `TDoc` model with these extracted fields:

- `tdoc_id` (str)
  - The 3GPP document identifier, typically formatted like `R1-000001`.

- `title` (str)
  - The document title.

- `meeting` (str | None)
  - Optional meeting identifier associated with the document.
  - Example: `RAN3#100`.

- `url` (str | None)
  - Optional URL for the document.

### Planned TDoc sources

The project currently has a placeholder for additional TDoc extraction.

Typical TDoc source candidates include:

- `GenerateDocumentList.aspx` pages from the 3GPP site.
- FTP `tdoc_list` files inside meeting-specific subfolders.

These sources are noted in `docs/implementation-status.md` and are not yet fully implemented.

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
   - A revision TDoc eventually becomes the new official TDoc, with new TDoc IDs and is uploaded to `Docs/` or `Tdocs/`.
   - The TDoc list XLSX is updated over time to capture new TDocs and statuses such as revised, agreed, withdrawn, etc.

3. `Report/`
   - Stores meeting minutes and reports.
   - Draft and Final versions may be present.

4. `Inbox/`
   - If present, stores temporary documents during the meeting.
   - This often includes intermediate revision TDocs, new draft TDocs, liaison statements (LSs), and discussion papers created during the meeting.

5. `LSin/` and `LSout/`
   - Store incoming and outgoing liaison statements, respectively.

## Naming Conventions

### TSG short names

The CLI uses TSG short names such as:

- `R5` for RAN5
- `R1` for RAN1
- `CT1` for CT1
- `SA1` for SA1

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

Example:

```text
R5-260001
C6-260045
RP-260123
```

### 3GPP document path conventions

Meeting pages and document listings may use FTP-style links containing `ftp/` or `\ftp\` paths. The parser normalizes these to forward slashes.

## Source file references

- `src/doc3gpp/cli.py`
  - Builds the meeting URL from `--tsg`.

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
- `updated_at`

### TDocs

- `tdoc_id`
- `title`
- `meeting`
- `url`

## Notes

- The current implementation focuses on meeting report scraping.
- TDoc extraction is supported via manual CLI insertion and planned future automation.
- The meeting URL is derived from the TSG short name and not entered as a full URL.

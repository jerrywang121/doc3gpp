# 3GPP Knowledge Reference

This guide covers upstream URLs, naming, and source-field semantics. Runtime
sync/parse/cache behavior is in [processes.md](processes.md), configuration in
[config.md](config.md), and implementation navigation in [code-map.md](code-map.md).
Use the resource `schema` commands for current SQL column definitions.

> Last reviewed: 2026-08-13

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

- `tdoc_for`, `abstract`, `secretary_remarks`, `ls_to`, `ls_cc`, `original_ls`
  (str | None)
  - Additional portal-list columns. `tdoc_for` represents the upstream “For” field; the
    original text is not inferred from converted document content.

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

An outer-ZIP inventory is a different data product from this list or the
auxiliary-file ledger. It records all non-directory member names in original
order, including duplicates and unsupported extensions; see
[TDoc parsing](processes.md#tdoc-parsing).

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

## Spec Source URLs

3GPP specifications (TS/TR) are exposed through the DynaReport
infrastructure under two URL patterns — one page per TSG for the
list of owned specs, and one page per spec for the per-version
detail table.

### Spec list page (per TSG)

Pattern:

```text
https://www.3gpp.org/dynareport?code=Spec-{TSG}.htm
```

Where `{TSG}` is the TSG short name, such as `R5` for RAN5.

Example:

```text
https://www.3gpp.org/dynareport?code=Spec-R5.htm
```

The page renders a single `<table>` (CSS class `dsp-tsgwgxspec`)
that lists every spec owned by the TSG. Each row carries the
spec id, the type token (`TS` / `TR`), the title, the current
status, the radio technologies, and the initial release marker.

The CLI composes the URL from the uppercased `--tsg` value via
`scraping/spec_source.py::build_spec_list_url`, so `--tsg r5`,
  and `--tsg R5` map to the same page.

### Spec detail page (per spec)

Pattern:

```text
https://www.3gpp.org/dynareport?code={spec_id_no_dot}.htm
```

Where `{spec_id_no_dot}` is the dotted spec id with the dot
removed — e.g. `36.579-5` → `36579-5`. The slug has no
leading zero padding; the rule is purely `.replace(".", "")`.

Example:

```text
https://www.3gpp.org/dynareport?code=36579-5.htm
```

The page renders two tables. The first carries the header
metadata (status, title, radio technologies, related WIs,
etc.). The second carries one row per published version of
the spec — version, release, FTP URL, meeting reference,
upload date, and the related CR list. The CLI parses the
detail page into `(Spec, list[SpecVersion])` via
`parsers/spec_parser.py::parse_spec_detail`, then optionally
follows the per-version ETSI deliverable HTML (`wki_id`) and
the per-version CR list HTML (`version_id`) when the
follow-ups are enabled and their identifier/recency/cached-value checks pass.
Single-spec bootstrap uses the alternate
`https://www.3gpp.org/DynaReport/{spec_id_no_dot}.htm` URL to recover a missing
header before the normal detail pipeline.

### Conventions

- The slug `spec_id_no_dot` is the dotted `spec_id` with the
  literal dot removed — there is no zero-padding rule.
- Per-version follow-ups are disabled by default, preserving cached PDF/CR
  fields. `--per-version-details` enables them but does not bypass their
  availability/recency/cache checks. See [spec sync](processes.md#specifications-and-work-items).
- Metadata pages use DynaReport; the absolute `SpecVersion.ftp_url` is read
  from the detail page itself and points to the separate FTP ZIP corpus.

## Extracted Spec Fields

Headers are parsed from the list page; versions are parsed from
the detail page. The same `Spec` / `SpecVersion` domain models
flow through `SpecService` → `SQLAlchemySpecRepository` →
`specs` / `spec_versions` rows.

### `Spec` header fields

- `spec_id` (str) — PK; dotted form e.g. `36.579-5`.
- `type` (str) — `TS` or `TR`, parsed from the row's type icon.
- `title` (str) — full title from the list page; refined to the
  more accurate detail-page title after the per-spec pass.
- `status` (str | None) — current spec status (e.g. `Under change
  control`, `Draft`).
- `radio_tech` (str | None) — comma-joined radio technologies
  (e.g. `LTE, NR`).
- `initial_release` (str | None) — release marker from the list
  page (e.g. `Rel-15`).
- `tsg` (str) — owning TSG short name, uppercased and stamped
  on every header row as a FK into `tsgs.short_name`.
- `wis` (str | None) — comma-joined related WI ids from the
  detail page; stored as a flat text column (no join table).
- `rapporteurs` (str | None) — comma-joined company names from the
  detail page rapporteurs grid; `None` when absent.
- `last_synced_at` (datetime) — UTC timestamp of the last
  successful per-spec upsert.

### `SpecVersion` fields

- `spec_id` (str) — FK to `specs.spec_id` (composite PK with
  `version`).
- `version` (str) — version label exactly as shown on the page
  (e.g. `18.0.0`, `18.1.0`).
- `release` (str | None) — release marker (`Rel-18`, etc.).
- `ftp_url` (str) — absolute 3GPP FTP URL of the version zip.
- `pdf_url` (str | None) — ETSI "download as PDF" link, populated
  only when enabled follow-ups have a `wki_id`, a recent upload date,
  and an empty cached `pdf_url`.
- `meeting_id` (int | None) — meeting row that approved this
  version, when the page carries it.
- `meeting_name` (str | None) — same meeting's human-readable
  name.
- `upload_date` (date | None) — upload date of the version zip;
  drives the recency gate for the CR-list follow-up.
- `crs` (str | None) — comma-joined TDoc ids from the per-version
  CR list page. When follow-ups are enabled, refresh is gated by recent
  upload or an empty cached value; the default sync does not fetch it.
- `version_id` (int | None) — DynaReport `version_id` parameter
  used to fetch the CR list HTML.
- `wki_id` (int | None) — DynaReport `wki_id` parameter used to
  fetch the ETSI deliverable HTML.
- `last_synced_at` (datetime) — UTC timestamp of the last
  successful per-version upsert.

### Parsing rules

The spec parser implementation is in
`src/doc3gpp/parsers/spec_parser.py`.

- The list page is located by CSS class `dsp-tsgwgxspec`; an
  empty list returns `[]` (no error).
- The type token is read from the row's `<img>` `alt` first; if
  missing, it falls back to the anchor text. Both sources are
  normalized to `TS` / `TR` (everything else is dropped).
- The release marker is normalised via
  `release_from_version`, which biases on the longest
  matching `Rel-N` prefix and falls back to the original string
  when no prefix matches.
- The detail page is parsed into header + versions. The version
  table is the second `<table>` on the page; rows with fewer
  than 9 cells are skipped.

## RAN5 TTCN status snapshots (testcases)

RAN5 conformance testcase statuses are published as a single
`*TTCN CR Agreement Status …​.zip*` per week under a `History/`
folder. Each zip holds one `*.xlsx` workbook with one sheet per
test group; `doc3gpp testcase sync` resolves the latest file,
downloads it once, and upserts the headers + per-path statuses.

### History URL

```text
https://www.3gpp.org/ftp/tsg_ran/WG5_Test_ex-T1/TTCN/Reporting/TTCN_status/History/
```

`HISTORY_URL` is a module constant in
`src/doc3gpp/scraping/testcase_source.py` (not a setting).

### Filename grammar

Weekly files look like `TTCN CR Agreement Status 2024-wk32.zip`;
revised weeks carry a `_rev<N>` / `-r<N>` suffix (e.g.
`TTCN CR Agreement Status 2019-wk15_rev1.zip` → revision 1).
Ordering is the `(year, week, revision)` tuple — `select_latest`
picks the max.

Regex (`scraping/testcase_source.py`, case-insensitive):

```text
TTCN CR Agreement Status (\d{4})-wk(\d{2})(?:[_-]?(?:r|rev)(\d+))?\.zip$
```

Missing capture group 3 means revision `0`. Anchors whose decoded
basename matches no grammar are ignored; when nothing matches,
`select_latest` raises `TestcaseSourceNotFoundError`.

### Sheet / column map

The workbook holds up to six sheets (missing sheets warn + note,
never raise):

| Sheet | Group |
| --- | --- |
| `5G_TC_Status` | `5G` |
| `LTE_TC_Status` | `LTE` |
| `IMS_TC_Status` | `IMS` |
| `UTRA_TC_Status` | `UTRA` |
| `Positioning_TC_Status` | `POS` |
| `MCX_TC_Status` | `MCX` |

Header row is row 1. Common columns are located by normalised
header (`tc`, `title`, `ats`, `feature`, `release`, `ran wic`,
`part of`). Status pairs are fixed `(gcf_idx, ttcn_idx, path)`
0-based column indices per group (`STATUS_PAIRS` in
`parsers/testcase_parser.py`):

| Group | Pairs |
| --- | --- |
| `5G` | `(2, 3, FR1)`, `(2, 15, FR2)`, `(2, 22, FR1+FR2)` (shared GCF col 2) |
| `LTE` | `(2, 3, FDD)`, `(15, 16, TDD)` |
| `IMS` / `UTRA` / `POS` | `(2, 3, default)` |
| `MCX` | `(2, 3, IPCAN-4G)`, `(17, 18, EUTRA)`, `(26, 27, IPCAN-5G)`, `(37, 38, NR5GC)` |

Each pair's row-1 headers are validated (`GCF/PTCRB`-like in the
GCF slot, `TTCN-Status`-like in the TTCN slot); an invalid pair is
skipped with a warning naming sheet + Excel letter + actual text.
One `TestCaseStatus` is emitted per valid pair where at least one
of the two cells is non-empty (both-`None` pairs are dropped; a TC
with zero emitted pairs still yields its `TestCase` header).
`extract_workbook` picks the lexically-first `*.xlsx` member
(case-insensitive); zero members raise
`TestcaseWorkbookNotFoundError`.

### Fixed-spec rules

- `5G` rows always store `spec = 38.523-1` (ignores `part of`).
- `LTE` rows always store `spec = 36.523-1` (ignores `part of`).
- Every other group stores the row's `part of` cell verbatim
  (e.g. IMS `34.229-1`).

### Path vocabulary notes

- Stored MCX paths are the stripped vocab entries (`IPCAN-4G`,
  `EUTRA`, `IPCAN-5G`, `NR5GC`) — a leading `MCX-` prefix on the
  comparison value is stripped case-insensitively
  (`_strip_mcx_prefix`); the raw header text is never stored.
- The upstream request text lists the MCX paths as `IPCAN-4G,
  EUTRA, IOCAN-5Gm, NR5GC` — `IOCAN-5Gm` is read as a typo for the
  sheet's `MCX-IPCAN-5G` column and normalised to `IPCAN-5G`.
- Single-path groups (`IMS`, `UTRA`, `POS`) store the literal path
  `'default'` (never `NULL`): `path` is part of the composite PK
  and public JSON uses a nested array of `{path, gcf_ptcrb, ttcn_status}`.
- The live workbook contains cross-sheet duplicate TC ids (e.g.
  the same id on both the IMS and UTRA sheets). Identity is
  `(testcase_id, group)`, so each sheet keeps its own header row
  and its own group-scoped status rows — both groups coexist, and
  CLI/MCP show reads and `TestCaseService.get_all` return every matching group.
  The lower-level repository `get(id)` fallback selects one deterministic group;
  use explicit group/get-all APIs when identity matters.

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

## TTCN CR sidecar fields

R5 TTCN CR documents (`R5s\d{6}`) carry an additional overview + corrections
section on top of the standard CR cover page. The cover-page fields (spec,
cr_num, release, etc.) flow through the slim `tdoc_cr_cover_page` table; the
TTCN-specific slice lands in a dedicated sidecar.

The TTCN sidecar (`tdoc_cr_ttcn_details`) is keyed by the immutable
download `ftp_url` (one row per URL, FK `tdoc_id` → `tdocs.tdoc_id`,
`ON DELETE CASCADE`) and carries six overview columns plus a structured
corrections list:

| Column | Type | Source |
| --- | --- | --- |
| `testcase` | `String(256)` | Overview section, `Test case:` field. |
| `ue` | `String(256)` | Overview section, `UE:` field. |
| `ss` | `String(256)` | Overview section, `SS:` field. |
| `ats_version` | `String(64)` | Overview section, `ATS version:` field. |
| `ttcn_release` | `String(16)` | Derived from the `ats_version` prefix. |
| `test_suite` | `String(256)` | Overview section, `Test suite:` field. |
| `required_changes` | `LargeBinary(16 MB)` — gzip-compressed UTF-8 JSON | Corrections table: list of correction dicts (one per row). |

TTCN detection happens automatically in the parser. The structural
helper `is_ttcn_tdoc(tdoc_id)` (in `src/doc3gpp/parsers/cr/header.py`)
gates `TTCNCRParser` registration in the default parser registry
(`TTCNCRParser` is registered before the generic `CRParser`), so a TDoc
id matching `R5s\d{6}` routes to the TTCN-aware parser while every other
CR id falls through to the generic parser. Non-TTCN CRs produce no
`tdoc_cr_ttcn_details` row — the parser returns
`TDocCRParseResult(cover, ttcn=None)` and the service skips the sidecar
upsert. The table is registered in the main metadata and created by
`doc3gpp db init --scope main`; repositories also have compatibility probes for
some older databases. Changed-function aggregates are derived from corrections,
not an independent upstream field; see [parse flows](processes.md#prepare-convert-persist).

## Extraction semantics beyond source metadata

Word and presentation members are converted as described in
[processes.md](processes.md#tdoc-parsing). Presentations supply text, not CR
covers or inferred spec sections/tables. SQL `parsed`/inventory enrichment and
the distinction between converted sources and all ZIP members are application
concepts, not extra upstream spreadsheet columns.

For current field definitions use `<resource> schema`; for source symbols use
[the code map](code-map.md). This guide deliberately does not repeat either
complete inventory.

## ETSI LISTSERV archives

The archive importer reads public pages from the HTTPS LISTSERV host
`list.etsi.org`. Sync accepts an exact list ID without a hardcoded vocabulary;
the public RAN5 list `3GPP_TSG_RAN_WG5` is an example, not an allowlist.
The application's `listserv list` view reads a main-database
catalog: it includes maintained reference entries and IDs discovered by sync,
not an exhaustive upstream enumeration. A new ID is added only after a
successfully parsed page yields a message matching the requested date filters;
previously unknown zero-match pages are buffered for that sync and are not
persisted if no matching page registers the list. The listing exposes `list_id`,
`tsg`, and `description`; new discovery and legacy SQLite backfill preserve null
metadata rather than inventing descriptions or unsupported TSG codes.

R6 is the historical, closed RAN WG6 record: TSG short name `R6`, description
`(CLOSED) GSM/EDGE Radio Access Network (GERAN) and UMTS Radio Access (UTRA)`,
and no TSG URL. The LISTSERV catalog's predefined association is authoritative;
do not infer or synthesize a replacement URL for R6.

Archive week/month labels identify listing pages, not necessarily every
message's displayed date. A displayed week may have multiple actual page URLs,
and a listing can contain a message dated outside its label. Stored coverage is
therefore keyed by page URL and date predicate. For month-only pages, a message's
week is derived from its displayed calendar day. When available, the source
datetime and offset are retained; dates without a time carry date precision,
and an unknown time zone is not invented.

Message subjects and bodies can contain 3GPP TDoc IDs such as `R1-2610793`,
`RP-2601234`, or `R5s260009`. These are examples of recognized identifier
shapes, not a list of supported mailing lists. The parser extracts IDs from
the current body, subject, and attachment metadata before body truncation;
recognizable quoted prior-message content is excluded. See the
[LISTSERV process guide](processes.md#listserv-archive-flow) for parsing,
search indexing, date filters, and source constraints.

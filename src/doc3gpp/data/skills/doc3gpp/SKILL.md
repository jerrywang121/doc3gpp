---
name: doc3gpp
description: Use when querying or refreshing 3GPP meetings, TDocs, specifications, specification documents, or RAN5 testcases through the doc3gpp MCP server.
---

# Using doc3gpp MCP

doc3gpp exposes a local 3GPP database through MCP.
Use read tools to inspect cached data.
Queue sync or parse jobs when data is missing or stale.
Do not treat a queued job as completed work.

## Legacy Office dependencies

On the machine running the doc3gpp server, install the CLI with the extraction
extra (for example, `uv tool install --reinstall 'doc3gpp[cli,extract]'`; include
any other extras you want to retain). This enables Word and PowerPoint
extraction. Parsing legacy `.doc` also needs LibreOffice Writer; parsing `.ppt`
needs LibreOffice Impress. Modern `.docx` and `.pptx` do not need LibreOffice.

- **Linux (Debian/Ubuntu):** run `sudo apt update` and `sudo apt install libreoffice-writer libreoffice-impress`; verify with `command -v soffice` or `command -v libreoffice`. Other distributions may use different package names.
- **Windows:** install Writer and Impress using the [official LibreOffice installer](https://www.libreoffice.org/download/download-libreoffice/). Add its `program` directory (normally `C:\Program Files\LibreOffice\program`) to the user or system `Path`, open a new PowerShell window, and verify with `Get-Command soffice.exe`. For the current session only, use `$env:Path += ";C:\Program Files\LibreOffice\program"` before starting doc3gpp. Adjust the path for the actual installation and restart an already-running server to inherit it.

## 3GPP context for document searches

### Groups, specifications, and work items

The [3GPP structure](https://www.3gpp.org/3gpp-groups) is TSG -> working group (WG) -> specifications.
Technical Specification Groups (TSGs) include RAN, SA, and CT.
Working groups (WGs) are the primary operational units within TSGs. They handle the detailed technical work and produce specifications, reports, and TDocs for their respective areas. This tool use combined identifiers for TSGs+WGs for document searches, such as `R5` for RAN WG5, `C1` for CT WG1, etc.

Use `list_tsgs({})` to discover groups.
Use `get_tsg({"short_name":"R5"})` for RAN5 details and recent meetings.
The tool's TSG identifiers also represent WGs, such as `R5` and `C1`.
Use `list_specs({"tsg":"R5"})` to find specifications under that WG.

Technical work and associated TDocs relate to one or more Work Items (WIs) or Study Items (SIs).
A WI or SI can span multiple WGs, with one main WG owner.
Use `list_wis` with `tsg`, `name`, `acronym`, or `release` to find WI/SI records.
For example, use `list_wis({"name":"%MIMO%","release":"Rel-18"})`.
Check returned ownership and scope before restricting searches to one WG.
Use returned WI/SI identifiers to connect specifications, meetings, and TDocs.
Do not assume administrative TDocs contain WI/SI metadata.

### Meetings, TDocs, CRs, and specification versions

WGs generally hold quarterly meetings, with TDocs submitted for each meeting.
Some WGs also hold a `bis` meeting between quarterly meetings.
Check `list_meetings` and `get_meeting` for actual dates and FTP locations.

TDocs are contributions submitted to a WG meeting. There are many types of TDocs, including discussion, LS (liaison statement), WI status reports, CR (change request), etc. 
Each TDoc is identified by a unique document number, in the format `WG-YYNNNNN`, where `WG` is the working group acronym, `YY` is the last two digits of the year, and `NNNNN` is a 6-7 digit sequential number. e.g `R1-23012345`, `S3-2301234`.
RAN5 TTCN CR TDoc has prefix `R5s`, e,g, `R5s2301234`. 
RAN5 TTCN Workshop TDocs have prefix `R5w`, e.g, `R5w2301234`.
TSG Plenary packages have the form `RP-`, `SP-`, or `CP-` depending on the TSG, as mentioned later in the text.

A Change Request (CR) TDoc proposes changes to one specification at a specific release and version. e.g. v18.10.0 for Rel-18, main version 10, minor version 0.
WG-agreed CRs form packages for approval at the quarterly TSG plenary.
Packages group CRs by specification, release/version, and WI.

Specification main versions advance after the meeting cycle incorporates approved plenary packages. e.g. v18.1.0 -> v18.2.0. The minor version increments for minor correction after the meeting, e.g. agreed CRs missed during implementation, or editorial changes.
A new release of a spec are created after incorporated a CR associated with a WI of later release.
A spec maintains multiple releases in parallel, with each release advancing its own set of main and minor versions.
Exception is RAN5 test specs, where each of their testcase has it own release applicability. The spec release advances when testcases of new release feature are incorporated. Old releases of the same spec become obsolete.
Check stored spec versions and their associated CRs rather than assuming every meeting changes every specification.

### Special RAN5 TTCN meetings

The yearly RAN5 TTCN email meeting spans approximately week#50 of the previous year through week#50 of its named year.
TTCN CR TDocs target a test suite and a specific TTCN release, identified as `YYwk##`.
This release is distinct from the 3GPP specification release and version.
TTCN test suites release normally are updated two to three weeks after a quarterly RAN5 meeting.
RAN5 TTCN Workshops meeting discuss TTCN test models, typically four to six times per year.
Search email meetings and workshops separately from regular RAN5 meetings.

## Connect and run jobs

For a fresh setup with [uv](https://docs.astral.sh/uv/), install the CLI with
all runtime extras and initialize the databases:

```bash
uv tool install 'doc3gpp[all]'
doc3gpp db init --scope all
```

Configure a local stdio MCP client to launch the server as a foreground subprocess:

```bash
doc3gpp mcp
```

For a client using `mcpServers` JSON, use:

```json
{
  "mcpServers": {
    "doc3gpp": {
      "command": "doc3gpp",
      "args": ["mcp"]
    }
  }
}
```

Run initialization and the MCP client with the same environment and working
directory so they resolve the same configuration. The databases start without
synced 3GPP data; use the sync workflows below to populate them.

Alternatively, run `doc3gpp server start --no-open` and connect an HTTP MCP client to `http://127.0.0.1:13999/mcp/` by default.

The job queue is single-process: concurrent HTTP or stdio workers sharing a database can interfere with job ownership and recovery.

The MCP tool names below are literal.
The JSON objects are tool **arguments**, not HTTP request bodies.

Read tools return one text item that contains JSON.
The `get_tdoc_content` tool instead returns the content itself, and `get_skill({})` returns this complete skill as raw Markdown including its frontmatter.
Parse JSON before using IDs, versions, or links.

Sync, parse, and index tools return `{job_id, status, message, links}`.
Poll `get_job({"job_id":"..."})` until `status` is `succeeded`, `failed`, or `cancelled`.
Inspect `result`, `error`, and `log_tail`.
A `queued` or `running` job does not prove that rows are available.
If a job fails, report the error.
Do not present old cached data as fresh.

## Workflow by resource

### Meeting calendar

1. Use `list_meetings` with `tsg`, `name`, or `year` to find existing rows.
2. Use `get_meeting` with a numeric `meeting_id` for details.
3. Use `sync_meetings({"tsg":"R5"})` to fetch and parse that TSG's calendar.
4. List the meetings again after synchronization.
5. Use `get_meeting` to retrieve the meeting ID and FTP URL.

There is no separate meeting-parse tool.

### Meeting TDocs

1. Use `list_tdocs` with filters such as `meeting_id`, `tdoc_id`, `title`, or `spec`.
2. Use `get_tdoc` with `tdoc_id` or `ftp_url` for details.
3. Use `sync_tdocs({"meeting_id":260013})` after the meeting exists.
   Alternatively, use `sync_tdocs_by_meeting({"meeting":"RAN5#106"})`.
4. Use `parse_tdocs({"filter":{"tdoc_id":"R5-260013"}})` for document extraction.
   For a direct 3GPP FTP URL, use `parse_tdoc_url({"url":"https://www.3gpp.org/ftp/..."})`.
5. Use `get_tdoc` for cover/extract metadata.
6. Use `get_tdoc_content({"tdoc_id":"R5-260013"})` for cached markdown, if available.
7. Use `search_tdoc` for indexed content and filters.

`get_tdoc_content` returns full Markdown without pagination; a large response may exceed the MCP client's limits.
Select one ZIP member with `source_file`, use `search_tdoc` for snippets, or save it locally with `doc3gpp tdoc show --tdoc <id> --format raw --source-file '<path>' --output annex.md`.
TDoc parsing skips the entire source if it exceeds `[tdoc_parse] max_tdoc_size_kb` (default 1000 KiB) or the supported ZIP members exceed `max_uncompressed_size_kb` combined (default 100 MiB); inspect `get_job` and raise the limits before retrying.
An individual annex conversion failure instead appears in `skipped_sources` in `get_tdoc_content` JSON and leaves extraction incomplete.

### TDoc revisions, reviews, and supporting ZIPs

`parse_tdocs` (or `doc3gpp tdoc parse --tdoc <id>`) parses only the main ZIP in `tdocs.ftp_url`.
It does not automatically parse separate TDoc revisions, MCC review or supporting ZIPs.
Examples include MCC160 review comments and supporting prose draft CRs, and TDoc revisions.

Associated documents can be outside the main TDoc ZIP and absent from the meeting's TDoc list.
TDoc revisions during a meeting often reside in the meeting FTP location's `inbox` subfolder, with suffix `r1`, `r2`, etc.
TTCN MCC reviews and supporting prose draft CRs can reside in its `review` subfolder.
Registered associated files appear in `get_tdoc` details or the CLI `tdoc show` Files section.
Use returned URLs rather than constructing filenames from these folder conventions.

To parse and inspect a separate revision, review, or supporting ZIP:

1. Call `get_tdoc({"tdoc_id":"R5s261234"})` (or run `doc3gpp tdoc show --tdoc R5s261234`).
2. Find the target attachment entry in `files` (`type`: `review`, `support`, or `revision`).
3. Copy its `ftp_url` and build the canonical `https://www.3gpp.org/ftp/<ftp_url>` URL when needed.
4. Parse that exact URL with `parse_tdoc_url`:
   ```text
   parse_tdoc_url  {"url":"https://www.3gpp.org/ftp/.../Review/R5s261234_MCC160Comments.zip"}
   get_job         {"job_id":"<parse-url job_id>"}  # poll until terminal
   ```
   CLI equivalent:
   ```bash
   doc3gpp tdoc parse --from-url \
     'https://www.3gpp.org/ftp/.../Review/R5s261234_MCC160Comments.zip'
   ```
5. Replace the example URL with the actual URL from `get_tdoc` (`files[].ftp_url`).
6. Inspect the extracted snapshot by URL, not by `tdoc_id`:
   - Metadata and TTCN change details (including `mcc160_comment`): `get_tdoc({"ftp_url":"<review-or-support-url>"})` (or `doc3gpp tdoc show --ftp-url <review-or-support-url>`).
   - Extracted markdown content: `get_tdoc_content({"ftp_url":"<review-or-support-url>"})`.

Parsing requires a supported, recognizable document inside the ZIP.
Not every supporting ZIP is parseable.
When the TDoc exists in the database, a successful 3GPP-URL parse is stored under that attachment ZIP's `ftp_url`, separate from the main TDoc extraction.

### Specifications

1. Use `list_specs` with `tsg`, `spec_id`, `title`, or `parsed`.
2. Use `get_spec` with a dotted `spec_id`.
3. Use `sync_specs({"tsg":"R5"})` or `sync_specs({"spec_id":"38.331"})` with exactly one selector.
4. Paginate versions with `limit`/`offset` when necessary.

The `force` parameter bypasses the sync interval.
The `per_version_details` parameter fetches extra PDF/CR-list details.
The `get_spec` response contains versions, release markers, ZIP URLs, and per-version `parsed` state.

### Specification documents

1. Use `get_spec` to identify a version with a ZIP URL.
2. Use `parse_spec_docs({"spec_id":"38.331","version":"18.5.0"})` to download if necessary and parse one specification.
   Omit `version` to resolve a suitable stored version.
   Optionally, constrain that selection with `release`.
3. Check that release numbers align when you supply both `release` and `version`, for example `Rel-18` with `18.5.0`.
4. Use `get_spec_toc({"spec_id":"38.331","version":"18.5.0"})` for the TOC.
5. Use `search_spec_docs` with `spec_id`, `version`, `text` or `semantic`, `sections`, and `tables` as needed.
6. Use '%' wildcards to match section or table titles.
7. Append '%' to section or table numbers to match subsections or subtables.
   Examples include `"sections":"5.3.2%"` and `"tables":"6.1.1.2.3-%"`.

The `get_spec_toc` tool requires prior parsing of the exact `(spec_id, version)`.
The `force` parameter repeats the download and parsing.

### Specification ZIP annexes and supporting files

The ZIP for a specification version can contain multiple files, including annexes and presentations.
`get_spec({"spec_id":"38.331"})` returns `zip_files` on each version: the ordered outer-ZIP member paths, preserving duplicates and all file extensions.
`null` means the inventory is unknown; `[]` means the ZIP was inspected and contains no files.
The inventory is collected during an actual spec-document parse, not spec sync, and is distinct from parsed content.

Spec-document parsing converts `.docx`, `.doc`, `.pptx`, and `.ppt` ZIP members into searchable chunks.
Word files contribute TOC entries; presentations do not contribute TOC entries or section/table metadata.
Presentation extraction is text only: it does not interpret images, charts, or OCR.
Other ZIP members, including PDFs, spreadsheets, images, and nested ZIPs, can appear in the inventory but are not supported for parsing or searching.
The MCP tools do not return those original attachments; use the version's ZIP URL if the original files are needed.
Use the legacy Office dependencies above for parsing `.doc` and `.ppt`.

For a complete presentation annex, copy its exact path from the selected version's `zip_files` and call:

```text
get_spec_doc_content {"spec_id":"38.331","version":"18.5.0","source_file":"<exact ZIP member path>.pptx"}
```

The response is JSON with `spec_id`, `version`, `source_file`, and full `markdown`.
This SQL-only read does not download or parse; a missing presentation requires a successful parse of that version.
`get_spec_doc_content` supports presentations only, not Word or arbitrary attachments; use `get_spec_toc` and `search_spec_docs` to find parsed Word text.
Search returns chunks, not necessarily a complete source file.

### RAN5 testcases

1. Use `list_testcases` with `testcase` (ID pattern), `title`, `spec`, `group`, `status` (TTCN), or `gcf_status` (GCF/PTCRB).
2. Use `sync_testcases({})` to fetch and parse the latest RAN5 status snapshot in one job.
3. Use `{"force":true}` to repeat download and parsing for a previously recorded snapshot.
4. Use `get_testcase({"testcase_id":"<id>"})` to retrieve all stored groups.
5. Pass `group` to retrieve one group.

There is no separate testcase parse tool.
Each result contains nested `statuses` entries with `path`, `gcf_ptcrb`, and `ttcn_status`.

### Filters, pagination, and search indexes

Filters on list/search tools generally accept SQL-LIKE `%` wildcards, for example `title:"%handover%"`.
Plain values match exactly.
Testcase `group` requires an exact match: `5G`, `LTE`, `IMS`, `UTRA`, `POS`, or `MCX`.

The `list_meetings`, `list_tdocs`, `list_specs`, and `list_testcases` tools default to 50 rows.
Use `limit` and `offset` to paginate.

The `search_tdoc` and `search_spec_docs` tools select these search modes:
- FTS5 with `text`.
- Vector search with `semantic`.
- Hybrid search with both.
- Filter-only search with neither.

### Search result provenance and coverage

`search_spec_docs` returns each matching chunk's full `text` and exact,
case-preserving ZIP member `source_file` in every mode.

Each `search_tdoc` row contains `matched_documents`.
This list can be empty for metadata-only or filter-only results.
Each entry includes the source ZIP URL (`ftp_url`), member path (`source_file`),
`document_id`, score, FTS previews, and `matched_chunk`.
The chunk is `{chunk_id, chunk_index, text}` or `null`.
Use the entry's URL and path for a source-specific read.

TDoc FTS5 searches metadata and SQL-stored content documents.
These documents include CR annexes and main documents and annexes of WID, LS,
and discussion TDocs.
FTS5 searches whole documents and indexes every extracted `.ppt` and `.pptx`
text chunk, regardless of the semantic cap.
A whole-document match can have `matched_chunk: null`.
This can occur when query terms span chunks or a Word match has no source chunk.



## Example: investigate a meeting and its spec text

These are successive MCP tool calls.
Replace example IDs with values from earlier reads.
After **each** job-producing call, poll `get_job` to `succeeded` before proceeding.

```text
sync_meetings       {"tsg":"R5"}
get_job             {"job_id":"<meeting-sync job_id>"}  # poll until terminal
list_meetings       {"tsg":"R5","name":"%RAN5%","limit":10}
get_meeting         {"meeting_id":<meeting_id from list_meetings>}
sync_tdocs          {"meeting_id":<meeting_id>}
get_job             {"job_id":"<tdoc-sync job_id>"}     # poll until terminal
list_tdocs          {"meeting_id":<meeting_id>,"title":"%handover%"}
parse_tdocs         {"filter":{"tdoc_id":"<tdoc_id from list_tdocs>"}}
get_job             {"job_id":"<tdoc-parse job_id>"}    # poll until terminal
get_tdoc            {"tdoc_id":"<tdoc_id>"}
search_tdoc         {"text":"handover","meeting_id":<meeting_id>}
sync_specs          {"spec_id":"38.331"}
get_job             {"job_id":"<spec-sync job_id>"}    # poll until terminal
get_spec            {"spec_id":"38.331"}
parse_spec_docs     {"spec_id":"38.331","version":"<ZIP-backed version from get_spec>"}
get_job             {"job_id":"<spec-doc-parse job_id>"} # poll until terminal
get_spec_toc        {"spec_id":"38.331","version":"<parsed version>"}
search_spec_docs    {"text":"handover","spec_id":"38.331","version":"<parsed version>", "sections":"5.2.2.3%"}
```

For a RAN5 testcase status question, use this independent path.
The list filter is `testcase`.
The detail selector is `testcase_id`:

```text
sync_testcases      {}
get_job             {"job_id":"<testcase-sync job_id>"} # poll until terminal
list_testcases      {"spec":"38.523-1","group":"5G","limit":10}
get_testcase        {"testcase_id":"<testcase_id from list_testcases>","group":"5G"}
```

The `list_testcases` tool returns one row per testcase/group with nested path statuses.
The `get_testcase` tool always returns an array, even when `group` selects one row.
Omit `group` to inspect every stored group for an ID.
Status filters match if **any** path has the specified value.

Synchronization can skip processing when a previous parse includes the latest source.
Inspect the job's `result` before interpreting an empty list.

If a list is empty, check that the upstream sync job succeeded.
Check that the filters match the returned IDs.
TDoc synchronization needs a stored meeting.
Specification-document parsing needs a stored specification/version with a ZIP link.
A TOC or body-content cache miss means parsing did not yet place the relevant document in that cache.

Use the returned rows, search hits, and source/version identifiers when answering.
Do not infer document contents or testcase status from a title alone.

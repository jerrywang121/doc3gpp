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

## Auto-sync behavior

The read commands `meeting list`, `tdoc list`, `tdoc show`, and database-mode
`tdoc parse` can automatically trigger internal syncs before querying. This is
controlled by the `sync.auto_sync` setting:

```toml
[sync]
auto_sync = false   # default; set to true to enable
```

When enabled:

- `meeting list` may internally call the equivalent of `meeting sync --tsg`
  for the candidate TSG(s) inferred from `--tsg` or `--tdoc`.
- `tdoc list` may internally call both `meeting sync` (for inferred TSGs) and
  `tdoc sync --meeting-id` (for inferred meeting IDs).
- `tdoc show` delegates to the same auto-sync logic as `tdoc list --tdoc`.
- `tdoc parse` (without `--from-path` or `--from-url`) delegates to the same
  auto-sync logic as `tdoc list` with the same filters.

The internal sync calls always respect the existing skip rules
(`meeting_sync_interval`, `tdoc_list_closed_window`, `tdoc_list_sync_interval`).
They never bypass those rules and they cannot be forced from the read
commands. When a sync actually runs, the command prints
`[auto-sync] <reason>`; when it is skipped, the skip reason is also printed
with the same prefix. Sync failures are logged as warnings and do not abort
the parent read command.

Direct-mode `tdoc parse` (`--from-path` or `--from-url`) never triggers
auto-sync.

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
- Seeds the `tsgs` table with the canonical 3GPP TSG list (19 rows). Existing
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
- --force / -f: bypass the sync interval skip rule.

Behavior:

- Builds the 3GPP meeting report URL from the TSG short name.
- Fetches the full HTML calendar page without date-window filtering.
- Parses every meeting row and upserts it into the `meetings` table.
- Stamps the canonical (`--tsg` upper-cased) short name onto every
  parsed `Meeting` so the persisted `meetings.tsg` FK column is
  populated. The parent row in `tsgs` must exist (auto-seeded on a
  fresh install); sync without a matching `tsgs` row will fail the FK
  constraint.
- Updates `tsgs.meeting_last_sync` for the synced TSG.
- Skips the sync when `tsgs.meeting_last_sync` is newer than
  `Settings.sync.meeting_sync_interval` (default `24h`).
- Prints `Meeting sync complete: N meeting row(s) stored` or a skip
  reason; exits `0` on skip.

### doc3gpp meeting list

Purpose:

- List recent meeting rows from database.

Options:

- --limit: number of rows.
  - default: 20
- --offset: number of rows to skip before applying `--limit` (pagination).
  - default: 0
- --tsg: SQL LIKE pattern to filter the `meetings.tsg` FK.
  - default: none
  - supports `%` and `_` wildcards; input is upper-cased before the
    lookup so lowercase patterns match the canonical stored values.
    Rows whose `tsg` is `NULL` (e.g. imported before the column was
    added) are excluded.

Additional options:

- --name: SQL LIKE pattern to filter meeting `name` (supports `%` and `_`).
- --location: SQL LIKE pattern to filter meeting `location` (supports `%` and `_`).
- --year: filter meetings by the year of the `end_date`.
- --tdoc: find the meeting containing the given TDoc. Accepts a
  9-character CR-shape id (`R5-260013`, `R5s260009`, `R5w260013`,
  etc.); the value is validated against
  `cli_filters.TDOC_ID_RE` (`[RSC][1-9][-sw]\d{6}`) before any
  database lookup and rejected with a clear error on bad shape.
  A meeting matches when its `start_doc` 3-char prefix equals the
  TDoc's prefix and its 6-digit `start_doc` number is `≤` the
  TDoc number; if `end_doc` is non-null the same prefix + number
  `≥` rule applies. Meetings without a `start_doc` never match.
  Prefix match is case-insensitive (`r5s`, `R5S`, `r5S` all match).
- --fields: comma-separated list of fields to include in output, or `all`.
- --format: see "Common list output options" below (table | json | markdown).
- -o, --output: write the result to a file instead of stdout.

Default output fields:

- `meeting_id`, `name`, `location`, `start_date`, `end_date`, `ftp_url`, `start_doc`, `end_doc`

Note: by default `title` and `tsg` are excluded to keep the listing
compact; use `--fields all` to include every available column (or
`--fields tsg` to add just the owning TSG).

Auto-sync: when `sync.auto_sync` is enabled, `meeting list` may internally
sync the meeting calendar for the TSG derived from `--tsg` or `--tdoc`
before listing. See [Auto-sync behavior](#auto-sync-behavior) above.

Examples:

- List recent R5 meetings (default fields):

```bash
doc3gpp meeting list --tsg r5
```

- Match every TSG short name starting with `R` using SQL LIKE:

```bash
doc3gpp meeting list --tsg 'R%'
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

- Find the meeting containing a given TDoc:

```bash
doc3gpp meeting list --tdoc R5-260013
```

## tdoc Commands

### doc3gpp tdoc sync

Purpose:

- Download each meeting's TDoc-list XLSX from the 3GPP portal
  (`GenerateDocumentList.aspx?meetingId={meeting_id}`) and persist the
  rows. Auxiliary TDoc files (revisions, review packs, supporting
  documents) are still scanned from the meeting's FTP folders. With no
  selector, refresh every meeting currently tracked in the `tdocs`
  table.

Options:

- --meeting-id: numeric meeting ID from the meetings database (see `doc3gpp meeting sync`).
- --meeting: exact meeting name from the meetings database (see `doc3gpp meeting sync`).
- --force / -f: bypass the sync skip rules for every meeting in the run.

Notes:

- Exactly one of `--meeting-id` or `--meeting` may be provided. Passing
  both is a `BadParameter` error.
- When **neither** selector is provided, every distinct non-null
  `meeting_id` currently stored in the `tdocs` table is synced
  individually. This is the "bulk" mode.

Behavior (single-meeting: `--meeting-id` or `--meeting`):

- Loads the meeting record from storage.
- Resolves the stored FTP URL from that meeting.
- Skips the sync when any of the following is true (checked in order):
  1. `meetings.end_date` is older than
     `Settings.sync.tdoc_list_closed_window` (default `90d`).
  2. `meetings.tdoc_list_last_sync` is newer than
     `Settings.sync.tdoc_list_sync_interval` (default `30m`).
- If none of the skip rules apply, downloads the meeting's TDoc-list XLSX
  from the URL built by `Settings.sync.tdoc_list_url_template` (default
  `https://portal.3gpp.org/ngppapp/GenerateDocumentList.aspx?meetingId={meeting_id}`),
  parses and persists TDoc rows, then updates
  `meetings.tdoc_list_last_sync`.
- Prints `TDoc sync complete: N TDoc row(s) and M auxiliary TDoc file(s) stored`
  or a skip reason; exits `0` on skip.
- `MeetingNotFoundError` and `MeetingMissingFtpUrlError` are converted to
  `BadParameter` with the original message preserved.

Behavior (bulk: no selector):

- Reads the distinct `meeting_id` values from the `tdocs` table
  (orphaned TDocs with `meeting_id IS NULL` are excluded).
- For each meeting, resolves the record via `MeetingService.get_by_id` and
  runs the same per-meeting sync path — closed window and sync interval
  checks apply individually. `--force` bypasses both for every meeting
  in the run.
- Prints a single summary block (no per-meeting lines):
  ```
  TDoc bulk sync: <N> meeting(s) processed
    Synced:  <S>
    Skipped: <K>
    Failed:  <F>
  Failed meetings:
    meeting_id=<id>  <ErrorClass>  <message>
  ```
- A missing meeting row or missing FTP URL is recorded in the `Failed`
  section and does not abort the sweep. Iteration continues so a partial
  sweep still completes.
- Empty discovery (no tracked meetings) prints
  `No stored meetings with TDocs found; nothing to sync.` and exits `0`.
- Exit code is `1` only when **every** meeting failed (`F == N`); otherwise `0`.

Examples:

```bash
# Bulk: refresh every tracked meeting
doc3gpp tdoc sync
doc3gpp tdoc sync --force              # ignore the per-meeting skip rules

# Single-meeting: unchanged behaviour
doc3gpp tdoc sync --meeting-id 85434
doc3gpp tdoc sync --meeting "R5--TTCN Workshop#74"
```

### doc3gpp tdoc list

Purpose:

- List recent stored TDoc records with optional filters.

Options:

- --limit: maximum number of rows.
  - default: 20
- --offset: number of rows to skip before applying `--limit` (pagination).
- --tdoc: SQL `LIKE` pattern on `tdoc_id` (e.g. `R5s26%` for every TDoc
  in the RAN5 2026 cycle, or `R5s260009` for an exact id). Accepts the
  same value grammar as `tdoc parse --tdoc` (see
  [Filter syntax](#filter-syntax) below) — the literal tokens
  `null` / `not-null`, a `!pattern` form, or any other text applied as
  a LIKE pattern with `%` / `_` wildcards. The flag is singular;
  combine with other filters instead of passing it twice.
- --meeting: SQL LIKE pattern to filter by meeting name (supports % and _).
- --meeting-id: exact match on the parent meeting's numeric ID (see
  `doc3gpp meeting list`). Combinable with `--meeting`; rows must satisfy
  both predicates.
- --source: filter by TDoc source/contributor.
- --spec: filter by technical specification.
- --wi: filter by related work items.
- --title: filter by TDoc title.
- --cr-cat: filter by CR category (`cr_cat`).
- --status: filter by TDoc status.
- --type: filter by TDoc type.
- --revision-of: filter by `is_revision_of`.
- --revised-to: filter by `revised_to`.
- --ftp-url: filter by `ftp_url`.
- --release: filter by `release` (e.g. `Rel-18`).
- --version: filter by `version` (e.g. `18.1.0`).
- --cr-num: filter by `cr_num` (e.g. `3790`).
- --cr-pack: filter by `cr_pack` (e.g. `RP-220001`).
- --uploaded-date: filter by `uploaded_date`. See
  [Filter syntax](#filter-syntax) below for the
  accepted forms (including date comparisons).
- --fields: comma-separated list of fields to include in output, or `all`.
- --format: see "Common list output options" below (table | json | markdown).
- -o, --output: write the result to a file instead of stdout.

Default output fields:

- `tdoc_id`, `meeting_name`, `title`, `source`, `type`, `status`, `cr_cat`, `spec`, `version`, `related_wis`

Auto-sync: when `sync.auto_sync` is enabled, `tdoc list` may internally sync
meeting calendars and TDoc lists for the TSG(s) and meeting ID(s) inferred
from `--tdoc`, `--meeting`, and `--meeting-id` before listing. See
[Auto-sync behavior](#auto-sync-behavior) above.

Examples:

- Match every RAN5 2026 TDoc (`--tdoc` LIKE pattern):

```bash
doc3gpp tdoc list --tdoc 'R5s26%'
```

- Exclude all RAN5 TDocs from the result (`--tdoc` NOT LIKE):

```bash
doc3gpp tdoc list --tdoc '!R5s%'
```

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
- `--format {table,json,markdown,raw}`: output format. Default
  `table`, unless overridden by `[output].format` in the resolved
  TOML config (`output.format`).
  - `table` (default): the historical line-oriented dump — `[TDoc]`
    section followed by a single `[Extracted Details]` block from
    `tdoc_cr_details` (URL-keyed on `tdoc.ftp_url`) and, when the
    TDoc is a TTCN CR and a matching `tdoc_cr_ttcn_details` row
    exists, an extra `[TTCN Details]` block with the six overview
    fields plus a `required_changes: N item(s)` summary line. The
    `parser_version` and `details` lines are gone — `extracted_at`
    is sourced from `tdoc_extracts` at the same URL and rendered
    as `extracted_at: -` when the row is missing. Every
    `tdoc_files` row matching `tdoc_id` renders under an
    `[Auxiliary Files]` block with the four informative fields
    (`type`, `file`, `ftp_url`, `uploaded_date`); the autoincrement
    `id` and the `tdoc_id` match key are dropped because the parent
    `[TDoc]` block already carries the match key. When the TDoc has
    no auxiliary files, the `[Auxiliary Files]` header is omitted
    and a placeholder line points the reader at `tdoc sync` (the
    flow that populates `tdoc_files`). Long free-text fields are
    truncated to 200 characters with an ellipsis. Matches every
    prior release on the visible line positions.
  - `json`: one JSON object with the following top-level keys
    (optional keys are **omitted**, not emitted as `null`, when
    no corresponding row exists):
    - `tdoc` — every `TDoc` field, with `date` / `datetime` values
      ISO-8601-encoded.
    - `cover` — the slim cover-page dataclass keyed by
      `tdoc.ftp_url` (omitted when no `tdoc_cr_details` row).
    - `ttcn` — the TTCN sidecar dataclass keyed by
      `tdoc.ftp_url`, populated only for TTCN CRs (omitted
      otherwise or when no row exists).
    - `extracted_at` — ISO-8601 cache-extract timestamp from the
      `tdoc_extracts` row at `tdoc.ftp_url` (omitted when no row
      exists). Sits at the top level rather than nested under
      `cover` / `ttcn` because neither detail table carries its
      own timestamp.
    - `files` — array of every `TDocFile` row matching `tdoc_id`
      (auxiliary revisions / reviews / support files). Every
      dataclass field of `TDocFile` is serialised (`id`,
      `tdoc_id`, `type`, `file`, `ftp_url`, `uploaded_date`).
      Omitted when the TDoc has no auxiliary files.
  - `markdown`: a Markdown document — `# TDoc` heading, bullet list
    of TDoc fields under `## Metadata`, then `## Extracted Cover
    Details` (slim cover-page fields when present) and `##
    TTCN Details` (TTCN overview fields + `required_changes` as a
    JSON fenced block when the TDoc is a TTCN CR and the sidecar
    exists). When no extract row is present, a single `_No
    extracted details; run \`doc3gpp tdoc parse --tdoc <id>\`
    first._ placeholder is emitted. Every `tdoc_files` row
    matching `tdoc_id` renders under `## Auxiliary Files` with one
    nested bullet group per file (`type`, `file`, `ftp_url`,
    `uploaded_date`); when no files exist, a `_No auxiliary
    files; run \`doc3gpp tdoc sync\` first if you haven't synced
    this meeting yet._` placeholder keeps the document skeleton
    stable. Long fields are **not** truncated in this mode.
  - `raw`: the converted `.docx` markdown body (the artefact the CR
    parser consumes) for CR-type TDocs. The markdown is loaded from
    `{cache.dir}/markdown/<cache_file>` where `cache_file` is the
    `tdoc_extracts.cache_file` column value derived from
    `tdoc.ftp_url` via `derive_cache_file()`. Requires `python-docx`
    (`pip install doc3gpp[extract]`) when the cache is cold; surfaces a
    friendly error otherwise. When no cached markdown exists, this
    format triggers a fresh `TDocCrService.extract()` to populate the
    cache + DB. Non-CR-type TDocs are rejected with a friendly error.
    If the markdown cache file is missing or unreadable, the error
    message is:
    `Markdown cache for TDoc '<tdoc>' is empty or unreadable (cache_file: <cache_file>, cache_dir: <dir>)`.
- `-o PATH`, `--output PATH`: write the result to `PATH` instead of
  stdout. Pass `-` for stdout (the historical default).

Behavior:

- When `sync.auto_sync` is enabled, runs the same internal sync logic as
  `tdoc list --tdoc <id>` before the lookup.
- Looks up the row in the `tdocs` table via a PK lookup.
- On miss: raises `BadParameter` listing the requested id and pointing
  to `doc3gpp tdoc sync` / `doc3gpp tdoc list`.
- On hit, every TDoc row resolves a single primary `ftp_url` for the
  show lookup. The CLI performs three URL-keyed reads against that
  primary URL (no per-revision fan-out for the slim schema):
  - `tdoc_cr_details` cover row via
    `SQLAlchemyTDocCrRepository.get_by_url(tdoc.ftp_url)`.
  - `tdoc_extracts` metadata row via
    `SQLAlchemyTDocCrRepository.get_extract_meta_by_url(tdoc.ftp_url)`
    — this is the sole source of the displayed `extracted_at`.
  - `tdoc_cr_ttcn_details` sidecar via
    `SQLAlchemyTDocCrTtcnRepository.get_by_url(tdoc.ftp_url)`,
    gated on `is_ttcn_tdoc(tdoc.tdoc_id)` so non-TTCN CRs never hit
    the sidecar table.
  - A single `tdoc_id`-keyed read on `tdoc_files` via
    `SQLAlchemyTDocFileRepository.get_for_tdoc_id(tdoc.tdoc_id)`,
    ordered by `(type, ftp_url) ASC` so the output groups by
    category (revision / review / support). Runs unconditionally —
    a TDoc may have auxiliary files even when the cover has not
    been extracted.
- The bundled `TDocShowRecord(tdoc, cover, ttcn, extracted_at,
  files)` is rendered by `table` (default) / `json` / `markdown`
  to four separate sections (`cover`, optional `ttcn` block, the
  standalone `extracted_at` line, and the auxiliary files block
  or placeholder). Optional keys are **omitted** (not emitted as
  `null`) in the JSON payload when the corresponding row is
  absent. The legacy `details` / `parser_version` fields no longer
  appear in any output.
- `raw` bypasses the DB-row render entirely and reads the converted
  markdown from the cache. When the cache is cold, the extract
  pipeline runs (download zip, render markdown, persist across
  `tdoc_cr_details` + the optional `tdoc_cr_ttcn_details` +
  `tdoc_extracts` in three independent upserts) before the result is
  emitted.

Examples:

```bash
# Show the TDoc + any extracted CR cover-page fields (default table).
doc3gpp tdoc show --tdoc R5s260009

# JSON for downstream tooling.
doc3gpp tdoc show --tdoc R5s260009 --format json

# Markdown for archival / review.
doc3gpp tdoc show --tdoc R5s260009 --format markdown -o R5s260009.md

# Converted .docx markdown (raw). Triggers an extract on cache miss.
doc3gpp tdoc show --tdoc R5s260009 --format raw -o R5s260009.md
```

### doc3gpp tdoc parse

Purpose:

- Download a TDoc zip from the 3GPP FTP, render its `.docx` body to
  markdown, parse the cover-page fields, and persist the result
  across the slim cover-page table (`tdoc_cr_details`), the cache
  metadata table (`tdoc_extracts`), and — when the parser
  recognises a TTCN CR — the new `tdoc_cr_ttcn_details` sidecar.
  TTCN detection is automatic: the parser returns a
  `TDocCRParseResult(cover, ttcn)` and `TDocCrService` writes the
  sidecar only when `ttcn is not None` (i.e. when
  `is_ttcn_tdoc(tdoc_id)` and the cover-page document contains a
  TTCN overview + corrections section). The CLI surface itself is
  unchanged — only the storage fan-out is wider. Wraps the Phase 6
  `TDocCrService.extract_many` for batch CLI use.

Every flag is a **filter** — the candidate set is the intersection of
every supplied predicate, and CR-type is the implicit default (the
extractor only handles CR TDocs). The two batch-style selectors from
earlier releases (`--tdoc-id` as an integer PK and the mutual
exclusivity with `--meeting-id`) have been removed; `--tdoc` is now a
LIKE pattern on `tdoc_id` that can be freely combined with
`--meeting-id` and every text or date filter. At least one filter is
required — an empty call exits non-zero before any DB lookup.

Auto-sync: when `sync.auto_sync` is enabled, the database-mode path
(without `--from-path` or `--from-url`) triggers the same internal sync
logic as `tdoc list` with the same filters before building the candidate
set. Direct-mode parse (`--from-path` or `--from-url`) never triggers
auto-sync. See [Auto-sync behavior](#auto-sync-behavior) above.

Options:

- `--tdoc PATTERN`: SQL `LIKE` pattern on `tdoc_id`. Accepts a
  literal id (`R5s260009` for an exact match), a pattern with `%`
  / `_` wildcards (`R5s26%`, `R5_260001`), the literal tokens
  `null` / `not-null`, or a `!pattern` form. The flag is singular
  — combining multiple `--tdoc` values is not supported; build the
  pattern instead.
- `--meeting-id N`: exact integer match against `meetings.meeting_id`.
  Combinable with every other filter; rows must satisfy every
  supplied predicate.
- `--meeting PATTERN`: SQL `LIKE` pattern on `meetings.name` (joins
  the `meetings` table to filter the candidate set).
- `--status PATTERN`: filter on `status`.
- `--cr-cat PATTERN`: filter on `cr_cat` (CR category — `F`, `B`,
  etc.). Renamed from `--cat` to mirror the column name.
- `--spec PATTERN`: filter on technical specification (`spec`).
- `--wi PATTERN`: filter on `related_wis`.
- `--revision-of PATTERN`: filter on `is_revision_of`.
- `--revised-to PATTERN`: filter on `revised_to`.
- `--title PATTERN`: filter on `title`.
- `--ftp-url PATTERN`: filter on `ftp_url`.
- `--source PATTERN`: filter on source / contributor.
- `--type PATTERN`: filter on document `type`. Defaults to `CR`
  when no type filter is supplied (the extractor only handles CR
  TDocs); pass an explicit `--type` to override.
- `--release PATTERN`: filter on the TDoc's `release` (e.g.
  `Rel-18`). Accepts the same rich-filter grammar as `--spec`.
- `--version PATTERN`: filter on `version` (e.g. `18.1.0`).
  Accepts the same rich-filter grammar as `--spec`.
- `--cr-num PATTERN`: filter on `cr_num` (e.g. `3790`).
  Accepts the same rich-filter grammar as `--spec`.
- `--cr-pack PATTERN`: filter on `cr_pack` (e.g. `RP-220001`).
  Accepts the same rich-filter grammar as `--spec`.
- `--uploaded-date EXPR`: filter on `uploaded_date` — see
  [Filter syntax](#filter-syntax) for accepted forms.
- `--force`: skip the persisted `tdoc_cr_details` /
  `tdoc_extracts` short-circuit and re-render markdown (bypassing
  the markdown cache) so every id is re-parsed from scratch.
  The on-disk zip cache is **always** consulted first regardless of
  `--force` — `download_tdoc_zip` keys the cache on `tdocs.ftp_url`
  (via `derive_cache_file`) and reuses the cached bytes on a hit.
- `--full`: reserved for the parser's `full=True` mode (pulls in
  `before_change` / `after_change` per correction). The current
  service does not yet wire this through; accepted silently so
  existing scripts keep parsing.
- `--yes` / `-y`: skip the confirmation prompt before extracting.
  Useful in scripts and CI where an interactive prompt would block.

#### Filter syntax

Every text-column filter above (`--tdoc`, `--meeting`, `--status`,
`--cr-cat`, `--spec`, `--wi`, `--revision-of`, `--revised-to`,
`--title`, `--ftp-url`, `--source`, `--type`, `--release`,
`--version`, `--cr-num`, `--cr-pack`) accepts the same value
grammar:

| Value              | Effect                                                          |
| ------------------ | --------------------------------------------------------------- |
| `null`             | match rows whose column is `NULL`                               |
| `not-null`         | match rows whose column is not `NULL`                           |
| `!<pattern>`       | match rows whose column does NOT LIKE `<pattern>` — the `!` is consumed and the rest is bound as the LIKE pattern (e.g. `!%Sidelink%` excludes titles containing `Sidelink`) |
| any other text     | applied as a SQL `LIKE` pattern (use `%` / `_`)                 |

`--uploaded-date` accepts the same `null` / `not-null` tokens plus a
parameterised SQL comparison of the form ` "<op> 'YYYY-MM-DD'"` where
`<op>` is one of `=`, `!=`, `<`, `<=`, `>`, `>=`. The operator and
the date literal are bound as SQLAlchemy parameters — the date string
is never string-interpolated into the SQL, so the surface is safe to
expose to operator input. Anything else is rejected at the CLI
boundary with a clear error before the database is touched:

```
Invalid date filter 'yesterday'. Expected 'null', 'not-null',
or an expression like ">= 'YYYY-MM-DD'" with one of =, !=, <, <=, >, >=.
```

The filters compose with `AND`; combining several narrows the
candidate set. Patterns that would scan the whole table (e.g.
`--tdoc '%'`) are still allowed — pair them with a more specific
filter when feasible.

#### Filter → display column mapping

In the confirmation prompt, the CLI renders each matched TDoc as a
row with a base column set plus one extra column per active filter:

| Filter flag         | Base / extra column       |
| ------------------- | ------------------------- |
| always              | `tdoc_id`, `title`, `type`, `cr_cat`, `status` |
| `--meeting` / `--meeting-id` | extra: `meeting_name` |
| `--spec`            | extra: `spec`             |
| `--wi`              | extra: `related_wis`      |
| `--revision-of`     | extra: `is_revision_of`   |
| `--revised-to`      | extra: `revised_to`       |
| `--ftp-url`         | extra: `ftp_url`          |
| `--release`         | extra: `release`          |
| `--version`         | extra: `version`          |
| `--cr-num`          | extra: `cr_num`           |
| `--cr-pack`         | extra: `cr_pack`          |
| `--source`          | extra: `source`           |
| `--uploaded-date`   | extra: `uploaded_date`    |
| `--tdoc` / `--status` / `--cr-cat` / `--title` / `--type` | already in the base columns |

Duplicate columns are dropped; missing values render as `-`; the
table is truncated to the first 20 rows per group with an explicit
`... and N more` suffix when larger.

#### Confirmation prompt and completion summary

After filters resolve, the CLI:

1. In normal mode, the SQL query already excludes rows that have a
   `tdoc_cr_details` entry, so the candidate set consists only of
   **pending** TDocs. With `--force`, the exclusion is disabled and
   every match (including already-parsed rows) becomes a candidate.
2. Prints the candidate table with the column set above. If the
   candidate set is empty, prints `Nothing to extract — every match is
   already parsed.` and exits `0` (successful no-op).
3. Unless `--yes` / `-y` was passed, prompts with `Extract N TDoc(s)?`
   (`y/N`, default `N`). A declined prompt exits 0 with `Aborted.`
   — no work happened, so non-zero is misleading.
4. Dispatches the batch through `TDocCrService.extract_many`.
5. Prints a completion summary on four counters:
   - `Skipped (already parsed before this run): N`
   - `Re-parsed (with --force): N`
   - `Newly parsed: N`
   - `Failures: N`
   When the filter result exceeded `max_batch`, a `Remaining
   (truncated by max_batch=…): N` line is appended with a hint to
   re-run the same command **without** `--force` to continue with the
   next batch of pending rows.

#### Batch limits

The candidate set is capped by `Settings.tdoc_parse.max_batch`
(default `100`, use `[tdoc_parse] max_batch` in TOML or `doc3gpp 
config set tdoc_parse.max_batch <value>` to override). In normal 
mode the cap is applied **after** the SQL-level exclusion of 
already-parsed rows, so it limits only pending work. When the 
pending candidate set exceeds the cap, a warning describes the 
continuation flow:

- Raise the cap via the TOML config (`tdoc_parse.max_batch = 500`) to
  ingest everything in one go;
- Re-run the same command **without** `--force` to pick up the
  next batch of pending rows — already-parsed rows are excluded at
  the SQL level so the second run continues exactly where the first
  stopped.

The cap is checked against **actual work** (`total` when `--force`
is set, otherwise `total - already-parsed`), so a flag combination
that mostly hits cached rows does not fire the warning.

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
- `--meeting-id` first validates the meeting row exists (otherwise
  prints `Unknown meeting_id N` and exits non-zero); the rest of the
  pipeline then runs identically to the filter-only path.

Exit codes:

- `0` — at least one TDoc extracted successfully; **or** every match
  was already parsed and there was nothing new; **or** the prompt was
  declined before any work started.
- `1` — every TDoc failed, **or** `python-docx` is missing and the
  batch could not even start, **or** the filter set matched zero
  TDocs, **or** an invalid `--uploaded-date` value was supplied.

Install the optional dependency before first use:

```bash
pip install "doc3gpp[extract]"
```

Examples:

```bash
# Extract a single CR.
doc3gpp tdoc parse --tdoc R5s260009

# Wildcard pattern — any TDoc id starting with the 2026 source prefix.
doc3gpp tdoc parse --tdoc 'R5s26%' --yes

# Parse every not-yet-parsed CR-type TDoc under meeting 85434.
doc3gpp tdoc parse --meeting-id 85434

# Re-parse every CR-type TDoc under the meeting (DB row + markdown
# cache bypassed; on-disk zip cache is still consulted first).
doc3gpp tdoc parse --meeting-id 85434 --force

# Combine a meeting-id scope with a tdoc-id LIKE pattern and a meeting filter.
doc3gpp tdoc parse --meeting-id 85434 --meeting '%RAN5%' --tdoc 'R5s26%'

# Narrow the batch: only 38.331 CRs sourced from Qualcomm, uploaded in Q1.
doc3gpp tdoc parse --meeting-id 85434 \
    --spec '38.331%' \
    --source 'Qualcomm%' \
    --uploaded-date ">= '2026-01-01'"

# Re-parse CRs whose `cr_cat` is currently NULL (i.e. not yet classified).
doc3gpp tdoc parse --meeting-id 85434 --cr-cat null --force

# Find revisions of a known TDoc id under the meeting.
doc3gpp tdoc parse --meeting-id 85434 --revision-of 'R5-260050'

# Exclude Sidelink titles from the batch (NOT LIKE).
doc3gpp tdoc parse --meeting-id 85434 --title '!%Sidelink%'

# Non-interactive run (script / CI): pick up after a previously truncated run.
doc3gpp tdoc parse --meeting-id 85434 --yes
```

### doc3gpp tdoc parse (local mode)

Purpose:

- Parse a local `.docx` (or a zip-wrapped `.docx`) from a single file
  or an entire folder tree — without going through the `tdocs` row /
  meeting / batch plumbing. The command takes the local-mode branch
  when `--from-path` is supplied; `--from-url` is the online
  equivalent. All filter arguments are silently ignored with a warning
  in these modes (they have no meaning outside the DB-driven batch
  path).

Options:

- `--from-path PATH`: parse a local `.docx`/`.zip` file, or parse
  every `.docx`/`.zip` under a directory tree. The path type is auto-
  detected: a file is parsed as a single source, a directory is
  processed as a batch. The cache and database are never touched for
  local files.
- `--from-url URL`: download the URL (HTTP or HTTPS) and parse it.
  When the URL is on the canonical 3GPP FTP root
  (`https://www.3gpp.org/ftp/...`) the result is cached on disk and
  written to `tdoc_extracts` + `tdoc_cr_details` (and to
  `tdoc_cr_ttcn_details` when the parser produced a TTCN sidecar —
  subject to the FK matrix below); any other URL is parsed
  in-memory only. Schemes other than `http(s)` (e.g. `ftp://`,
  `file://`) are rejected — download out of band and use
  `--from-path` instead. A URL ending in `/` is treated as a 3GPP
  FTP folder and processed as a batch; URLs ending in `.docx`/`.zip`
  are treated as single files;
  ambiguous URLs are probed once and routed accordingly.
- `--format {table,markdown,json,raw}`: output format. Default
  `table` (tab-separated header + single data row, matching
  `tdoc list --format table`). `markdown` writes a single-row GFM
  table, `json` writes a single JSON object via `dataclasses.asdict`,
  `raw` writes the converted markdown verbatim and skips the parser
  entirely. The set is wider than the regular
  `* list --format` literal (`raw` is local-mode-only) so the CLI
  rejects unknown values at the boundary.
- `-o PATH`, `--output PATH`: write the result to `PATH` instead of
  stdout. Required when `--from-path` is a directory; optional when
  `--from-path` is a single file or `--from-url` is a folder. In batch
  modes the path must be a directory and the upstream folder structure
  is mirrored underneath it. The file is opened in text mode with
  `newline=""` so the markdown emitter's line endings round-trip
  cleanly.
- `--recursive`, `-r`: descend into subfolders when `--from-path` or
  `--from-url` (folder batch) is used. Silently ignored for single
  file sources.
- `--max-depth INT`: override the configured `tdoc_parse.max_ftp_depth`
  for a `--from-url` folder batch. Implies `--recursive`. Values must
  be between `0` and `10`; `0` scans the root folder only.
- `--force`: when `--from-path` is a directory or `--from-url` is a
  folder, overwrite existing output files. Rejected for single file
  sources.
- `--full`: forward `full=True` to the parser (TTCN corrections).

Mutual exclusivity:

- `--from-url` and `--from-path` are mutually exclusive. Setting both
  surfaces
  `BadParameter: --from-url, --from-path are mutually exclusive; specify exactly one source`.

Ignored / rejected flags in local mode:

- All filter flags (`--tdoc`, `--meeting-id`, `--meeting`, `--status`,
  `--cr-cat`, `--spec`, `--wi`, `--revision-of`, `--revised-to`,
  `--title`, `--ftp-url`, `--release`, `--version`, `--cr-num`,
  `--cr-pack`, `--source`, `--type`, `--uploaded-date`) are silently
  ignored with a stderr warning of the form
  `warning: ignoring filter flag(s) in direct-parse mode: --tdoc, --spec`
  for a single file, or
  `warning: ignoring filter flag(s) in local-batch mode: --tdoc, --spec`
  for a directory. Per the plan, the warning fires when at least one
  filter is supplied; pass-through output still proceeds.
- `--force` is **rejected** when `--from-path` points to a single file
  (`BadParameter: --force is not applicable when --from-path points to a
  single file`). It is allowed for directories, where it means
  "overwrite existing outputs".
- `--yes` is **rejected** in all local/online modes — there is no DB
  batch to confirm.

Single-file behaviour:

- The file is read with `Path.read_bytes()`.
- The TDoc id is auto-extracted from the filename using the existing
  [`_TDOC_HEADER_PATTERN`](src/doc3gpp/parsers/cr_parser.py) (matches
  `R5s260009`, `R5-227476`, `C6-250028`, etc.). Files without a
  matching pattern get a synthetic `LOCAL-<stem>` id internally so
  the parser can still run, but no DB row is ever written under that
  synthetic id.
- No cache or database writes occur.
- Exit code `0` when the parsed record (or raw markdown) is emitted
  successfully; `1` on file missing, permission denied, parser error
  (`CRHeaderMissingError`), or rejected flags.

Directory (batch) behaviour (local or 3GPP URL folder):

- Only filenames ending in `.docx` or `.zip` (case-insensitive) whose
  name contains a 3GPP TDoc id pattern are considered.
- One output file is written per input file; the output filename keeps
  the input stem and changes the extension according to `--format`.
  For URL folder batches the FTP folder structure is mirrored under
  `--output`.
- Per-file parse failures are logged and counted; the batch continues
  so one bad file does not abort the run.
- No per-file output is printed to stdout. Instead a summary reports:
  `Skipped (output already exists)`, `Re-parsed (with --force)`,
  `Newly parsed`, `Cache hits`, and `Failures` for URL batches.
- DB/cache writes happen automatically for 3GPP URL folder batches
  when the extracted TDoc id exists in `tdocs`; missing ids are parsed
  in-memory and warned per file.
- Exit code `0` when at least one file was parsed successfully, **or**
  every file was skipped because its output already existed; `1` when
  the input folder does not exist, `--output` was omitted for a local
  directory batch, every file failed to parse, or `--yes` was supplied.

FK-aware behaviour matrix (3GPP URL):

| Filename / id state | Cache writes? | `tdoc_extracts` row? | `tdoc_cr_details` row? | `tdoc_cr_ttcn_details` row? | Output? | Warning? |
|---|---|---|---|---|---|---|
| `tdoc_id ∈ tdocs` (extracted from filename) | yes | yes | yes (skipped when `--format raw`) | yes — only when the parser emitted a TTCN sidecar (i.e. `is_ttcn_tdoc(tdoc_id)` and the document contained a TTCN overview / corrections section) | always | no |
| `tdoc_id ∉ tdocs` (extracted but no FK target) | no | no | no | no | always | yes — actionable `meeting sync --tsg R5` recipe |
| No `tdoc_id` pattern in filename | no | no | no | no | always | yes — pattern-miss notice |

Local files and non-3GPP URLs always emit output and never touch
the cache or the database.

Auto-sync from URL candidates (3GPP only): when `Settings.sync.auto_sync`
is enabled, `tdoc parse --from-url` extracts `tdoc_id` candidates from
the URL **before** dispatching to the parse helpers, then fires
`trigger_auto_sync(...)` with the candidate set:

- **3GPP FTP file URL** (`.docx` / `.zip` suffix) → one candidate parsed
  out of the basename via `extract_tdoc_id_from_filename()`. Empty when
  the basename has no recognised `tdoc_id` pattern.
- **3GPP FTP folder URL** (trailing `/`) → BFS via
  `TDocCrService.collect_3gpp_file_urls()` (up to `--max-depth` /
  `--recursive`), then extract `tdoc_id`s per file. Empty when BFS
  fails (warning only — the parse still proceeds).
- **3GPP URL of unknown shape** → best-effort basename extraction.
- **Non-3GPP URL** → empty. The CLI gates the call on
  `is_3gpp_ftp_url()` so non-3GPP URLs never trigger the candidate
  helper.

Same skip rules as DB-mode apply (intervals, closed window, upstream
mtime — never bypassed, never broken by a sync failure). When the
candidate set is non-empty, `trigger_auto_sync` runs the TSG sync
first so the meeting_id resolution usually finds the parent row by the
time the parse fires; ordering is **TSG sync → meeting sync → parse**.
Non-3GPP URLs, parse errors, and BFS failures all stay warnings and
never abort the parse.

Cache naming: both the zip cache and the markdown cache are keyed on
the same `cache_file` column from `tdoc_extracts`, derived from the
`tdoc.ftp_url` via `derive_cache_file()` (format:
`<stem>-<md5(ftp_url)>.zip`, max 200 chars). This replaces the legacy
dual-key scheme where the zip cache used the sanitized filename and the
markdown cache used `sha256(docx_bytes)`. The unified key makes the
cache portable when `cache.dir` moves and prevents collisions across
revisions of the same `tdoc_id`. Example outputs:
`R5s260162-5186a7d62c6ae3ab3a0c02fa128e41da.zip` and
`R5s260034_MCC160Comments-5415a41d39774d1e74e27420153f65cc.zip`.

Install the optional dependency before first use (same as the
filter path):

```bash
pip install "doc3gpp[extract]"
```

Examples:

```bash
# Local docx → stdout as a tab-separated record.
doc3gpp tdoc parse --from-path ~/Downloads/R5s260009.docx

# Local docx → JSON file.
doc3gpp tdoc parse --from-path ~/Downloads/R5s260009.docx \
    --format json -o /tmp/r5s260009.json

# Local zip containing a .docx.
doc3gpp tdoc parse --from-path ~/Downloads/R5s260009.zip \
    --format markdown

# Parse every .docx/.zip in ./tdocs and write .tsv files under ./parsed.
doc3gpp tdoc parse --from-path ./tdocs --output ./parsed

# Recurse into subfolders and produce JSON.
doc3gpp tdoc parse --from-path ./tdocs --output ./parsed --recursive --format json

# Overwrite any existing outputs.
doc3gpp tdoc parse --from-path ./tdocs --output ./parsed --recursive --force

# Emit converted markdown for every file.
doc3gpp tdoc parse --from-path ./tdocs --output ./parsed --format raw

# 3GPP URL on the canonical FTP root — caches + writes both DB rows.
doc3gpp tdoc parse --from-url \
    https://www.3gpp.org/ftp/tsg_ran/WG5_Test_ex-T1/TTCN/TTCN_CRs/2026/Docs/R5s260009.zip

# 3GPP URL → emit the converted markdown verbatim, skip the parser.
doc3gpp tdoc parse --from-url \
    https://www.3gpp.org/ftp/.../R5s260009.zip --format raw

# Non-3GPP URL → in-memory parse only; never touches the cache or DB.
doc3gpp tdoc parse --from-url https://example.com/some.zip \
    --format json -o /tmp/result.json

# 3GPP FTP folder batch — scan root only, write cache/DB for FK hits.
doc3gpp tdoc parse --from-url \
    https://www.3gpp.org/ftp/tsg_ran/WG5_Test_2026/Docs/

# 3GPP FTP folder batch — recurse up to 2 levels (default) and mirror
# results under ./parsed.
doc3gpp tdoc parse --from-url \
    https://www.3gpp.org/ftp/tsg_ran/WG5_Test_2026/Docs/ \
    --recursive --output ./parsed --format json

# 3GPP FTP folder batch — recurse exactly 1 level.
doc3gpp tdoc parse --from-url \
    https://www.3gpp.org/ftp/tsg_ran/WG5_Test_2026/Docs/ \
    --recursive --max-depth 1

# 3GPP URL where the tdoc_id is missing from the `tdocs` table.
# Output is still produced; a warning with the suggested
# `meeting sync --tsg R5` recipe prints to stderr.
doc3gpp tdoc parse --from-url \
    https://www.3gpp.org/ftp/.../R5s260043_MCC160Comments_r1.zip
# warning: extracted tdoc_id 'R5s260043' from filename '...'
#          is not present in the 'tdocs' table; skipping cache and DB writes.
#
#   To add this TDoc to the database so the result can be persisted, run:
#
#       doc3gpp meeting sync --tsg R5
#       doc3gpp meeting list --tdoc R5s260043
#       doc3gpp tdoc sync --meeting-id <meeting_id_from_previous_step>
```

## cache Commands

The `cache` sub-app exposes the on-disk cache that backs the TDoc
extraction pipeline (Phase 1 `TDocCache`). The cache lives under
`settings.cache.dir` (default `~/.cache/doc3gpp/tdocs`) with two
subtrees: `zips/` (raw 3GPP zip downloads) and `markdown/` (python-docx
output). Both subtrees share the same filename — the `cache_file`
column from `tdoc_extracts` (derived from `tdoc.ftp_url` via
`derive_cache_file()`). Both commands are pure file-system operations —
they do **not** touch the database.

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

- Delete cached files in the markdown subtree, the zips subtree, or
  both. By default only the rendered markdown sidecars are removed;
  pass `--scope zips` or `--scope all` to widen the wipe.

Options:

- `--yes`, `-y`: skip the confirmation prompt.
- `--scope`: which subtree to purge. One of:
    - `markdown` (default) — only the rendered markdown sidecars
      (cheap artefacts; the next `tdoc parse` re-renders from the
      preserved zip blobs without re-downloading).
    - `zips` — only the 3GPP-served zip blobs (the expensive
      downloads).
    - `all` — both subtrees (the original wipe-everything behaviour).

Behavior:

- When `settings.cache.purge_confirm` is `True` (the default) and
  `--yes` is **not** passed, the command prompts for confirmation
  (`typer.confirm(..., abort=True)`). The prompt names the scope
  explicitly: "Delete all cached markdown?" for `--scope markdown`,
  "Delete all cached zips?" for `--scope zips`, and "Delete all
  cached zips and markdown?" for `--scope all`. In a non-interactive
  environment the prompt raises `Abort` and no files are deleted.
- Set `purge_confirm = false` in the TOML config file (the
  `[cache]` table) to skip the prompt globally (CI / scripted use).
- The on-disk artefacts referenced from `tdoc_extracts.cache_file`
  become stale for the wiped subtree(s) — the next `tdoc parse`
  will repopulate them.
- Unknown `--scope` values fail with `typer.BadParameter` and a
  non-zero exit before any files are touched.

Examples:

```bash
# Interactive confirmation; default scope = markdown only.
doc3gpp cache purge

# Skip the prompt (scripted).
doc3gpp cache purge --yes

# Force a redownload of every TDoc: wipe only the zip subtree, keep
# the rendered markdown so a fresh `tdoc parse --force` re-downloads.
doc3gpp cache purge --scope zips --yes

# Wipe both subtrees (the legacy behaviour).
doc3gpp cache purge --scope all --yes
```

## tsg Commands

The `tsg` sub-app exposes the canonical 3GPP TSG reference table. The table
is created and seeded automatically by `doc3gpp db init`, and the canonical
short names (R1..R5, RT, RP, S1..S6, SP, C1, C3, C4, C6, CP) are used to validate the
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
- Upserts the 19 canonical rows; existing rows are updated in place rather
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

### doc3gpp config init

Synopsis:

```
doc3gpp config init [--target {auto,project,user}] [--force]
```

Purpose:

- Bootstrap a fresh TOML config file populated with the packaged
  default template (every setting commented out so the file resolves
  to built-in defaults). The write is atomic via a `tempfile` +
  `os.replace` pair, so a crashed write cannot leave a partial file
  behind. After the write completes the settings cache is cleared so
  subsequent commands see the new file.
- Refuses to run when `DOC3GPP_CONFIG` is set in the environment —
  the env pin would mask the bootstrapped file, so unset it first.
- Refuses to overwrite an existing file unless `--force` is passed.

Flags:

- `--target {auto,project,user}` (default `auto`): where to write the
  config. `project` lands at `./doc3gpp.toml` (requires a project root
  marker — `pyproject.toml`, `.git/`, or `doc3gpp.toml.example` —
  somewhere up the cwd tree); `user` lands at
  `~/.config/doc3gpp/config.toml` (honors `$XDG_CONFIG_HOME`). `auto`
  picks `project` when a project root is found, otherwise `user`.
- `--force`, `-f`: overwrite an existing file at the bootstrap target.
  Required when the chosen target already has a config file.

Examples:

```bash
doc3gpp config init                       # bootstrap at the auto-detected target
doc3gpp config init --target user         # bootstrap at ~/.config/doc3gpp/config.toml
doc3gpp config init --force               # overwrite an existing file
doc3gpp config init --target project -f   # force-overwrite ./doc3gpp.toml
```

Failure (target file already exists — `--force` is required to
overwrite):

```text
$ doc3gpp config init
Error: file exists at /home/me/.config/doc3gpp/config.toml; pass --force to overwrite
```

Failure (`DOC3GPP_CONFIG` pin blocks bootstrap — unset it first):

```text
$ DOC3GPP_CONFIG=/etc/doc3gpp.toml doc3gpp config init
Error: config init refuses when DOC3GPP_CONFIG is set; unset it to bootstrap a config file.
```

On success the command prints the file it wrote and a one-liner
pointing at `config set` / `config show`:

```text
Initialized config at /home/me/.config/doc3gpp/config.toml (full default settings).
  Run 'doc3gpp config set <key> <value>' to edit; 'doc3gpp config show' to verify.
```

### doc3gpp config set

Synopsis:

```
doc3gpp config set [OPTIONS] KEY VALUE
```

Purpose:

- Write a single dotted key (e.g. `sync.auto_sync`) into the active
  TOML config file. Pydantic coerces `value` to the schema field type,
  so `24h` is accepted for `timedelta` fields and `true` / `false` for
  booleans. The settings cache is cleared so the new value is visible
  to subsequent commands in the same process. Refuses when no config
  file is in use — run `doc3gpp config init` first to bootstrap one.

Arguments:

- `KEY`: dotted setting key (`sync.auto_sync`, `output.format`,
  `tdoc_parse.max_batch`, …). Must match a field declared on
  `doc3gpp.settings.schema.Settings` — run `doc3gpp config show` to
  list valid keys.
- `VALUE`: value as a string. Pydantic validates the coerced result
  before the file is written.

Options:

- `--dry-run`: validate the key + value and print what would be
  written, without touching the file.

> **Note** — the previous `config set --init`, `--target`, `--force`,
> and `--init-force` flags were removed when `config init` was split
> out as a standalone command. To bootstrap a new file, run
> `doc3gpp config init` first (with `--target` and `--force` as
> needed); then `config set` to edit individual keys.

Examples:

```bash
doc3gpp config set sync.auto_sync true
doc3gpp config set output.format json
doc3gpp config set tdoc_parse.max_batch 200
```

Failure (no config file in use — `config set` now refuses and points
at `config init`):

```text
$ doc3gpp config set sync.auto_sync true
Error: no config file in use; run 'doc3gpp config init' to create one.
  Run 'doc3gpp config path' to see what's checked.
```

Failure (bad value — pydantic rejects the coerced value before the
file is touched):

```text
$ doc3gpp config set sync.meeting_sync_interval not-a-duration
Error: Invalid value: sync.meeting_sync_interval must be a valid duration
  (e.g. '24h', '30m', '90d', or an ISO 8601 string)
```

The full set of accepted value shapes is the same as the schema
declaration in [`doc3gpp.toml.example`](https://github.com/jerrywang121/doc3gpp/blob/main/doc3gpp.toml.example).

Only the closed allowlist (`doc3gpp.settings.schema.ALLOWED_ENV_VARS`)
of `DOC3GPP_*` env vars overrides TOML values at runtime. Any other
`DOC3GPP_<SECTION>__<FIELD>` is silently ignored. Precedence:
CLI > allowlisted env > file > defaults.

## Examples

```bash
doc3gpp db init
doc3gpp db check
doc3gpp db reset --yes           # destructive: wipe + recreate SQLite schema
doc3gpp tsg list
doc3gpp meeting sync --tsg r5
doc3gpp meeting list --limit 20
doc3gpp meeting list --tdoc R5-260013
doc3gpp tdoc sync --meeting-id 85434
# or resolve by exact meeting name:
doc3gpp tdoc sync --meeting "R5#74"
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
doc3gpp config show
```

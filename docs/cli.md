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
- --offset: number of rows to skip before applying `--limit` (pagination).
  - default: 0
- --tsg: only list meetings for the given TSG short name.
  - default: none
  - exact-match on the `meetings.tsg` FK (case-insensitive on input;
    stored canonicalised to upper case by `meeting sync`). Rows whose
    `tsg` is `NULL` (e.g. imported before the column was added) are
    excluded.

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

- Find the meeting containing a given TDoc:

```bash
doc3gpp meeting list --tdoc R5-260013
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

Every flag is a **filter** — the candidate set is the intersection of
every supplied predicate, and CR-type is the implicit default (the
extractor only handles CR TDocs). The two batch-style selectors from
earlier releases (`--tdoc-id` as an integer PK and the mutual
exclusivity with `--meeting-id`) have been removed; `--tdoc` is now a
LIKE pattern on `tdoc_id` that can be freely combined with
`--meeting-id` and every text or date filter. At least one filter is
required — an empty call exits non-zero before any DB lookup.

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
- `--force`: skip both the on-disk zip/markdown cache and the
  persisted `tdoc_cr_details` row so every id is re-fetched and
  re-parsed.
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

1. Splits the matches into an **already-parsed** group (rows that
   already have a `tdoc_cr_details` entry) and a **to-parse** group
   (rows that do not). With `--force`, every match goes into the
   to-parse group regardless of parsed status.
2. Prints both groups with the column set above. A group with no
   rows prints `(none)`.
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
   re-run the same command without `--force` to continue.

#### Batch limits

The candidate set is capped by `Settings.tdoc_parse.max_batch`
(default `100`, override via `[tdoc_parse] max_batch` in TOML or
`DOC3GPP_TDOC_PARSE__MAX_BATCH` in env). When the filter result
exceeds the cap, a warning describes the continuation flow:

- Raise the cap (`DOC3GPP_TDOC_PARSE__MAX_BATCH=500` or the TOML
  equivalent) to ingest everything in one go;
- Re-run the same command **without** `--force` to pick up the
  next batch — already-parsed rows are skipped so the second run
  continues exactly where the first stopped.

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

# Re-parse every CR-type TDoc under the meeting (cache + DB row bypassed).
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
  written to `tdoc_extracts` + `tdoc_cr_details` (subject to the FK
  matrix below); any other URL is parsed in-memory only. Schemes
  other than `http(s)` (e.g. `ftp://`, `file://`) are rejected —
  download out of band and use `--from-path` instead.
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
  `--from-path` is a single file. The file is opened in text mode with
  `newline=""` so the markdown emitter's line endings round-trip
  cleanly.
- `--recursive`, `-r`: descend into subfolders when `--from-path` is a
  directory. Silently ignored when `--from-path` is a single file.
- `--force`: when `--from-path` is a directory, overwrite existing
  output files. Rejected when `--from-path` is a single file.
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

Directory (batch) behaviour:

- Only filenames ending in `.docx` or `.zip` (case-insensitive) whose
  name contains a 3GPP TDoc id pattern are considered.
- One output file is written per input file; the output filename keeps
  the input stem and changes the extension according to `--format`.
- Per-file parse failures are logged and counted; the batch continues
  so one bad file does not abort the run.
- No per-file output is printed to stdout. Instead a summary reports:
  `Skipped (output already exists)`, `Re-parsed (with --force)`,
  `Newly parsed`, and `Failures`.
- Exit code `0` when at least one file was parsed successfully, **or**
  every file was skipped because its output already existed; `1` when
  the input folder does not exist, `--output` was omitted, every file
  failed to parse, or `--yes` was supplied.

FK-aware behaviour matrix (3GPP URL):

| Filename / id state | Cache writes? | `tdoc_extracts` row? | `tdoc_cr_details` row? | Output? | Warning? |
|---|---|---|---|---|---|---|
| `tdoc_id ∈ tdocs` (extracted from filename) | yes | yes | yes (skipped when `--format raw`) | always | no |
| `tdoc_id ∉ tdocs` (extracted but no FK target) | no | no | no | always | yes — actionable `meeting sync --tsg R5` recipe |
| No `tdoc_id` pattern in filename | no | no | no | always | yes — pattern-miss notice |

Local files and non-3GPP URLs always emit output and never touch
the cache or the database.

Cache naming (D10 fix): the zip cache is keyed on the **original
(sanitized) filename**, not the TDoc id. Multiple revisions of the
same id (`R5s260008_MCC160Comments_r1.zip` vs `…_r2.zip`) land in
distinct cache slots; the legacy tdoc-id key would have silently
served the first downloaded file forever. The markdown cache stays
keyed by sha256 of the docx bytes (content-addressed, already
collision-free).

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

# Architecture

The project is implemented as a layered Python package under `src/doc3gpp/`,
shipped both as a library (SDK) and a CLI. Each layer depends only on the
layer below it; cross-layer imports flow strictly downward.

Current scope:

- Configurable SQL backends (sqlite default, mysql, postgres).
- Calendar scraping from the 3GPP DynaReport meetings pages.
- TDoc list scraping from the 3GPP portal
  (`GenerateDocumentList.aspx?meetingId={meeting_id}`); auxiliary TDoc
  files still come from the per-meeting FTP folders.
- Work-item (WI) scraping from the per-TSG DynaReport WI pages.
- TDoc CR extraction pipeline (download zip → on-disk cache → python-docx
  render → markdown cache → cover-page parser → persist).
- Calendar / TDoc / WI / TDoc-CR persistence in SQLAlchemy.

## Layers

The seven layers sit between the CLI entry point and the database driver.
Each layer owns one concern; everything above depends only on the layer
immediately below it (with `services` reaching down into `storage` via
the `repository/` Protocols rather than touching the concrete ORM).

```
                  ┌──────────────────────────────────────┐
                  │            cli.py  (Typer)           │
                  └──────────────────┬───────────────────┘
                                     │
                  ┌──────────────────▼───────────────────┐
                  │      services/  (orchestration)      │
                  │    Protocol-typed repos in / out      │
                  └──────────────────┬───────────────────┘
                                     │
                  ┌──────────────────▼───────────────────┐
                  │   repository/  (abstract contracts)  │
                  └──────────────────┬───────────────────┘
                                     │
   ┌────────────┐ ┌─────────────────▼─┐ ┌──────────────────┐
   │ settings/  │ │    storage/        │ │   models/        │
   │ (config)   │ │   (ORM + repos +  │ │  (domain DTOs)   │
   │            │ │    engine)        │ │                  │
   └────────────┘ └─────────┬─────────┘ └──────────────────┘
                           │
                  ┌────────▼─────────┐
                  │    parsers/       │  ←─┐
                  │  (HTML/Excel →    │    │ shared inputs:
                  │   domain objects) │    │ bytes / str
                  └────────┬──────────┘    │
                           │               │
                  ┌────────▼──────────┐    │
                  │  scraping/        │ ───┘
                  │ (network I/O)     │
                  └───────────────────┘
```

Per-layer modules:

- `settings/` — schema and loader for environment-driven and TOML config;
  exposes `get_settings()` (cached) with `Settings` (root) +
  `SyncSettings` / `OutputSettings` / `OutputFieldsSettings` /
  `CacheSettings` / `TDocParseSettings` sub-models.
    - `src/doc3gpp/settings/schema.py`
    - `src/doc3gpp/settings/loader.py`
    - `src/doc3gpp/settings/config_source.py` (TOML discovery)
- `scraping/` — HTTP/FTP transport. Knows about URLs and bytes; never
  parses content.
    - `scraping/client.py` — `ScraperClient` (retry/backoff, UA, `httpx`)
    - `scraping/calendar_source.py` — DynaReport meetings HTML
    - `scraping/ftp_source.py` — FTP-directory listings for auxiliary TDoc files
    - `scraping/portal_source.py` — `GenerateDocumentList.aspx` TDoc-list XLSX
    - `scraping/wi_source.py` — DynaReport WI list HTML per TSG
    - `scraping/tdoc_zip_source.py` — TDoc zip URL builder + downloader
      (`R5s` TTCN + `R5w` Workshop branches)
    - `scraping/cache.py` — `TDocCache` (two-subtree on-disk cache for
      zip + markdown, size-based FIFO eviction)
- `parsers/` — `bytes|str` → domain objects. No network I/O.
    - `parsers/calendar_parser.py`, `parsers/html_parsers.py`,
      `parsers/normalizers.py` — meetings HTML → `Meeting`
    - `parsers/tdoc_parser.py`, `parsers/tdoc_file_parser.py` — TDoc
      list XLSX → `TDoc` / `TDocFile`
    - `parsers/wi_parser.py` — DynaReport HTML → `Wi`
    - `parsers/docx_converter.py` — `.docx` → markdown via the
      optional `python-docx` extra (raises
      `PythonDocxNotInstalledError` when missing). The legacy `.doc`
      binary format is rejected at the wrapper boundary because
      python-docx only supports the OOXML container.
    - `parsers/cr_parser.py` — thin re-export shim around
      `parsers/cr/`, exposing `parse_cr_details(markdown) ->
      TDocCRParseResult(cover, ttcn)`. The actual implementations
      (`CRParserBase` / `CRParser` / `TTCNCRParser`,
      `cover_page.py`, `header.py`, `helpers.py`,
      `ttcn_sections.py`) live in the `parsers/cr/` subpackage; the
      shim delegates to `build_default_registry()` so the public
      surface stays a single import.
- `models/` — pure domain dataclasses (`@dataclass(slots=True)`),
  passed between layers; never leak ORM attributes.
    - `models/meeting.py`, `models/tdoc.py`, `models/tsg.py`,
      `models/wi.py`, `models/tdoc_file.py`, `models/tdoc_cr.py`
      (`TDocCRDetails` slim cover-page dataclass, `TDocCRTTCNDetails`
      sidecar, `TDocCRParseResult` parser bundle, `TDocExtractMeta`
      cache-pointer sidecar, `DirectParseResult` direct-mode outcome)
- `repository/` — abstract `Protocol` contracts used by services.
    - `repository/protocols.py` — `MeetingRepository`,
      `TDocRepository` (+ `get_by_id`), `TsgRepository`,
      `WiRepository`, `TDocFileRepository`,
      `TDocCrDetailRepository` (slim cover-page repo + the
      `tdoc_extracts` sidecar, written through a separate
      `upsert_extract_meta` method),
      `TDocCrTTCNDetailRepository` (TTCN sidecar)
- `services/` — orchestration. Constructed via `services/factory.py`
  (`build_*` helpers); the CLI never imports a concrete SQL repository
  directly.
    - `services/meetings_service.py`, `services/tdoc_service.py`,
      `services/tsg_service.py`, `services/wi_service.py`
    - `services/tdoc_file_service.py` — auxiliary FTP files
    - `services/tdoc_sync_coordinator.py` — cross-service orchestration
      for `tdoc sync`
    - `services/tdoc_cr_service.py` — end-to-end CR extraction pipeline
      (accepts `cr_ttcn_repository` in its constructor; fans the
      parser's `TDocCRParseResult` out across the cover-page repo,
      the TTCN sidecar repo, and the extract-metadata repo)
    - `services/factory.py` — `build_meeting_service`,
      `build_tdoc_service`, `build_tdoc_file_service`,
      `build_tdoc_sync_coordinator`, `build_tdoc_cr_service`,
      `build_tsg_service`, `build_wi_service`,
      `build_tdoc_repository`, `build_tdoc_cr_repository`,
      `build_tdoc_cr_ttcn_repository`
- `storage/` — SQLAlchemy ORM models, engine / session factory,
  backend-specific options, concrete Protocol implementations.
    - `storage/db/models.py` — ORM classes (including
      `TDocCrDetailOrm` slim cover-page, `TDocCrTtcnDetailOrm`
      TTCN sidecar, `TDocExtractOrm` cache-pointer sidecar)
    - `storage/compression.py` — shared gzip JSON helpers
      (`compress_json` / `decompress_json`) used by the cover-page
      repo and the TTCN sidecar repo for any binary JSON column
      (the sidecar's `required_changes` blob today; tolerant
      decoding covers future binary detail columns)
    - `storage/db/session.py` — `get_engine`, `get_session_factory`
      (cached)
    - `storage/db/base.py` — declarative `Base`
    - `storage/db/migrate.py` — `create_schema` (calls
      `Base.metadata.create_all`)
    - `storage/db/migrations/` — placeholder for future Alembic
    - `storage/backends/{sqlite,mysql,postgres}.py` — engine kwargs
    - `storage/repositories/{meeting,tdoc,tsg,wi,tdoc_file,tdoc_cr}_sql.py`
      and `storage/repositories/tdoc_cr_ttcn_sql.py` — concrete
      `SQLAlchemy*Repository` classes (the cover-page repo also
      owns `tdoc_extracts` writes via `upsert_extract_meta`)

## Runtime Data Flow

The CLI composes a service via the factory, the service drives the
scrapers + parsers + repos through the Protocols, and the repos own the
SQLAlchemy session. There are four primary end-to-end flows; the
"meeting-based TDoc sync" flow is itself composed of two sub-flows,
and the TDoc CR extraction is the deepest.

### Meetings sync\n\n1. `doc3gpp meeting sync --tsg <short>` validates `<short>` against\n   the `tsgs` table (auto-seeded if empty).\n2. `MeetingService.sync` checks `tsgs.meeting_last_sync` against\n   `Settings.sync.meeting_sync_interval` (default `24h`) and skips\n   the upstream fetch when the last sync is still fresh. `--force`\n   bypasses this check.\n3. On a non-skipped run: `fetch_calendar` (DynaReport HTML) →\n   `parse_3gpp_calendar` (HTML → `Meeting` list). Every parsed\n   `Meeting` is then stamped with `Meeting.tsg = <short>` (canonicalised\n   to upper case) before being handed to\n   `SQLAlchemyMeetingRepository.upsert_many`. The FK constraint\n   requires the parent row to exist in `tsgs`, so the auto-seed in\n   step 1 is a hard prerequisite.\n4. `SQLAlchemyMeetingRepository.upsert_many` writes the rows; a final\n   `delete_with_end_before(cutoff)` pass trims out-of-window rows.\n5. `doc3gpp meeting list --tsg <pattern>` is a SQL ``LIKE`` lookup on\n     the indexed `meetings.tsg` column (case-insensitive on input). Rows\n     without an owning TSG are excluded.

### TDoc list sync (per meeting)

1. `doc3gpp tdoc sync --meeting-id <id>` (or `--meeting <name>`)
   resolves the meeting and reads its stored `meeting_id` and `ftp_url`.
2. `TDocSyncCoordinator.sync_for_meeting_id` applies two skip rules
   in order: closed window (`meetings.end_date` older than
   `Settings.sync.tdoc_list_closed_window`, default `90d`) and recent local
   sync (`meetings.tdoc_list_last_sync` newer than
   `Settings.sync.tdoc_list_sync_interval`, default `30m`). `--force`
   bypasses both rules.
3. On a non-skipped run, the coordinator orchestrates:
      - `TDocService.sync_tdoc_list` →
        `fetch_tdocs_from_portal` →
        `read_tdoc_sheet` (XLSX → `TDoc` list) →
        `SQLAlchemyTDocRepository.upsert_many`.
      - `TDocFileService.sync_from_meeting_ftp` uses the freshly-persisted
        TDoc IDs as the prefix list to recognise attachments under
        `Inbox/`, `Docs/`, `Tdocs/`, `Review/`.
4. `SQLAlchemyTDocFileRepository.upsert_many` persists revision / review
   / support files keyed by the unique `ftp_url`.

### TDoc list sync (bulk / no selector)

1. `doc3gpp tdoc sync` (no `--meeting-id` and no `--meeting`) calls
   `TDocSyncCoordinator.sync_all_tracked_meetings`.
2. The coordinator reads the distinct non-null `meeting_id` values from
   the `tdocs` table via
   `SQLAlchemyTDocRepository.list_distinct_meeting_ids` (sorted ascending,
   orphaned TDocs excluded).
3. For each meeting ID, it resolves the record via
   `MeetingService.get_by_id` and runs the same per-meeting sync path as
   the single-meeting flow above (closed window and sync interval checks
   apply individually). `--force` bypasses both checks for every meeting
   in the run.
4. A single meeting failure (`MeetingNotFoundError` /
   `MeetingMissingFtpUrlError`) is recorded in `BulkSyncOutcome.failures`
   and does not abort the sweep; iteration continues so a partial sweep
   still completes.
5. The CLI prints a single summary block (no per-meeting lines):
   `TDoc bulk sync: N meeting(s) processed / Synced / Skipped / Failed`
   plus the per-failure detail. Exit code is `1` only when every meeting
   failed; otherwise `0`.

### TDoc CR extraction

1. `doc3gpp tdoc parse` is filter-driven. At least one filter must be
   supplied (`--tdoc` as a LIKE pattern, `--meeting-id`, `--meeting`, or
   any text/date filter); the CLI validates `--meeting-id` when present,
   applies `type == "CR"` by default when no explicit `--type` is
   supplied, and in normal mode the SQL query excludes rows already
   present in `tdoc_cr_details` before applying the batch cap, so the
   preview and confirmation list only pending TDocs. With `--force`, the
   exclusion is disabled and every match (including already-parsed rows)
   becomes a candidate. If the pending set is empty, the CLI prints
   `Nothing to extract — every match is already parsed.` and exits `0`
   (successful no-op).
2. `TDocCrService.extract(tdoc_id, *, force=False)`:
    - Pre-resolves the candidate download URL(s) via
      `resolve_download_url(tdoc_id, build_ftp_url(tdocs.ftp_url))`
      (combining the stored `tdocs.ftp_url` rebuilt to an absolute URL
      via `build_ftp_url`, and the template URL), then probes
      `TDocCrRepository.get_by_url` (normalised via `normalize_ftp_path`)
      per candidate. A hit short-circuits with
      `ExtractResult.from_cache = True` and skips the network.
    - Else, `download_tdoc_zip` first checks the on-disk zip cache via
      the `ftp_url`-derived cache key (regardless of `force`) and
      returns the cached path on a hit. On a miss, it resolves the
      TDoc id to its 3GPP URL via the template table (`R5s` → TTCN
      email CR, `R5w` → workshop CR), hits `ScraperClient.get_bytes`,
      and stages the zip in `TDocCache.put_bytes(key, payload, "zips")`.
      The function tries the stored `tdocs.ftp_url` (rebuilt to an
      absolute URL) first, falling back to the template on a terminal
      HTTP error.
    - `extract_docx_from_zip` returns `(filename, docx_bytes)`.
    - The markdown for that exact `docx_bytes` is looked up by the
      shared `cache_file` (URL-derived via
      `scraping.cache_keys.derive_cache_file`) in
      `TDocCache.get_bytes(cache_file, "markdown")`; on miss,
      `convert_document_to_markdown` runs (raises
      `PythonDocxNotInstalledError` if `python-docx` is not installed)
      and the result is written to `markdown/<cache_file>` as a **real
      ZIP archive** (single entry named `<docx stem>.md`,
      `zipfile.ZIP_DEFLATED`) — so the `.zip` extension matches a
      format that `unzip` / 7z / WinZip understand directly. The
      writer is `_wrap_markdown_zip`; the reader
      (`_decompress_markdown`) magic-byte-sniffs the on-disk bytes
      (`PK\x03\x04` for the new format, `\x1f\x8b` for the legacy
      gzip blob, plain UTF-8 for the pre-gzip era) so previously
      written cache files still decode transparently. The same gzip
      JSON convention continues to apply to the SQL-side
      `tdoc_cr_ttcn_details.required_changes` blob via the shared
      helpers in `storage/compression.py` (`compress_json` /
      `decompress_json`) — same `compresslevel=9`, same tolerant
      fallback (`None` / empty / gzip / JSON / Unicode errors all
      resolve to `None` plus a warning, and legacy uncompressed
      blobs decode transparently).
    - `parse_cr_details(markdown, tdoc_id=...)` returns a typed
      `TDocCRParseResult(cover, ttcn)` — the slim cover-page fields
      bundled with an optional `TDocCRTTCNDetails` sidecar (populated
      only when `tdoc_id` matches `R5s\d{6}` and the parser ran the
      TTCN overview + corrections sub-parsers; non-TTCN CRs get
      `ttcn=None`).
    - The service fans the result out across THREE independent
      upserts in `TDocCrService.extract_many` /
      `TDocCrService.extract_from_url`: the slim cover-page row in
      `tdoc_cr_details` (`cr_repo.upsert(cover)`), the optional
      TTCN sidecar in `tdoc_cr_ttcn_details`
      (`cr_ttcn_repo.upsert(ttcn)` — only when `ttcn is not None`),
      and the cache metadata row in `tdoc_extracts`
      (`cr_repo.upsert_extract_meta(extract_meta)`). All three are
      keyed by the relative `ftp_url`; multiple extracts at distinct
      URLs for the same `tdoc_id` write distinct rows, one per
      revision. The fan-out replaces the previous
      `TDocCrRepository.upsert(details, extract_meta)` two-table
      transaction; partial failure is now possible across the three
      tables, but the unbuffered HTTP fetch we already trust keeps
      that window negligible.
    - Returns `ExtractResult(details, extract_meta, from_cache=False)`.
  3. `doc3gpp tdoc show --tdoc <id>` resolves the parent `tdoc` row
    via `TDocRepository.get_by_id` (PK lookup on `tdocs.tdoc_id`),
    then performs THREE URL-keyed reads against the immutable
    `tdoc.ftp_url`:
    1. `SQLAlchemyTDocCrRepository.get_by_url(tdoc.ftp_url)` — the
       slim cover-page row from `tdoc_cr_details`.
    2. `SQLAlchemyTDocCrRepository.get_extract_meta_by_url(tdoc.ftp_url)`
       — the cache metadata row from `tdoc_extracts`. The
       `extracted_at` display value is sourced from this row
       (both `tdoc_cr_details` and `tdoc_cr_ttcn_details` no longer
       carry their own timestamps after the slimming).
    3. `SQLAlchemyTDocCrTtcnRepository.get_by_url(tdoc.ftp_url)` —
       the TTCN sidecar from `tdoc_cr_ttcn_details`, gated on
       `is_ttcn_tdoc(tdoc.tdoc_id)` so non-TTCN CRs never hit the
       sidecar table.

    The bundled `TDocShowRecord(tdoc, cover, ttcn, extracted_at)`
    is rendered by `table` / `json` / `markdown` to three separate
    sections: `cover`, the optional `ttcn` block, and the standalone
    `extracted_at` line. Optional keys are **omitted** (not emitted
    as `null`) in the JSON payload when the corresponding row is
    absent. The legacy `details` / `parser_version` fields no longer
    appear in any output. The `raw` format delegates to
    `TDocCrService.extract()` and writes the converted `.docx`
    markdown (DB-cache short-circuit, otherwise download + render +
    persist).

### Cache + CLI

- `doc3gpp cache status` → `TDocCache.status()` (file count, total
  bytes, limit, per-subdir breakdown; non-mutating).
- `doc3gpp cache purge [--yes] [--scope {markdown,zips,all}]`
  (default scope: `markdown` — only the rendered sidecars; gated by
  `CacheSettings.purge_confirm`, configurable only via TOML since
  `DOC3GPP_CACHE__PURGE_CONFIRM` is outside the env-var allowlist)
  → `TDocCache.purge_subdir(scope)` for the scoped case, or
  `TDocCache.purge()` for `--scope all`.

## Database Schema

Tables live in `src/doc3gpp/storage/db/models.py`. Schema bootstrap is
`Base.metadata.create_all` via `doc3gpp db init`.

- `tdocs`:
    - `tdoc_id` (PK), `title`, `meeting_id` (FK → `meetings.meeting_id`),
      `ftp_url`, `source`, `type`, `status`, `reservation_date`,
      `uploaded_date`, `cr_cat`, `is_revision_of`, `revised_to`,
      `release`, `spec`, `version`, `related_wis`, `cr_num`,
      `cr_pack`. `ftp_url` is a regular nullable text column (no
      DB-level `UNIQUE` constraint); the upload pipeline maintains it
      as a 1:1 invariant (one `tdoc_id` per `ftp_url`), so the new
      `TDocRepository.get_by_ftp_url` reads with
      `ORDER BY tdoc_id ASC LIMIT 1` as a deterministic fallback if
      the invariant is ever violated.
- `tdoc_files`:
    - `id` (PK), `tdoc_id` (FK → `tdocs.tdoc_id`, no cascade),
      `type` (`revision` / `review` / `support`), `file`, `ftp_url`
      (unique, the upsert key; stored as a path relative to the
      canonical 3GPP FTP root), `uploaded_date`.
- `tdoc_cr_details`:
    - `ftp_url` (PK, immutable download URL stored relative to the
      3GPP FTP root) + `tdoc_id` (non-PK FK → `tdocs.tdoc_id` with
      `ondelete="CASCADE"`, indexed for the per-tdoc lookup), one
      column per parsed cover-page field (`spec`, `cr_num`, `rev`,
      `version`, `title`, `source`, `tsg`, `related_wis`, `date`,
      `cr_cat`, `release`, `reason_for_change`,
      `consequences_if_not_approved`, `clauses_affected`,
      `other_comments`, `revision_history`, `extracted_tdoc_id`).
      Identity is the URL because 3GPP assets are byte-for-byte
      identical for the lifetime of the URL while a single `tdoc_id`
      may map to multiple URLs across revisions — every revision's
      parsed record is preserved. The table is **slim** post-Wave-1:
      it carries cover-page fields only. TTCN-specific fields
      (`testcase`, `ue`, `ss`, `ats_version`, `ttcn_release`,
      `test_suite`, `required_changes`) and the per-row timestamps
      (`extracted_at`, `parser_version`) have moved to
      `tdoc_extracts` (for timestamps) and `tdoc_cr_ttcn_details`
      (for the TTCN slice).
- `tdoc_cr_ttcn_details`:
    - `ftp_url` (PK, immutable download URL — same identity
      convention as `tdoc_cr_details`) + `tdoc_id` (non-PK FK →
      `tdocs.tdoc_id` with `ondelete="CASCADE"`, indexed for the
      per-tdoc lookup), six overview columns (`testcase`, `ue`,
      `ss`, `ats_version`, `ttcn_release`, `test_suite`) plus
      `required_changes` (`LargeBinary(16 MB)` — gzip-compressed
      UTF-8 JSON list of correction dicts, written via
      `storage/compression.py`). One row per immutable URL, so
      multiple revisions of the same `tdoc_id` still land at
      distinct URLs and occupy distinct rows. No `extracted_at`
      or `parser_version` — the sidecar is purely the parsed
      payload; timestamps and parser versioning live in
      `tdoc_extracts`.
- `tdoc_extracts`:
    - `ftp_url` (PK, matches `tdoc_cr_details.ftp_url`) + `tdoc_id`
      (non-PK FK → `tdocs.tdoc_id` with `ondelete="CASCADE"`, indexed
      for the per-tdoc lookup), `cache_file` (String(255), indexed),
      `doc_filename`, `extracted_at`, `parser_version`. Cache-pointer
      sidecar — the two child tables share the URL as their identity
      but have **no FK between themselves**: the on-disk cache can be
      purged (deleting every `tdoc_extracts` row) without dropping the
      parsed `tdoc_cr_details` history, and the parsed record can be
      rebuilt (deleting `tdoc_cr_details`) without invalidating the
      cached zip/markdown. The `cache_file` column stores the unified
      basename (derived from `ftp_url` via `derive_cache_file()`); the
      on-disk paths are reconstructed as `{cache.dir}/zips/<cache_file>`
      and `{cache.dir}/markdown/<cache_file>`.
- `meetings`:
    - `meeting_id` (PK), `name`, `title`, `location`, `start_date`,
      `end_date`, `ftp_url`, `start_doc`, `end_doc`, `tsg` (nullable
      FK → `tsgs.short_name`, indexed for the `meeting list --tsg` filter).
- `tsgs`:
    - `short_name` (PK), `tsg_name` (unique), `description`, `url`.
      Seeded on `db init`; validates `--tsg` in `meeting sync` and
      `wi sync`.
- `wis`:
    - `(wi_id, tsg_short)` composite PK, `acronym`, `release`, `name`.
      `tsg_short` FK → `tsgs.short_name`; composite PK keeps the natural
      identifier stable across multi-TSG ownership.

Cascading FK deletes are deliberately inconsistent across the schema:
`tdoc_cr_details` / `tdoc_cr_ttcn_details` / `tdoc_extracts` cascade
on `tdocs.tdoc_id` deletion (they are derived artefacts of the parent
TDoc and are safe to wipe with it), while `tdoc_files` does not
(revision files survive a TDoc re-sync). The `tdoc_cr_details`,
`tdoc_cr_ttcn_details`, and `tdoc_extracts` tables have **no FK
between each other**: the cache sidecar can be purged without
dropping parsed detail history, the parsed detail can be rebuilt
without invalidating the cached zip/markdown, and the TTCN sidecar
lives independently of the cover-page row at the same URL (a
non-TTCN extract leaves no `tdoc_cr_ttcn_details` row). The
`test_cascade_delete_via_fk` ORM test exercises the cascade
end-to-end via a `PRAGMA foreign_keys=ON` connect listener (SQLite
default is OFF).

## Backend Selection

The active backend is selected from the allowlisted
`DOC3GPP_DATABASE_URL` env var (or its TOML counterpart):

- sqlite default: `sqlite+pysqlite:///~/.local/share/doc3gpp/doc3gpp.db`
- mysql example: `mysql+pymysql://user:pass@localhost:3306/doc3gpp`
- postgres example: `postgresql+psycopg://user:pass@localhost:5432/doc3gpp`

Backend-specific engine kwargs are applied in
`src/doc3gpp/storage/db/session.py` via:

- `src/doc3gpp/storage/backends/sqlite.py`
- `src/doc3gpp/storage/backends/mysql.py`
- `src/doc3gpp/storage/backends/postgres.py`

## CLI Surface

Implemented command groups in `src/doc3gpp/cli.py` (seven groups,
twenty commands):

- `db`:
    - `check`
    - `init` — creates the schema and seeds the `tsgs` reference table
    - `reset` — SQLite-only destructive reset; deletes the DB file and
      sidecars, clears the engine cache, recreates the schema, and re-seeds
      `tsgs`
- `meeting`:
    - `sync` — validates `--tsg` against the reference table
    - `list` — filters by `--tsg`, `--name`, `--location`, `--year`,
      `--tdoc`, `--limit`, `--offset`. `--name` / `--location` are raw
      SQL LIKE patterns (use `%` / `_` explicitly). `--tdoc` accepts a
      9-character CR-shape id (e.g. `R5-260013`, `R5s260009`,
      `R5w260013`) and resolves to the meeting whose `start_doc` /
      `end_doc` range brackets the TDoc; prefix match is case-insensitive.
- `tdoc`:
    - `sync` — exactly one of `--meeting-id` or `--meeting`; delegates to
      `TDocSyncCoordinator`
    - `list` — filters by `--tdoc`, `--meeting`, `--meeting-id`,
      `--source`, `--spec`, `--wi`, `--title`, `--cr-cat`, `--status`,
      `--type`, `--revision-of`, `--revised-to`, `--ftp-url`, `--release`,
      `--version`, `--cr-num`, `--cr-pack`, `--uploaded-date`.
      Text-column filters accept the rich grammar from
      `src/doc3gpp/cli_filters.py` (`null` / `not-null` / `!<pattern>` for
      `NOT LIKE` / SQL `LIKE`); `--uploaded-date` additionally accepts
      `OP 'YYYY-MM-DD'` parameterised comparisons — the same surface
      `tdoc parse` exposes.
    - `parse` — `--tdoc` (LIKE pattern on `tdoc_id`), `--meeting-id`,
      `--meeting`, plus every text-column filter and `--uploaded-date`;
      `--force` re-extracts already-parsed rows, `--full` is reserved
      for the parser's `full=True` mode. End-to-end filter-driven:
      candidates are the intersection of every supplied predicate, with
      CR-type as the implicit default and a `max_batch` cap.
    - `show` — `--tdoc` (mutually exclusive with `--ftp-url`); renders
      the matching TDoc, the slim cover-page row from
      `tdoc_cr_details` (URL-keyed on `tdoc.ftp_url`), the
      `extracted_at` timestamp from `tdoc_extracts` (same URL), and,
      when the TDoc is a TTCN CR, a `[TTCN Details]` block from
      `tdoc_cr_ttcn_details`. Every matching `tdoc_files` row
      (`tdoc_id`-keyed read, no URL match) renders under an
      `[Auxiliary Files]` block (table), `## Auxiliary Files`
      section (markdown), or `files` key (JSON). JSON payload keys
      are `tdoc` (always), `cover` (omitted when absent), `ttcn`
      (omitted when absent), `extracted_at` (omitted when absent),
      `files` (omitted when no auxiliary files exist). `--ftp-url`
      resolves the URL across `tdocs` / `tdoc_cr_details` /
      `tdoc_cr_ttcn_details` / `tdoc_files` directly (no parent
      TDoc needed) and bundles the result into a separate
      `TDocShowRecordByUrl(ftp_url, tdoc, cover, ttcn, extracted_at,
      files)` DTO rendered under a `# FTP URL` / `[FTP URL]`
      anchor.
- `tsg`:
    - `list`, `show`, `seed`
- `wi`:
    - `sync` — `--tsg`
    - `list` — filters by `--tsg`, `--name`, `--acronym`, `--release`
- `config`:
    - `init` — bootstrap a default TOML at `--target {auto,project,user}`
      (default `auto`: `./doc3gpp.toml` from a project root, otherwise
      `~/.config/doc3gpp/config.toml`); refuses when `DOC3GPP_CONFIG`
      is set; `--force/-f` overwrites an existing file.
    - `path` — which TOML file is in effect (or
      `"(no config file found)"`)
    - `show` — fully-resolved `Settings` as JSON for diffing against
      `doc3gpp.toml.example`
    - `set` — `<key> <value>` write one setting into the active TOML
      config file (refuses when none is in use; run `config init`
      first); `--dry-run` prints the resulting TOML without writing.
- `cache`:
    - `status` — file count, total bytes, limit, per-subdir breakdown
    - `purge` — `[--scope {markdown,zips,all}]` (default `markdown`)
      selects which subtree to evict; `[--yes]` skips the interactive
      confirm; gated by `CacheSettings.purge_confirm` (TOML-only)

Every `* list` command also accepts `--format table|json|markdown`
and `-o/--output PATH`. `meeting list`, `tdoc list`, and `tsg list` also
accept `--fields`; `wi list` uses the configured `output.fields.wi` list
without a per-command `--fields` override. `tdoc show` additionally
accepts `--format {table,json,markdown,raw}` (the `raw` mode reads the
converted `.docx` markdown body straight from the cache, bypassing the
DB-row render — `--format raw` on the `--ftp-url` path is a
deterministic cache read because the URL is the row identity), and
the direct-mode `tdoc parse --from-path/--from-url` also accepts
`--format raw` for local-batch use.

## Composition

The CLI layer never instantiates a concrete `SQLAlchemy*Repository`
directly; everything goes through `services/factory.py::build_*`. The
factory wires:

- `get_settings()` (cached; `cache_clear()` in tests that mutate
  allowlisted `DOC3GPP_*` env vars)
- `get_engine()` / `get_session_factory()` (cached; same clear
  contract)
- `ScraperClient()` — single instance per CLI invocation

`_build_cache` in the CLI constructs `TDocCache(settings.cache.dir,
size_limit_bytes=settings.cache.size_limit_mb * 1024 * 1024)` directly
for the `cache status` / `cache purge` commands, which don't need the
service stack.

## Testing Layout

- `tests/unit/` — pure-Python unit tests that mock external calls. Coverage
  is concentrated in:
    - parser fixtures (`test_calendar_parser.py`,
      `test_cr_parser.py`, `test_tdoc_parser.py`,
      `test_tdoc_file_parser.py`, `test_wi_parser.py`,
      `test_docx_converter.py`)
    - scraping + cache contracts (`test_tdoc_cache.py`,
      `test_tdoc_zip_source.py`, `test_ftp_source.py`,
      `test_scraper_client.py`)
    - repositories (CRUD + filter combinations for concrete
      `SQLAlchemy*Repository` classes)
    - services (`test_meetings_service_sync.py`,
      `test_tdoc_service_sync.py`, `test_tdoc_sync_coordinator.py`)
    - CLI (`test_meeting_cli*`, `test_tdoc_cli_fields.py`,
      `test_tdoc_sync_cli.py`, `test_tdoc_parse_cli.py`,
      `test_cache_cli.py`, `test_wi_cli.py`, `test_tsg_cli.py`,
      `test_db_reset_cli.py`)
- `tests/integration/` — sqlite-only by default; online + mysql
  opt-in. Coverage includes:
    - `test_sqlite_backend.py`, `test_sdk_integration.py`,
      `test_cli_sqlite.py`, `test_db_reset_sqlite.py`
    - `test_meeting_service_sqlite.py`,
      `test_tdoc_sqlite.py`, `test_tdoc_file_sqlite.py`,
      `test_tdoc_cr_sqlite.py`, `test_tsg_sqlite.py`, `test_wi_sqlite.py`
    - `test_online_3gpp_calendar.py`, `test_online_tdoc_parse.py`,
      `test_online_tdoc_fetch_r5.py` (live 3GPP endpoints,
      `@pytest.mark.online`)
    - `test_mysql_backend.py` (gated on
      `DOC3GPP_TEST_MYSQL_URL`)
- `tests/fixtures/tdoc_cr_doc/` — 7 CR zip fixtures
  (`C6-250028.zip`, `R5-227476.zip`, `R5-253079.zip`,
  `R5s260009.zip`, `R5s260051.zip`, `R5s260135.zip`,
  `R5s260176.zip`). Regression corpus for `cr_parser` and
  `tdoc_cr_service`.
- Pytest markers: `online`, `mysql`. The sqlite profile is
  `pytest -m "not mysql and not online"`; `./scripts/test_sqlite.sh`
  is the canonical wrapper.

## Cross-cutting design rules

These are enforced by code review (see `AGENTS.md` §Conventions for the
original convention list) and re-stated here for the architecture
readers:

- **Ruff clean at every phase boundary** — `ruff check src/doc3gpp
  tests` before merging.
- **No `as any` / `# type: ignore`** — use typed code paths instead.
- **Schema bootstrap is create-all, not versioned migrations.** `db init`
  is the intended schema boundary for normal use, while `meeting sync`,
  `wi sync`, and `tsg seed` still call `create_schema()` idempotently for
  fresh-database ergonomics. `tdoc sync` and `tdoc parse` assume the schema
  already exists. Existing installs need `doc3gpp db reset --yes` (SQLite)
  or a backend-native migration/reset after ORM shape changes.
- **Protocol ↔ impl signature parity** — when changing a filter
  signature on any repo, update both the Protocol and the impl.
- **CLI depends on `services/factory.py` only** — never instantiate a
  concrete `SQLAlchemy*Repository` from `cli.py`.
- **Settings caching** — `get_settings` and `get_engine` are
  `@lru_cache(maxsize=1)`; any test that mutates an allowlisted
  `DOC3GPP_*` env var must call `cache_clear()` on both in teardown
  (the `sqlite_env` fixture is the canonical pattern).

## Out of scope (today)

The full list of open constraints — schema bootstrap policy, settings
caching, hardcoded FTP root, calendar-parser coupling, TDoc source
coverage, R5-/C6- URL-template status, `python-docx` opt-in, and the
test-surface limits — lives in
[`docs/known-constraints.md`](known-constraints.md). That file is the
single source of truth; update it in the same change set when a
constraint is lifted.

Out-of-scope features that have not been implemented yet:

- TDoc types other than CR (LS, DRAFT, BB, etc.).
- Workplan / spec status extraction.
- Alembic / versioned migrations (the schema bootstrap is
  `Base.metadata.create_all` via `db init`).

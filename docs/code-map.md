# Code Map

Where each public symbol lives. Use this when you want to jump from a
class / function name straight to the source file. Long inline descriptions
that risk going stale live in the docstring of the symbol itself; the
table below is for navigation only.

## Domain models (`src/doc3gpp/models/`)

| Symbol | Kind | File | Role |
| --- | --- | --- | --- |
| `Meeting` | dataclass | `models/meeting.py` | Domain model for meetings (`tsg` is the owning TSG FK). |
| `TDoc` | dataclass | `models/tdoc.py` | Domain model for TDocs. |
| `TDocWithMeeting` | dataclass | `models/tdoc.py` | Presentation-time DTO: `TDoc` JOINed with `meetings.name`. |
| `TDocFile` | dataclass | `models/tdoc_file.py` | Auxiliary file attached to a TDoc (revision / review / support). |
| `TDocCRDetails` | dataclass | `models/tdoc_cr.py` | Parsed CR cover-page fields (spec, cr_num, release, …) — slimmed: no `details` blob, no `parser_version` field. Mirrors the slim `tdoc_cr_details` table 1:1. |
| `TDocCRTTCNDetails` | dataclass | `models/tdoc_cr.py` | TTCN sidecar (six overview fields + `required_changes` list). Mirrors the new `tdoc_cr_ttcn_details` table 1:1. |
| `TDocCRParseResult` | dataclass | `models/tdoc_cr.py` | Parser bundle: `cover: TDocCRDetails` + `ttcn: TDocCRTTCNDetails | None`. The service fans each slice out to its own repo. |
| `TDocExtractMeta` | dataclass | `models/tdoc_cr.py` | Cache-pointer sidecar (`cache_file`, `doc_filename`). |
| `DirectParseResult` | dataclass | `models/tdoc_cr.py` | Outcome of `tdoc parse --from-path/--from-url` (source kind, markdown, details, persistence flags). |
| `SyncOutcome` | dataclass | `models/sync.py` | Result of `meeting sync` / `tdoc sync`: synced/skipped status, reason, and counts. |
| `BulkSyncOutcome` | dataclass | `models/sync.py` | Result of `tdoc sync` bulk mode: per-meeting outcomes plus typed failures. |
| `BulkSyncFailure` | dataclass | `models/sync.py` | Per-meeting failure captured during `tdoc sync` bulk mode (meeting_id, error class, reason). |
| `Tsg` | dataclass | `models/tsg.py` | Domain model for 3GPP TSG reference records. |
| `Wi` | dataclass | `models/wi.py` | Domain model for 3GPP Work Items (FK to `tsg_short`). |

## Repository contracts (`src/doc3gpp/repository/`)

| Symbol | Kind | File | Role |
| --- | --- | --- | --- |
| `MeetingRepository` | Protocol | `repository/protocols.py` | Contract for meeting storage. |
| `TDocRepository` | Protocol | `repository/protocols.py` | Contract for TDoc storage; `get_by_id` resolves canonical id strings. |
| `TDocFileRepository` | Protocol | `repository/protocols.py` | Contract for `tdoc_files` storage. |
| `TDocCrDetailRepository` | Protocol | `repository/protocols.py` | Contract for the slim `tdoc_cr_details` table. `tdoc_extracts` reads are still exposed here for convenience but writes go through the separate `upsert_extract_meta` method. |
| `TDocCrTTCNDetailRepository` | Protocol | `repository/protocols.py` | Contract for the new `tdoc_cr_ttcn_details` TTCN sidecar (one row per immutable `ftp_url`). |
| `TsgRepository` | Protocol | `repository/protocols.py` | Contract for TSG reference storage. |
| `WiRepository` | Protocol | `repository/protocols.py` | Contract for WI storage; upsert keyed by `(wi_id, tsg_short)`. |

## Services (`src/doc3gpp/services/`)

| Symbol | Kind | File | Role |
| --- | --- | --- | --- |
| `MeetingService` | class | `services/meetings_service.py` | Meeting sync + list orchestration. |
| `TDocService` | class | `services/tdoc_service.py` | TDoc sync + list orchestration. |
| `TDocSyncCoordinator` | class | `services/tdoc_sync_coordinator.py` | Cross-service orchestration for `tdoc sync`. Exposes `sync_for_meeting_id`, `sync_for_meeting_name`, and `sync_all_tracked_meetings` (bulk). |
| `TDocFileService` | class | `services/tdoc_file_service.py` | Auxiliary TDoc-file sync. |
| `TDocCrService` | class | `services/tdoc_cr_service.py` | End-to-end CR extraction (zip → cache → python-docx → parse → persist). Constructor takes `cr_ttcn_repository`; fans `TDocCRParseResult(cover, ttcn)` out across the slim cover-page repo, the optional TTCN sidecar repo, and the `tdoc_extracts` repo in three independent upserts. Also exposes `extract_from_url` / `extract_from_bytes` for the `tdoc parse --from-path/--from-url` direct-mode path. |
| `TsgService` | class | `services/tsg_service.py` | TSG seeding + validation; exposes `build_tsg_url`. |
| `WiService` | class | `services/wi_service.py` | WI sync from DynaReport + list with SQL `LIKE` filters. |
| `build_*` | helpers | `services/factory.py` | Factory used by the CLI to wire repo / service instances. |

## Scraping, caching, parsers (`src/doc3gpp/scraping/`, `src/doc3gpp/parsers/`)

| Symbol | Kind | File | Role |
| --- | --- | --- | --- |
| `ScraperClient` | class | `scraping/client.py` | HTTP transport with retry / backoff via `httpx`. |
| `fetch_calendar` | function | `scraping/calendar_source.py` | Fetch DynaReport meeting HTML. |
| `fetch_tdoc_files_from_meeting_ftp` | function | `scraping/ftp_source.py` | Scan a meeting's FTP subfolders for auxiliary TDoc files. |
| `fetch_tdocs_from_portal` | function | `scraping/portal_source.py` | Download a meeting's TDoc-list XLSX from `GenerateDocumentList.aspx`. |
| `fetch_wis` | function | `scraping/wi_source.py` | Fetch DynaReport WI list HTML for a TSG. |
| `download_tdoc_zip` / `get_tdoc_zip_url` | functions | `scraping/tdoc_zip_source.py` | Resolve TDoc id → 3GPP URL + on-disk zip via `TDocCache`. `download_tdoc_zip` accepts an optional `cache_key_override` so the direct-parse path can key the zip cache on the original filename (D10 fix). |
| `derive_cache_file` | function | `scraping/cache_keys.py` | Derive unified cache filename `<stem>-<md5(ftp_url)>.zip` from a 3GPP relative FTP URL. Used for both zip and markdown cache keys. |
| `TDocCache` / `CacheStatus` | class | `scraping/cache.py` | On-disk `zips/` + `markdown/` cache with FIFO eviction. |
| `parse_3gpp_calendar` | function | `parsers/calendar_parser.py` | DynaReport HTML → `Meeting` list. |
| `read_tdoc_sheet` | function | `parsers/tdoc_parser.py` | TDoc-list XLSX → `TDoc` list. |
| `parse_3gpp_wis` | function | `parsers/wi_parser.py` | WI DynaReport HTML → `Wi` list (extracts `wi_id`, `acronym`, `release`, `name`). |
| `convert_document_to_markdown` / `extract_docx_from_zip` | functions | `parsers/docx_converter.py` | python-docx conversion (`.docx` only; legacy `.doc` is rejected). |
| `parse_cr_details` | function | `parsers/cr_parser.py` | Markdown → `TDocCRDetails` (cover-page, optional TTCN overview, optional corrections). |
| `is_3gpp_ftp_url` / `direct_parse_bytes` / `derive_zip_cache_key` / `extract_tdoc_id_from_filename` | functions | `parsers/direct_extractor.py` | Helpers for the `tdoc parse --from-path/--from-url` direct path. `is_3gpp_ftp_url` is the 3GPP-FTP detection rule; `direct_parse_bytes` glues docx conversion + cover-page parsing. |
| `TDocParser` Protocol / `TDocParserRegistry` / `build_default_registry` | Protocol / class / function | `parsers/tdoc_parsers.py` | `TDocParser.parse()` returns a `TDocCRParseResult` (cover + optional TTCN sidecar). `build_default_registry` registers `TTCNCRParser` before the generic `CRParser` so TTCN CRs route to the overview + corrections parser and everything else falls through. |
| `CRParserBase` / `CRParser` / `TTCNCRParser` | classes | `parsers/cr/cr_parsers.py` | Concrete parser implementations. `CRParserBase.parse()` returns `TDocCRParseResult`; `TTCNCRParser` populates the sidecar from the TTCN overview + corrections section payloads; `CRParser` always emits `ttcn=None`. |

## Storage (`src/doc3gpp/storage/`)

| Symbol | Kind | File | Role |
| --- | --- | --- | --- |
| `Base` | declarative base | `storage/db/base.py` | SQLAlchemy `DeclarativeBase`. |
| ORM classes | `Mapped[]` | `storage/db/models.py` | `TDocORM`, `MeetingORM`, `TsgORM`, `WiORM`, `TDocFileORM`, slim `TDocCrDetailOrm` (cover-page only), `TDocCrTtcnDetailOrm` (TTCN sidecar), `TDocExtractOrm` (cache metadata: `cache_file` String(255), indexed). |
| `get_engine` / `get_session_factory` | functions | `storage/db/session.py` | Cached engine + session factory. |
| `create_schema` | function | `storage/db/migrate.py` | `Base.metadata.create_all` bootstrap. |
| `compress_json` / `decompress_json` | functions | `storage/compression.py` | Shared gzip JSON helpers used by both `SQLAlchemyTDocCrRepository` and `SQLAlchemyTDocCrTtcnRepository` for any binary JSON detail column (currently the TTCN sidecar's `required_changes`). `decompress_json` is tolerant — `None` / empty / gzip / JSON / Unicode errors all resolve to `None` plus a warning; legacy uncompressed blobs decode transparently. |
| `SQLAlchemyMeetingRepository` | class | `storage/repositories/meeting_sql.py` | SQL impl of `MeetingRepository`. |
| `SQLAlchemyTDocRepository` | class | `storage/repositories/tdoc_sql.py` | SQL impl of `TDocRepository`. |
| `SQLAlchemyTDocFileRepository` | class | `storage/repositories/tdoc_file_sql.py` | SQL impl of `TDocFileRepository`. |
| `SQLAlchemyTDocCrRepository` | class | `storage/repositories/tdoc_cr_sql.py` | SQL impl of `TDocCrDetailRepository`. Owns both the slim `tdoc_cr_details` cover-page table (`upsert(details)`) and the `tdoc_extracts` cache-pointer sidecar (`upsert_extract_meta(meta)`). Reads via `get_by_url`, `get`, `list_all`, `get_extract_meta`, `get_extract_meta_by_url`. |
| `SQLAlchemyTDocCrTtcnRepository` | class | `storage/repositories/tdoc_cr_ttcn_sql.py` | SQL impl of `TDocCrTTCNDetailRepository` for the new `tdoc_cr_ttcn_details` sidecar. One row per immutable `ftp_url`; six overview columns + a gzip-compressed `required_changes` blob. Lazy-bootstrap (`_ensure_table_exists`) catches `OperationalError "no such table"` and runs `Base.metadata.create_all` once per process. |
| `SQLAlchemyTsgRepository` | class | `storage/repositories/tsg_sql.py` | SQL impl of `TsgRepository`. |
| `SQLAlchemyWiRepository` | class | `storage/repositories/wi_sql.py` | SQL impl of `WiRepository`. |
| `_apply_text_filter` / `_apply_date_filter` | helpers | `storage/repositories/tdoc_sql.py` | SQLAlchemy helpers that consume `cli_filters.DATE_FILTER_RE` and the rich-filter grammar. |
| `*_engine_kwargs` | functions | `storage/backends/{sqlite,mysql,postgres}.py` | Per-dialect engine configuration. |

## Settings / config (`src/doc3gpp/settings/`)

| Symbol | Kind | File | Role |
| --- | --- | --- | --- |
| `Settings` | pydantic-settings | `settings/schema.py` | Root config: allowlisted `DOC3GPP_*` env vars + nested sub-models. |
| `ALLOWED_ENV_VARS` | frozenset | `settings/schema.py` | Closed allowlist of `DOC3GPP_*` env vars honoured by `Settings`. |
| `FilteredEnvSettingsSource` | class | `settings/schema.py` | `EnvSettingsSource` subclass that filters to the allowlist. |
| `env_var_for_dotted_key` | function | `settings/schema.py` | Render the `DOC3GPP_*` env-var name for a dotted key, or `None` if TOML-only. |
| `OutputSettings` | model | `settings/schema.py` | Default `format` + per-command field lists. |
| `OutputFieldsSettings` | model | `settings/schema.py` | Per-list-command `default_fields` lists. |
| `CacheSettings` | model | `settings/schema.py` | Disk cache (`dir`, `size_limit_mb`, `purge_confirm`). |
| `TDocParseSettings` | model | `settings/schema.py` | `tdoc parse` knobs (`max_batch`). |
| `get_settings` | function | `settings/loader.py` | Cached settings loader (env + TOML file). |
| `find_config_file` | function | `settings/config_source.py` | TOML discovery (`$DOC3GPP_CONFIG` → `./doc3gpp.toml` → XDG). |
| `load_config_data` | function | `settings/config_source.py` | `(path, dict)` for the active TOML file. |
| `parse_dotted_key` | function | `settings/config_writer.py` | Split `a.b.c` into `[a, b, c]` segments. |
| `patch_dotted` | function | `settings/config_writer.py` | Apply one dotted-key value into a TOML dict (deep set). |
| `prune_empty_tables` | function | `settings/config_writer.py` | Drop tables that became empty after `patch_dotted`. |
| `validate_against_settings` | function | `settings/config_writer.py` | Build a `Settings` model from the in-memory dict to validate a candidate value. |
| `walk_known_dotted_keys` | function | `settings/config_writer.py` | Collect every dotted key reachable from a `Settings` subclass. |
| `resolve_echo_subtree` | function | `settings/config_writer.py` | Slice the resolved `Settings` back to the dotted-key's subtree for `--dry-run` output. |
| `write_toml` | function | `settings/config_writer.py` | Persist the patched dict to disk via `tomli_w`. |
| `resolve_init_target` | function | `settings/config_writer.py` | Resolve `--target` to a writable `Path` (project / user / auto). |

## Filter / ID helpers (`src/doc3gpp/cli_filters.py`)

| Symbol | Kind | File | Role |
| --- | --- | --- | --- |
| `validate_date_filter` | function | `cli_filters.py` | Boundary guard for `--uploaded-date`. |
| `TDOC_ID_RE` | regex | `cli_filters.py` | CR-shape TDoc id regex `[RSC][1-9][-sw]\d{6}` (case-insensitive). |
| `parse_tdoc_id` | function | `cli_filters.py` | Boundary guard for `meeting list --tdoc`; returns `(prefix, number)` or raises. |
| `validate_tdoc_id` | function | `cli_filters.py` | `parse_tdoc_id` wrapper that raises on bad input. |
| `DATE_FILTER_RE` | regex | `cli_filters.py` | `<op> 'YYYY-MM-DD'` pattern for date comparisons. |

## CLI entry (`src/doc3gpp/cli.py`)

Seven Typer sub-apps: `db` (`check` / `init` / `reset`), `meeting` (`sync` / `list`), `tdoc` (`sync` / `list` / `parse` / `show`), `tsg` (`list` / `show` / `seed`), `wi` (`sync` / `list`), `config` (`path` / `show` / `set`), `cache` (`status` / `purge`). Per-command option and behavior details live in [`docs/cli.md`](cli.md).

| Symbol | Kind | File | Role |
| --- | --- | --- | --- |
| `config_set` | Typer command | `cli.py` | Write one dotted key into the active TOML config file (or bootstrap one with `--init`); clears the settings cache so the new value is visible in the same process. |

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
| `TDocCRDetails` | dataclass | `models/tdoc_cr.py` | Parsed CR cover-page fields (spec, cr_num, release, …) — slimmed: no `details` blob, no `parser_version` field. Mirrors the slim `tdoc_cr_cover_page` table 1:1. |
| `TDocCRTTCNDetails` | dataclass | `models/tdoc_cr.py` | TTCN sidecar (six overview fields + `required_changes` list). Mirrors the new `tdoc_cr_ttcn_details` table 1:1. |
| `TDocCRChangeDetails` | dataclass | `models/tdoc_cr_change_details.py` | Body-derived change sidecar (`clauses` + `changes`). Mirrors the new `tdoc_cr_change_details` table 1:1; non-TTCN CRs only. |
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
| `TDocRepository` | Protocol | `repository/protocols.py` | Contract for TDoc storage; `get_by_id` resolves canonical id strings; `get_by_ftp_url` (1:1 invariant, `LIMIT 1`) anchors the new `tdoc show --ftp-url` selector on the URL. |
| `TDocFileRepository` | Protocol | `repository/protocols.py` | Contract for `tdoc_files` storage; `get_by_ftp_url` (URL-unique-indexed) is the new URL-keyed read used by `tdoc show --ftp-url`. |
| `TDocCrDetailRepository` | Protocol | `repository/protocols.py` | Contract for the slim `tdoc_cr_cover_page` table. `tdoc_extracts` reads are still exposed here for convenience but writes go through the separate `upsert_extract_meta` method. |
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
| `TDocCrService` | class | `services/tdoc_cr_service.py` | End-to-end CR extraction (zip → cache → python-docx → parse → persist). Constructor takes `cr_ttcn_repository`; fans `TDocCRParseResult(cover, ttcn)` out across the slim cover-page repo, the optional TTCN sidecar repo, and the `tdoc_extracts` repo in three independent upserts. Also exposes `extract_from_url` / `extract_from_bytes` for the `tdoc parse --from-path/--from-url` direct-mode path, and the public `collect_3gpp_file_urls(url, *, max_depth)` alias (same body as the private `_collect_3gpp_file_urls`) used by the auto-sync URL-candidate helper. |
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
| `ttcn_functions` module | module | `parsers/cr/ttcn_functions.py` | Regex-driven helpers for the `tdoc_cr_ttcn_details.changed_functions` aggregate: `extract_module_basename` (handles Unix paths, Windows paths, bare basenames), `extract_function_name` (matches `f_`/`fl_`/`fx_`/`a_`/`tsc_`/`cs_`/`cr_`/`crs_`/`cas_`/`car_`/`cds_`/`cdr_`/`cms_`/`cmr_`/`cads_` prefixes with a `*_type` fallback), and the thin aggregator `extract_changed_functions` that returns a sorted, deduplicated `list[str]` of `"<module_basename>.<function_name>"` entries derived from `required_changes`. Partial-extraction markers (`"<module>."` trailing-dot, `".<function>"` leading-dot) cover the cases where one regex returns `None`; entries where both regexes return `None` are dropped. Consumed by `CRParserBase.parse()` when building the `TDocCRTTCNDetails` sidecar. |
| `extract_module_basename` | function | `parsers/cr/ttcn_functions.py` | Rightmost-alphanumeric-run regex with optional `\.ttcn` suffix; returns the module basename or `None` for blank / non-alphanumeric input. |
| `extract_function_name` | function | `parsers/cr/ttcn_functions.py` | Regex match against the TTCN function-name prefix set (with a `*_type` fallback); strips a trailing non-alphanumeric boundary (e.g. ` (NR)`); returns `None` when neither group matches. |
| `extract_changed_functions` | function | `parsers/cr/ttcn_functions.py` | Aggregator: walks `required_changes`, applies the 4-form contract — both extract → `"<module>.<function>"`, only module → `"<module>."` (trailing-dot sentinel), only function → `".<function>"` (leading-dot sentinel), neither → drop — deduplicates, and returns `sorted(set(...))`. |
| `extract_body_changes` | function | `parsers/cr/body_changes.py` | Pure function: line-by-line scan of the converted markdown body to capture change blocks and clause numbers. |

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
| `SQLAlchemyTDocCrRepository` | class | `storage/repositories/tdoc_cr_sql.py` | SQL impl of `TDocCrDetailRepository`. Owns both the slim `tdoc_cr_cover_page` cover-page table (`upsert(details)`) and the `tdoc_extracts` cache-pointer sidecar (`upsert_extract_meta(meta)`). Reads via `get_by_url`, `get`, `list_all`, `get_extract_meta`, `get_extract_meta_by_url`. |
| `SQLAlchemyTDocCrTtcnRepository` | class | `storage/repositories/tdoc_cr_ttcn_sql.py` | SQL impl of `TDocCrTTCNDetailRepository` for the new `tdoc_cr_ttcn_details` sidecar. One row per immutable `ftp_url`; six overview columns + a gzip-compressed `required_changes` blob. Lazy-bootstrap (`_ensure_table_exists`) catches `OperationalError "no such table"` and runs `Base.metadata.create_all` once per process. |
| `SQLAlchemyTDocCrChangeDetailsRepository` | class | `storage/repositories/tdoc_cr_change_details_sql.py` | SQL impl of the body-change sidecar. |
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
| `OutputSettings.compact` | field | `settings/schema.py:209` | `Settings.output.compact` — `--compact` default (`bool`, default `False`); CLI flag wins when `True`. |
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
| `load_default_template` | function | `settings/config_writer.py` | Read the packaged default TOML template (`doc3gpp/data/doc3gpp.toml.example`) via `importlib.resources` with a source-tree fallback for editable installs; consumed by `config init`. |

## Filter / ID helpers (`src/doc3gpp/cli_filters.py`)

| Symbol | Kind | File | Role |
| --- | --- | --- | --- |
| `validate_date_filter` | function | `cli_filters.py` | Boundary guard for `--uploaded-date`. |
| `TDOC_ID_RE` | regex | `cli_filters.py` | CR-shape TDoc id regex `[RSC][1-9][-sw]\d{6}` (case-insensitive). |
| `parse_tdoc_id` | function | `cli_filters.py` | Boundary guard for `meeting list --tdoc`; returns `(prefix, number)` or raises. |
| `validate_tdoc_id` | function | `cli_filters.py` | `parse_tdoc_id` wrapper that raises on bad input. |
| `DATE_FILTER_RE` | regex | `cli_filters.py` | `<op> 'YYYY-MM-DD'` pattern for date comparisons. |

## Auto-sync helpers (`src/doc3gpp/cli_auto_sync.py`, `src/doc3gpp/cli_url_helpers.py`)

| Symbol | Kind | File | Role |
| --- | --- | --- | --- |
| `extract_tsg_from_tdoc_id_or_pattern` | function | `cli_auto_sync.py` | Pull the TSG short name out of a TDoc id or SQL `LIKE` pattern (e.g. `R5-260013`, `R5%` → `R5`). |
| `resolve_meeting_id_for_tdoc_id` | function | `cli_auto_sync.py` | Look up the `meeting_id` for a full CR-shape TDoc id via the meeting repo. |
| `resolve_meetings_for_name_pattern` | function | `cli_auto_sync.py` | `LIKE`-pattern lookup for `meeting list --meeting`. |
| `sync_tsg_internal` / `sync_meeting_internal` | functions | `cli_auto_sync.py` | Drive meeting-calendar + TDoc-list syncs with skip-rule honoured, warning-only failure. |
| `trigger_auto_sync` | function | `cli_auto_sync.py` | Build candidate sets (`tsg_candidates`, `meeting_candidates`) from CLI filters — `tsg`, `tdoc`, `meeting_id`, `meeting_name`, and the iterable `tdoc_ids` for `tdoc parse --from-url`. Set-based dedup; returns `(meeting_syncs_done, tdoc_syncs_done)`. |
| `collect_tdoc_candidates_for_url` | function | `cli_auto_sync.py` | Derive a tdoc-id candidate set from a URL alone (4-branch contract: non-3GPP ∅ / file basename / folder BFS via `TDocCrService.collect_3gpp_file_urls` / unknown-shape basename). Always returns a set — never raises. |
| `is_3gpp_ftp_url` | function | `cli_url_helpers.py` | 3GPP-FTP detection rule; re-exported from `parsers.direct_extractor` for shared use between `cli.py` and `cli_auto_sync.py`. |
| `_looks_like_3gpp_file_url` | function | `cli_url_helpers.py` | True when the URL ends with `.docx` or `.zip` (3GPP file shape). |
| `_looks_like_3gpp_folder_url` | function | `cli_url_helpers.py` | True when the URL ends with `/` (3GPP folder shape). |

## Search subsystem (`src/doc3gpp/models/search.py`, `src/doc3gpp/services/search_service.py`, `src/doc3gpp/storage/db/fts5_query.py`, `src/doc3gpp/storage/repositories/search_sql.py`)

| `doc3gpp.models.search.SearchHit` | One FTS5 hit joined back to `tdocs` + `meetings` |
| `doc3gpp.models.search.SearchFilters` | Filter arguments for a search query |
| `doc3gpp.models.search.SearchIndexStatus` | Snapshot of the index state for `search index` |
| `doc3gpp.models.search.SearchError` (and 3 subclasses) | Error hierarchy for the search subsystem |
| `doc3gpp.services.search_service.SearchService` | Orchestration: `upsert_for_tdoc`, `remove_for_tdoc`, `search`, `rebuild`, `status` |
| `doc3gpp.services.search_service.PassthroughReranker` | Default `EmbeddingReranker` impl |
| `doc3gpp.storage.db.fts5_query.normalize_query` | Index-time pre-processor for TDoc ID + spec ID recognition |
| `doc3gpp.storage.repositories.search_sql.SQLAlchemySearchIndexRepository` | Concrete FTS5-backed `SearchIndexRepository` impl |

## CLI entry (`src/doc3gpp/cli.py`)

Seven Typer sub-apps: `db` (`check` / `init` / `reset`), `meeting` (`sync` / `list`), `tdoc` (`sync` / `list` / `parse` / `show`), `tsg` (`list` / `show` / `seed`), `wi` (`sync` / `list`), `config` (`path` / `show` / `set` / `init`), `cache` (`status` / `purge`). Per-command option and behavior details live in [`docs/cli.md`](cli.md).

| Symbol | Kind | File | Role |
| --- | --- | --- | --- |
| `config_init` | Typer command | `cli.py` | Bootstrap a fresh TOML config file with the packaged default template at `--target` (auto / project / user); `--force` overwrites an existing file. Refuses while `DOC3GPP_CONFIG` is set. |
| `config_set` | Typer command | `cli.py` | Write one dotted key into the active TOML config file (refuses when none is in use — run `config init` first); clears the settings cache so the new value is visible in the same process. |
| `_resolve_compact` | function | `cli.py:247` | Resolve `--compact` against `Settings.output.compact` (CLI flag wins when `True`); keeps the Typer `Option` a plain `bool` without a `--no-compact` toggle. |

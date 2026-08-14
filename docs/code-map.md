# Code Map

> Last reviewed: 2026-08-13

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
| `Spec` | dataclass | `models/spec.py` | Domain model for 3GPP specifications (TS/TR). One row per dotted spec id (e.g. `36.579-5`) keyed by `spec_id`. |
| `SpecVersion` | dataclass | `models/spec.py` | Domain model for one published version of a spec; `spec_id` FK plus `version`, `ftp_url`, `pdf_url`, `crs`, etc. |
| `TDocShowRecord` | dataclass | `models/tdoc_show.py` | Composite render record for `tdoc show --tdoc`: `TDoc` + slim cover + optional TTCN + `extracted_at` + `TDocFile` list. |
| `TDocShowRecordByUrl` | dataclass | `models/tdoc_show.py` | URL-anchored variant of `TDocShowRecord` for `tdoc show --ftp-url`. |
| `TDocShowRepos` | dataclass | `models/tdoc_show.py` | Bundle of repos (`tdoc`, `cr`, `cr_ttcn`, `cr_change_details`, `file`) consumed by `TDocShowRecord.from_tdoc_id`. |

## Repository contracts (`src/doc3gpp/repository/`)

| Symbol | Kind | File | Role |
| --- | --- | --- | --- |
| `MeetingRepository` | Protocol | `repository/protocols.py` | Contract for meeting storage. |
| `TDocRepository` | Protocol | `repository/protocols.py` | Contract for TDoc storage; `get_by_id` resolves canonical id strings; `get_by_ftp_url` (1:1 invariant, `LIMIT 1`) anchors the new `tdoc show --ftp-url` selector on the URL. |
| `TDocFileRepository` | Protocol | `repository/protocols.py` | Contract for `tdoc_files` storage; `get_by_ftp_url` (URL-unique-indexed) is the new URL-keyed read used by `tdoc show --ftp-url`. |
| `TDocCrDetailRepository` | Protocol | `repository/protocols.py` | Contract for the slim `tdoc_cr_cover_page` table. `tdoc_extracts` reads are still exposed here for convenience but writes go through the separate `upsert_extract_meta` method. |
| `TDocCrTTCNDetailRepository` | Protocol | `repository/protocols.py` | Contract for the new `tdoc_cr_ttcn_details` TTCN sidecar (one row per immutable `ftp_url`). |
| `TsgRepository` | Protocol | `repository/protocols.py` | Contract for TSG reference storage. Spec sync throttling is **not** in this Protocol — the per-spec skip rule lives on `SpecRepository` / `SpecORM.last_synced_at` and is enforced per-worker inside `SpecService.sync` and per-call inside `SpecService.sync_spec`. |
| `WiRepository` | Protocol | `repository/protocols.py` | Contract for WI storage; upsert keyed by `(wi_id, tsg_short)`. |
| `SpecRepository` | Protocol | `repository/protocols.py` | Contract for spec storage. `upsert(spec)` writes the header row; `upsert_versions(versions)` writes per-version rows; `list(filters)` returns filtered header rows; `list_distinct_tsgs()` returns the distinct TSGs present in the `specs` table (used by the no-selector `spec sync` fallback); `get(spec_id)` / `list_versions(spec_id)` are the lookups used by `spec show`. |

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
| `SpecService` | class | `services/spec_service.py` | Spec sync + list orchestration. `sync(tsg, force=False, per_version_details=False)` fetches the DynaReport list page once, then fans out across per-spec detail pages in a thread pool (capped at `min(32, cpu+4)` workers), runs ETSI PDF + CR-list follow-ups inside each worker (gated on recency / emptiness by default; `per_version_details=True` always re-fetches both for every version), and honours the **per-spec** `specs.last_synced_at` skip rule — each per-worker `_sync_one_spec` short-circuits specs whose own `last_synced_at` is within `Settings.sync.spec_sync_interval` (no TSG-level gate) and stamps the spec's own `last_synced_at` on a successful re-sync. `sync_spec(spec_id, force=False, per_version_details=False)` syncs a single stored spec (recovers its TSG, fetches only that spec's detail page + versions — no list page; honours the same per-spec `last_synced_at` skip rule). `list_distinct_tsgs()` returns the distinct TSGs in the `specs` table for the no-selector fallback. `list_recent` returns the cached header rows; `get` / `list_versions` are the lookups used by `spec show`. |
| `build_*` | helpers | `services/factory.py` | Factory used by the CLI to wire repo / service instances. `build_spec_service` injects `SQLAlchemySpecRepository` + the `sync.spec_sync_interval` setting. |

## Scraping, caching, parsers (`src/doc3gpp/scraping/`, `src/doc3gpp/parsers/`)

| Symbol | Kind | File | Role |
| --- | --- | --- | --- |
| `ScraperClient` | class | `scraping/client.py` | HTTP transport with retry / backoff via `httpx`. |
| `fetch_calendar` | function | `scraping/calendar_source.py` | Fetch DynaReport meeting HTML. |
| `fetch_tdoc_files_from_meeting_ftp` | function | `scraping/ftp_source.py` | Scan a meeting's FTP subfolders for auxiliary TDoc files. |
| `fetch_tdocs_from_portal` | function | `scraping/portal_source.py` | Download a meeting's TDoc-list XLSX from `GenerateDocumentList.aspx`. |
| `fetch_wis` | function | `scraping/wi_source.py` | Fetch DynaReport WI list HTML for a TSG. |
| `build_spec_list_url` / `build_spec_detail_url` | functions | `scraping/spec_source.py` | DynaReport URL builders for the spec list page (`?code=Spec-<tsg>.htm`) and per-spec detail page (`?code=<id-no-dot>.htm`). |
| `fetch_spec_list` / `fetch_spec_detail` | functions | `scraping/spec_source.py` | Fetch DynaReport spec list / detail HTML. |
| `fetch_etsi_pdf_text` / `fetch_cr_list` | functions | `scraping/spec_source.py` | Conditional follow-ups: ETSI deliverable HTML (`wki_id`) and per-version CR list HTML (`version_id`). Both are gated in `SpecService` on recency / emptiness. |
| `download_tdoc_zip` / `get_tdoc_zip_url` | functions | `scraping/tdoc_zip_source.py` | Resolve TDoc id → 3GPP URL + on-disk zip via `TDocCache`. `download_tdoc_zip` accepts an optional `cache_key_override` so the direct-parse path can key the zip cache on the original filename (D10 fix). |
| `derive_cache_file` | function | `scraping/cache_keys.py` | Derive unified cache filename `<stem>-<md5(ftp_url)>.zip` from a 3GPP relative FTP URL. Used for both zip and markdown cache keys. |
| `TDocCache` / `CacheStatus` | class | `scraping/cache.py` | On-disk `zips/` + `markdown/` cache with FIFO eviction. |
| `parse_3gpp_calendar` | function | `parsers/calendar_parser.py` | DynaReport HTML → `Meeting` list. |
| `parse_title` | function | `parsers/html_parsers.py` | Pull the `<title>` out of a DynaReport HTML page. |
| `clean_whitespace` / `normalize_ftp_path` / `build_ftp_url` | functions | `parsers/normalizers.py` | Shared text / URL normalisers used by the spec + tdoc parsers. |
| `normalise_release` / `release_from_version` | functions | `parsers/spec_release.py` | Spec release-string normalisation (e.g. `Rel-18` ↔ `18.x.y`). |
| `classify_tdoc_filename` / `parse_tdoc_files_from_listing` | functions | `parsers/tdoc_file_parser.py` | Classify FTP-listing filenames into `revision` / `review` / `support` and parse the per-file metadata. |
| `read_tdoc_sheet` | function | `parsers/tdoc_parser.py` | TDoc-list XLSX → `TDoc` list. |
| `parse_3gpp_wis` | function | `parsers/wi_parser.py` | WI DynaReport HTML → `Wi` list (extracts `wi_id`, `acronym`, `release`, `name`). |
| `parse_spec_list` | function | `parsers/spec_parser.py` | DynaReport list HTML → `Spec` header rows (one per `<tr>` in the spec table). |
| `parse_spec_detail` | function | `parsers/spec_parser.py` | DynaReport detail HTML → `(Spec, list[SpecVersion])` (header + per-version rows from the version table). |
| `extract_etsi_pdf_url` | function | `parsers/spec_parser.py` | ETSI deliverable HTML → "download as PDF" URL (consumed by `SpecService._maybe_fetch_etsi_pdf`). |
| `extract_cr_tdocs` | function | `parsers/spec_parser.py` | Per-version CR list HTML → `list[str]` of TDoc ids (consumed by `SpecService._maybe_fetch_crs`). |
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
| `CRCoverPageParser` | class | `parsers/cr/cover_page.py` | Cover-page markdown → `TDocCRDetails` (spec, cr_num, release, source, title, …). |
| `is_cr_header_present` / `is_ttcn_tdoc` | functions | `parsers/cr/header.py` | `is_cr_header_present` probes the markdown body for the CR header marker; `is_ttcn_tdoc` decides whether a TDoc id is a TTCN CR (drives the `tdoc_cr_ttcn_details` sidecar writes). |
| `TTCNOverviewParser` / `TTCNCorrectionsParser` | classes | `parsers/cr/ttcn_sections.py` | TTCN-specific parsers for the overview block and the per-correction table. |

## Storage (`src/doc3gpp/storage/`)

| Symbol | Kind | File | Role |
| --- | --- | --- | --- |
| `Base` | declarative base | `storage/db/base.py` | SQLAlchemy `DeclarativeBase`. |
| ORM classes | `Mapped[]` | `storage/db/models.py` | `TDocORM`, `MeetingORM`, `TsgORM`, `WiORM`, `TDocFileORM`, slim `TDocCrDetailOrm` (cover-page only), `TDocCrTtcnDetailOrm` (TTCN sidecar), `TDocExtractOrm` (cache metadata: `cache_file` String(255), indexed), `SpecORM` (header table keyed by `spec_id`), `SpecVersionORM` (one row per `(spec_id, version)` with `ftp_url`, `pdf_url`, `crs`, etc.). |
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
| `SQLAlchemySpecRepository` | class | `storage/repositories/spec_sql.py` | SQL impl of `SpecRepository`. Owns `specs` (header, incl. `rapporteurs`) + `spec_versions` (one row per `(spec_id, version)`); `upsert(spec)` writes the header row, `upsert_versions(versions)` writes the per-version rows (PARAMS-bound via SQLAlchemy `insert` with `ON CONFLICT DO UPDATE`), `list(filters)` applies the rich filter grammar, `get(spec_id)` / `list_versions(spec_id)` are the lookups used by `spec show`. |
| `_apply_text_filter` / `_apply_date_filter` | helpers | `storage/repositories/tdoc_sql.py` | SQLAlchemy helpers that consume `cli_filters.DATE_FILTER_RE` and the rich-filter grammar. |
| `configure_sqlite_engine` | function | `storage/backends/sqlite.py` | SQLite engine configuration (sole backend). |

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

| Symbol | Kind | File | Role |
| --- | --- | --- | --- |
| `doc3gpp.models.search.SearchHit` | dataclass | `models/search.py` | One FTS5 hit joined back to `tdocs` + `meetings` |
| `doc3gpp.models.search.SearchFilters` | dataclass | `models/search.py` | Filter arguments for a search query |
| `doc3gpp.models.search.SearchIndexStatus` | dataclass | `models/search.py` | Snapshot of the index state for `search index` |
| `doc3gpp.models.search.SearchError` (+ 3 subclasses) | exception hierarchy | `models/search.py` | `SearchUnavailableError`, `SearchQueryError`, `SearchIndexCorruptError` for the search subsystem |
| `doc3gpp.services.search_service.SearchService` | service | `services/search_service.py` | Orchestration: `upsert_for_tdoc`, `remove_for_tdoc`, `search`, `rebuild`, `status` |
| `doc3gpp.services.search_service.PassthroughReranker` | service | `services/search_service.py` | Default `EmbeddingReranker` impl |
| `doc3gpp.services.semantic_reranker.SemanticReranker` | service | `services/semantic_reranker.py` | Embedding-based reranker used by `search query --sem-query`; consults `VectorIndexRepository` and applies `MISSING_FLOOR` for unindexed rows |
| `doc3gpp.storage.db.fts5_query.normalize_query` | function | `storage/db/fts5_query.py` | Index-time pre-processor for TDoc ID + spec ID recognition |
| `doc3gpp.storage.repositories.search_sql.SQLAlchemySearchIndexRepository` | repository | `storage/repositories/search_sql.py` | Concrete FTS5-backed `SearchIndexRepository` impl |

## Semantic search subsystem (`src/doc3gpp/models/semantic_search.py`, `src/doc3gpp/services/semantic_search_service.py`, `src/doc3gpp/services/embedding/`, `src/doc3gpp/storage/repositories/vector_sql.py`)

| Symbol | Kind | File | Role |
| --- | --- | --- | --- |
| `doc3gpp.models.semantic_search.SemanticSearchHit` | dataclass | `models/semantic_search.py` | One hybrid (FTS5 + vector) hit with merged `rrf_score` and per-source ranks |
| `doc3gpp.models.semantic_search.SemanticSearchFilters` | dataclass | `models/semantic_search.py` | Filter arguments for `search sem` |
| `doc3gpp.models.semantic_search.SemanticSearchError` (+ subclasses) | exception hierarchy | `models/semantic_search.py` | Errors raised by the semantic-search subsystem (incl. dim mismatch) |
| `doc3gpp.services.semantic_search_service.SemanticSearchService` | service | `services/semantic_search_service.py` | Hybrid RRF orchestration: `search`, `index_for_tdoc`, `rebuild_embeddings`, `status` |
| `doc3gpp.services.embedding.chunker.chunk_text` | function | `services/embedding/chunker.py` | Pure `_chunks(text, size, overlap)` window splitter |
| `doc3gpp.services.embedding.embedder.SentenceTransformerEmbedder` | class | `services/embedding/embedder.py` | Concrete `Embedder` impl using sentence-transformers (lazy model load) |
| `doc3gpp.services.embedding.stopwords.strip_stopwords` | function | `services/embedding/stopwords.py` | spaCy + custom-stopword strip; respects `user_defined_stop_words` and `keep_negation_words` |
| `doc3gpp.storage.repositories.vector_sql.SQLAlchemyVectorIndexRepository` | repository | `storage/repositories/vector_sql.py` | Concrete `VectorIndexRepository` impl backed by sqlite-vec (`vec_tdoc_embeddings` + `vec_meta`) |

## CLI entry (`src/doc3gpp/cli.py`)

Seven Typer sub-apps: `db` (`check` / `init` / `reset`), `meeting` (`sync` / `list`), `tdoc` (`sync` / `list` / `parse` / `show`), `tsg` (`list` / `show` / `seed`), `wi` (`sync` / `list`), `spec` (`sync` / `list` / `show`), `config` (`path` / `show` / `set` / `init`), `cache` (`status` / `purge`). Per-command option and behavior details live in [`docs/cli.md`](cli.md).

| Symbol | Kind | File | Role |
| --- | --- | --- | --- |
| `config_init` | Typer command | `cli.py` | Bootstrap a fresh TOML config file with the packaged default template at `--target` (auto / project / user); `--force` overwrites an existing file. Refuses while `DOC3GPP_CONFIG` is set. |
| `config_set` | Typer command | `cli.py` | Write one dotted key into the active TOML config file (refuses when none is in use — run `config init` first); clears the settings cache so the new value is visible in the same process. |
| `_resolve_compact` | function | `cli.py:247` | Resolve `--compact` against `Settings.output.compact` (CLI flag wins when `True`); keeps the Typer `Option` a plain `bool` without a `--no-compact` toggle. |

## Web layer + MCP + Jobs (`src/doc3gpp/web/`, `src/doc3gpp/cli_server.py`)

The `doc3gpp[web]` extra adds a single-port FastAPI server (HTML UI + JSON API + Streamable-HTTP MCP) with a shared asyncio job worker. `[server] enabled` gates every `server` subcommand and the MCP mount. CLI↔HTTP JSON parity is byte-for-byte (compact separators + `ensure_ascii=False`) — see [`docs/web-server.md`](web-server.md).

| Symbol | Kind | File | Role |
| --- | --- | --- | --- |
| `build_app` | factory | `web/app.py` | Compose the FastAPI app: lifespan builds `WebState`, starts `JobWorker`, mounts `/mcp` (when `server.enabled` and `mcp.enabled`), registers error handlers + static + `all_routers()`; `GET /healthz` |
| `build_state` | factory | `web/app.py` | Build `WebState(settings, engine, services, jobs)` via `services/factory.build_*` + `SQLAlchemyJobRepository` |
| `WebState` | dataclass | `web/state.py` | Request-scoped app state: `settings`, `engine`, `services` (`ServiceContainer`), `jobs` (`JobWorkerHandle`) |
| `ServiceContainer` | dataclass | `web/state.py` | Bundle of services/repos injected into routes |
| `JobWorkerHandle` | class | `web/state.py` | asyncio handle over the worker: `enqueue`, `cancel`, `event_queues`, `register_queue`/`unregister_queue`, `shutdown` |
| `get_state`/`get_settings`/`get_engine`/`get_services` | dependency | `web/deps.py` | FastAPI `Depends` helpers reading `request.app.state.web` |
| `get_meeting_service`/`get_tdoc_service`/`get_tdoc_cr_service`/`get_wi_service`/`get_tsg_service`/`get_search_service`/`get_semantic_search_service`/`get_tdoc_file_repo` | dependency | `web/deps.py` | Per-service `Depends` helpers |
| `get_job_repo` / `get_job_worker` | dependency | `web/deps.py` | Job repository + worker-handle deps (overridden in tests) |
| `build_mcp_server` | factory | `web/mcp_server.py` | Streamable-HTTP MCP via `mcp.server.mcpserver.MCPServer`; 23 tools (12 read + 11 job) |
| `_to_json` | function | `web/mcp_server.py` | `json.dumps(value, separators=(",", ":"), ensure_ascii=False)` — byte-matches Starlette `JSONResponse` |
| `meeting_rows`/`tdoc_rows`/`tsg_rows`/`wi_rows`/`spec_rows`/`spec_version_rows` | function | `web/render.py` | List-of-dict rows matching CLI `--format json` (`_coerce_cell`: `None`→`"-"`, date→isoformat) |
| `to_jsonable` | function | `web/render.py` | Recursively convert dataclasses/values to JSON-safe structures |
| `register_error_handlers`/`map_domain_error` | function | `web/errors.py` | Map domain errors→HTTP status (404/400/409/503/502/500) with stable slugs |
| `render_systemd_unit`/`render_launchd_plist`/`install_systemd`/`install_launchd`/`uninstall_systemd`/`uninstall_launchd` | function | `web/install.py` | OS service-unit install/uninstall helpers with `X-Doc3gpp-Managed` marker guard |
| `InstallNotManagedError` | exception | `web/install.py` | Raised when uninstalling a missing/non-managed unit |
| `all_routers` | function | `web/routes/__init__.py` | Aggregate `[landing, meetings, tdocs, tsgs, wis, specs, search, jobs]` |
| `is_htmx_request` | function | `web/filters.py` | `request.headers["HX-Request"] == "true"` — list routes use this to switch between full page (no header) and `partials/<resource>_results.html` fragment (HTMX-driven swap target). |
| `routes/jobs.py` | APIRouter | `web/routes/jobs.py` | `/jobs` — enqueue (sync/meetings, sync/tdocs, sync/tdocs/all, sync/specs, parse/tdocs, search/rebuild, cache/purge, sync_tdocs), list, get, SSE `/events`, cancel |
| `JobWorker` | class | `web/workers/job_worker.py` | asyncio worker: polls `QUEUED` jobs at `Settings.server.poll_interval_seconds` (default `1.0`s, range `0.05..60.0`), runs handlers (semaphore-bounded by `max_concurrent_jobs`), streams SSE, emits throttled periodic progress lines at `Settings.server.progress_interval_seconds` (default 5.0), cooperative cancel, skips handlers when the `mark_running` claim loses the race (`(claimed, job)` return), and sweeps orphaned `RUNNING` rows on startup → `FAILED` with `error="orphaned_after_restart"`. Retention cleanup runs on the independent `cleanup_interval_seconds` cadence. |
| `JobHandlers.KIND_TO_HANDLER` | mapping | `web/workers/handlers.py` | `JobKind`→async handler (network-touching sync/parse/rebuild/purge) |
| `Job` | dataclass | `models/jobs.py` | `id, kind, status, params, log_lines, result_summary, error, created_at, started_at, finished_at` |
| `JobKind` / `JobStatus` | enum | `models/jobs.py` | `SYNC_MEETINGS/SYNC_TDOCS/SYNC_TDOCS_ALL/SYNC_SPECS/PARSE_TDOCS/REBUILD_SEARCH/CACHE_PURGE`; `QUEUED/RUNNING/SUCCEEDED/FAILED/CANCELLED` |
| `SQLAlchemyJobRepository` | repository | `storage/repositories/jobs_sql.py` | SQL impl of `JobRepository`: `create/get/list/mark_running` (idempotent `UPDATE ... WHERE status = 'queued'` — `rowcount == 0` is a no-op so two workers can't both overwrite `started_at` / `log_lines`; returns `(claimed, job)` so the caller can detect a lost claim)/`append_log` (FIFO-capped at 50)/`mark_succeeded`/`mark_failed`/`mark_cancelled`/`delete_older_than` |
| `server_app` (6 commands) | Typer group | `cli_server.py` | `server start|stop|status|logs|install|uninstall`; `_require_server_enabled` gates all |

### Tests

| Symbol | Kind | File | Role |
| --- | --- | --- | --- |
| `test_web_app.py` | module | `tests/unit/test_web_app.py` | `build_app` wiring + `/healthz` |
| `test_web_routes.py` | module | `tests/unit/test_web_routes.py` | Read routes with fake services |
| `test_web_jobs_routes.py` | module | `tests/unit/test_web_jobs_routes.py` | Job routes + SSE framing with fake repo/handle |
| `test_web_install.py` | module | `tests/unit/test_web_install.py` | Install/uninstall render + marker guard |
| `test_cli_server_stubs.py` | module | `tests/unit/test_cli_server_stubs.py` | `server` sub-app registration + gate |
| `test_web_end_to_end.py` | module | `tests/integration/test_web_end_to_end.py` | Full lifecycle + cache-miss hint + cancel (real app, worker paused) |
| `test_mcp_end_to_end.py` | module | `tests/integration/test_mcp_end_to_end.py` | MCP tools + byte-parity with HTTP `?format=json` |
| `test_cli_server.py` | module | `tests/integration/test_cli_server.py` | CLI start/stop/status/logs/install/uninstall |

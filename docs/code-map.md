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
| `TDocCRDetails` | dataclass | `models/tdoc_cr.py` | Parsed CR cover-page fields (spec, cr_num, release, …). |
| `TDocExtractMeta` | dataclass | `models/tdoc_cr.py` | Cache-pointer sidecar (zip / markdown paths, `doc_filename`). |
| `DirectParseResult` | dataclass | `models/tdoc_cr.py` | Outcome of `tdoc parse --from-path/--from-url` (source kind, markdown, details, persistence flags). |
| `Tsg` | dataclass | `models/tsg.py` | Domain model for 3GPP TSG reference records. |
| `Wi` | dataclass | `models/wi.py` | Domain model for 3GPP Work Items (FK to `tsg_short`). |

## Repository contracts (`src/doc3gpp/repository/`)

| Symbol | Kind | File | Role |
| --- | --- | --- | --- |
| `MeetingRepository` | Protocol | `repository/protocols.py` | Contract for meeting storage. |
| `TDocRepository` | Protocol | `repository/protocols.py` | Contract for TDoc storage; `get_by_id` resolves canonical id strings. |
| `TDocFileRepository` | Protocol | `repository/protocols.py` | Contract for `tdoc_files` storage. |
| `TDocCrDetailRepository` | Protocol | `repository/protocols.py` | Contract for `tdoc_cr_details` + `tdoc_extracts` storage. |
| `TsgRepository` | Protocol | `repository/protocols.py` | Contract for TSG reference storage. |
| `WiRepository` | Protocol | `repository/protocols.py` | Contract for WI storage; upsert keyed by `(wi_id, tsg_short)`. |

## Services (`src/doc3gpp/services/`)

| Symbol | Kind | File | Role |
| --- | --- | --- | --- |
| `MeetingService` | class | `services/meetings_service.py` | Meeting sync + list orchestration. |
| `TDocService` | class | `services/tdoc_service.py` | TDoc sync + list orchestration. |
| `TDocSyncCoordinator` | class | `services/tdoc_sync_coordinator.py` | Cross-service orchestration for `tdoc sync`. |
| `TDocFileService` | class | `services/tdoc_file_service.py` | Auxiliary TDoc-file sync. |
| `TDocCrService` | class | `services/tdoc_cr_service.py` | End-to-end CR extraction (zip → cache → python-docx → parse → persist). Also exposes `extract_from_url` / `extract_from_bytes` for the `tdoc parse --from-path/--from-url` direct-mode path. |
| `TsgService` | class | `services/tsg_service.py` | TSG seeding + validation; exposes `build_tsg_url`. |
| `WiService` | class | `services/wi_service.py` | WI sync from DynaReport + list with SQL `LIKE` filters. |
| `build_*` | helpers | `services/factory.py` | Factory used by the CLI to wire repo / service instances. |

## Scraping, caching, parsers (`src/doc3gpp/scraping/`, `src/doc3gpp/parsers/`)

| Symbol | Kind | File | Role |
| --- | --- | --- | --- |
| `ScraperClient` | class | `scraping/client.py` | HTTP transport with retry / backoff via `httpx`. |
| `fetch_calendar` | function | `scraping/calendar_source.py` | Fetch DynaReport meeting HTML. |
| `fetch_tdocs_from_meeting_ftp` | function | `scraping/ftp_source.py` | Discover + fetch the meeting's TDoc-list XLSX. |
| `fetch_wis` | function | `scraping/wi_source.py` | Fetch DynaReport WI list HTML for a TSG. |
| `download_tdoc_zip` / `get_tdoc_zip_url` | functions | `scraping/tdoc_zip_source.py` | Resolve TDoc id → 3GPP URL + on-disk zip via `TDocCache`. `download_tdoc_zip` accepts an optional `cache_key_override` so the direct-parse path can key the zip cache on the original filename (D10 fix). |
| `TDocCache` / `CacheStatus` | class | `scraping/cache.py` | On-disk `zips/` + `markdown/` cache with FIFO eviction. |
| `parse_3gpp_calendar` | function | `parsers/calendar_parser.py` | DynaReport HTML → `Meeting` list. |
| `read_tdoc_sheet` | function | `parsers/tdoc_parser.py` | TDoc-list XLSX → `TDoc` list. |
| `parse_3gpp_wis` | function | `parsers/wi_parser.py` | WI DynaReport HTML → `Wi` list (extracts `wi_id`, `acronym`, `release`, `name`). |
| `convert_document_to_markdown` / `extract_docx_from_zip` | functions | `parsers/docx_converter.py` | python-docx conversion (`.docx` only; legacy `.doc` is rejected). |
| `parse_cr_details` | function | `parsers/cr_parser.py` | Markdown → `TDocCRDetails` (cover-page, optional TTCN overview, optional corrections). |
| `is_3gpp_ftp_url` / `direct_parse_bytes` / `derive_zip_cache_key` / `extract_tdoc_id_from_filename` | functions | `parsers/direct_extractor.py` | Helpers for the `tdoc parse --from-path/--from-url` direct path. `is_3gpp_ftp_url` is the 3GPP-FTP detection rule; `direct_parse_bytes` glues docx conversion + cover-page parsing. |

## Storage (`src/doc3gpp/storage/`)

| Symbol | Kind | File | Role |
| --- | --- | --- | --- |
| `Base` | declarative base | `storage/db/base.py` | SQLAlchemy `DeclarativeBase`. |
| ORM classes | `Mapped[]` | `storage/db/models.py` | `TDocORM`, `MeetingORM`, `TsgORM`, `WiORM`, `TDocFileORM`, `TDocCrDetailOrm`, `TDocExtractOrm`. |
| `get_engine` / `get_session_factory` | functions | `storage/db/session.py` | Cached engine + session factory. |
| `create_schema` | function | `storage/db/migrate.py` | `Base.metadata.create_all` bootstrap. |
| `SQLAlchemyMeetingRepository` | class | `storage/repositories/meeting_sql.py` | SQL impl of `MeetingRepository`. |
| `SQLAlchemyTDocRepository` | class | `storage/repositories/tdoc_sql.py` | SQL impl of `TDocRepository`. |
| `SQLAlchemyTDocFileRepository` | class | `storage/repositories/tdoc_file_sql.py` | SQL impl of `TDocFileRepository`. |
| `SQLAlchemyTDocCrRepository` | class | `storage/repositories/tdoc_cr_sql.py` | SQL impl of `TDocCrDetailRepository`. |
| `SQLAlchemyTsgRepository` | class | `storage/repositories/tsg_sql.py` | SQL impl of `TsgRepository`. |
| `SQLAlchemyWiRepository` | class | `storage/repositories/wi_sql.py` | SQL impl of `WiRepository`. |
| `_apply_text_filter` / `_apply_date_filter` | helpers | `storage/repositories/tdoc_sql.py` | SQLAlchemy helpers that consume `cli_filters.DATE_FILTER_RE` and the rich-filter grammar. |
| `*_engine_kwargs` | functions | `storage/backends/{sqlite,mysql,postgres}.py` | Per-dialect engine configuration. |

## Settings / config (`src/doc3gpp/settings/`)

| Symbol | Kind | File | Role |
| --- | --- | --- | --- |
| `Settings` | pydantic-settings | `settings/schema.py` | Root config: flat `DOC3GPP_*` + nested sub-models. |
| `OutputSettings` | model | `settings/schema.py` | Default `format` + per-command field lists. |
| `OutputFieldsSettings` | model | `settings/schema.py` | Per-list-command `default_fields` lists. |
| `CacheSettings` | model | `settings/schema.py` | Disk cache (`dir`, `size_limit_mb`, `purge_confirm`). |
| `TDocParseSettings` | model | `settings/schema.py` | `tdoc parse` knobs (`max_batch`). |
| `get_settings` | function | `settings/loader.py` | Cached settings loader (env + TOML file). |
| `find_config_file` | function | `settings/config_source.py` | TOML discovery (`$DOC3GPP_CONFIG` → `./doc3gpp.toml` → XDG). |
| `load_config_data` | function | `settings/config_source.py` | `(path, dict)` for the active TOML file. |

## Filter / ID helpers (`src/doc3gpp/cli_filters.py`)

| Symbol | Kind | File | Role |
| --- | --- | --- | --- |
| `validate_date_filter` | function | `cli_filters.py` | Boundary guard for `--uploaded-date`. |
| `TDOC_ID_RE` | regex | `cli_filters.py` | CR-shape TDoc id regex `[RSC][1-9][-sw]\d{6}` (case-insensitive). |
| `parse_tdoc_id` | function | `cli_filters.py` | Boundary guard for `meeting list --tdoc`; returns `(prefix, number)` or raises. |
| `validate_tdoc_id` | function | `cli_filters.py` | `parse_tdoc_id` wrapper that raises on bad input. |
| `DATE_FILTER_RE` | regex | `cli_filters.py` | `<op> 'YYYY-MM-DD'` pattern for date comparisons. |

## CLI entry (`src/doc3gpp/cli.py`)

Seven Typer sub-apps: `db` (`check` / `init` / `reset`), `meeting` (`sync` / `list`), `tdoc` (`sync` / `list` / `parse` / `show`), `tsg` (`list` / `show` / `seed`), `wi` (`sync` / `list`), `config` (`path` / `show`), `cache` (`status` / `purge`). Per-command option and behavior details live in [`docs/cli.md`](cli.md).

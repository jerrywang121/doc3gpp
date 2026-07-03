# doc3gpp TODO

Track issues discovered during code review. Fix priority reflects correctness/impact, not effort.

Source review: TDoc handling code review (2026-07-02).
Source review: Meetings handling code review (2026-07-02).
Source review: Design-issues sweep (2026-07-03).

## Resolved (2026-07-02)

Fixed in commit `fix/tdoc-pipeline-critical-fixes`:

- **#1 N+1 transactions** — added `TDocRepository.upsert_many`; `TDocService.sync_from_meeting_ftp` now uses a single transaction.
- **#2 Header-detection false positives** — `read_tdoc_sheet` now requires a TDoc column AND ≥1 expected header marker; title-only rows like `"TDoc List — RAN5#111"` are correctly rejected.
- **#3 Empty title NOT NULL violation** — `TDoc.title` is now `str | None` (model + ORM nullable). Parser emits `None` for empty/whitespace cells.
- **#4 Bare `except Exception`** — narrowed to `httpx.HTTPError`; tracks per-subfolder failures (directory + XLSX) and raises `RuntimeError` with all URLs when all subfolders exhaust.
- **#22 Indentation inconsistency** in `ftp_source.py:89-110` — fixed incidentally during #3 parser cleanup.

Tests added: `tests/unit/test_tdoc_parser.py`, `tests/unit/test_tdoc_repository_crud.py`, `tests/unit/test_ftp_source.py` (19 cases total).

## Resolved (2026-07-02, important batch)

Fixed in commit `fix/tdoc-pipeline-important-fixes`:

- **#5 Retry/backoff in `ScraperClient`** — added `ScraperClient._request_with_retry` with exponential backoff. Retries 5xx/408/429 status codes and transient exceptions (connect/read/write/pool timeouts, network/protocol errors). Configurable via `DOC3GPP_HTTP_MAX_RETRIES` (default 3) and `DOC3GPP_HTTP_RETRY_BACKOFF` (default 0.5s).
- **#6 `Date` columns** — `TDocORM.reservation_date` and `uploaded_date` are now `Date` (was `String(64)`); `TDoc.reservation_date`/`uploaded_date` are `date | None`; parser uses `_parse_date_cell` to coerce ISO-style strings and `datetime`/`date` instances.
- **#7 `updated_at`** — added `TDocORM.updated_at` and surface in `TDoc.updated_at`; stamped on every `upsert_many` write so re-syncs bump the timestamp. New rows start with `updated_at` populated.
- **#8 `pick_col` exact match** — first pass matches exact (case-insensitive) header; substring match is fallback only. `"Type"` no longer matches `"Type of CR"` when both columns exist.
- **#9 Skip-count logging** — `read_tdoc_sheet` counts rows dropped because `CR_ID_RE` did not match and emits a single `WARNING` with the count and regex pattern.
- **#10 Coordinator** — introduced `TDocSyncCoordinator` (Protocol-typed repos in, `TDocService`/`MeetingService` orchestration out). `cli.py tdoc sync` now delegates to it; raised exceptions are typed (`MeetingNotFoundError`, `MeetingMissingFtpUrlError`).
- **#11 No concrete repo imports in CLI** — services constructed via `doc3gpp.services.factory.build_*` helpers. CLI no longer imports `SQLAlchemy*Repository` directly.
- **#18 `User-Agent`** — replaced placeholder with `doc3gpp/0.1 (+https://github.com/jerrywang121/doc3gpp)`.
- **#23 `to_text`** — now returns `Optional[str]`; `None` for missing or empty/whitespace cells. Keeps optional ORM columns correctly typed.
- **#24 `create_schema()` removed from `tdoc sync`** — `db init` remains the single boundary. `tdoc sync` now assumes the schema has been created.

Tests added: `tests/unit/test_scraper_client.py`, `tests/unit/test_tdoc_sync_coordinator.py`, plus extensions to `tests/unit/test_tdoc_parser.py`, `tests/unit/test_tdoc_repository_crud.py`. Total coverage of new files: `tdoc_sync_coordinator.py` 100%, `factory.py` 100%, `client.py` 98%.

## Resolved (2026-07-03, design-issues sweep)

Fixed in branch `fix/design-issues-sweep`:

- **#12 `TDocORM.title: Text`** — switched from `String(500)` to `Text` to match `WiORM.name`. Existing SQLite/Postgres tables will need a one-time migration (no Alembic yet; `Base.metadata.create_all` is no-op for existing schemas).
- **#13 `meeting_like` auto-wrap** — CLI now auto-wraps `--meeting` patterns with `%...%` when no SQL wildcards are present, so `--meeting RAN5#111` matches substrings. Explicit wildcards (`%`, `_`) are passed through unchanged.
- **#14 `year` filter decoupled from `CR_ID_RE`** — new `tdoc_id_year()` helper in `parsers/tdoc_parser.py` extracts the 2-digit year in Python; `SQLAlchemyTDocRepository.list` now pre-fetches `tdoc_id`s and applies `IN (...)` based on Python-side year decoding. SQL no longer hard-codes `substr(..., 4, 2)`.
- **#15 `TDocService.save()` removed** — was dead code per the TODO. `test_tdoc_service_save_and_list` (integration) was exercising only the removed path; removed since `test_tdoc_repository_upsert_and_list` and `test_tdoc_service_sync_from_meeting_ftp` cover the live surface.
- **#16 `TDocWithMeeting` DTO** — new `@dataclass(slots=True) TDocWithMeeting(tdoc: TDoc, meeting_name: str | None)` separates persistence from presentation. `TDoc` no longer carries `meeting_name`. `SQLAlchemyTDocRepository.list_with_meeting` / `TDocService.list_recent_with_meeting` produce DTOs for CLI/export. CLI and `storage/export.py` updated to consume the DTO. Tests updated to construct TDocWithMeeting where the joined view is needed.
- **#17 `TDoc` docstring drift** — drift was already resolved by `de50995`; verified clean (only `meeting_id` and `meeting_name` are referenced, both correctly named).
- **M7 `Meeting.start_date` / `end_date` relaxed** — now `date | None = None` on the dataclass; `MeetingORM.start_date` / `end_date` are `nullable=True`. The CLI test that already used `None` now matches the type.
- **M9 `_parse_field_selection` helper extracted** — `meeting_list`, `tdoc_list`, `tsg_list` now share one field-selection helper. The `"all" in [f.lower() for f in requested]` substring bug is fixed (`any(f.lower() == "all" for f in selected)` is a proper equality check). Error message format preserved for backward-compat with existing tests.
- **M10 `meeting list --tsg` validation** — `--tsg` is validated via `TsgService.is_known_short_name` and uppercased for consistency with `meeting sync`. Unknown values raise `typer.BadParameter` listing the known short names.
- **M12 `ftp_url` in `meeting list` defaults** — the default field set now includes `ftp_url` (the most useful column for `tdoc sync` planning); `title` and `updated_at` remain excluded.
- **M13 `meeting list --offset` pagination** — new `--offset` option threads through `MeetingService.list_recent` → `MeetingRepository.list` → `SQLAlchemyMeetingRepository.list` (Protocol signature in sync) → SQL `OFFSET` clause. `--limit` / `--offset` pagination enabled without re-running the filters.
- **M18 `_build_meeting_url` parameterised** — takes an `ext` argument (default `"htm"`); `ext="html"` is supported. Validates the extension to keep callers from passing arbitrary values.
- **M19 `meeting sync` help text** — now describes the additive year-window trim behavior explicitly: narrower `--closed-years` deletes older rows, widening does not resurrect.

Tests added: `tests/unit/test_tdoc_service_sync.py` (5 cases for `TDocService.sync_from_meeting_ftp`), `tests/unit/test_tdoc_sync_cli.py` (6 cases for the `tdoc sync` CLI selector + error conversion), and an extension to `tests/unit/test_tdoc_cli_fields.py` covering the new `_auto_wrap_like` helper. Test fakes updated to construct `TDocWithMeeting` where the joined view is exercised.

## TDoc pipeline issues

### Important (fix soon)

_None remaining — important batch resolved 2026-07-02._

### Design (address when touching adjacent code)

_None remaining — design batch resolved 2026-07-03._

### Track but don't fix without input

| # | Issue | Location |
|---|---|---|
| 19 | Hardcoded `https://www.3gpp.org/ftp/` — silently returns empty if 3GPP moves to CDN | `ftp_source.py:72` |
| 20 | `ScraperClient.get_text` broad `except Exception` — programming errors look identical to network errors in logs | `client.py:35-37, 46-48` |
| 21 | `get_settings` `@lru_cache` + `ScraperClient.__init__` reads settings once — env changes mid-process don't propagate | `client.py:20-26`, `settings/loader.py` |

## Test coverage gaps

| Symbol | Status | Suggested test |
|---|---|---|
| `TDocService.sync_from_meeting_ftp` | covered | `tests/unit/test_tdoc_service_sync.py` |
| CLI `tdoc sync` / `tdoc list` | covered | `tests/unit/test_tdoc_sync_cli.py`, `tests/unit/test_tdoc_cli_fields.py` |

Resolved since review:

- `read_tdoc_sheet` — covered by `tests/unit/test_tdoc_parser.py` (header detection + empty-cell coercion, pick_col exact match, WARNING skip count, date parsing)
- `get_text`, `get_bytes` — covered by `tests/unit/test_scraper_client.py` (retry, backoff, status codes, UA)
- `TDocORM` — covered via `tests/unit/test_tdoc_repository_crud.py` (date columns, updated_at, upsert_many)
- `SQLAlchemyTDocRepository.upsert_many` — covered via `tests/unit/test_tdoc_repository_crud.py`
- `TDocSyncCoordinator` — covered by `tests/unit/test_tdoc_sync_coordinator.py` (typed errors, Protocol-typed repos, both selectors)
- `SQLAlchemyTDocRepository.list_with_meeting` — covered via `tests/unit/test_tdoc_repository_filters.py::test_list_filter_meeting`

## Meetings pipeline issues

### Resolved (2026-07-02, important batch)

- **M1 `_filter_by_year_window` drift** — replaced `timedelta(days=356 * N)` with a stdlib-only `years_ago(today, N)` helper (`date.replace(year=...)` with Feb 29 clamp). No drift; a meeting ending exactly N years ago is now included.
- **M2 Protocol ↔ impl signature drift** — `MeetingRepository.list` now declares `limit, tsg, name_like, location_like, year`, matching the impl signature.
- **M3 `Meeting.updated_at` never written** — `SQLAlchemyMeetingRepository.upsert_many` now stamps `datetime.now(tz=timezone.utc)` on every write via a bulk fetch-existing-IDs + INSERT/UPDATE pattern (mirrors `wi_sql._persist`).
- **M4 Non-defensive calendar parser** — `_parse_date` returns `None` on bad input; malformed rows (bad dates, short rows, swapped dates) are logged + skipped instead of aborting the whole sync. FTP/doc extraction wrapped in defensive try/except.
- **M5 Silent empty-result on missing table** — `parse_3gpp_calendar` now logs a WARNING when the page has content but no `<table class="meetings">` is found; empty pages stay silent.
- **M6 `sync` never trimmed out-of-window rows** — `MeetingService.sync` now calls `MeetingRepository.delete_with_end_before(start_cutoff)` after the upsert, so narrowing `--closed-years` on a re-sync purges the older rows. Added `MeetingRepository.delete_with_end_before` to the Protocol and the SQL impl.

Tests added: `tests/unit/test_calendar_parser_defensive.py`, `tests/unit/test_meetings_year_window.py`, `tests/unit/test_meeting_repository_upsert.py`, `tests/unit/test_meetings_service_sync.py` (28 cases total).

### Important (fix soon)

_None remaining._

### Design (address when touching adjacent code)

_None remaining — design batch resolved 2026-07-03._

### Track but don't fix without input

_None remaining — design batch resolved 2026-07-03 (M18 parameterised; M19 help text updated)._

### Meetings test coverage gaps

| Symbol | Status | Suggested test |
|---|---|---|
| `filter_by_year_window` (year drift) | covered | `tests/unit/test_meetings_year_window.py` |
| `parse_3gpp_calendar` malformed row + CANCELLED variants | covered | `tests/unit/test_calendar_parser_defensive.py` |
| `meeting_sql.upsert_many` (updated_at, bulk fetch+update) | covered | `tests/unit/test_meeting_repository_upsert.py` |
| `MeetingService.sync` trim behaviour | covered | `tests/unit/test_meetings_service_sync.py` |
| `meeting list --tsg` validation | covered (M10) | `tests/unit/test_meeting_sync_tsg_validation.py` + updated `tests/unit/test_meeting_cli_filters_combined.py` |
| `meeting list --offset` pagination | covered by extension | `tests/unit/test_meeting_cli_filters_combined.py` (asserts `offset` in captured filters) |
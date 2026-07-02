# doc3gpp TODO

Track issues discovered during code review. Fix priority reflects correctness/impact, not effort.

Source review: TDoc handling code review (2026-07-02).
Source review: Meetings handling code review (2026-07-02).

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

## TDoc pipeline issues

### Important (fix soon)

_None remaining — important batch resolved 2026-07-02._

### Design (address when touching adjacent code)

| # | Issue | Location |
|---|---|---|
| 12 | `TDocORM.title: String(500)` arbitrary; `Text` would match `WiORM.name` convention | `storage/db/models.py:28` |
| 13 | `meeting_like` filter is hidden LIKE — `--meeting RAN5#111` returns nothing without `%` | `cli.py:284-287` |
| 14 | `year` filter uses `substr(tdoc_id, 4, 2)` — coupled to `CR_ID_RE` regex shape | `tdoc_sql.py:120` |
| 15 | `TDocService.save()` is dead code | `tdoc_service.py:19-22` |
| 16 | `meeting_name` mixes presentation-time data with persistence schema — consider `TDocWithMeeting` DTO | `models/tdoc.py:21`, `tdoc_sql.py:154-176` |
| 17 | `TDoc` docstring drift — says `meeting` but field is `meeting_id` | `models/tdoc.py:13` |

### Track but don't fix without input

| # | Issue | Location |
|---|---|---|
| 19 | Hardcoded `https://www.3gpp.org/ftp/` — silently returns empty if 3GPP moves to CDN | `ftp_source.py:72` |
| 20 | `ScraperClient.get_text` broad `except Exception` — programming errors look identical to network errors in logs | `client.py:35-37, 46-48` |
| 21 | `get_settings` `@lru_cache` + `ScraperClient.__init__` reads settings once — env changes mid-process don't propagate | `client.py:20-26`, `settings/loader.py` |

## Test coverage gaps

| Symbol | Status | Suggested test |
|---|---|---|
| `TDocService.sync_from_meeting_ftp` | no direct unit test (covered via integration) | mock `fetch_tdocs_from_meeting_ftp`, verify `upsert_many` called once |
| CLI `tdoc sync` / `tdoc list` | partial CLI test (filter) | field-selection rejects unknown fields, `--meeting` resolves, `--meeting` missing → BadParameter |

Resolved since review:

- `read_tdoc_sheet` — covered by `tests/unit/test_tdoc_parser.py` (header detection + empty-cell coercion, pick_col exact match, WARNING skip count, date parsing)
- `get_text`, `get_bytes` — covered by `tests/unit/test_scraper_client.py` (retry, backoff, status codes, UA)
- `TDocORM` — covered via `tests/unit/test_tdoc_repository_crud.py` (date columns, updated_at, upsert_many)
- `SQLAlchemyTDocRepository.upsert_many` — covered via `tests/unit/test_tdoc_repository_crud.py`
- `TDocSyncCoordinator` — covered by `tests/unit/test_tdoc_sync_coordinator.py` (typed errors, Protocol-typed repos, both selectors)

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

| # | Issue | Location |
|---|---|---|
| M7 | `Meeting` model `start_date`/`end_date` typed `date` but `tests/unit/test_meetings_cli.py` instantiates them as `None`. Either relax to `date \| None = None` (and ORM `nullable=True`) or fix the test. | `models/meeting.py:29-30` vs `tests/unit/test_meetings_cli.py:17-18` |
| ~~M8~~ | ~~`session.merge()` in a loop is N SELECTs per upsert...~~ — resolved alongside M3. | `storage/repositories/meeting_sql.py:36-72` |
| M9 | CLI `--fields` "all"/invalid-field handling duplicated in `meeting_list`, `tdoc_list`, `tsg_list`. Extract `_parse_field_selection(requested, allowed, default)` helper; tighten `if "all" in [f.lower() for f in requested]:` to a generator. | `cli.py:176-205, 332-359, 428-444` |
| M10 | `meeting list` does not validate `--tsg` against the `tsgs` table (only `meeting sync` does). Validate via `TsgService.is_known_short_name` after upper-casing for consistency. | `cli.py:151` |
| ~~M11~~ | ~~Brittle CANCELLED detection...~~ — resolved alongside M4. Detection now uses `if "CANCELLED" in title.upper():` and the parser test covers 4 variants. | `parsers/calendar_parser.py:96-99` |
| M12 | `meeting list` default omits `ftp_url` — the most useful column for `tdoc sync` planning. Include it (or expose as an extra column). | `cli.py:190` |
| M13 | No pagination on `meeting list` — only a `limit` cap (max 500). Add `--offset` / `LIMIT … OFFSET …` before the dataset grows. | `cli.py:148-159` |
| ~~M14~~ | ~~Ordering non-deterministic on ties...~~ — resolved: `meeting_sql.list` adds `MeetingORM.meeting_id.desc()` as a secondary sort. | `storage/repositories/meeting_sql.py:78-82` |
| ~~M15~~ | ~~No `start_date <= end_date` validation in parser...~~ — resolved alongside M4. Swapped dates now log + skip. | `parsers/calendar_parser.py:96-99` |
| ~~M16~~ | ~~Duplicated row-to-domain mapping in three near-identical `Meeting(**)` constructions...~~ — resolved alongside M3. Extracted `_orm_to_domain(row)`. | `storage/repositories/meeting_sql.py:97-108` |
| ~~M17~~ | ~~`_filter_by_year_window` is a `@staticmethod`...~~ — resolved alongside M1. The helper is now a module-level function (`filter_by_year_window`) with an injectable `today` parameter. | `services/meetings_service.py:118-142` |

### Track but don't fix without input

| # | Issue | Location |
|---|---|---|
| M18 | `_build_meeting_url` hard-codes `.htm` extension; 3GPP also serves `.html`. Parameterise. | `cli.py:65-66` |
| M19 | ~~CLI help does not describe the additive year-filter behaviour...~~ — partly resolved by the M6 trim, but CLI help text could still call out that narrower `--closed-years` now deletes older rows. | `cli.py:119-139` |

### Meetings test coverage gaps

| Symbol | Status | Suggested test |
|---|---|---|
| `filter_by_year_window` (year drift) | covered | `tests/unit/test_meetings_year_window.py` |
| `parse_3gpp_calendar` malformed row + CANCELLED variants | covered | `tests/unit/test_calendar_parser_defensive.py` |
| `meeting_sql.upsert_many` (updated_at, bulk fetch+update) | covered | `tests/unit/test_meeting_repository_upsert.py` |
| `MeetingService.sync` trim behaviour | covered | `tests/unit/test_meetings_service_sync.py` |
| `meeting list` `--tsg` validation | not tested | pass unknown short name; expect `typer.BadParameter` |
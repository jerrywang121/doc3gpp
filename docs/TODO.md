# doc3gpp TODO

Track issues discovered during code review. Fix priority reflects correctness/impact, not effort.

Source review: TDoc handling code review (2026-07-02).

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
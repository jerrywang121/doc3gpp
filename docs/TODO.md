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

## TDoc pipeline issues

### Important (fix soon)

| # | Issue | Location |
|---|---|---|
| 5 | No retry/backoff in `ScraperClient` (transient network failure aborts sync) | `scraping/client.py` |
| 6 | `reservation_date`/`uploaded_date` stored as `String(64)` — should be `Date` | `models/tdoc.py`, `TDocORM` |
| 7 | No `updated_at` on TDoc; `created_at` does not bump on update | `TDocORM` |
| 8 | `pick_col` substring match — column `"Type of CR"` matches `"Type"` first | `parsers/tdoc_parser.py:49` |
| 9 | `CR_ID_RE` only matches `[RSC][1-9][-sw]\d{6}` — silent data loss on new conventions; log skip count at WARNING | `parsers/tdoc_parser.py:13` |
| 10 | CLI `tdoc sync` orchestrates `MeetingService` + `TDocService` — belongs in a coordinator | `cli.py:245-275` |
| 11 | CLI instantiates concrete `SQLAlchemy*Repository` — bypasses Protocol | `cli.py:246-247` |
| 18 | `User-Agent` placeholder `https://github.com` (also in AGENTS.md anti-patterns) | `client.py:23` |
| 23 | `to_text` returns `""` for None — conflates "missing" with "empty" for optional fields | `parsers/tdoc_parser.py:37-41`, `models/tdoc.py` |
| 24 | `create_schema()` called inside `tdoc sync` — blurs `db init` boundary (also in AGENTS.md) | `cli.py:245` |

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
| `ScraperClient` | no tests | mock `httpx.Client.get`, assert headers + verify |
| `TDocService.sync_from_meeting_ftp` | no direct unit test (covered via integration) | mock `fetch_tdocs_from_meeting_ftp`, verify `upsert_many` called once |
| CLI `tdoc sync` / `tdoc list` | partial CLI test (filter) | field-selection rejects unknown fields, `--meeting` resolves, `--meeting` missing → BadParameter |

Resolved since review:

- `read_tdoc_sheet` — covered by `tests/unit/test_tdoc_parser.py` (header detection + empty-cell coercion)
- `get_text`, `get_bytes` — covered indirectly via `tests/unit/test_ftp_source.py` mocks
- `TDocORM` — covered via `tests/unit/test_tdoc_repository_crud.py`
- `SQLAlchemyTDocRepository.upsert_many` — covered via `tests/unit/test_tdoc_repository_crud.py`
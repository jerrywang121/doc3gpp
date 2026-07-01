# doc3gpp Agent Guide

**Generated:** 2026-06-30
**Branch:** main

A Python CLI/library that scrapes 3GPP meeting calendars and TDocs and stores them in SQL.

## QUICK START

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
doc3gpp db init
doc3gpp db check
```

Build system: **hatchling**. Stack: Python 3.10+, SQLAlchemy 2.0, Pydantic v2 + pydantic-settings, httpx, BeautifulSoup4 + lxml, openpyxl, alembic (installed but not wired).
Extras: `pip install -e ".[cli]"` (Typer CLI), `.[mysql]` (pymysql), `.[postgres]` (psycopg[binary]).
`pip install doc3gpp` installs the SDK only; `pip install "doc3gpp[cli]"` or `pipx install "doc3gpp[cli]"` adds the `doc3gpp` CLI command.
`references-external/` is gitignored local scratch — never commit changes there.

## STRUCTURE

```
doc3gpp/
├── src/doc3gpp/          # package root
│   ├── cli.py            # Typer commands (7): db, meetings, tdoc, tsg
│   ├── config.py         # re-export shim (legacy)
│   ├── models/           # Meeting, TDoc, Tsg dataclasses
│   ├── parsers/          # HTML/Excel → domain objects (no network)
│   ├── repository/       # Protocol contracts (abstract)
│   ├── scraping/         # HTTP/FTP transport (no parsing)
│   ├── services/         # orchestration: MeetingService, TDocService, TsgService
│   ├── settings/         # pydantic-settings (env-driven, @lru_cache)
│   └── storage/          # persistence umbrella
│       ├── backends/     # engine kwargs per dialect
│       ├── db/           # ORM models, session, migrate, base
│       │   └── migrations/   # placeholder for future Alembic
│       └── repositories/ # SQLAlchemy impls of Protocols
├── tests/
│   ├── unit/             # 19 files (mock external calls)
│   ├── integration/      # 8 files (sqlite + online + mysql)
│   └── fixtures/         # sample HTML + XLSX
├── docs/                 # architecture, CLI ref, implementation status
└── scripts/              # test_sqlite.sh, dev_run.sh
```

## WHERE TO LOOK

| Task | Location | Notes |
|------|----------|-------|
| Add a CLI command | `src/doc3gpp/cli.py` | Follow pattern: service → repo → CLI |
| Add a data source | `src/doc3gpp/scraping/` + `src/doc3gpp/parsers/` | Network in scraping/, parsing in parsers/ |
| Add a domain model | `src/doc3gpp/models/` | Pydantic dataclass, no ORM leak |
| Add a storage backend | `src/doc3gpp/storage/backends/` | Engine kwargs per dialect |
| Change filters for list | `src/doc3gpp/repository/protocols.py` + `src/doc3gpp/storage/repositories/` | Update BOTH protocol and impl |
| Run all tests | `./scripts/test_sqlite.sh` | Unit + integration, sqlite-only |
| Run online tests | `python -m pytest -m online -rs` | Hits live 3gpp.org + FTP |

## CODE MAP

| Symbol | Type | File | Role |
|--------|------|------|------|
| `Meeting` | dataclass | `models/meeting.py` | Domain model for meetings |
| `TDoc` | dataclass | `models/tdoc.py` | Domain model for TDocs |
| `Tsg` | dataclass | `models/tsg.py` | Domain model for 3GPP TSG reference records |
| `MeetingRepository` | Protocol | `repository/protocols.py` | Contract for meeting storage |
| `TDocRepository` | Protocol | `repository/protocols.py` | Contract for TDoc storage |
| `TsgRepository` | Protocol | `repository/protocols.py` | Contract for TSG reference storage |
| `MeetingService` | class | `services/meetings_service.py` | Meeting sync + list orchestration |
| `TDocService` | class | `services/tdoc_service.py` | TDoc sync + list orchestration |
| `TsgService` | class | `services/tsg_service.py` | TSG seeding + validation; also exposes `build_tsg_url` URL pattern |
| `ScraperClient` | class | `scraping/client.py` | HTTP transport with httpx |
| `fetch_calendar` | function | `scraping/calendar_source.py` | Fetch DynaReport HTML |
| `fetch_tdocs_from_meeting_ftp` | function | `scraping/ftp_source.py` | Discover + fetch TDoc XLSX from FTP |
| `parse_3gpp_calendar` | function | `parsers/calendar_parser.py` | HTML→Meeting list |
| `read_tdoc_sheet` | function | `parsers/tdoc_parser.py` | XLSX→TDoc list |
| `SQLAlchemyMeetingRepository` | class | `storage/repositories/meeting_sql.py` | SQL impl of MeetingRepository |
| `SQLAlchemyTDocRepository` | class | `storage/repositories/tdoc_sql.py` | SQL impl of TDocRepository |
| `SQLAlchemyTsgRepository` | class | `storage/repositories/tsg_sql.py` | SQL impl of TsgRepository |
| `get_engine` | function | `storage/db/session.py` | Cached engine factory |
| `create_schema` | function | `storage/db/migrate.py` | Base.metadata.create_all |
| `get_settings` | function | `settings/loader.py` | Cached settings loader |
| `Settings` | model | `settings/schema.py` | pydantic-settings for DOC3GPP_* |

## COMMANDS

```bash
# Lint (ruff is the only configured tool)
ruff check .

# Full sqlite test suite (unit + integration, excludes online + mysql)
./scripts/test_sqlite.sh

# Online tests (opt-in, hits live 3gpp.org + FTP)
python -m pytest -m online -rs

# MySQL tests (needs DOC3GPP_TEST_MYSQL_URL)
python -m pytest -m mysql

# Bootstrap dev environment
./scripts/dev_run.sh
```

- `pyproject.toml [tool.pytest.ini_options]` sets `pythonpath = ["src"]`, so tests resolve `doc3gpp.*` without editable install.
- Default `pytest` excludes both `online` and `mysql` markers. New tests stay in the default pool unless they need network or a MySQL server.
- **No CI pipeline exists.** The project relies on local `scripts/test_sqlite.sh` runs. No `.github/workflows/`, no Makefile, no Dockerfile.

## ARCHITECTURE BOUNDARIES

`src/doc3gpp/` — strict layer separation:

| Layer | Path | Rule |
|---|---|---|
| `models/` | `Meeting`, `TDoc` | pass between layers; **never leak ORM attributes** |
| `repository/` | `protocols.py` | abstract repo contracts only |
| `services/` | `meetings_service.py`, `tdoc_service.py` | orchestration; injected with a repo impl |
| `scraping/` | `client.py`, `calendar_source.py`, `ftp_source.py` | network/HTTP only — **no HTML parsing** |
| `parsers/` | `calendar_parser.py`, `html_parsers.py`, `tdoc_parser.py`, `normalizers.py` | HTML/Excel → domain only — **no network** |
| `storage/` | `db/`, `backends/`, `repositories/` | persistence only — **no business logic** |
| `settings/` | `schema.py`, `loader.py` | env-driven config |
| `cli.py` | Typer commands | thin: build service, call it, format output |

Flow:
- `doc3gpp meetings sync` → `MeetingService.sync` → fetch DynaReport HTML → `parse_3gpp_calendar` → `SQLAlchemyMeetingRepository.upsert_many`
- `doc3gpp tdoc sync --meeting-id <id>` resolves stored `Meeting.ftp_url` from DB, fetches `TDoc_List_Meeting_*.xlsx` from FTP. **No meeting row → no TDoc sync.**
- `doc3gpp db init` calls `create_schema()` and then `TsgService.seed_defaults()` to populate the `tsgs` reference table.
- `doc3gpp meetings sync --tsg <short>` validates `<short>` against the `tsgs` table (auto-seeded if empty); an unknown value raises `typer.BadParameter` listing the known short names.
- `doc3gpp tsg list` and `doc3gpp tsg show` read from the `tsgs` table via `SQLAlchemyTsgRepository`. `doc3gpp tsg seed` upserts the canonical 16 rows.

## SETTINGS CACHING — FLUSH IN TESTS

Both loaders are `@lru_cache(maxsize=1)`:

- `doc3gpp.settings.loader.get_settings`
- `doc3gpp.storage.db.session.get_engine`

If a test or fixture changes `DOC3GPP_*` env vars via `monkeypatch`, it **must** `cache_clear()` both. See the `sqlite_env` fixture in `tests/conftest.py` for the canonical pattern.

Recognised env vars: `DOC3GPP_DATABASE_URL`, `DOC3GPP_DB_ECHO`, `DOC3GPP_DB_POOL_SIZE`, `DOC3GPP_DB_AUTO_MIGRATE`, `DOC3GPP_LOG_LEVEL`, `DOC3GPP_HTTP_VERIFY`. MySQL tests additionally use `DOC3GPP_TEST_MYSQL_URL`.

## CONVENTIONS

- Static type hints on all new code (project targets py310, `ruff target-version = "py310"`).
- New features ship with both a **unit test** (mock external calls) and an **integration test** against sqlite under `tests/integration/`.
- Ruff only: `line-length = 100`, no custom rule selection (defaults). No mypy/pyright configured.
- Keep `README.md`, `AGENTS.md`, and `docs/*.md` in sync when behaviour or CLI surface changes.
- Do not auto-commit. Plan first, implement, run lint + the sqlite test profile, then hand off.
- Scripts in `scripts/` use `set -euo pipefail`.

## ANTI-PATTERNS (THIS PROJECT)

- **Protocol ↔ Impl signature drift.** `MeetingRepository.list` in `repository/protocols.py` declares only `limit`, but `SQLAlchemyMeetingRepository.list` takes `limit, tsg, name_like, location_like, year`. Update BOTH when changing filter signatures.
- **CLI bypasses the Protocol.** `cli.py` imports concrete `SQLAlchemy*Repository` classes directly (lines 18-19), hard-wiring the backend. New commands should ideally depend on the Protocol-typed service interface.
- **`create_schema()` called redundantly.** Every `sync` command calls `create_schema()` — it's idempotent but blurs the `db init` boundary.
- **Cross-service orchestration in CLI.** `tdoc sync` instantiates both `MeetingService` and `TDocService` and stitches them together. This logic belongs in a service or coordinator.
- **Doc drift.** `docs/architecture.md` lists a `tdoc add` command that doesn't exist. Keep docs in sync when CLI surface changes.
- **Acknowledged `# noqa: F401`.** Three in `storage/db/migrate.py` (lines 4-6) — side-effect imports required for SQLAlchemy `Base.metadata` registration. Do not remove.
- **Placeholder User-Agent.** `scraping/client.py:23` uses `https://github.com` as placeholder — replace with actual project URL before publishing.

## UNIQUE STYLES

- **`repository/` (abstract) and `storage/repositories/` (concrete) are separate packages.** Abstractions live in `src/doc3gpp/repository/`, implementations in `src/doc3gpp/storage/repositories/`. This split means a reader follows two paths to trace a repo from contract to SQL.
- **`config.py` is a re-export shim** for backwards compatibility. New imports should go to `doc3gpp.settings` directly.
- **`cache.py` and `export.py` sit at `storage/` root**, not in a subpackage. Mildly unconventional but stable.
- **MySQL tests double-gated**: `pytestmark` marker + `@pytest.mark.skipif` on env var.

## KNOWN CONSTRAINTS

- **No Alembic.** Schema bootstrap is `Base.metadata.create_all` via `storage/db/migrate.py`. `DOC3GPP_DB_AUTO_MIGRATE` is a flag only — does not run migrations.
- Calendar parser coupled to **current 3GPP DynaReport table layout** — upstream changes will break `meetings sync`.
- TDoc extraction covers **FTP Excel lists only**. `GenerateDocumentList.aspx` and expanded metadata columns are unimplemented.
- Online tests access live `3gpp.org` + FTP — flaky; run with `-rs` to surface skip reasons.

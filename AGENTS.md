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
Extras: `pip install -e ".[cli]"` (Typer CLI), `.[mysql]` (pymysql), `.[postgres]` (psycopg[binary]), `.[extract]` (`python-docx` for the TDoc extraction pipeline).
`pip install doc3gpp` installs the SDK only; `pip install "doc3gpp[cli]"` or `pipx install "doc3gpp[cli]"` adds the `doc3gpp` CLI command.
`references-external/` is gitignored local scratch — never commit changes there.

## STRUCTURE

```
doc3gpp/
├── src/doc3gpp/          # package root
│   ├── cli.py            # Typer commands (7 groups): db, meetings, tdoc, tsg, wi, config, cache
│   ├── config.py         # re-export shim (legacy)
│   ├── models/           # Meeting, TDoc, Tsg, Wi dataclasses
│   ├── parsers/          # HTML/Excel → domain objects (no network)
│   ├── repository/       # Protocol contracts (abstract)
│   ├── scraping/         # HTTP/FTP transport (no parsing)
│   ├── services/         # orchestration: MeetingService, TDocService, TsgService, WiService
│   ├── settings/         # pydantic-settings + TOML discovery (schema, config_source, loader)
│   └── storage/          # persistence umbrella
│       ├── backends/     # engine kwargs per dialect
│       ├── db/           # ORM models, session, migrate, base
│       │   └── migrations/   # placeholder for future Alembic
│       └── repositories/ # SQLAlchemy impls of Protocols
├── tests/
│   ├── unit/             # 24 files (mock external calls)
│   ├── integration/      # 10 files (sqlite + online + mysql)
│   └── fixtures/         # sample HTML + XLSX + zip docs
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
| `Meeting` | dataclass | `models/meeting.py` | Domain model for meetings (`tsg` is the owning TSG FK; populated by `meeting sync --tsg`) |
| `TDoc` | dataclass | `models/tdoc.py` | Domain model for TDocs |
| `TDocCRDetails` | dataclass | `models/tdoc_cr.py` | Parsed CR cover-page fields (spec, cr_num, release, ...) |
| `TDocExtractMeta` | dataclass | `models/tdoc_cr.py` | Cache-pointer sidecar for an extracted TDoc (zip/markdown paths, doc_filename) |
| `Tsg` | dataclass | `models/tsg.py` | Domain model for 3GPP TSG reference records |
| `Wi` | dataclass | `models/wi.py` | Domain model for 3GPP Work Items (FK to tsg_short, updated_at) |
| `MeetingRepository` | Protocol | `repository/protocols.py` | Contract for meeting storage |
| `TDocRepository` | Protocol | `repository/protocols.py` | Contract for TDoc storage; `get_by_id` resolves canonical id strings |
| `TDocCrDetailRepository` | Protocol | `repository/protocols.py` | Contract for `tdoc_cr_details` + `tdoc_extracts` storage |
| `TsgRepository` | Protocol | `repository/protocols.py` | Contract for TSG reference storage |
| `WiRepository` | Protocol | `repository/protocols.py` | Contract for WI storage; upsert keyed by `(wi_id, tsg_short)` |
| `MeetingService` | class | `services/meetings_service.py` | Meeting sync + list orchestration |
| `TDocService` | class | `services/tdoc_service.py` | TDoc sync + list orchestration |
| `TDocCrService` | class | `services/tdoc_cr_service.py` | End-to-end CR extraction (zip → cache → python-docx → parse → persist) |
| `TsgService` | class | `services/tsg_service.py` | TSG seeding + validation; also exposes `build_tsg_url` URL pattern |
| `WiService` | class | `services/wi_service.py` | WI sync from DynaReport + list with SQL `LIKE` filters |
| `ScraperClient` | class | `scraping/client.py` | HTTP transport with httpx |
| `fetch_calendar` | function | `scraping/calendar_source.py` | Fetch DynaReport HTML |
| `fetch_tdocs_from_meeting_ftp` | function | `scraping/ftp_source.py` | Discover + fetch TDoc XLSX from FTP |
| `fetch_wis` | function | `scraping/wi_source.py` | Fetch DynaReport WI list HTML for a TSG |
| `parse_3gpp_calendar` | function | `parsers/calendar_parser.py` | HTML→Meeting list |
| `parse_3gpp_wis` | function | `parsers/wi_parser.py` | HTML→Wi list (extracts wi_id, acronym, release, name) |
| `read_tdoc_sheet` | function | `parsers/tdoc_parser.py` | XLSX→TDoc list |
| `SQLAlchemyMeetingRepository` | class | `storage/repositories/meeting_sql.py` | SQL impl of MeetingRepository |
| `SQLAlchemyTDocRepository` | class | `storage/repositories/tdoc_sql.py` | SQL impl of TDocRepository; rich filter helpers `_apply_text_filter` / `_apply_date_filter` consume `cli_filters.DATE_FILTER_RE` to interpret `null` / `not-null` / `LIKE` / `OP 'YYYY-MM-DD'` filter values for both `tdoc list` and `tdoc parse --meeting-id` |
| `validate_date_filter` | function | `cli_filters.py` | Boundary guard for `--uploaded-date`; rejects anything that doesn't match `null` / `not-null` / `"<op> 'YYYY-MM-DD'"` before the database is touched |
| `SQLAlchemyTDocCrRepository` | class | `storage/repositories/tdoc_cr_sql.py` | SQL impl of TDocCrDetailRepository |
| `SQLAlchemyTsgRepository` | class | `storage/repositories/tsg_sql.py` | SQL impl of TsgRepository |
| `SQLAlchemyWiRepository` | class | `storage/repositories/wi_sql.py` | SQL impl of WiRepository |
| `get_engine` | function | `storage/db/session.py` | Cached engine factory |
| `create_schema` | function | `storage/db/migrate.py` | Base.metadata.create_all |
| `get_settings` | function | `settings/loader.py` | Cached settings loader (env + TOML file) |
| `Settings` | model | `settings/schema.py` | Root pydantic-settings (flat DOC3GPP_* + nested sub-models) |
| `MeetingSyncSettings` | model | `settings/schema.py` | Fetch knobs (`closed_years`, `future_years`) |
| `OutputSettings` | model | `settings/schema.py` | Default `format` + per-command field lists |
| `OutputFieldsSettings` | model | `settings/schema.py` | Per-list-command `default_fields` lists |
| `find_config_file` | function | `settings/config_source.py` | TOML discovery (DOC3GPP_CONFIG → ./doc3gpp.toml → XDG) |
| `load_config_data` | function | `settings/config_source.py` | Returns `(path, dict)` for the active TOML file |

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
| `models/` | `Meeting`, `TDoc`, `Wi` | pass between layers; **never leak ORM attributes** |
| `repository/` | `protocols.py` | abstract repo contracts only |
| `services/` | `meetings_service.py`, `tdoc_service.py`, `wi_service.py` | orchestration; injected with a repo impl |
| `scraping/` | `client.py`, `calendar_source.py`, `ftp_source.py`, `wi_source.py` | network/HTTP only — **no HTML parsing** |
| `parsers/` | `calendar_parser.py`, `html_parsers.py`, `tdoc_parser.py`, `normalizers.py`, `wi_parser.py` | HTML/Excel → domain only — **no network** |
| `storage/` | `db/`, `backends/`, `repositories/` | persistence only — **no business logic** |
| `settings/` | `schema.py`, `loader.py` | env-driven config |
| `cli.py` | Typer commands | thin: build service, call it, format output |

Flow:
- `doc3gpp meeting sync` → `MeetingService.sync` → fetch DynaReport HTML → `parse_3gpp_calendar` → stamp `Meeting.tsg` from validated `--tsg` → `SQLAlchemyMeetingRepository.upsert_many`
- `doc3gpp tdoc sync --meeting-id <id>` resolves stored `Meeting.ftp_url` from DB, fetches `TDoc_List_Meeting_*.xlsx` from FTP. **No meeting row → no TDoc sync.**
- `doc3gpp wi sync --tsg <short>` → `WiService.sync` → `fetch_wis` → `parse_3gpp_wis` → `SQLAlchemyWiRepository.upsert_many`. The `wis.tsg_short` column is a foreign key into `tsgs.short_name`, so the `tsgs` table is auto-seeded and `--tsg` is validated against it.
- `doc3gpp db init` calls `create_schema()` and then `TsgService.seed_defaults()` to populate the `tsgs` reference table.
- `doc3gpp db reset [--yes]` is the destructive recovery path: delete the SQLite file (plus WAL/SHM/journal sidecars), clear the cached engine, then re-run `create_schema()` + `seed_defaults()`. Refuses non-SQLite URLs. Use after an ORM change leaves the live schema out of sync (no Alembic is wired up).
- `doc3gpp meeting sync --tsg <short>` validates `<short>` against the `tsgs` table (auto-seeded if empty); an unknown value raises `typer.BadParameter` listing the known short names.
- `doc3gpp tsg list` and `doc3gpp tsg show` read from the `tsgs` table via
  `SQLAlchemyTsgRepository`. `doc3gpp tsg seed` upserts the canonical 16 rows.
- `doc3gpp config path` reports which TOML file is in effect (or "(no
  config file found)"); `doc3gpp config show` dumps the fully-resolved
  settings as JSON for diffing against the schema in `doc3gpp.toml.example`.
## SETTINGS CACHING — FLUSH IN TESTS

Both loaders are `@lru_cache(maxsize=1)`:

- `doc3gpp.settings.loader.get_settings`
- `doc3gpp.storage.db.session.get_engine`

If a test or fixture changes `DOC3GPP_*` env vars via `monkeypatch`, it
**must** `cache_clear()` both. See the `sqlite_env` fixture in
`tests/conftest.py` for the canonical pattern.

Recognised env vars: `DOC3GPP_DATABASE_URL`, `DOC3GPP_DB_ECHO`, `DOC3GPP_DB_POOL_SIZE`, `DOC3GPP_DB_AUTO_MIGRATE`, `DOC3GPP_LOG_LEVEL`, `DOC3GPP_HTTP_VERIFY`, `DOC3GPP_HTTP_MAX_RETRIES`, `DOC3GPP_HTTP_RETRY_BACKOFF`. Nested settings are overridable via the `__` delimiter: `DOC3GPP_MEETING_SYNC__CLOSED_YEARS=5`, `DOC3GPP_OUTPUT__FORMAT=json`, `DOC3GPP_CACHE__DIR=~/.cache/doc3gpp/tdocs`, `DOC3GPP_CACHE__PURGE_CONFIRM=false`. MySQL tests additionally use `DOC3GPP_TEST_MYSQL_URL`.

## CONFIG FILE LAYER

TOML configuration files are merged into `Settings` below env vars in
precedence (CLI > env > file > defaults). Discovery order:

1. `$DOC3GPP_CONFIG` (file or directory).
2. `./doc3gpp.toml` (project-local).
3. `~/.config/doc3gpp/config.toml` (XDG; honors `$XDG_CONFIG_HOME`).

A missing file is silent (defaults are used); a malformed file raises
`ValueError` pointing at the path. `Settings` uses `extra="ignore"` so
unrelated keys in the file are dropped rather than rejected — keeps the
file co-tenanted with third-party tooling metadata. See
`doc3gpp.toml.example` for the full schema and `doc3gpp config path` /
`doc3gpp config show` for inspection. The schema lives in
`src/doc3gpp/settings/schema.py`; discovery in
`src/doc3gpp/settings/config_source.py`; loader merge in
`src/doc3gpp/settings/loader.py`.
## CONVENTIONS

- Static type hints on all new code (project targets py310, `ruff target-version = "py310"`).
- New features ship with both a **unit test** (mock external calls) and an **integration test** against sqlite under `tests/integration/`.
- Ruff only: `line-length = 100`, no custom rule selection (defaults). No mypy/pyright configured.
- Keep `README.md`, `AGENTS.md`, and `docs/*.md` in sync when behaviour or CLI surface changes.
- Do not auto-commit. Plan first, implement, run lint + the sqlite test profile, then hand off.
- Scripts in `scripts/` use `set -euo pipefail`.
- **Filter values for `tdoc list` and `tdoc parse --meeting-id`** share a single grammar defined in `src/doc3gpp/cli_filters.py`: `null` / `not-null` select by nullability, `!<pattern>` (the `!` is consumed) emits `NOT LIKE <pattern>`, anything else is a SQL `LIKE` pattern, and `--uploaded-date` additionally accepts `"<op> 'YYYY-MM-DD'"` (op ∈ `= != < <= > >=`). Both CLIs validate with `validate_date_filter` before touching the DB; the repository's `_apply_*_filter` helpers emit SQLAlchemy parameter bindings — never string interpolation — so the surface is injection-safe. The text-column flags are now uniform across both commands (`--status`, `--cat`, `--spec`, `--wi`, `--title`, `--source`, `--type`, `--revision-of`, `--revised-to`, `--ftp-url`, `--uploaded-date`).

## ANTI-PATTERNS (THIS PROJECT)

- **Protocol ↔ Impl signature drift.** Previously: `MeetingRepository.list` declared only `limit`, but `SQLAlchemyMeetingRepository.list` took `limit, tsg, name_like, location_like, year`. Resolved 2026-07-02 (M2). When changing filter signatures for any other repo, keep the Protocol and impl in sync.
- **`create_schema()` called redundantly.** `meetings sync`, `wi sync`, and `tsg seed` still call `create_schema()` — idempotent but blurs the `db init` boundary. (`tdoc sync` and `tdoc parse` already drop it.)
- **Cross-service orchestration in CLI.** Mostly addressed: `tdoc sync` delegates to `TDocSyncCoordinator`. Other commands still construct their own services via `services.factory.build_*` helpers.
- **Doc drift.** `docs/architecture.md` lists a `tdoc add` command that doesn't exist. Keep docs in sync when CLI surface changes.
- **Acknowledged `# noqa: F401`.** Four in `storage/db/migrate.py` — side-effect imports required for SQLAlchemy `Base.metadata` registration. Do not remove.
- **Retryable error surface.** `ScraperClient._is_retryable_exception` deliberately treats only transient `httpx` subclasses as retryable. Programming errors (e.g. `InvalidURL`) raise immediately — do not broaden the catch.

## UNIQUE STYLES

- **`repository/` (abstract) and `storage/repositories/` (concrete) are separate packages.** Abstractions live in `src/doc3gpp/repository/`, implementations in `src/doc3gpp/storage/repositories/`. This split means a reader follows two paths to trace a repo from contract to SQL.
- **`config.py` is a re-export shim** for backwards compatibility. New imports should go to `doc3gpp.settings` directly.
- **`cache.py` and `export.py` sit at `storage/` root**, not in a subpackage. Mildly unconventional but stable.
- **MySQL tests double-gated**: `pytestmark` marker + `@pytest.mark.skipif` on env var.

## KNOWN CONSTRAINTS

- **No Alembic.** Schema bootstrap is `Base.metadata.create_all` via `storage/db/migrate.py`. `DOC3GPP_DB_AUTO_MIGRATE` is a flag only — does not run migrations. **Existing SQLite installs must run `doc3gpp db reset --yes` after pulling a change that adds a column (e.g. `meetings.tsg`).**
- Calendar parser coupled to **current 3GPP DynaReport table layout** — upstream changes will break `meetings sync`.
- TDoc extraction covers **FTP Excel lists only**. `GenerateDocumentList.aspx` and expanded metadata columns are unimplemented.
- TDoc CR extraction covers the `R5s` (TTCN) and `R5w` (Workshop) URL templates verified against offline fixtures; the `R5-` and `C6-` templates are intentionally unresolved until exercised against the live site.
- `python-docx` is an opt-in extra; without it the `tdoc parse` CLI prints a friendly install hint and exits 1.
- Online tests access live `3gpp.org` + FTP — flaky; run with `-rs` to surface skip reasons.


<!-- headroom:rtk-instructions -->
# RTK (Rust Token Killer) - Token-Optimized Commands

When running shell commands, **always prefix with `rtk`**. This reduces context
usage by 60-90% with zero behavior change. If rtk has no filter for a command,
it passes through unchanged — so it is always safe to use.

## Key Commands
```bash
# Git (59-80% savings)
rtk git status          rtk git diff            rtk git log

# Files & Search (60-75% savings)
rtk ls <path>           rtk read <file>         rtk grep <pattern>
rtk find <pattern>      rtk diff <file>

# Test (90-99% savings) — shows failures only
rtk pytest tests/       rtk cargo test          rtk test <cmd>

# Build & Lint (80-90% savings) — shows errors only
rtk tsc                 rtk lint                rtk cargo build
rtk prettier --check    rtk mypy                rtk ruff check

# Analysis (70-90% savings)
rtk err <cmd>           rtk log <file>          rtk json <file>
rtk summary <cmd>       rtk deps                rtk env

# GitHub (26-87% savings)
rtk gh pr view <n>      rtk gh run list         rtk gh issue list

# Infrastructure (85% savings)
rtk docker ps           rtk kubectl get         rtk docker logs <c>

# Package managers (70-90% savings)
rtk pip list            rtk pnpm install        rtk npm run <script>
```

## Rules
- In command chains, prefix each segment: `rtk git add . && rtk git commit -m "msg"`
- For debugging, use raw command without rtk prefix
- `rtk proxy <cmd>` runs command without filtering but tracks usage
<!-- /headroom:rtk-instructions -->

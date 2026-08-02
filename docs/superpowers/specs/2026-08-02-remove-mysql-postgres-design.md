# Remove MySQL & PostgreSQL Backends — Design Spec

**Status:** Approved (pending user review of the written spec)
**Date:** 2026-08-02
**Branch:** remove-mysql-postgres-add-lancedb
**Author:** brainstorming session

## Goal

Remove all MySQL and PostgreSQL support from doc3gpp, leaving SQLite as
the sole storage backend. No new backend is added in this change.

## Non-goals

- Adding LanceDB (or any other) backend — previously discussed, now
  explicitly out of scope.
- Changing the FTS5 search or sqlite-vec vector-search subsystems.
- Refactoring the repository layer or ORM models.
- Touching the `cli` / `extract` / `search` / `semantic` / `dev` extras.

## What gets removed

| Area | Files |
| --- | --- |
| Backend configs | `src/doc3gpp/storage/backends/mysql.py`, `src/doc3gpp/storage/backends/postgres.py` (delete) |
| Backend re-exports | `src/doc3gpp/storage/backends/__init__.py` — drop mysql/postgres imports |
| Engine factory dispatch | `src/doc3gpp/storage/db/session.py` — drop mysql/postgresql URL-prefix branches |
| Optional extras | `pyproject.toml` — remove `mysql = ["pymysql>=1.1.1"]` and `postgres = ["psycopg[binary]>=3.2.0"]` |
| Test markers | `pyproject.toml` — remove `mysql` marker |
| Integration tests | `tests/integration/test_mysql_backend.py` (delete) |
| Unit tests | `tests/unit/test_db_reset_cli.py` — remove `test_db_reset_refuses_mysql_url` and `test_db_reset_refuses_postgres_url` |
| Search dialect test | `tests/integration/test_search_extras_disabled.py` — remove the `postgresql`-dialect case |
| Migration branches | `src/doc3gpp/storage/db/migrate.py` — collapse the `_migrate_rename_tdoc_cr_details()` dialect-specific rename branches to SQLite-only |
| Settings | `src/doc3gpp/settings/schema.py` — remove `db_pool_size` field (SQLite doesn't pool) |
| Config template | `src/doc3gpp/data/doc3gpp.toml.example` — remove the `db_pool_size` line |
| Docs | `docs/architecture.md`, `docs/conventions.md`, `docs/code-map.md`, `docs/known-constraints.md`, plus any spec/plan docs referencing the extras or markers |
| README / AGENTS.md | `README.md`, `AGENTS.md` — remove references to mysql/postgres extras and test markers |

## What gets simplified

- `_engine_kwargs()` in `src/doc3gpp/storage/db/session.py`: no URL-prefix
  dispatch — always `configure_sqlite_engine(...)`; the function can be
  inlined or collapsed since the sqlite check no longer needs a fallback.
- `build_search_service()` / `build_semantic_search_service()` in
  `src/doc3gpp/services/factory.py`: remove the
  `engine.dialect.name != "sqlite"` gating — SQLite is now the only
  dialect, so the guards are dead code.
- `create_schema()` in `src/doc3gpp/storage/db/migrate.py`: the FTS5 /
  vector schema creation no longer needs the `engine.dialect.name ==
  "sqlite"` gates (they can stay as harmless checks or be removed — spec
  choice: remove them since they gate nothing).
- `db check` / `db init` in `cli.py`: no changes needed; they already
  work on any backend and now only SQLite exists.
- `db reset`: unchanged — already SQLite-only, but the error message for
  non-SQLite URLs can be simplified (it will never fire with valid URLs).

## Behavior after the change

- `DOC3GPP_DATABASE_URL` pointing at `mysql://` or `postgresql://` is no
  longer supported. The engine factory silently ignores the scheme and
  builds SQLite kwargs — matching today's unknown-scheme fallback. (No
  new validation is added; the previous fallback behavior is preserved.)
- `pip install doc3gpp[mysql]` / `[postgres]` no longer exists.
- `pytest -m mysql` no longer exists.
- Everything else (CLI surface, commands, formats, search, semantic
  search) is behavior-identical on SQLite.

## Testing

- Run the full sqlite suite: `./scripts/test_sqlite.sh`
- Lint: `ruff check .`
- Grep for residual references: `mysql`, `postgresql`, `pymysql`,
  `psycopg`, `db_pool_size` across `src/`, `tests/`, `docs/`,
  `pyproject.toml`, `README.md`, `AGENTS.md` — all should be gone except
  historical mentions in old spec/plan docs (which are dated artifacts).

## Doc pointers to update

- `docs/architecture.md`: backend selection section (lines ~9, 146,
  515-523, 660, 670, 677-678), test markers, URL examples.
- `docs/conventions.md`: default pytest excludes list.
- `docs/code-map.md`: engine-kwargs symbol row.
- `docs/known-constraints.md`: test profile notes.
- `README.md` / `AGENTS.md`: extras list, test command notes.

## Acceptance criteria

1. `rg -i "mysql|postgres|pymysql|psycopg" src/ tests/ pyproject.toml`
   returns nothing (excluding `docs/` historical artifacts).
2. `./scripts/test_sqlite.sh` passes.
3. `ruff check .` passes.
4. `doc3gpp db init` / `db check` / `db reset` work on a fresh SQLite DB.
5. `Settings` no longer exposes `db_pool_size`.

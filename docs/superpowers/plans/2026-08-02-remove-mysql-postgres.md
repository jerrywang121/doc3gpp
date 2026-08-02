# Remove MySQL & PostgreSQL Backends — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove all MySQL and PostgreSQL support from doc3gpp, leaving SQLite as the sole storage backend, with zero behavior change on SQLite.

**Architecture:** Delete the mysql/postgres backend modules, collapse the engine-kwargs dispatch and migration dialect branches to SQLite-only, remove `db_pool_size` from Settings, drop the dialect gates in the search factory, remove the mysql pytest marker + extras, delete the mysql/postgres tests, and scrub all references from docs. SQLite behavior must remain byte-identical.

**Tech Stack:** Python 3.10+, SQLAlchemy 2.0, pydantic v2, Typer, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-08-02-remove-mysql-postgres-design.md` (approved).

## Global Constraints

- **No new backend is added** — SQLite is the sole backend after this change. No LanceDB, no FTS5/sqlite-vec changes, no repository-layer or ORM refactor.
- **Do not touch** the `cli` / `extract` / `search` / `semantic` / `dev` extras (only `mysql` and `postgres` extras are removed).
- **Fallback behavior preserved:** a `DOC3GPP_DATABASE_URL` pointing at `mysql://` or `postgresql://` is silently ignored by the engine factory — it builds SQLite kwargs, matching today's unknown-scheme fallback. No new validation is added.
- **Acceptance criteria** (verbatim from spec §Acceptance criteria):
  1. `rg -i "mysql|postgres|pymysql|psycopg" src/ tests/ pyproject.toml` returns nothing (excluding `docs/` historical artifacts).
  2. `./scripts/test_sqlite.sh` passes.
  3. `ruff check .` passes.
  4. `doc3gpp db init` / `db check` / `db reset` work on a fresh SQLite DB.
  5. `Settings` no longer exposes `db_pool_size`.
- Historical mentions inside `docs/superpowers/**` (old specs/plans) are dated artifacts and are **exempt** from the grep.
- Work happens on branch `remove-mysql-postgres-add-lancedb` (branch name kept from the earlier LanceDB discussion even though LanceDB is out of scope).
- Repo commit style: `type: subject` (e.g. `refactor: ...`, `test: ...`, `docs: ...`).

---

### Task 1: Delete backend modules and collapse engine dispatch

**Files:**
- Delete: `src/doc3gpp/storage/backends/mysql.py`, `src/doc3gpp/storage/backends/postgres.py`
- Modify: `src/doc3gpp/storage/backends/__init__.py` (entire file)
- Modify: `src/doc3gpp/storage/db/session.py` (entire file)
- Test: none new — the existing suite (`test_sdk_integration.py::test_sdk_backend_engine_kwargs` exercises `configure_sqlite_engine`; every repo test exercises `get_engine`) is the safety net.

**Interfaces:**
- Consumes: `configure_sqlite_engine(database_url: str, db_echo: bool) -> dict` (unchanged, in `src/doc3gpp/storage/backends/sqlite.py`).
- Produces: `get_engine()` (unchanged public signature, `@lru_cache(maxsize=1)`); `get_session_factory()` (unchanged). `_engine_kwargs` and `configure_mysql_engine` / `configure_postgres_engine` no longer exist.

- [ ] **Step 1: Delete the two backend modules**

```bash
git rm src/doc3gpp/storage/backends/mysql.py src/doc3gpp/storage/backends/postgres.py
```

- [ ] **Step 2: Rewrite `src/doc3gpp/storage/backends/__init__.py`**

Replace the entire file with:

```python
"""Backend-specific database helpers."""

from doc3gpp.storage.backends.sqlite import configure_sqlite_engine

__all__ = [
    "configure_sqlite_engine",
]
```

- [ ] **Step 3: Rewrite `src/doc3gpp/storage/db/session.py`**

Replace the entire file with (the `_engine_kwargs` dispatch is inlined into `get_engine` — SQLite is the only branch):

```python
from __future__ import annotations

from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from doc3gpp.config import get_settings
from doc3gpp.storage.backends import configure_sqlite_engine


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    settings = get_settings()
    return create_engine(
        settings.database_url,
        **configure_sqlite_engine(
            database_url=settings.database_url,
            db_echo=settings.db_echo,
        ),
    )


def get_session_factory() -> sessionmaker:
    return sessionmaker(bind=get_engine(), autoflush=False, autocommit=False)
```

- [ ] **Step 4: Verify**

```bash
ruff check src/doc3gpp/storage/
python -m pytest tests/integration/test_sdk_integration.py tests/integration/test_sqlite_backend.py -q
```

Expected: ruff clean; all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: remove mysql/postgres backends and collapse engine dispatch"
```

---

### Task 2: Collapse migration dialect branches to SQLite-only

**Files:**
- Modify: `src/doc3gpp/storage/db/migrate.py` (imports; `_migrate_rename_tdoc_cr_details`; `_create_search_schema`; `_create_vector_schema`)
- Test: `tests/integration/test_tdoc_cr_rename_migration.py`, `tests/integration/test_search_schema.py` (existing — unchanged, they cover the SQLite paths)

**Interfaces:**
- Consumes: `get_engine()` from Task 1.
- Produces: `create_schema()` with identical public behavior on SQLite.

- [ ] **Step 1: Remove the now-unused `OperationalError` import**

In `src/doc3gpp/storage/db/migrate.py` line 4, delete:

```python
from sqlalchemy.exc import OperationalError
```

(It is used only by the MySQL/PostgreSQL `RENAME TABLE` branch being removed below.)

- [ ] **Step 2: Collapse `_migrate_rename_tdoc_cr_details()`**

In `src/doc3gpp/storage/db/migrate.py`, replace the body of `_migrate_rename_tdoc_cr_details()` — from `    engine = get_engine()` through the end of the `else:` branch (current lines 38–73) — with:

```python
    engine = get_engine()
    with engine.begin() as conn:
        # Probe sqlite_master for the legacy name.
        legacy_exists = conn.execute(
            text(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='tdoc_cr_details' LIMIT 1"
            )
        ).first()
        if legacy_exists:
            # Drop the empty new table (if any) so RENAME can land;
            # legacy data must survive — only rename, never drop.
            conn.execute(
                text("DROP TABLE IF EXISTS tdoc_cr_cover_page")
            )
            conn.execute(
                text("ALTER TABLE tdoc_cr_details RENAME TO tdoc_cr_cover_page")
            )
```

Also update the function docstring: remove the bullet line `* MySQL and PostgreSQL support ``RENAME TABLE`` natively.` (current line 36) and reword the first bullet to drop the SQLite qualifier:

```
    * ``ALTER TABLE ... RENAME TO`` is the only DDL SQLite supports
      that mutates a table. ``CREATE TABLE IF NOT EXISTS`` is used as
      the post-condition probe so a pre-existing new-name table
      (partial migration) does not raise.
```

- [ ] **Step 3: Remove the dialect gate in `_create_search_schema()`**

Delete these two lines (current lines 99–100):

```python
    engine = get_engine()
    if engine.dialect.name != "sqlite":
        return
```

…replacing them with just:

```python
    engine = get_engine()
```

Then update the docstring's first paragraph (current lines 79–81) from:

```
    Gated on the engine dialect being sqlite and on the runtime
    availability of FTS5 — on every other path this is a no-op.
    The check uses ``PRAGMA compile_options`` (FTS5 is reported as
```

to:

```
    Gated on the runtime availability of FTS5 — when missing this is
    a no-op. The check uses ``PRAGMA compile_options`` (FTS5 is
    reported as
```

- [ ] **Step 4: Remove the dialect gate in `_create_vector_schema()`**

Delete these two lines (current lines 186–187):

```python
    engine = get_engine()
    if engine.dialect.name != "sqlite":
        return
```

…replacing them with just:

```python
    engine = get_engine()
```

Then update the docstring's first paragraph (current lines 170–172) from:

```
    Gated on the engine dialect being sqlite and on the runtime
    availability of the sqlite-vec extension — on every other path
    this is a no-op. The check tries to import ``sqlite_vec`` and
```

to:

```
    Gated on the runtime availability of the sqlite-vec extension —
    when missing this is a no-op. The check tries to import
    ``sqlite_vec`` and
```

- [ ] **Step 5: Verify**

```bash
ruff check src/doc3gpp/storage/db/migrate.py
python -m pytest tests/integration/test_tdoc_cr_rename_migration.py tests/integration/test_search_schema.py -q
```

Expected: ruff clean; all tests PASS (rename migration works; FTS5 + vector schema created).

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/storage/db/migrate.py
git commit -m "refactor: collapse migration rename and schema gates to sqlite-only"
```

---

### Task 3: Remove `db_pool_size` from Settings

**Files:**
- Modify: `src/doc3gpp/settings/schema.py` (remove field; update allowlist comment)
- Modify: `src/doc3gpp/data/doc3gpp.toml.example` (remove line; update semantic-search comment)
- Modify: `tests/unit/test_settings_config_file.py` (remove three assertions/lines)
- Test: `tests/unit/test_settings_config_file.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `Settings` without `db_pool_size` (acceptance criterion 5).

- [ ] **Step 1: Remove the field in `src/doc3gpp/settings/schema.py`**

Delete line 555:

```python
    db_pool_size: int = Field(default=5)
```

- [ ] **Step 2: Update the `ALLOWED_ENV_VARS` comment in the same file**

Replace lines 54–57:

```
#: ``DOC3GPP_CONFIG`` (TOML config file location pin) and
#: ``DOC3GPP_TEST_MYSQL_URL`` (test-only MySQL URL) are read directly
#: by :mod:`doc3gpp.settings.config_source` and the test fixtures,
#: respectively, and are intentionally **not** part of this allowlist.
```

with:

```
#: ``DOC3GPP_CONFIG`` (TOML config file location pin) is read directly
#: by :mod:`doc3gpp.settings.config_source` and is intentionally
#: **not** part of this allowlist.
```

- [ ] **Step 3: Update `src/doc3gpp/data/doc3gpp.toml.example`**

Delete line 28:

```toml
# db_pool_size = 5
```

Also update line 162 in the semantic-search comment block from:

```
# support matrix; on MySQL / PostgreSQL or when sqlite-vec is
```

to:

```
# support matrix; when sqlite-vec is
```

- [ ] **Step 4: Update `tests/unit/test_settings_config_file.py`**

In `test_non_allowlisted_env_vars_are_silently_ignored` (lines 415–447):
- Delete line 427: `assert "DOC3GPP_DB_POOL_SIZE" not in ALLOWED_ENV_VARS`
- Delete line 431: `monkeypatch.setenv("DOC3GPP_DB_POOL_SIZE", "99")`
- Delete line 442: `assert s.db_pool_size == 5`

The `ALLOWED_ENV_VARS` import at line 422 stays (still used for the other two sanity asserts).

- [ ] **Step 5: Verify**

```bash
ruff check src/doc3gpp/settings/ tests/unit/test_settings_config_file.py
python -m pytest tests/unit/test_settings_config_file.py -q
python -c "from doc3gpp.settings.schema import Settings; assert not hasattr(Settings(), 'db_pool_size'); print('ok')"
```

Expected: ruff clean; tests PASS; the one-liner prints `ok`.

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/settings/schema.py src/doc3gpp/data/doc3gpp.toml.example tests/unit/test_settings_config_file.py
git commit -m "refactor: remove db_pool_size setting"
```

---

### Task 4: Drop dialect gating from the search factory

**Files:**
- Modify: `src/doc3gpp/services/factory.py` (remove dialect check + unused import; adjust two docstrings)
- Modify: `tests/integration/test_search_extras_disabled.py` (remove the `postgresql`-dialect case; update module docstring)
- Test: `tests/integration/test_search_extras_disabled.py`, `tests/unit/test_cli_search.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `build_search_service()` / `build_semantic_search_service()` with identical SQLite behavior (still return `None` when search disabled, FTS5 missing, or extra missing — via `SearchUnavailableError` / `VectorIndexUnavailableError` caught inside).

- [ ] **Step 1: Remove the dialect check in `build_search_service()`**

In `src/doc3gpp/services/factory.py`, replace lines 304–311:

```python
    try:
        resolved_engine = get_engine()
        if resolved_engine.dialect.name != "sqlite":
            raise SearchUnavailableError(
                f"search requires sqlite FTS5; current dialect is "
                f"{resolved_engine.dialect.name!r}"
            )
        if repo is None:
            repo = SQLAlchemySearchIndexRepository()
```

with:

```python
    try:
        if repo is None:
            repo = SQLAlchemySearchIndexRepository()
```

- [ ] **Step 2: Remove the now-unused import in `factory.py`**

Delete line 30:

```python
from doc3gpp.storage.db.session import get_engine
```

- [ ] **Step 3: Update the two docstrings in `factory.py`**

Line 171–173 — from:

```
      index in sync. Returns ``None`` when search is disabled, the
      dialect is non-sqlite, or FTS5 is missing; in those cases the
      service simply skips the auto-index hook.
```

to:

```
      index in sync. Returns ``None`` when search is disabled or
      FTS5 is missing; in those cases the service simply skips the
      auto-index hook.
```

Line 280–282 — from:

```
    The factory is best-effort: any :class:`SearchUnavailableError`
    raised by the repo (wrong dialect, missing FTS5, missing extra)
    is caught here once at startup and returned as ``None``. The
```

to:

```
    The factory is best-effort: any :class:`SearchUnavailableError`
    raised by the repo (missing FTS5, missing extra) is caught here
    once at startup and returned as ``None``. The
```

- [ ] **Step 4: Remove the postgresql-dialect test**

In `tests/integration/test_search_extras_disabled.py`, delete the whole `test_non_sqlite_returns_none` function (lines 29–41):

```python
def test_non_sqlite_returns_none() -> None:
    settings = Settings()

    class _NonSqlite:
        name = "postgresql"

    class _Engine:
        dialect = _NonSqlite()

    with patch(
        "doc3gpp.services.factory.get_engine", return_value=_Engine(),
    ):
        assert build_search_service(settings) is None
```

Then update the module docstring (lines 1–13) from three scenarios to two — remove scenario 2 and renumber:

```
"""Graceful degradation when FTS5 is missing or disabled.

Two scenarios, each must end with the system behaving as if search
were unavailable — never crashing, never surfacing an internal
traceback:

1. ``Settings.search.enabled = False`` → ``build_search_service``
   returns ``None``.
2. Build-but-don't-register flow → repo raises
   ``SearchUnavailableError`` which the factory converts to ``None``.
"""
```

- [ ] **Step 5: Verify**

```bash
ruff check src/doc3gpp/services/factory.py tests/integration/test_search_extras_disabled.py
python -m pytest tests/integration/test_search_extras_disabled.py tests/unit/test_cli_search.py -q
```

Expected: ruff clean; all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/services/factory.py tests/integration/test_search_extras_disabled.py
git commit -m "refactor: drop dialect gating from search factory"
```

---

### Task 5: Remove mysql/postgres-specific tests and test-comment references

**Files:**
- Delete: `tests/integration/test_mysql_backend.py`
- Modify: `tests/unit/test_db_reset_cli.py` (remove two tests; update docstring)
- Modify: `tests/unit/test_docx_converter.py` (comment)
- Modify: `tests/integration/test_config_init_cli_sqlite.py` (comment)
- Modify: `tests/integration/test_config_set_cli_sqlite.py` (comment)
- Modify: `tests/integration/test_tdoc_cr_rename_migration.py` (docstring)
- Test: the modified test files

**Interfaces:**
- Consumes: nothing new.
- Produces: no test references the removed backends.

- [ ] **Step 1: Delete the MySQL integration test**

```bash
git rm tests/integration/test_mysql_backend.py
```

- [ ] **Step 2: Remove the two refusal tests in `tests/unit/test_db_reset_cli.py`**

Delete `test_db_reset_refuses_mysql_url` (lines 118–135) and `test_db_reset_refuses_postgres_url` (lines 138–152), exactly as shown in the file (both begin with `def test_db_reset_refuses_` and end with the `finally:` block + two `get_* .cache_clear()` calls).

Then update the module docstring (lines 1–18) — change line 7 from:

```
* only SQLite backends are accepted (MySQL/Postgres raise an error);
```

to:

```
* only SQLite backends are accepted (non-SQLite URLs raise an error);
```

- [ ] **Step 3: Update the `test_docx_converter.py` comment**

Line 37 — from:

```
    the default pool (pytest.ini excludes ``online`` and ``mysql`` but
```

to:

```
    the default pool (pytest.ini excludes ``online`` but
```

- [ ] **Step 4: Update the two config-CLI test comments**

`tests/integration/test_config_init_cli_sqlite.py` line 16 — from:

```
or non-sqlite backend, and carry no ``online``/``mysql`` marker.
```

to:

```
or non-sqlite backend, and carry no ``online`` marker.
```

`tests/integration/test_config_set_cli_sqlite.py` line 18 — from:

```
network or non-sqlite backend, and carry no ``online``/``mysql``
```

to:

```
network or non-sqlite backend, and carry no ``online`` marker.
```

- [ ] **Step 5: Update the `test_tdoc_cr_rename_migration.py` docstring**

Lines 17–20 — from:

```
The tests are SQLite-only because the SQLite branch is the only one
unit-tested here. MySQL / PostgreSQL use the native ``RENAME TABLE``
statement and rely on the engine dialect branching in
``_migrate_rename_tdoc_cr_details``.
```

to:

```
The tests are SQLite-only — SQLite is the sole backend, and the
rename relies on the SQLite ``ALTER TABLE ... RENAME TO`` branch in
``_migrate_rename_tdoc_cr_details``.
```

- [ ] **Step 6: Verify**

```bash
ruff check tests/unit/test_db_reset_cli.py tests/unit/test_docx_converter.py tests/integration/test_config_init_cli_sqlite.py tests/integration/test_config_set_cli_sqlite.py tests/integration/test_tdoc_cr_rename_migration.py
python -m pytest tests/unit/test_db_reset_cli.py tests/integration/test_tdoc_cr_rename_migration.py -q
```

Expected: ruff clean; all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "test: remove mysql/postgres-specific tests"
```

---

### Task 6: Remove extras and pytest marker; update the test script

**Files:**
- Modify: `pyproject.toml` (remove `mysql` + `postgres` extras; remove `mysql` marker)
- Modify: `scripts/test_sqlite.sh`
- Test: none new

**Interfaces:**
- Consumes: nothing new.
- Produces: `doc3gpp[mysql]` / `doc3gpp[postgres]` and `pytest -m mysql` no longer exist (spec §Behavior after the change).

- [ ] **Step 1: Edit `pyproject.toml`**

Delete lines 72–73:

```toml
mysql = ["pymysql>=1.1.1"]
postgres = ["psycopg[binary]>=3.2.0"]
```

Delete line 89 (the marker):

```toml
  "mysql: tests that require a mysql backend",
```

The remaining markers block (lines 87–91) then reads:

```toml
markers = [
  "online: tests that access live internet endpoints (for example 3gpp.org)",
  "semantic: tests that require the [semantic] extra (sentence-transformers, sqlite-vec)",
]
```

- [ ] **Step 2: Edit `scripts/test_sqlite.sh`**

Line 4 — from:

```bash
# SQLite-only profile: excludes mysql backend tests and online tests.
```

to:

```bash
# SQLite-only profile: excludes online tests.
```

Line 8 — from:

```bash
  -m "not mysql and not online"
```

to:

```bash
  -m "not online"
```

- [ ] **Step 3: Verify**

```bash
python -m pytest --markers | grep -c "mysql"  # expect 0
./scripts/test_sqlite.sh 2>&1 | tail -5
```

Expected: grep count `0`; full suite PASSES (note: this also re-verifies Tasks 1–5).

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml scripts/test_sqlite.sh
git commit -m "build: drop mysql/postgres extras and pytest marker"
```

---

### Task 7: Simplify `db reset` message and migrations README

**Files:**
- Modify: `src/doc3gpp/cli.py` (docstring + error message of `db_reset`)
- Modify: `src/doc3gpp/storage/db/migrations/README.md`
- Test: `tests/unit/test_db_reset_cli.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `db reset` still rejects non-SQLite URLs with the same `BadParameter` behavior; message no longer mentions MySQL/PostgreSQL.

- [ ] **Step 1: Edit the `db_reset` docstring in `src/doc3gpp/cli.py`**

Lines 622–624 — from:

```
    Destructive: all data is wiped. SQLite URLs only; MySQL/PostgreSQL are
    rejected. Prompts for confirmation unless ``--yes`` is passed. After
    reset the ``tsgs`` reference table is re-seeded.
```

to:

```
    Destructive: all data is wiped. SQLite URLs only; non-SQLite URLs are
    rejected. Prompts for confirmation unless ``--yes`` is passed. After
    reset the ``tsgs`` reference table is re-seeded.
```

- [ ] **Step 2: Edit the error message**

Lines 630–635 — from:

```python
    if not parsed.drivername.startswith("sqlite"):
        raise typer.BadParameter(
            f"'db reset' only supports SQLite backends "
            f"(configured URL: {settings.database_url}). "
            "Use the backend-native schema reset for MySQL or PostgreSQL."
        )
```

to:

```python
    if not parsed.drivername.startswith("sqlite"):
        raise typer.BadParameter(
            f"'db reset' only supports SQLite backends "
            f"(configured URL: {settings.database_url})."
        )
```

- [ ] **Step 3: Edit `src/doc3gpp/storage/db/migrations/README.md`**

Line 8 — from:

```
ORM model updates plus a one-time `doc3gpp db reset --yes` (SQLite) or a
backend-native migration (MySQL / PostgreSQL).
```

to:

```
ORM model updates plus a one-time `doc3gpp db reset --yes`.
```

- [ ] **Step 4: Verify**

```bash
ruff check src/doc3gpp/cli.py
python -m pytest tests/unit/test_db_reset_cli.py -q
```

Expected: ruff clean; all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/cli.py src/doc3gpp/storage/db/migrations/README.md
git commit -m "refactor: simplify db reset non-sqlite error message"
```

---

### Task 8: Scrub docs, README, and AGENTS.md

**Files:**
- Modify: `docs/architecture.md`, `docs/conventions.md`, `docs/code-map.md`, `docs/known-constraints.md`, `README.md`, `AGENTS.md`
- Test: none (docs-only task; the plan's final verification task validates the grep)

**Interfaces:**
- Consumes: nothing new.
- Produces: no live doc references mysql/postgres (historical `docs/superpowers/**` exempt).

- [ ] **Step 1: Edit `docs/architecture.md`**

1. Line 9 — from `- Configurable SQL backends (sqlite default, mysql, postgres).` to `- SQLite storage backend (sole backend).`
2. Line 146 — from `- \`storage/backends/{sqlite,mysql,postgres}.py\` — engine kwargs` to `- \`storage/backends/sqlite.py\` — engine kwargs`
3. Backend Selection section (lines 509–523) — from:

```
The active backend is selected from the allowlisted
`DOC3GPP_DATABASE_URL` env var (or its TOML counterpart):

- sqlite default: `sqlite+pysqlite:///~/.local/share/doc3gpp/doc3gpp.db`
- mysql example: `mysql+pymysql://user:pass@localhost:3306/doc3gpp`
- postgres example: `postgresql+psycopg://user:pass@localhost:5432/doc3gpp`

Backend-specific engine kwargs are applied in
`src/doc3gpp/storage/db/session.py` via:

- `src/doc3gpp/storage/backends/sqlite.py`
- `src/doc3gpp/storage/backends/mysql.py`
- `src/doc3gpp/storage/backends/postgres.py`
```

to:

```
The backend is selected from the allowlisted
`DOC3GPP_DATABASE_URL` env var (or its TOML counterpart):

- sqlite: `sqlite+pysqlite:///~/.local/share/doc3gpp/doc3gpp.db`

Engine kwargs are applied in `src/doc3gpp/storage/db/session.py` via:

- `src/doc3gpp/storage/backends/sqlite.py`
```

4. Line 660 — from `- \`tests/integration/\` — sqlite-only by default; online + mysql` to `- \`tests/integration/\` — sqlite-only by default; online` (keep `  opt-in. Coverage includes:` on the next line).
5. Lines 670–671 — delete the bullet:

```
    - `test_mysql_backend.py` (gated on
      `DOC3GPP_TEST_MYSQL_URL`)
```

6. Lines 677–678 — from:

```
- Pytest markers: `online`, `mysql`. The sqlite profile is
  `pytest -m "not mysql and not online"`; `./scripts/test_sqlite.sh`
  is the canonical wrapper.
```

to:

```
- Pytest markers: `online`. The sqlite profile is
  `pytest -m "not online"`; `./scripts/test_sqlite.sh` is the
  canonical wrapper.
```

- [ ] **Step 2: Edit `docs/conventions.md`**

Lines 15–20 — from:

```
- Default `pytest` excludes both `online` and `mysql` markers. New tests
  stay in the default pool unless they need network or a MySQL server.
- MySQL tests are double-gated (`pytestmark` marker +
  `@pytest.mark.skipif` on `DOC3GPP_TEST_MYSQL_URL`); online tests hit
  live 3gpp.org + FTP and are flaky — run with `-rs` to surface skip
  reasons.
```

to:

```
- Default `pytest` excludes the `online` marker. New tests stay in
  the default pool unless they need network.
- Online tests hit live 3gpp.org + FTP and are flaky — run with
  `-rs` to surface skip reasons.
```

- [ ] **Step 3: Edit `docs/code-map.md`**

Line 97 — from:

```
| `*_engine_kwargs` | functions | `storage/backends/{sqlite,mysql,postgres}.py` | Per-dialect engine configuration. |
```

to:

```
| `configure_sqlite_engine` | function | `storage/backends/sqlite.py` | SQLite engine configuration (sole backend). |
```

- [ ] **Step 4: Edit `docs/known-constraints.md`**

Lines 159–165 — from:

```
- **`online` tests access live `3gpp.org` + FTP** and are flaky.
  Always run with `-rs` to surface skip reasons; do not gate CI on
  them. They live behind the `online` pytest marker so the default
  profile (`pytest -m "not mysql and not online"`) skips them.
- **MySQL tests are double-gated** (`pytestmark` marker +
  `@pytest.mark.skipif` on `DOC3GPP_TEST_MYSQL_URL`). They do not
  run in the default profile.
```

to:

```
- **`online` tests access live `3gpp.org` + FTP** and are flaky.
  Always run with `-rs` to surface skip reasons; do not gate CI on
  them. They live behind the `online` pytest marker so the default
  profile (`pytest -m "not online"`) skips them.
```

- [ ] **Step 5: Edit `README.md`**

1. Lines 87–88 — delete:

```bash
pip install "doc3gpp[mysql]"      # MySQL driver (pymysql)
pip install "doc3gpp[postgres]"   # PostgreSQL driver (psycopg)
```

2. Line 351 — from `` `output.*`, `db_pool_size`, `db_auto_migrate`, `http_max_retries`, `` to `` `output.*`, `db_auto_migrate`, `http_max_retries`, ``

3. Lines 356–366 — from:

```bash
# default sqlite (omit DOC3GPP_DATABASE_URL to use the pydantic default,
# which resolves to ~/.local/share/doc3gpp/doc3gpp.db)
DOC3GPP_DATABASE_URL=sqlite+pysqlite:////absolute/path/to/doc3gpp.db

# mysql
DOC3GPP_DATABASE_URL=mysql+pymysql://user:pass@localhost:3306/doc3gpp

# postgresql
DOC3GPP_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/doc3gpp
```

to:

```bash
# sqlite (omit DOC3GPP_DATABASE_URL to use the pydantic default,
# which resolves to ~/.local/share/doc3gpp/doc3gpp.db)
DOC3GPP_DATABASE_URL=sqlite+pysqlite:////absolute/path/to/doc3gpp.db
```

4. Line 480 — from `SQLite-only profile (excludes `mysql` and `online` markers):` to `SQLite-only profile (excludes `online` markers):`

5. Line 483 — from `python -m pytest -q --cov=src/doc3gpp --cov-report=term-missing -m "not mysql and not online"` to `python -m pytest -q --cov=src/doc3gpp --cov-report=term-missing -m "not online"`

6. Lines 498–502 — delete:

```bash
MySQL tests (requires `DOC3GPP_TEST_MYSQL_URL`):

```bash
python -m pytest -m mysql
```
```

- [ ] **Step 6: Edit `AGENTS.md`**

1. Line 29 — delete the bullet `- \`.[mysql]\` / \`.[postgres]\` — DB drivers.` (and the line above it, `- \`.[cli]\` — Typer CLI (also in \`[dev]\`).`, stays).
2. Line 56 — from ``│   ├── integration/      # sqlite by default; online + mysql opt-in`` to ``│   ├── integration/      # sqlite by default; online opt-in``
3. Line 306 — from `# Full sqlite test suite (unit + integration, excludes online + mysql)` to `# Full sqlite test suite (unit + integration, excludes online)`
4. Lines 312–313 — delete:

```bash
# MySQL tests (needs DOC3GPP_TEST_MYSQL_URL)
python -m pytest -m mysql
```

Also check the "Where to look" table (AGENTS.md line ~29) and the Quick-start section for any other mysql/postgres mention and remove it.

- [ ] **Step 7: Verify**

```bash
rg -i "mysql|postgres|pymysql|psycopg" docs/ README.md AGENTS.md --glob "!docs/superpowers/**"
```

Expected: no matches (only `docs/superpowers/**` historical artifacts remain).

- [ ] **Step 8: Commit**

```bash
git add docs/architecture.md docs/conventions.md docs/code-map.md docs/known-constraints.md README.md AGENTS.md
git commit -m "docs: remove mysql/postgres references"
```

---

### Task 9: Final acceptance verification

**Files:** none modified — verification only.

- [ ] **Step 1: Residual-reference grep (acceptance criterion 1)**

```bash
rg -i "mysql|postgres|pymysql|psycopg" src/ tests/ pyproject.toml
rg -n "db_pool_size|DB_POOL_SIZE|DOC3GPP_TEST_MYSQL_URL" src/ tests/ pyproject.toml
```

Expected: both return **nothing**.

- [ ] **Step 2: Full sqlite suite (acceptance criterion 2)**

```bash
./scripts/test_sqlite.sh 2>&1 | tail -5
```

Expected: all tests PASS, `0 failed` (also `pytest -m mysql` would now error out as an unknown marker — that is expected and matches the spec).

- [ ] **Step 3: Lint (acceptance criterion 3)**

```bash
ruff check .
```

Expected: clean.

- [ ] **Step 4: Fresh-SQLite CLI smoke test (acceptance criterion 4)**

```bash
export DOC3GPP_DATABASE_URL="sqlite+pysqlite:////tmp/doc3gpp-accept.db"
rm -f /tmp/doc3gpp-accept.db
doc3gpp db init
doc3gpp db check
doc3gpp db reset --yes
```

Expected: `db init` prints `Database schema initialized; seeded 19 TSG records`; `db check` prints `Database connection OK: sqlite+pysqlite:////tmp/doc3gpp-accept.db`; `db reset --yes` prints `Database reset complete; seeded 19 TSG records`.

- [ ] **Step 5: Settings check (acceptance criterion 5)**

```bash
python -c "from doc3gpp.settings.schema import Settings; s = Settings(); assert not hasattr(s, 'db_pool_size'); print('ok')"
```

Expected: prints `ok`.

- [ ] **Step 6: Final review sweep**

Skim `git diff main...HEAD --stat` — the change set should contain exactly: 2 deleted backend modules, 1 deleted test file, edits to `session.py`, `migrate.py`, `factory.py`, `schema.py`, `doc3gpp.toml.example`, `cli.py`, `migrations/README.md`, `pyproject.toml`, `scripts/test_sqlite.sh`, 4 test files, and 6 doc files. If anything unexpected appears, investigate before finishing.

- [ ] **Step 7: Report**

Summarize for the user: what was removed, the acceptance-criteria results, and that the branch is ready for review (no commit needed at this step — commits landed per-task).

---

## Self-review notes

- **Spec coverage:** every row of the spec's "What gets removed" table maps to a task: backends (T1), re-exports (T1), engine dispatch (T1), extras + marker (T6), `test_mysql_backend.py` (T5), db-reset tests (T5), search dialect test (T4), migration branches (T2), `db_pool_size` (T3), config template (T3), docs (T8), README/AGENTS (T8). "What gets simplified": `_engine_kwargs` (T1), factory gating (T4), `create_schema` gates (T2), db-reset message (T7). All 5 acceptance criteria are tasks in T9.
- **Deliberately out of scope** (spec non-goals): `search_sql.py::_check_fts5` dialect check and `vector_sql.py::_check_sqlite_vec` dialect check stay — they contain no mysql/postgres references and still perform real FTS5/sqlite-vec runtime probing. `search_service.py` `_touch_rebuild_at` / `_touch_indexed_uploaded_date` dialect guards stay (dead checks, harmless, no mysql/postgres references). `schema.py` `db_auto_migrate` stays.
- **Test-file edits are removals, not TDD additions** — the existing suite is the safety net; each task's verify step runs the relevant slice, and Task 9 runs the whole suite.
- **Interfaces are stable across tasks:** `get_engine`, `get_session_factory`, `create_schema`, `build_search_service`, `build_semantic_search_service`, `db reset` keep their public signatures; only `_engine_kwargs` and the two `configure_*_engine` functions disappear, and nothing outside `session.py` referenced them.

# Testcase Separate Database Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move `testcases` / `testcase_status` / `testcase_sources` into their own sqlite file with an independent engine, and give `db init|reset|check` a `--scope main|testcase|all` flag.

**Architecture:** New `TestCaseBase` declarative base owns the 3 testcase ORMs; new `get_testcase_engine()` / `get_testcase_session_factory()` pair bound to a new `testcase_database_url` setting (default: sibling `<main-stem>_testcase.db`); `create_schema(scope)` builds each side independently; the testcase repository binds to the testcase factory; `WebState` carries both engines.

**Tech Stack:** Python 3.10+, SQLAlchemy 2.0 (`make_url`, `inspect`), pydantic-settings v2, Typer, FastAPI. No new packages.

## Global Constraints

- Python `>=3.10`; SQLAlchemy `>=2.0.30`; no new dependencies.
- Ruff line-length 100, target `py310` (`ruff check .` must pass after every task).
- Layering: `scraping/` = HTTP only, no parsing; `parsers/` = pure functions, no network; `services/` reach storage only through `repository/` Protocols; `cli.py` / `web/` never instantiate SQL repos directly; `models/` are `@dataclass(slots=True)`, never leak ORM attrs.
- Fresh tables only: `Base.metadata.create_all` / `TestCaseBase.metadata.create_all` pickup, no alembic, no data-migration helpers.
- `table` output is tab-separated via `_emit_table`; CLI `--scope` validation mirrors the `_resolve_cache_purge_scope` / `VALID_PURGE_SCOPES` pattern in `src/doc3gpp/cli.py:334-353`.
- TDD: failing test → run → minimal impl → run → commit per task. Offline by default (`pytest -m "not online"` is the default via `pyproject.toml` addopts).
- `./scripts/test_sqlite.sh` is the full offline gate (run at the end; targeted `pytest` per task).
- Commit messages follow Conventional Commits (`feat(testcase): ...`, `docs(testcase): ...`, `test(testcase): ...`).

---

## File map

| File | Responsibility |
|---|---|
| Modify `src/doc3gpp/settings/schema.py` | `testcase_database_url` field + `ALLOWED_ENV_VARS` entry |
| Modify `src/doc3gpp/storage/db/session.py` | `resolve_testcase_database_url()` + `get_testcase_engine()` + `get_testcase_session_factory()` |
| Create `src/doc3gpp/storage/db/testcase_base.py` | `TestCaseBase(DeclarativeBase)` |
| Modify `src/doc3gpp/storage/db/models.py` | Re-parent the 3 testcase ORMs to `TestCaseBase` |
| Modify `src/doc3gpp/storage/db/migrate.py` | `create_schema(scope="main"\|"testcase"\|"all")` |
| Modify `src/doc3gpp/storage/repositories/testcase_sql.py` | Default to the testcase session factory |
| Modify `src/doc3gpp/cli.py` | `--scope` on `db init/reset/check`; `create_schema("all")` at the 7 sync call sites; `_resolve_db_scope` + sqlite-file helpers |
| Modify `src/doc3gpp/web/state.py` | `WebState.testcase_engine` field |
| Modify `src/doc3gpp/web/app.py` | Wire + dispose the testcase engine |
| Modify `tests/conftest.py` | `sqlite_env` pins + clears the testcase engine |
| Create `tests/unit/test_testcase_db_url.py` | URL derivation + engine separation tests |
| Modify `tests/unit/test_settings_config_file.py` | Allowlist count 6 → 7 |
| Modify `tests/integration/test_testcase_sql.py` | Fixture uses `TestCaseBase` |
| Modify `tests/unit/test_db_reset_cli.py` | Scope tests + dual cache clears |
| Modify `tests/integration/test_db_reset_sqlite.py` | Independent-reset tests |
| Modify `tests/unit/test_job_worker.py` | 3 × `WebState(...)` gain `testcase_engine=None` |
| Modify `tests/integration/test_mcp_end_to_end.py` | 1 × `WebState(...)` gains real testcase engine |
| Modify `tests/unit/test_web_app.py` | Assert `build_state` wires the testcase engine |
| Modify `src/doc3gpp/data/doc3gpp.toml.example` | Commented `testcase_database_url` key (root `doc3gpp.toml.example` is a symlink to this file — one edit covers both) |
| Modify `AGENTS.md`, `docs/cli.md`, `docs/architecture.md`, `docs/code-map.md`, `docs/known-constraints.md` | Doc sync (see Task 7) |

---

### Task 1: Settings — `testcase_database_url` + sibling resolver

**Files:**
- Modify: `src/doc3gpp/settings/schema.py:58-67` (`ALLOWED_ENV_VARS`), `src/doc3gpp/settings/schema.py:755-758` (`Settings.database_url`)
- Modify: `src/doc3gpp/storage/db/session.py` (full file is 26 lines — read it first)
- Create: `tests/unit/test_testcase_db_url.py`
- Modify: `tests/unit/test_settings_config_file.py:477` (allowlist count)

**Interfaces:**
- Consumes: `Settings.database_url`, `ALLOWED_ENV_VARS`, `configure_sqlite_engine` (existing).
- Produces: `Settings.testcase_database_url: str | None` (TOML key `testcase_database_url`, env `DOC3GPP_TESTCASE_DATABASE_URL`); `resolve_testcase_database_url(settings: Settings | None = None) -> str` in `doc3gpp.storage.db.session`; later tasks rely on both exact names.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_testcase_db_url.py` with this exact content:

```python
"""Unit tests for testcase DB URL resolution (no engine, no I/O)."""

from __future__ import annotations

import pytest

from doc3gpp.settings.schema import ALLOWED_ENV_VARS, Settings
from doc3gpp.storage.db.session import resolve_testcase_database_url


@pytest.fixture()
def _clean_db_env(monkeypatch: pytest.MonkeyPatch):
    """Strip DB URL env vars so direct ``Settings(...)`` construction is hermetic."""
    for key in (
        "DOC3GPP_DATABASE_URL",
        "DOC3GPP_TESTCASE_DATABASE_URL",
        "DOC3GPP_CONFIG",
    ):
        monkeypatch.delenv(key, raising=False)


def test_testcase_env_var_is_allowlisted() -> None:
    assert "DOC3GPP_TESTCASE_DATABASE_URL" in ALLOWED_ENV_VARS


def test_sibling_derivation_appends_testcase_stem(_clean_db_env) -> None:
    s = Settings(database_url="sqlite+pysqlite:////tmp/x/doc3gpp.db")
    assert s.testcase_database_url is None
    assert (
        resolve_testcase_database_url(s)
        == "sqlite+pysqlite:////tmp/x/doc3gpp_testcase.db"
    )


def test_sibling_derivation_preserves_suffixless_name(_clean_db_env) -> None:
    s = Settings(database_url="sqlite+pysqlite:////tmp/x/doc3gpp")
    assert (
        resolve_testcase_database_url(s)
        == "sqlite+pysqlite:////tmp/x/doc3gpp_testcase"
    )


def test_memory_main_gives_private_memory_testcase(_clean_db_env) -> None:
    s = Settings(database_url="sqlite+pysqlite:///:memory:")
    assert resolve_testcase_database_url(s) == "sqlite+pysqlite:///:memory:"


def test_explicit_url_wins_over_derivation(
    _clean_db_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "DOC3GPP_TESTCASE_DATABASE_URL", "sqlite+pysqlite:////tmp/x/custom-tc.db"
    )
    from doc3gpp.settings.loader import get_settings

    get_settings.cache_clear()
    try:
        s = get_settings()
        assert s.testcase_database_url == "sqlite+pysqlite:////tmp/x/custom-tc.db"
        assert resolve_testcase_database_url(s) == "sqlite+pysqlite:////tmp/x/custom-tc.db"
    finally:
        get_settings.cache_clear()


def test_non_sqlite_main_without_override_raises(_clean_db_env) -> None:
    s = Settings(database_url="oracle://user:pass@localhost/db")
    with pytest.raises(ValueError, match="testcase_database_url"):
        resolve_testcase_database_url(s)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_testcase_db_url.py -v`
Expected: FAIL — `testcase_database_url` field and `resolve_testcase_database_url` do not exist (`AttributeError` / `ImportError`).

- [ ] **Step 3: Add the setting**

In `src/doc3gpp/settings/schema.py`, add `"DOC3GPP_TESTCASE_DATABASE_URL"` to `ALLOWED_ENV_VARS` (keep the set sorted — it currently reads `DOC3GPP_DATABASE_URL`, `DOC3GPP_DB_ECHO`, ...; insert the new entry directly after `DOC3GPP_DATABASE_URL`).

Directly after the `database_url` field (`src/doc3gpp/settings/schema.py:755-758`), add:

```python
    testcase_database_url: str | None = Field(
        default=None,
        validation_alias="DOC3GPP_TESTCASE_DATABASE_URL",
    )
```

This mirrors `database_url` exactly: the TOML key is the field name (`testcase_database_url`), the env binding is the alias. `None` means "derive a sibling of `database_url`". Also update the `Settings` docstring line that lists flat root fields (line ~744: "The flat fields at the root (``database_url``, ``db_echo``, ...)") to include ``testcase_database_url``.

- [ ] **Step 4: Add the resolver + engines to `session.py`**

Replace `src/doc3gpp/storage/db/session.py` with:

```python
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import sessionmaker

from doc3gpp.config import get_settings
from doc3gpp.storage.backends import configure_sqlite_engine

if TYPE_CHECKING:
    from doc3gpp.settings.schema import Settings


def resolve_testcase_database_url(settings: Settings | None = None) -> str:
    """Return the effective testcase database URL.

    Explicit ``testcase_database_url`` wins. Otherwise derive a sibling
    of ``database_url`` in the same directory with ``_testcase``
    suffixed to the stem (``doc3gpp.db`` → ``doc3gpp_testcase.db``).
    ``:memory:`` mains map to a private ``:memory:`` URL (each engine
    keeps its own in-memory DB). A non-sqlite main URL cannot yield a
    sibling — raises :class:`ValueError` telling the operator to set
    ``testcase_database_url`` explicitly.
    """
    if settings is None:
        settings = get_settings()
    if settings.testcase_database_url:
        return settings.testcase_database_url
    parsed = make_url(settings.database_url)
    if parsed.database in (None, ":memory:"):
        return "sqlite+pysqlite:///:memory:"
    if not parsed.drivername.startswith("sqlite"):
        raise ValueError(
            "cannot derive a testcase database from non-sqlite "
            f"database_url {settings.database_url!r}; set "
            "testcase_database_url explicitly (TOML key "
            "'testcase_database_url' or DOC3GPP_TESTCASE_DATABASE_URL)."
        )
    db_path = Path(parsed.database)
    sibling = db_path.with_name(f"{db_path.stem}_testcase{db_path.suffix}")
    return f"sqlite+pysqlite:///{sibling}"


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


@lru_cache(maxsize=1)
def get_testcase_engine() -> Engine:
    """Return the cached engine for the testcase corpus DB."""
    settings = get_settings()
    database_url = resolve_testcase_database_url(settings)
    return create_engine(
        database_url,
        **configure_sqlite_engine(
            database_url=database_url,
            db_echo=settings.db_echo,
        ),
    )


def get_session_factory() -> sessionmaker:
    return sessionmaker(bind=get_engine(), autoflush=False, autocommit=False)


def get_testcase_session_factory() -> sessionmaker:
    """Return a session factory bound to the testcase engine."""
    return sessionmaker(bind=get_testcase_engine(), autoflush=False, autocommit=False)
```

- [ ] **Step 5: Update the allowlist-count assertion**

In `tests/unit/test_settings_config_file.py:477`, change `assert len(ALLOWED_ENV_VARS) == 6` to `assert len(ALLOWED_ENV_VARS) == 7`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_testcase_db_url.py tests/unit/test_settings_config_file.py -q`
Expected: PASS.

- [ ] **Step 7: Lint + commit**

Run: `ruff check src/doc3gpp/settings/schema.py src/doc3gpp/storage/db/session.py tests/unit/test_testcase_db_url.py tests/unit/test_settings_config_file.py`
Expected: clean.

```bash
git add src/doc3gpp/settings/schema.py src/doc3gpp/storage/db/session.py tests/unit/test_testcase_db_url.py tests/unit/test_settings_config_file.py
git commit -m "feat(testcase): add testcase_database_url setting and sibling resolver"
```

### Task 2: `TestCaseBase` + testcase engine wiring + fixture

**Files:**
- Create: `src/doc3gpp/storage/db/testcase_base.py`
- Modify: `src/doc3gpp/storage/db/models.py:1-20` (imports), `457-514` (`TestCaseORM`, `TestCaseStatusORM`, `TestCaseSourceORM`)
- Modify: `tests/conftest.py:9-17` (`sqlite_env`)
- Test: `tests/unit/test_testcase_db_url.py` (append)

**Interfaces:**
- Consumes: `resolve_testcase_database_url`, `get_testcase_engine` (Task 1).
- Produces: `TestCaseBase` in `doc3gpp.storage.db.testcase_base` with `TestCaseBase.metadata` holding exactly `{"testcases", "testcase_status", "testcase_sources"}`; `sqlite_env` pins `DOC3GPP_TESTCASE_DATABASE_URL` to `<tmp>/test_testcase.db` and clears both engine caches + settings cache on setup/teardown.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_testcase_db_url.py`:

```python
def test_testcase_tables_live_on_testcase_base_only() -> None:
    from doc3gpp.storage.db.base import Base
    from doc3gpp.storage.db.testcase_base import TestCaseBase

    assert set(TestCaseBase.metadata.tables) == {
        "testcases",
        "testcase_status",
        "testcase_sources",
    }
    assert {"testcases", "testcase_status", "testcase_sources"}.isdisjoint(
        Base.metadata.tables
    )


def test_engines_are_distinct(sqlite_env) -> None:
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.db.session import get_engine, get_testcase_engine

    create_schema()
    assert get_engine() is not get_testcase_engine()
    assert str(get_testcase_engine().url).endswith("test_testcase.db")
```

Note: `test_engines_are_distinct` uses the `sqlite_env` fixture, which does not pin the testcase URL yet — it fails until the fixture change below, and `create_schema()` (still scopeless at this point) must keep creating everything on the main engine so the `is not` assertion can even run. It passes fully once Tasks 2+3 land.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_testcase_db_url.py -q`
Expected: FAIL — `doc3gpp.storage.db.testcase_base` does not exist.

- [ ] **Step 3: Create `TestCaseBase`**

Create `src/doc3gpp/storage/db/testcase_base.py`:

```python
from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class TestCaseBase(DeclarativeBase):
    """Separate declarative base for the RAN5 testcase corpus.

    The ``testcases`` / ``testcase_status`` / ``testcase_sources``
    tables live in their own sqlite file (see
    :func:`doc3gpp.storage.db.session.get_testcase_engine`), so their
    ORM classes hang off this base instead of the main ``Base``. That
    way ``Base.metadata.create_all`` never touches them and
    ``TestCaseBase.metadata.create_all`` creates exactly those three.
    """
```

- [ ] **Step 4: Re-parent the 3 ORMs**

In `src/doc3gpp/storage/db/models.py`:
1. Add `from doc3gpp.storage.db.testcase_base import TestCaseBase` to the imports (after the `base` import).
2. Change `class TestCaseORM(Base):` → `class TestCaseORM(TestCaseBase):`, `class TestCaseStatusORM(Base):` → `class TestCaseStatusORM(TestCaseBase):`, `class TestCaseSourceORM(Base):` → `class TestCaseSourceORM(TestCaseBase):`. Nothing else in those classes changes (table names, columns, and the composite `ForeignKeyConstraint` stay byte-identical — the FK is contained inside the testcase DB).

- [ ] **Step 5: Update the `sqlite_env` fixture**

Replace `tests/conftest.py:9-17` with:

```python
@pytest.fixture()
def sqlite_env(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    testcase_db_path = tmp_path / "test_testcase.db"
    monkeypatch.setenv("DOC3GPP_DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv(
        "DOC3GPP_TESTCASE_DATABASE_URL", f"sqlite+pysqlite:///{testcase_db_path}"
    )
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_testcase_engine.cache_clear()
    yield db_path
    get_engine.cache_clear()
    get_testcase_engine.cache_clear()
    get_settings.cache_clear()
```

And update the import line to `from doc3gpp.storage.db.session import get_engine, get_testcase_engine`. The yielded value stays `db_path` (the main file); the testcase file is always `db_path.parent / "test_testcase.db"` — new tests should derive it that way, never hardcode another name.

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_testcase_db_url.py tests/integration/test_testcase_sql.py -q`
Expected: PASS. (`test_testcase_sql.py` passes because its fixture builds its own in-memory engine with an explicit `session_factory`, and `Base.metadata.create_all` on an empty in-memory DB is a harmless no-op for the repo. Its fixture update to `TestCaseBase` comes in Task 4 — note it still passes here because `Base.metadata.create_all` creates an empty schema and the repo never touches it... wait, no: the repo writes `testcases` rows through the explicit in-memory factory, so the tables MUST exist. After re-parenting, `Base.metadata.create_all(engine)` no longer creates them → these tests FAIL now. That is expected and desired: Task 4 fixes the fixture. If you want Task 2 green on its own, do Task 4's fixture edit here. Either way, do not commit red.)

- [ ] **Step 7: Lint + commit**

Run: `ruff check src/doc3gpp/storage/db/ tests/conftest.py tests/unit/test_testcase_db_url.py`
Expected: clean.

```bash
git add src/doc3gpp/storage/db/testcase_base.py src/doc3gpp/storage/db/models.py tests/conftest.py tests/unit/test_testcase_db_url.py
git commit -m "feat(testcase): separate TestCaseBase and testcase engine wiring"
```

### Task 3: `create_schema(scope)` + explicit call sites

**Files:**
- Modify: `src/doc3gpp/storage/db/migrate.py:1-22` (imports), `341-353` (`create_schema`)
- Modify: `src/doc3gpp/cli.py` — the 7 bare `create_schema()` call sites (lines 636, 697, 729, 3814, 3836, 3988, 4348; verify with `rg "create_schema\(\)" src/doc3gpp/cli.py` — line 693 is a comment, leave it)
- Test: `tests/unit/test_testcase_db_url.py` (append scope tests)

**Interfaces:**
- Consumes: `TestCaseBase`, `get_testcase_engine` (Task 2).
- Produces: `create_schema(scope: str = "all")` in `doc3gpp.storage.db.migrate`; `scope` must be `"main"`, `"testcase"`, or `"all"` (anything else raises `ValueError`). Default `"all"` preserves today's behavior for all existing callers (tests included).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_testcase_db_url.py`:

```python
def _table_names(engine) -> set[str]:
    from sqlalchemy import inspect

    return set(inspect(engine).get_table_names())


def test_create_schema_scope_main_only(sqlite_env) -> None:
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.db.session import get_engine, get_testcase_engine

    create_schema("main")
    assert "meetings" in _table_names(get_engine())
    assert "testcases" not in _table_names(get_engine())
    assert _table_names(get_testcase_engine()) == set()


def test_create_schema_scope_testcase_only(sqlite_env) -> None:
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.db.session import get_engine, get_testcase_engine

    create_schema("testcase")
    assert _table_names(get_engine()) == set()
    assert {"testcases", "testcase_status", "testcase_sources"} <= _table_names(
        get_testcase_engine()
    )


def test_create_schema_scope_all_creates_both(sqlite_env) -> None:
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.db.session import get_engine, get_testcase_engine

    create_schema("all")
    assert "meetings" in _table_names(get_engine())
    assert {"testcases", "testcase_status", "testcase_sources"} <= _table_names(
        get_testcase_engine()
    )
    assert "testcases" not in _table_names(get_engine())


def test_create_schema_rejects_unknown_scope(sqlite_env) -> None:
    from doc3gpp.storage.db.migrate import create_schema

    with pytest.raises(ValueError, match="scope"):
        create_schema("nope")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_testcase_db_url.py -q -k scope`
Expected: FAIL — `create_schema()` takes no arguments.

- [ ] **Step 3: Implement scoped `create_schema`**

In `src/doc3gpp/storage/db/migrate.py`:
1. Add `from doc3gpp.storage.db.testcase_base import TestCaseBase` to the imports.
2. Keep the `TestCaseORM, TestCaseSourceORM, TestCaseStatusORM` names in the models import list (the module still defines them — the import is what registers them on `TestCaseBase.metadata`).
3. Change `from doc3gpp.storage.db.session import get_engine` to `from doc3gpp.storage.db.session import get_engine, get_testcase_engine`.
4. Replace `create_schema` (lines 341-353) with:

```python
def create_schema(scope: str = "all") -> None:
    """Create database tables for configured backend(s).

    Args:
        scope: ``"main"`` creates the main schema (one-shot
            migrations + ``Base`` tables + FTS5/vector sidecars) on
            the main engine; ``"testcase"`` creates exactly the three
            testcase tables on the testcase engine; ``"all"``
            (default) does both. Anything else raises
            :class:`ValueError`.
    """
    if scope not in ("main", "testcase", "all"):
        raise ValueError(
            f"unknown schema scope {scope!r}; choose from: main, testcase, all."
        )
    if scope in ("main", "all"):
        engine = get_engine()
        _migrate_rename_tdoc_cr_details()
        _migrate_drop_tsg_spec_last_sync()
        _migrate_spec_rapporteurs()
        _migrate_tdoc_cr_cover_page_summary_of_change()
        _migrate_tdocs_xlsx_metadata()
        _migrate_spec_versions_drop_comment()
        Base.metadata.create_all(bind=engine)
        _create_search_schema()
        _create_vector_schema()
    if scope in ("testcase", "all"):
        TestCaseBase.metadata.create_all(bind=get_testcase_engine())
```

- [ ] **Step 4: Make the CLI call sites explicit**

In `src/doc3gpp/cli.py`, change each of the 7 bare `create_schema()` calls (in `db_init`, `db_reset`, `meeting_sync`, `tsg_seed`, `wi_sync`, `spec_sync`, `testcase_sync`) to `create_schema("all")`. Same behavior, explicit scope. (Task 5 rewrites `db_init`/`db_reset` to pass the selected scope.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_testcase_db_url.py -q`
Expected: PASS (all tests in the file, including Task 2's `test_engines_are_distinct`).

- [ ] **Step 6: Lint + commit**

Run: `ruff check src/doc3gpp/storage/db/migrate.py src/doc3gpp/cli.py tests/unit/test_testcase_db_url.py`
Expected: clean.

```bash
git add src/doc3gpp/storage/db/migrate.py src/doc3gpp/cli.py tests/unit/test_testcase_db_url.py
git commit -m "feat(testcase): add scoped create_schema for main/testcase engines"
```

### Task 4: Repository rebind + testcase SQL fixture

**Files:**
- Modify: `src/doc3gpp/storage/repositories/testcase_sql.py:17,37-38`
- Modify: `tests/integration/test_testcase_sql.py:13-14,17-27` (fixture)
- Test: `tests/integration/test_testcase_sql.py` (append cross-DB test)

**Interfaces:**
- Consumes: `get_testcase_session_factory` (Task 1), scoped `create_schema` (Task 3).
- Produces: `SQLAlchemyTestCaseRepository()` with no args talks to the testcase engine. The explicit-`session_factory` constructor override is unchanged (in-memory unit tests keep working).

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_testcase_sql.py`:

```python
def test_rows_land_in_testcase_db_not_main_db(sqlite_env) -> None:
    """Default-constructed repo reads/writes the testcase file only."""
    from sqlalchemy import inspect

    from doc3gpp.models.testcase import TestCase
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.db.session import get_engine, get_testcase_engine
    from doc3gpp.storage.repositories.testcase_sql import SQLAlchemyTestCaseRepository

    create_schema("all")
    repo = SQLAlchemyTestCaseRepository()
    assert repo.upsert_many([TestCase(testcase_id="TC_DB", group="5G")]) == 1
    assert repo.get("TC_DB", "5G") is not None
    assert "testcases" not in set(inspect(get_engine()).get_table_names())
    assert "testcases" in set(inspect(get_testcase_engine()).get_table_names())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/integration/test_testcase_sql.py::test_rows_land_in_testcase_db_not_main_db tests/integration/test_testcase_sql.py::test_upsert_get_list_status_round_trip -q`
Expected: FAIL — the repo still hits the main engine (first test), and the round-trip test fails because `Base.metadata.create_all` no longer creates `testcases` on the fixture's in-memory engine (Task 2 fallout).

- [ ] **Step 3: Rebind the repository**

In `src/doc3gpp/storage/repositories/testcase_sql.py`, change line 17 to `from doc3gpp.storage.db.session import get_testcase_session_factory` and line 38 to `self._session_factory = session_factory or get_testcase_session_factory()`. Update the class docstring (`SQLAlchemy implementation storing rows in ``testcases`` tables.`) to note it binds to the testcase engine by default:

```python
class SQLAlchemyTestCaseRepository:
    """SQLAlchemy implementation storing rows in ``testcases`` tables.

    Binds to the testcase engine (:func:`get_testcase_session_factory`)
    by default; pass an explicit ``session_factory`` to override
    (tests bind to in-memory SQLite).
    """
```

- [ ] **Step 4: Fix the SQL test fixture to use `TestCaseBase`**

In `tests/integration/test_testcase_sql.py`, change the imports to:

```python
from doc3gpp.storage.db.testcase_base import TestCaseBase
from doc3gpp.storage.db import models as m  # noqa: F401
```

and the fixture body `Base.metadata.create_all(engine)` to `TestCaseBase.metadata.create_all(engine)`. (The `import ... models as m` line stays — importing the module registers the ORM classes on `TestCaseBase.metadata`.)

- [ ] **Step 5: Sweep for other tests referencing the moved tables**

Run: `rg -l "TestCaseORM|TestCaseSourceORM|TestCaseStatusORM|testcase_status|testcase_sources" tests/`
Expected hits: `tests/integration/test_testcase_sql.py` (fixed above) and possibly end-to-end tests that go through services (those need no change — verify by running). Fix any additional direct-`Base`-metadata or raw-`testcases`-table usage the same way.

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/integration/test_testcase_sql.py tests/integration/test_testcase_cli.py tests/unit/test_testcase_service.py tests/unit/test_testcase_model.py tests/unit/test_testcase_parser.py tests/integration/test_web_end_to_end.py tests/integration/test_mcp_end_to_end.py -q`
Expected: PASS.

- [ ] **Step 7: Lint + commit**

Run: `ruff check src/doc3gpp/storage/repositories/testcase_sql.py tests/integration/test_testcase_sql.py`
Expected: clean.

```bash
git add src/doc3gpp/storage/repositories/testcase_sql.py tests/integration/test_testcase_sql.py
git commit -m "feat(testcase): bind testcase repository to the testcase engine"
```

### Task 5: CLI — `db --scope` for init/check/reset

**Files:**
- Modify: `src/doc3gpp/cli.py:334-353` (add `VALID_DB_SCOPES` + `_resolve_db_scope` next to the purge-scope pattern), `614-701` (`db_check`, `db_init`, `db_reset`)
- Modify: `tests/unit/test_db_reset_cli.py` (dual cache clears + scope tests)
- Modify: `tests/integration/test_db_reset_sqlite.py` (independent-reset tests), `tests/integration/test_cli_sqlite.py` (check output)

**Interfaces:**
- Consumes: `create_schema(scope)`, `resolve_testcase_database_url`, `get_testcase_engine` (Tasks 1-3).
- Produces: `db init|reset|check --scope main|testcase|all` (default `"all"`). `db check` always prints both URLs but only connects to selected engines. `db reset` validates ALL selected scopes are sqlite BEFORE deleting anything, then deletes per-scope files + sidecars, clears both engine caches, recreates selected schemas, re-seeds `tsgs` when main is in scope.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_db_reset_cli.py`:

```python
def test_db_reset_rejects_unknown_scope(sqlite_env) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["db", "reset", "--scope", "nope", "--yes"])
    assert result.exit_code != 0
    assert "Unknown --scope" in result.output


def test_db_reset_scope_testcase_keeps_main_rows(sqlite_env) -> None:
    from datetime import date

    from doc3gpp.models.meeting import Meeting
    from doc3gpp.models.testcase import TestCase
    from doc3gpp.storage.repositories.meeting_sql import SQLAlchemyMeetingRepository
    from doc3gpp.storage.repositories.testcase_sql import SQLAlchemyTestCaseRepository

    runner = CliRunner()
    assert runner.invoke(app, ["db", "init"]).exit_code == 0
    SQLAlchemyMeetingRepository().upsert_many(
        [
            Meeting(
                meeting_id=1, name="R5#1", title="T", location="Online",
                start_date=date(2026, 1, 1), end_date=date(2026, 1, 2),
            ),
        ]
    )
    SQLAlchemyTestCaseRepository().upsert_many(
        [TestCase(testcase_id="TC_1", group="5G")]
    )
    result = runner.invoke(app, ["db", "reset", "--scope", "testcase", "--yes"])
    assert result.exit_code == 0, result.output
    assert "Testcase database reset complete" in result.output
    assert _count_meetings() == 1
    assert _count_tsgs() == 19


def test_db_reset_scope_main_keeps_testcase_rows(sqlite_env) -> None:
    from doc3gpp.models.testcase import TestCase
    from doc3gpp.storage.repositories.testcase_sql import SQLAlchemyTestCaseRepository

    runner = CliRunner()
    assert runner.invoke(app, ["db", "init"]).exit_code == 0
    SQLAlchemyTestCaseRepository().upsert_many(
        [TestCase(testcase_id="TC_1", group="5G")]
    )
    result = runner.invoke(app, ["db", "reset", "--scope", "main", "--yes"])
    assert result.exit_code == 0, result.output
    assert "Database reset complete" in result.output
    assert SQLAlchemyTestCaseRepository().get("TC_1", "5G") is not None


def test_db_reset_refuses_non_sqlite_testcase_url(sqlite_env, monkeypatch) -> None:
    from doc3gpp.settings.loader import get_settings
    from doc3gpp.storage.db.session import get_testcase_engine

    monkeypatch.setenv(
        "DOC3GPP_TESTCASE_DATABASE_URL", "oracle://user:pass@localhost/tc"
    )
    get_settings.cache_clear()
    get_testcase_engine.cache_clear()
    try:
        runner = CliRunner()
        result = runner.invoke(app, ["db", "reset", "--yes"])
        assert result.exit_code != 0
        assert "only supports SQLite backends" in result.output
        assert "testcase" in result.output
    finally:
        get_testcase_engine.cache_clear()
        get_settings.cache_clear()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_db_reset_cli.py -q -k "scope or unknown"`
Expected: FAIL — `--scope` is an unknown option.

- [ ] **Step 3: Add the scope resolver + helpers**

After `VALID_PURGE_SCOPES` / `_resolve_cache_purge_scope` (`src/doc3gpp/cli.py:334-353`), add:

```python
VALID_DB_SCOPES: tuple[str, ...] = ("main", "testcase", "all")


def _resolve_db_scope(scope: str) -> str:
    """Resolve ``--scope`` for the ``db`` commands.

    Mirrors :func:`_resolve_cache_purge_scope`: normalises whitespace
    + case and validates against :data:`VALID_DB_SCOPES`. Unknown
    values raise :class:`typer.BadParameter`.
    """
    normalized = scope.strip().lower()
    if normalized not in VALID_DB_SCOPES:
        valid = ", ".join(VALID_DB_SCOPES)
        raise typer.BadParameter(
            f"Unknown --scope {scope!r}. Choose from: {valid}."
        )
    return normalized


def _sqlite_file_for_scope(database_url: str, scope: str) -> Path | None:
    """Return the sqlite file for ``database_url`` or ``None`` for ``:memory:``.

    Raises :class:`typer.BadParameter` naming ``scope`` when the URL is
    not sqlite. Call for every selected scope BEFORE deleting anything
    so a mixed-backend ``reset`` fails without touching any file.
    """
    parsed = make_url(database_url)
    if not parsed.drivername.startswith("sqlite"):
        raise typer.BadParameter(
            f"'db reset' only supports SQLite backends "
            f"(scope {scope!r}: {database_url})."
        )
    if parsed.database and parsed.database != ":memory:":
        return Path(parsed.database)
    return None


def _testcase_url_or_raise() -> str:
    """Resolve the testcase URL, mapping derivation errors to CLI errors."""
    try:
        return resolve_testcase_database_url()
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _delete_sqlite_file(db_file: Path | None, scope: str) -> None:
    """Delete ``db_file`` + WAL sidecars, echoing what happened."""
    if db_file is not None and db_file.exists():
        logger.info("Deleting SQLite database file %s", db_file)
        db_file.unlink()
        # Also remove any SQLite journal sidecar files (-wal, -shm, -journal)
        # so a half-written WAL from a previous session does not survive
        # the reset and confuse the new schema.
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = db_file.with_name(db_file.name + suffix)
            if sidecar.exists():
                sidecar.unlink()
                logger.debug("Removed SQLite sidecar %s", sidecar)
        typer.echo(f"Deleted {db_file}")
    else:
        typer.echo(f"No existing SQLite file to delete ({scope}).")
```

Add `resolve_testcase_database_url, get_testcase_engine` to the existing `from doc3gpp.storage.db.session import get_engine` import (line 102). `make_url`, `Path`, `typer`, `logger` are already imported in `cli.py` (used by the current `db_reset`).

- [ ] **Step 4: Rewrite `db_check` / `db_init` / `db_reset`**

```python
@db_app.command("check")
def db_check(
    scope: str = typer.Option(
        "all",
        "--scope",
        help="Which database to check: 'main', 'testcase', or 'all'.",
    ),
) -> None:
    """Validate database connectivity for configured backend(s)."""
    resolved_scope = _resolve_db_scope(scope)
    logger.info("Checking database connectivity")
    settings = get_settings()
    if resolved_scope in ("main", "all"):
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        typer.echo(f"Database connection OK: {settings.database_url}")
    if resolved_scope in ("testcase", "all"):
        tc_url = _testcase_url_or_raise()
        tc_engine = get_testcase_engine()
        with tc_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        typer.echo(f"Testcase database connection OK: {tc_url}")


@db_app.command("init")
def db_init(
    scope: str = typer.Option(
        "all",
        "--scope",
        help="Which database to initialise: 'main', 'testcase', or 'all'.",
    ),
) -> None:
    """Create schema for current backend(s) and seed the TSG reference table.

    Re-running this command is safe: the TSG seed is upsert-based, so existing
    rows are refreshed in place rather than duplicated.
    """
    resolved_scope = _resolve_db_scope(scope)
    logger.info("Initializing database schema")
    create_schema(resolved_scope)
    if resolved_scope in ("main", "all"):
        tsg_service = build_tsg_service()
        seeded = tsg_service.seed_defaults()
        logger.info("Seeded %s TSG reference records", seeded)
        typer.echo(f"Database schema initialized; seeded {seeded} TSG records")
    else:
        typer.echo("Testcase database schema initialized")


@db_app.command("reset")
def db_reset(
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the confirmation prompt.",
    ),
    scope: str = typer.Option(
        "all",
        "--scope",
        help="Which database to reset: 'main', 'testcase', or 'all'.",
    ),
) -> None:
    """Delete the SQLite database file(s) and recreate the schema.

    Destructive: all data in the selected scope is wiped. SQLite URLs
    only — every selected scope must be sqlite or the whole reset is
    rejected before anything is deleted. Prompts for confirmation
    unless ``--yes`` is passed. After reset the ``tsgs`` reference
    table is re-seeded when the main scope is selected.
    """
    resolved_scope = _resolve_db_scope(scope)
    settings = get_settings()
    urls: list[tuple[str, str]] = []
    if resolved_scope in ("main", "all"):
        urls.append(("main", settings.database_url))
    if resolved_scope in ("testcase", "all"):
        urls.append(("testcase", _testcase_url_or_raise()))
    # Validate every selected scope BEFORE deleting anything: a
    # mixed-backend reset fails without touching any file.
    files: list[tuple[str, Path | None]] = [
        (scope_name, _sqlite_file_for_scope(url, scope_name))
        for scope_name, url in urls
    ]

    targets = [str(f) for _, f in files if f is not None and f.exists()]
    if targets and not yes:
        typer.confirm(
            "Delete SQLite database file(s)?\n" + "\n".join(targets),
            abort=True,
        )
    for scope_name, db_file in files:
        _delete_sqlite_file(db_file, scope_name)

    # SQLAlchemy cached the engines from the pre-delete file paths; clear
    # both so create_schema() opens fresh connections.
    get_engine.cache_clear()
    get_testcase_engine.cache_clear()

    logger.info("Recreating database schema")
    create_schema(resolved_scope)
    if resolved_scope in ("main", "all"):
        tsg_service = build_tsg_service()
        seeded = tsg_service.seed_defaults()
        logger.info("Seeded %s TSG reference records", seeded)
        typer.echo(f"Database reset complete; seeded {seeded} TSG records")
    else:
        typer.echo("Testcase database reset complete")
```

Compatibility notes (verify while editing):
- Default `db reset --yes` prints `Deleted <file>` per existing file, `Database reset complete; seeded 19 TSG records` — existing unit/integration tests keep passing. The "No existing SQLite file to delete." message gains a ` (scope)` suffix — the existing tests assert it as a SUBSTRING (`assert "No existing SQLite file to delete" in result.output`), so they still pass.
- `db check` keeps the `Database connection OK:` line for main/all — `tests/integration/test_cli_sqlite.py` keeps passing.
- The abort-on-decline test feeds `n` to a single combined prompt — still aborts non-zero with the file untouched.

- [ ] **Step 5: Dual cache clears in manually-patched tests**

In `tests/unit/test_db_reset_cli.py`, the two tests that bypass `sqlite_env` (`test_db_reset_in_memory_sqlite_just_reinits`, `test_db_reset_refuses_non_sqlite_url`) call `get_engine.cache_clear()` in setup/teardown. Add `get_testcase_engine.cache_clear()` next to every such call (import it at the top of those functions: `from doc3gpp.storage.db.session import get_engine, get_testcase_engine`). Without this, a testcase engine cached from an earlier `sqlite_env` test would linger across the monkeypatched URL.

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_db_reset_cli.py tests/integration/test_db_reset_sqlite.py tests/integration/test_cli_sqlite.py -q`
Expected: PASS.

- [ ] **Step 7: Lint + commit**

Run: `ruff check src/doc3gpp/cli.py tests/unit/test_db_reset_cli.py tests/integration/test_db_reset_sqlite.py`
Expected: clean.

```bash
git add src/doc3gpp/cli.py tests/unit/test_db_reset_cli.py tests/integration/test_db_reset_sqlite.py tests/integration/test_cli_sqlite.py
git commit -m "feat(testcase): add --scope to db init/reset/check"
```

### Task 6: Web — carry + dispose the testcase engine

**Files:**
- Modify: `src/doc3gpp/web/state.py:161-173` (`WebState`)
- Modify: `src/doc3gpp/web/app.py:46-74` (`build_state`), `146-158` (lifespan)
- Modify: `tests/unit/test_job_worker.py:99,981,1328-1333` (3 × `WebState(...)`)
- Modify: `tests/integration/test_mcp_end_to_end.py:30-40` (imports), `800-820` (`WebState(...)` + teardown)
- Modify: `tests/unit/test_web_app.py` (append wiring test)

**Interfaces:**
- Consumes: `get_testcase_engine` (Task 1).
- Produces: `WebState.settings/engine/services/jobs` unchanged plus `WebState.testcase_engine: Engine`; lifespan disposes both engines. No route/handler changes (web + MCP reach testcases through `TestCaseService` → repo → testcase engine).

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_web_app.py`:

```python
def test_build_state_wires_testcase_engine(sqlite_env) -> None:
    """``build_state`` carries the shared testcase engine on ``WebState``."""
    from doc3gpp.storage.db.session import get_testcase_engine
    from doc3gpp.web.app import build_state
    from doc3gpp.settings.schema import Settings

    state = build_state(Settings())
    assert state.testcase_engine is get_testcase_engine()
    assert state.testcase_engine is not state.engine
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_web_app.py::test_build_state_wires_testcase_engine -q`
Expected: FAIL — `WebState.__init__() got an unexpected keyword argument 'testcase_engine'` (or `AttributeError` on access).

- [ ] **Step 3: Add the field + wiring**

1. In `src/doc3gpp/web/state.py`, add `testcase_engine: Engine` to `WebState` (after `engine`), and update the class docstring ("Holds the resolved ... the singleton SQLAlchemy :class:`Engine`, ..." → mention both engines).
2. In `src/doc3gpp/web/app.py` `build_state`, add `testcase_engine=get_testcase_engine()` to the `WebState(...)` call and extend the `get_engine` import to include `get_testcase_engine`.
3. In the lifespan `finally` block, dispose both:
```python
        finally:
            await handle.shutdown()
            state.engine.dispose()
            state.testcase_engine.dispose()
```

- [ ] **Step 4: Fix the test-only `WebState(...)` constructions**

- `tests/unit/test_job_worker.py` lines 99, 981: `WebState(settings=settings, engine=None, ...)` → add `testcase_engine=None` (same `# type: ignore[arg-type]` style already on those lines).
- `tests/unit/test_job_worker.py` lines 1328-1333: same — add `testcase_engine=None`.
- `tests/integration/test_mcp_end_to_end.py` lines 800-805: `engine=get_engine()` → also pass `testcase_engine=get_testcase_engine()` (extend the existing `get_engine` import). In the teardown near line 819-820 (`get_engine.cache_clear()` + `del state.engine`), add `get_testcase_engine.cache_clear()` and `del state.testcase_engine`.

Then sweep: `rg -n "WebState\(" tests/ src/` — every construction site must pass `testcase_engine`. Fix any remaining hits the same way.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_web_app.py tests/unit/test_job_worker.py tests/unit/test_web_routes.py tests/integration/test_mcp_end_to_end.py -q`
Expected: PASS.

- [ ] **Step 6: Lint + commit**

Run: `ruff check src/doc3gpp/web/state.py src/doc3gpp/web/app.py tests/unit/test_job_worker.py tests/integration/test_mcp_end_to_end.py tests/unit/test_web_app.py`
Expected: clean.

```bash
git add src/doc3gpp/web/state.py src/doc3gpp/web/app.py tests/unit/test_job_worker.py tests/integration/test_mcp_end_to_end.py tests/unit/test_web_app.py
git commit -m "feat(testcase): carry the testcase engine on WebState"
```

### Task 7: Docs, config template, full gate

**Files:**
- Modify: `src/doc3gpp/data/doc3gpp.toml.example` (root `doc3gpp.toml.example` is a symlink — verify with `ls -la`, edit once)
- Modify: `AGENTS.md`, `docs/cli.md`, `docs/architecture.md`, `docs/code-map.md`, `docs/known-constraints.md`
- Check: `rg -l "db reset|database_url" README.md docs/ AGENTS.md` for any other prose referencing single-DB behavior; update hits.

**Interfaces:**
- Consumes: all prior tasks. Produces: user-facing docs matching the new behavior; full offline gate green.

- [ ] **Step 1: Config template**

In `src/doc3gpp/data/doc3gpp.toml.example`, under the Database section after line 26 (`# database_url = ...`), add:

```toml
# testcase_database_url = "sqlite+pysqlite:////home/you/.local/share/doc3gpp/doc3gpp_testcase.db"
#   # Optional: RAN5 testcase corpus lives in its own sqlite file so it can
#   # be reset independently (`doc3gpp db reset --scope testcase`). Unset
#   # (default) derives a sibling of database_url with `_testcase` suffixed
#   # to the stem. Also settable via DOC3GPP_TESTCASE_DATABASE_URL.
```

Then run `python -m pytest tests/unit/test_settings_config_file.py tests/unit/test_config_cli.py -q` (find the actual config-CLI test filename with `ls tests/unit | rg config` first — if any test snapshots the template text, update it). Expected: PASS.

- [ ] **Step 2: `docs/cli.md`**

1. In the `db init` / `db check` / `db reset` section, document `--scope main|testcase|all` (default `all`), with examples:
   - `doc3gpp db reset --scope testcase --yes` — wipes only the testcase corpus, main data untouched.
   - `doc3gpp db check --scope testcase` — connectivity for the testcase file only (both URLs are always printed... no — per spec, `db check` prints both URLs regardless of scope but only connects to selected engines. Write exactly that).
2. Document `testcase_database_url` (TOML key + `DOC3GPP_TESTCASE_DATABASE_URL` env) and the sibling-derivation default.
3. Note the upgrade behavior: pre-existing main DB files keep orphan `testcases`/`testcase_status`/`testcase_sources` tables (harmless, unread); reclaim with `db reset --scope main` (destructive!) or leave them.

- [ ] **Step 3: `docs/architecture.md`**

1. Storage section: second declarative base + second engine. Name `TestCaseBase` (`storage/db/testcase_base.py`), `get_testcase_engine()` / `get_testcase_session_factory()` (`storage/db/session.py`), and the data-flow line: testcase traffic → `SQLAlchemyTestCaseRepository` → testcase factory → `testcase_database_url`; everything else → main factory → `database_url`.
2. Update the `get_engine()` / `get_session_factory()` "(cached; same clear ...)" note (~line 926) to cover both engine caches and the `sqlite_env` dual-clear.

- [ ] **Step 4: `AGENTS.md`, `docs/code-map.md`, `docs/known-constraints.md`**

1. `AGENTS.md`: in the testcase workflow one-liners, add the two-DB note — suggested prose: "Testcase tables live in a separate sqlite file (`testcase_database_url`, default sibling `<main-stem>_testcase.db`); `db reset --scope testcase` wipes only the corpus." Also update the `db reset` mentions if any.
2. `docs/code-map.md`: add rows — `TestCaseBase` → `storage/db/testcase_base.py`; `get_testcase_engine` / `get_testcase_session_factory` / `resolve_testcase_database_url` → `storage/db/session.py`; `create_schema(scope)` → `storage/db/migrate.py`.
3. `docs/known-constraints.md`: add the orphan-tables-on-upgrade limitation (one bullet).

- [ ] **Step 5: Full gate**

Run: `./scripts/test_sqlite.sh`
Expected: all pass except the known pre-existing `test_skip_when_within_auto_sync_interval` date-drift failure (fails on clean tree too — confirm with `git stash` if it appears; do NOT fix it in this change set).
Run: `ruff check .`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/data/doc3gpp.toml.example AGENTS.md docs/cli.md docs/architecture.md docs/code-map.md docs/known-constraints.md
git commit -m "docs(testcase): document the separate testcase database"
```

---

## Self-Review

**1. Spec coverage** (against `docs/superpowers/specs/2026-09-12-testcase-separate-db-design.md`):
- Separate base + second engine → Tasks 1-2. ✅
- All 3 tables move (sources ledger included) → Task 2 re-parents all three; scope test asserts all three. ✅
- Sibling default + explicit override + env → Task 1. ✅
- `create_schema(scope)` → Task 3. ✅
- `db --scope` on init/reset/check, both-must-be-sqlite on reset → Task 5 (validation loop runs before any deletion; `_sqlite_file_for_scope` raises per-scope). ✅
- Bare `create_schema()` call sites → `create_schema("all")` → Task 3 Step 4. ✅
- `WebState.testcase_engine` + dual dispose → Task 6. ✅
- `sqlite_env` pins + clears both → Task 2 Step 5. ✅
- Derivation/scope/FK/independent-reset/non-sqlite tests → Tasks 1/3/4/5. ✅ (FK containment: composite FK unchanged inside the testcase DB; covered implicitly by scoped replace tests — no new FK test needed since the constraint SQL is untouched.)
- Docs list (AGENTS, cli, architecture, code-map, toml.example; web-server only if visible — it isn't, skipped deliberately) → Task 7. ✅
- `db check` prints both URLs regardless of scope, connects per scope → Task 5 Step 4. ✅ (Spec contradiction already fixed in `5219f57`.)

**2. Placeholder scan:** every step has exact file paths, exact code blocks, exact commands, exact expected outputs. No TBD/TODO/"similar to". The two "find the actual filename" asides (config-CLI test file, `rg` sweeps) are bounded discovery steps with explicit fallback handling, not placeholders.

**3. Type consistency:** `resolve_testcase_database_url(settings | None) -> str` (Task 1) is consumed under the same name/signature in Tasks 5 (`_testcase_url_or_raise`) and session internals; `create_schema(scope: str = "all")` (Task 3) is called with `"all"` (Tasks 3-4) and `resolved_scope` (Task 5, values constrained to the same triple by `_resolve_db_scope`); `get_testcase_session_factory() -> sessionmaker` (Task 1) is consumed in Task 4; `WebState.testcase_engine: Engine` (Task 6) is constructed with `get_testcase_engine()` / `None` (tests). `VALID_DB_SCOPES` order `("main", "testcase", "all")` matches the `create_schema` vocabulary so `resolved_scope` passes straight through.

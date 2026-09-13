# RAN5 Testcases Sync / List / Show — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add RAN5 testcase sync/list/show across CLI, REST, web UI, background jobs, and MCP, byte-consistent on JSON.

**Architecture:** Layered like specs: `scraping/testcase_source.py` (network only) → `parsers/testcase_parser.py` (pure) → `services/testcase_service.py` (orchestration, skip rule) → `storage/repositories/testcase_sql.py` (SQL via `TestCaseRepository` Protocol) → CLI thin Typer + FastAPI thin routes + MCP thin tools + job handler. Three new tables via `Base.metadata.create_all`, no alembic.

**Tech Stack:** Python 3.10+, SQLAlchemy 2.0, pydantic-settings v2, httpx (`ScraperClient`), BeautifulSoup4 + lxml (history listing), openpyxl 3.1+ (`read_only=True, data_only=True`), Typer, FastAPI + Jinja2 + HTMX, MCP SDK v2.

## Global Constraints

- Python `>=3.10`; SQLAlchemy `>=2.0.30`; `openpyxl>=3.1.0` is already a hard dependency — no new packages.
- Ruff line-length 100, target `py310` (`ruff check .` must pass).
- Layering: `scraping/` = HTTP only, no parsing; `parsers/` = pure functions, no network; `services/` reach storage only through `repository/` Protocols; `cli.py` / `web/` never instantiate SQL repos directly (except the `get_tdoc`-style read path — not used here; testcase web/MCP go through `services.testcase`); `models/` are `@dataclass(slots=True)`, never leak ORM attrs.
- `HISTORY_URL` is a module constant in `scraping/testcase_source.py`, not a setting.
- No new `[sync]` interval, no `ALLOWED_ENV_VARS` change.
- Fresh tables only: `Base.metadata.create_all` pickup, no alembic, no data-migration helpers.
- `table` output is tab-separated via `_emit_table`; `json`/`markdown` via shared emitters; `--compact` semantics unchanged; CLI flag wins over `[output] compact`.
- List routes with HTMX filter forms must render `partials/<resource>_results.html` on `HX-Request: true` and the full page otherwise (`#results` outerHTML swap).
- MCP `_to_json` uses compact separators + `ensure_ascii=False`, byte-matching HTTP `?format=json` (Starlette `JSONResponse`).
- TDD: failing test → run → minimal impl → run → commit per task. Offline by default (`pytest -m "not online"`); online test opted in with `-m online`.
- `./scripts/test_sqlite.sh` is the full offline gate.

---

## File map

| File | Responsibility |
|---|---|
| Create `src/doc3gpp/models/testcase.py` | `TestCase`, `TestCaseStatus`, `TestCaseWithStatuses`, `TestCaseDetail`, `TestCaseSource` + `TestcaseSourceNotFoundError`, `TestcaseWorkbookNotFoundError` (errors live here so scraping/parsers/services share them without layering violations) |
| Modify `src/doc3gpp/storage/db/models.py` | `TestCaseORM` (`testcases`), `TestCaseStatusORM` (`testcase_status`), `TestCaseSourceORM` (`testcase_sources`) |
| Modify `src/doc3gpp/repository/protocols.py` | `TestCaseRepository` Protocol |
| Create `src/doc3gpp/storage/repositories/testcase_sql.py` | `SQLAlchemyTestCaseRepository` |
| Create `src/doc3gpp/scraping/testcase_source.py` | `HISTORY_URL`, `list_history_files`, `select_latest`, `fetch_testcase_zip` |
| Create `src/doc3gpp/parsers/testcase_parser.py` | `extract_workbook`, `parse_testcase_workbook`, header/column constants |
| Create `src/doc3gpp/services/testcase_service.py` | `TestCaseService.sync/list_recent/get`, `TestCaseProgressFn` |
| Modify `src/doc3gpp/services/factory.py` | `build_testcase_service()` |
| Modify `src/doc3gpp/settings/schema.py` | `OutputFieldsSettings.testcase` default |
| Modify `src/doc3gpp/cli.py` | `testcase_app` + `sync`/`list`/`show` |
| Modify `src/doc3gpp/models/jobs.py` | `JobKind.SYNC_TESTCASES` |
| Modify `src/doc3gpp/web/state.py`, `web/app.py`, `web/deps.py` | `ServiceContainer.testcase`, wiring, `get_testcase_service` |
| Modify `src/doc3gpp/web/render.py` | `testcase_rows`, `testcase_status_rows` |
| Create `src/doc3gpp/web/routes/testcases.py`; modify `web/routes/__init__.py`, `web/routes/jobs.py`, `web/errors.py`, `web/workers/handlers.py` | REST list/detail, jobs enqueue, `TestcaseNotFoundError`, `_sync_testcases` |
| Create templates `web/templates/testcase_list.html`, `testcase_show.html`, `partials/testcase_filters.html`, `partials/testcase_results.html`; modify `base.html`, `sync.html`, `web/static/js/sync_hub.js` | UI + sync-hub tenth form |
| Modify `src/doc3gpp/web/mcp_server.py` | `list_testcases`, `get_testcase`, `sync_testcases` |
| Tests (below) + docs (`AGENTS.md`, `docs/cli.md`, `docs/code-map.md`, `docs/architecture.md`, `docs/3gpp-knowledge.md`, `docs/web-server.md`, `README.md`) | Parity + reference |

Path rank (single source of truth, defined in `parsers/testcase_parser.py` as `PATH_RANK`, reused by repo + CLI):
`["FR1", "FR2", "FR1+FR2", "FDD", "TDD", "IPCAN-4G", "EUTRA", "IPCAN-5G", "NR5GC", "default"]`.

---

### Task 1: Domain models + errors

**Files:**
- Create: `src/doc3gpp/models/testcase.py`
- Test: `tests/unit/test_testcase_model.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `TestCase`, `TestCaseStatus`, `TestCaseWithStatuses`, `TestCaseDetail`, `TestCaseSource`, `TestcaseSourceNotFoundError`, `TestcaseWorkbookNotFoundError` — imported by Tasks 3–11.

- [ ] **Step 1: Write the failing test**

```python
from doc3gpp.models.testcase import (
    TestCase, TestCaseDetail, TestCaseSource, TestCaseStatus,
    TestCaseWithStatuses, TestcaseSourceNotFoundError,
    TestcaseWorkbookNotFoundError,
)

def test_dataclass_shapes():
    tc = TestCase(testcase_id="TC_1", title="T", group="5G", spec="38.523-1")
    assert tc.title == "T"
    st = TestCaseStatus(testcase_id="TC_1", path="FR1", gcf_ptcrb="Approved", ttcn_status="Approved")
    assert st.path == "FR1"
    assert TestCaseWithStatuses(testcase=tc, statuses={"FR1": "Approved"}).statuses == {"FR1": "Approved"}
    assert isinstance(TestCaseDetail(testcase=tc, statuses=[st]).statuses, list)
    src = TestCaseSource(filename="f.zip", year=2024, week=32, revision=0)
    assert src.parsed_at is None
    assert issubclass(TestcaseSourceNotFoundError, LookupError)
    assert issubclass(TestcaseWorkbookNotFoundError, ValueError)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_testcase_model.py -v`
Expected: FAIL with "No module named 'doc3gpp.models.testcase'".

- [ ] **Step 3: Write minimal implementation**

```python
"""Domain models for RAN5 conformance testcases."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


class TestcaseSourceNotFoundError(LookupError):
    """Raised when no status-file name matches the History grammar."""


class TestcaseWorkbookNotFoundError(ValueError):
    """Raised when the status zip holds no *.xlsx workbook member."""


@dataclass(slots=True)
class TestCase:
    testcase_id: str
    title: str | None = None
    ats: str | None = None
    feature: str | None = None
    release: str | None = None
    wis: str | None = None
    spec: str | None = None
    group: str | None = None


@dataclass(slots=True)
class TestCaseStatus:
    testcase_id: str
    path: str
    gcf_ptcrb: str | None = None
    ttcn_status: str | None = None


@dataclass(slots=True)
class TestCaseWithStatuses:
    testcase: TestCase
    statuses: dict[str, str | None]


@dataclass(slots=True)
class TestCaseDetail:
    testcase: TestCase
    statuses: list[TestCaseStatus]


@dataclass(slots=True)
class TestCaseSource:
    filename: str
    year: int
    week: int
    revision: int
    downloaded_at: datetime | None = None
    parsed_at: datetime | None = None
    testcase_count: int = 0
    status_count: int = 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_testcase_model.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/models/testcase.py tests/unit/test_testcase_model.py
git commit -m "feat(testcase): add domain models and typed errors"
```

---

### Task 2: ORM tables

**Files:**
- Modify: `src/doc3gpp/storage/db/models.py` (append after `SpecVersionORM`, before end of file)
- Test: `tests/integration/test_testcase_sql.py` (created in Task 3; this task's gate is `create_schema` + table presence)

**Interfaces:**
- Consumes: Task 1 (nothing directly; ORM mirrors the dataclasses).
- Produces: `TestCaseORM`, `TestCaseStatusORM`, `TestCaseSourceORM` → `Base.metadata` so `create_schema()` picks them up.

- [ ] **Step 1: Write the failing test** (lands in the Task 3 test file; write it now)

```python
def test_tables_exist(session_factory) -> None:
    from sqlalchemy import inspect
    from doc3gpp.storage.db.session import get_engine
    tables = set(inspect(get_engine()).get_table_names())
    assert {"testcases", "testcase_status", "testcase_sources"} <= tables
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/integration/test_testcase_sql.py::test_tables_exist -v`
Expected: FAIL (tables missing; file itself missing on first run — create the file with just the fixture + this test).

- [ ] **Step 3: Write minimal implementation** (append to `storage/db/models.py`; `Integer`, `DateTime`, `ForeignKey`, `String`, `Text` already imported)

```python
class TestCaseORM(Base):
    """Persisted RAN5 testcase header row (one per TC)."""

    __tablename__ = "testcases"

    testcase_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    ats: Mapped[str | None] = mapped_column(String(128), nullable=True)
    feature: Mapped[str | None] = mapped_column(String(256), nullable=True)
    release: Mapped[str | None] = mapped_column(String(32), nullable=True)
    wis: Mapped[str | None] = mapped_column(String(512), nullable=True)
    spec: Mapped[str | None] = mapped_column(String(32), nullable=True)
    group: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)


class TestCaseStatusORM(Base):
    """One `(testcase_id, path)` status pair; single-path groups use `'default'`."""

    __tablename__ = "testcase_status"

    testcase_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("testcases.testcase_id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
        index=True,
    )
    path: Mapped[str] = mapped_column(String(16), primary_key=True, nullable=False)
    gcf_ptcrb: Mapped[str | None] = mapped_column(Text, nullable=True)
    ttcn_status: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)


class TestCaseSourceORM(Base):
    """Sync ledger: one row per status file (filename identity is the skip key)."""

    __tablename__ = "testcase_sources"

    filename: Mapped[str] = mapped_column(String(256), primary_key=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    week: Mapped[int] = mapped_column(Integer, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    parsed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    testcase_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
```

No `migrate.py` change: `create_schema()` already calls `Base.metadata.create_all(bind=engine)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/integration/test_testcase_sql.py::test_tables_exist -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/storage/db/models.py tests/integration/test_testcase_sql.py
git commit -m "feat(testcase): add testcases/testcase_status/testcase_sources ORM tables"
```

---

### Task 3: Repository Protocol + SQL implementation

**Files:**
- Modify: `src/doc3gpp/repository/protocols.py` (append `TestCaseRepository` after `SpecRepository`)
- Create: `src/doc3gpp/storage/repositories/testcase_sql.py`
- Test: `tests/integration/test_testcase_sql.py`

**Interfaces:**
- Consumes: Task 1 dataclasses; `apply_text_filter` from `storage/repositories/rich_filters.py`; `is_null_token`, `is_not_null_token`, `split_not_like_prefix` from `doc3gpp.cli_filters`; `PATH_RANK` from Task 5 (import lazily inside method or duplicate the literal — to avoid a parsers→storage import, define the rank literal in this module as `_PATH_RANK` with identical order; plan locks both to the same list).
- Produces: `TestCaseRepository` Protocol + `SQLAlchemyTestCaseRepository` with methods `upsert_many`, `replace_statuses`, `list`, `get`, `list_statuses`, `get_source`, `record_download`, `record_parsed` (exact signatures below).

Protocol (paste verbatim into `protocols.py`, adding `from doc3gpp.models.testcase import TestCase, TestCaseSource, TestCaseStatus` to its imports):

```python
class TestCaseRepository(Protocol):
    def upsert_many(self, cases: list[TestCase]) -> int: ...
    def replace_statuses(self, testcase_id: str, rows: list[TestCaseStatus]) -> None: ...
    def list(
        self,
        limit: int = 50,
        offset: int = 0,
        testcase_id: str | None = None,
        title: str | None = None,
        ats: str | None = None,
        feature: str | None = None,
        release: str | None = None,
        wis: str | None = None,
        spec: str | None = None,
        group: str | None = None,
        status: str | None = None,
        gcf_status: str | None = None,
    ) -> list[TestCase]: ...
    def get(self, testcase_id: str) -> TestCase | None: ...
    def list_statuses(self, testcase_id: str) -> list[TestCaseStatus]: ...
    def get_source(self, filename: str) -> TestCaseSource | None: ...
    def record_download(self, source: TestCaseSource) -> None: ...
    def record_parsed(self, filename: str, parsed_at: datetime, testcase_count: int, status_count: int) -> None: ...
```

- [ ] **Step 1: Write the failing tests** (append to `tests/integration/test_testcase_sql.py`; fixture mirrors `test_spec_sql.py`: in-memory SQLite, `Base.metadata.create_all`, `sessionmaker(autoflush=False)`)

```python
def test_upsert_get_list_status_round_trip(session_factory) -> None:
    from doc3gpp.models.testcase import TestCase, TestCaseStatus
    from doc3gpp.storage.repositories.testcase_sql import SQLAlchemyTestCaseRepository
    repo = SQLAlchemyTestCaseRepository(session_factory)
    assert repo.upsert_many([TestCase(testcase_id="TC_A", title="T", group="5G", spec="38.523-1")]) == 1
    repo.replace_statuses("TC_A", [TestCaseStatus(testcase_id="TC_A", path="FR1", gcf_ptcrb="Approved", ttcn_status="Approved")])
    got = repo.get("TC_A")
    assert got is not None and got.group == "5G"
    assert [(s.path, s.ttcn_status) for s in repo.list_statuses("TC_A")] == [("FR1", "Approved")]
    assert [c.testcase_id for c in repo.list(status="Approved")] == ["TC_A"]
    assert repo.list(status="null") == []

def test_replace_statuses_cleans_stale_paths(session_factory) -> None:
    from doc3gpp.models.testcase import TestCase, TestCaseStatus
    from doc3gpp.storage.repositories.testcase_sql import SQLAlchemyTestCaseRepository
    repo = SQLAlchemyTestCaseRepository(session_factory)
    repo.upsert_many([TestCase(testcase_id="TC_B", group="5G")])
    repo.replace_statuses("TC_B", [
        TestCaseStatus(testcase_id="TC_B", path="FR1", ttcn_status="Approved"),
        TestCaseStatus(testcase_id="TC_B", path="FR2", ttcn_status="Not approved"),
    ])
    repo.replace_statuses("TC_B", [TestCaseStatus(testcase_id="TC_B", path="FR1", ttcn_status="Approved")])
    assert [s.path for s in repo.list_statuses("TC_B")] == ["FR1"]

def test_sources_ledger(session_factory) -> None:
    from datetime import datetime, timezone
    from doc3gpp.models.testcase import TestCaseSource
    from doc3gpp.storage.repositories.testcase_sql import SQLAlchemyTestCaseRepository
    repo = SQLAlchemyTestCaseRepository(session_factory)
    now = datetime(2026, 9, 11, tzinfo=timezone.utc)
    repo.record_download(TestCaseSource(filename="TTCN CR Agreement Status 2024-wk32.zip", year=2024, week=32, revision=0, downloaded_at=now))
    assert repo.get_source("TTCN CR Agreement Status 2024-wk32.zip").parsed_at is None
    repo.record_parsed("TTCN CR Agreement Status 2024-wk32.zip", now, 2, 5)
    src = repo.get_source("TTCN CR Agreement Status 2024-wk32.zip")
    assert (src.parsed_at, src.testcase_count, src.status_count) == (now, 2, 5)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/integration/test_testcase_sql.py -v`
Expected: FAIL with "No module named 'doc3gpp.storage.repositories.testcase_sql'".

- [ ] **Step 3: Write minimal implementation** (`src/doc3gpp/storage/repositories/testcase_sql.py`)

Key semantics (all required):
- `upsert_many`: `session.get(TestCaseORM, id)`; update in place; `0` for empty input; returns `len(cases)`.
- `replace_statuses`: one transaction: `delete(TestCaseStatusORM).where(testcase_id == ...)` then add all rows (empty list = delete only).
- `list`: `select(TestCaseORM)`; text cols via `apply_text_filter`; `group` exact upper-cased (`TestCaseORM.group == group.upper()`); `status`/`gcf_status` via `_apply_status_exists(stmt, column, value)` building `EXISTS (SELECT 1 FROM testcase_status s WHERE s.testcase_id == testcases.testcase_id AND <rich predicate>)` where the rich predicate is: `null` → `col.is_(None)`; `not-null` → `col.is_not(None)`; `!p` → `col.notlike(p)`; else `col.like(p)`, using `is_null_token/is_not_null_token/split_not_like_prefix` from `doc3gpp.cli_filters`. Order `testcase_id ASC`, `offset/limit`.
- `list_statuses`: `select(...).where(testcase_id == ...)` then Python-sort by `_PATH_RANK` index (unknown paths last, stable).
- `get_source`/`record_download` (upsert by filename, preserving existing `parsed_at` unless the incoming has one)/`record_parsed` (set `parsed_at` + counts). `_as_utc` naive→UTC normalisation like `spec_sql.py`.
- `_orm_to_case`, `_orm_to_status`, `_orm_to_source` converters.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/integration/test_testcase_sql.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/repository/protocols.py src/doc3gpp/storage/repositories/testcase_sql.py tests/integration/test_testcase_sql.py
git commit -m "feat(testcase): add TestCaseRepository protocol and SQL implementation"
```

---

### Task 4: Scraping transport

**Files:**
- Create: `src/doc3gpp/scraping/testcase_source.py`
- Test: `tests/unit/test_testcase_source.py`

**Interfaces:**
- Consumes: `ScraperClient` (`get_text`/`get_bytes`); raises `TestcaseSourceNotFoundError` from Task 1.
- Produces: `HISTORY_URL`, `list_history_files(client=None) -> list[str]`, `select_latest(filenames) -> str`, `parse_status_filename(name) -> tuple[int,int,int]`, `fetch_testcase_zip(filename, client=None) -> bytes`.

- [ ] **Step 1: Write the failing test**

```python
import re
from doc3gpp.scraping.testcase_source import (
    HISTORY_URL, parse_status_filename, select_latest,
)

def test_grammar_and_ordering():
    assert HISTORY_URL.endswith("/History/")
    assert parse_status_filename("TTCN CR Agreement Status 2019-wk15_rev1.zip") == (2019, 15, 1)
    assert parse_status_filename("TTCN CR Agreement Status 2024-wk32.zip") == (2024, 32, 0)
    assert parse_status_filename("random.zip") is None
    names = ["TTCN CR Agreement Status 2024-wk30.zip",
             "TTCN CR Agreement Status 2024-wk32.zip",
             "TTCN CR Agreement Status 2019-wk15_rev1.zip"]
    assert select_latest(names) == "TTCN CR Agreement Status 2024-wk32.zip"

def test_list_history_files_parses_anchors():
    from doc3gpp.scraping.testcase_source import list_history_files
    class Stub:
        def get_text(self, url): return (
            '<html><a href="TTCN%20CR%20Agreement%20Status%202024-wk32.zip">x</a>'
            '<a href="?C=N;O=D">sort</a><a href="other.zip">y</a></html>')
    assert list_history_files(Stub()) == ["TTCN CR Agreement Status 2024-wk32.zip"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_testcase_source.py -v`
Expected: FAIL with "No module named 'doc3gpp.scraping.testcase_source'".

- [ ] **Step 3: Write minimal implementation**

```python
"""Transport for the RAN5 TTCN status History folder (network only)."""

from __future__ import annotations

import logging
import re
from urllib.parse import quote, unquote, urljoin

from bs4 import BeautifulSoup

from doc3gpp.models.testcase import TestcaseSourceNotFoundError
from doc3gpp.scraping.client import ScraperClient

logger = logging.getLogger(__name__)

HISTORY_URL = "https://www.3gpp.org/ftp/tsg_ran/WG5_Test_ex-T1/TTCN/Reporting/TTCN_status/History/"

_FILENAME_RE = re.compile(
    r"TTCN CR Agreement Status (\d{4})-wk(\d{2})(?:[_-]?(?:r|rev)(\d+))?\.zip$",
    re.IGNORECASE,
)

def parse_status_filename(name: str) -> tuple[int, int, int] | None:
    m = _FILENAME_RE.search(name.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)) if m.group(3) else 0)

def list_history_files(client: ScraperClient | None = None) -> list[str]:
    own = client is None
    if own:
        client = ScraperClient()
    try:
        html = client.get_text(HISTORY_URL)
    finally:
        if own:
            client.close()
    out: list[str] = []
    for a in BeautifulSoup(html, "lxml").find_all("a", href=True):
        raw = unquote(str(a["href"]).split("?")[0].split("#")[0].split("/")[-1])
        if not raw.lower().endswith(".zip"):
            continue
        if parse_status_filename(raw) is None:
            logger.debug("Ignoring non-status zip %r", raw)
            continue
        out.append(raw)
    return out

def select_latest(filenames: list[str]) -> str:
    keyed = [(parse_status_filename(n), n) for n in filenames]
    keyed = [(k, n) for k, n in keyed if k is not None]
    if not keyed:
        raise TestcaseSourceNotFoundError("no TTCN CR Agreement Status zip found in History/")
    keyed.sort(key=lambda kv: kv[0])
    return keyed[-1][1]

def fetch_testcase_zip(filename: str, client: ScraperClient | None = None) -> bytes:
    url = urljoin(HISTORY_URL, quote(filename))
    logger.debug("Fetching testcase status zip at %s", url)
    if client is not None:
        return client.get_bytes(url)
    with ScraperClient() as c:
        return c.get_bytes(url)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_testcase_source.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/scraping/testcase_source.py tests/unit/test_testcase_source.py
git commit -m "feat(testcase): add History listing and zip transport"
```

---

### Task 5: Workbook parser (pure)

**Files:**
- Create: `src/doc3gpp/parsers/testcase_parser.py`
- Test: `tests/unit/test_testcase_parser.py`

**Interfaces:**
- Consumes: `TestCase`, `TestCaseStatus`, `TestcaseWorkbookNotFoundError` (Task 1); `openpyxl`, `zipfile` (stdlib).
- Produces: `SHEET_TO_GROUP`, `PATH_RANK`, `STATUS_PAIRS`, `extract_workbook(zip_bytes) -> bytes`, `parse_testcase_workbook(xlsx_bytes) -> tuple[list[TestCase], list[TestCaseStatus], list[str]]`.

Constants (paste verbatim):

```python
SHEET_TO_GROUP = {"5G_TC_Status": "5G", "LTE_TC_Status": "LTE", "IMS_TC_Status": "IMS",
                  "UTRA_TC_Status": "UTRA", "Positioning_TC_Status": "POS", "MCX_TC_Status": "MCX"}
PATH_RANK = ["FR1", "FR2", "FR1+FR2", "FDD", "TDD", "IPCAN-4G", "EUTRA", "IPCAN-5G", "NR5GC", "default"]
STATUS_PAIRS: dict[str, list[tuple[int, int, str]]] = {
    "5G": [(2, 3, "FR1"), (2, 15, "FR2"), (2, 22, "FR1+FR2")],
    "LTE": [(2, 3, "FDD"), (15, 16, "TDD")],
    "IMS": [(2, 3, "default")], "UTRA": [(2, 3, "default")], "POS": [(2, 3, "default")],
    "MCX": [(2, 3, "IPCAN-4G"), (17, 18, "EUTRA"), (26, 27, "IPCAN-5G"), (37, 38, "NR5GC")],
}
FIXED_SPEC = {"5G": "38.523-1", "LTE": "36.523-1"}
```

Helpers: `_norm_header(cell)` → `re.sub(r"\s+", " ", str(cell).strip().casefold())`; `_is_gcf_header(h)` → `"gcfptcrb" in re.sub(r"[^a-z0-9]", "", h)`; `_is_ttcn_header(h)` → `"ttcnstatus" in re.sub(r"[^a-z0-9]", "", h)`; `_cell(v)` → `None` for `None`/whitespace-only else `str(v).strip()`; `_strip_mcx_prefix(v)` → strip leading `MCX-` case-insensitively (path vocab comparison uses the stripped value; stored path is the stripped vocab entry from `STATUS_PAIRS`, not the raw header).

Row logic per sheet: header row = row 1; locate common cols by `_norm_header` in `{tc,title,ats,feature,release,ran wic,part of}`; skip data rows with empty TC; validate each `(gcf_idx, ttcn_idx, path)` pair's row-1 headers with `_is_gcf_header/_is_ttcn_header`, skipping invalid pairs with `logger.warning` naming sheet + Excel letter + actual text; emit one `TestCaseStatus` per valid pair where at least one of the two cells is non-`None` (both-`None` pairs are dropped; a TC with zero emitted pairs still yields its `TestCase` header with an empty statuses dict downstream); `spec` = `FIXED_SPEC[group]` or `part of` verbatim. `extract_workbook`: `*.xlsx` case-insensitive members; zero → raise; >1 → lexically-first + warning. Missing sheet → warning + note in third return element, never raises.

- [ ] **Step 1: Write the failing test** (build the workbook with openpyxl in-test; cover 5G shared-GCF, IMS default path, LT
E TDD pair, MCX prefix strip, relabelled-TTCN skip, empty-TC skip, fixed-spec rules)

```python
def _wb_bytes():
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active; ws.title = "5G_TC_Status"
    ws.append(["TC", "Title", "GCF/PTCRB", "TTCN-Status", "x", "x", "x", "x", "x", "x", "x", "x", "x", "x", "x", "GCF/PTCRB?", "TTCN Status", "ATS", "Feature", "Release", "RAN WIC", "part of", "TTCN-Status"])
    ws.append(["TC_1", "Title1", "Approved", "Approved", *[None]*11, "x", "Not approved", "ATS1", "F1", "Rel-17", "W1", ""])
    ws2 = wb.create_sheet("IMS_TC_Status")
    ws2.append(["TC", "Title", "GCF/PTCRB", "TTCN Status", "ATS", "Feature", "Release", "RAN WIC", "part of"])
    ws2.append(["TC_9", "T9", "g", "t", "a", "f", "Rel-18", "w", "34.229-1"])
    ws2.append(["", "skip me", "g", "t", "a", "f", "r", "w", "p"])
    import io
    buf = io.BytesIO(); wb.save(buf); return buf.getvalue()

def test_parse_workbook():
    from doc3gpp.parsers.testcase_parser import parse_testcase_workbook
    cases, statuses, notes = parse_testcase_workbook(_wb_bytes())
    by_id = {c.testcase_id: c for c in cases}
    assert by_id["TC_1"].spec == "38.523-1"
    assert by_id["TC_9"].spec == "34.229-1"
    assert "TC_9" in by_id and "" not in by_id
    fr1 = [s for s in statuses if s.testcase_id == "TC_1" and s.path == "FR1"]
    assert fr1 and fr1[0].ttcn_status == "Approved"
    assert any(s.path == "default" for s in statuses if s.testcase_id == "TC_9")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_testcase_parser.py -v`
Expected: FAIL with "No module named 'doc3gpp.parsers.testcase_parser'".

- [ ] **Step 3: Write minimal implementation** (per the row logic above; `load_workbook(..., read_only=True, data_only=True)`, `iter_rows(values_only=True)`; first row = header; data rows from row 2).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_testcase_parser.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/parsers/testcase_parser.py tests/unit/test_testcase_parser.py
git commit -m "feat(testcase): add pure workbook parser with header validation"
```

---

### Task 6: Service + factory

**Files:**
- Create: `src/doc3gpp/services/testcase_service.py`
- Modify: `src/doc3gpp/services/factory.py` (add `build_testcase_service`)
- Test: `tests/unit/test_testcase_service.py`

**Interfaces:**
- Consumes: Tasks 1/3/4/5; `SyncOutcome` from `models/sync.py`.
- Produces: `TestCaseProgressFn = Callable[[str, dict], None]`; `TestCaseService(repository)` with `sync(*, force=False, on_progress=None) -> SyncOutcome`, `list_recent(limit=50, offset=0, testcase_id=None, title=None, ats=None, feature=None, release=None, wis=None, spec=None, group=None, status=None, gcf_status=None) -> list[TestCaseWithStatuses]`, `get(testcase_id) -> TestCaseDetail | None`; `factory.build_testcase_service()`.

Locked reason strings: synced → `f"Testcase sync complete: {tc} testcases, {st} statuses from {filename}"` (`synced_count=tc`); skipped → `f"Testcase sync skipped: {filename} already parsed; use --force to override."` (`synced_count=None`).

`sync` flow: `list_history_files` → `select_latest` → `repository.get_source(filename)`; if row with `parsed_at is not None` and not `force` → skipped (no download). Else `fetch_testcase_zip` → `record_download(TestCaseSource(..., downloaded_at=now))` BEFORE parsing → `extract_workbook` → `parse_testcase_workbook` → `upsert_many` → per-TC `replace_statuses` (group parsed statuses by `testcase_id`) → `record_parsed(filename, now, tc, st)`. Events: `on_progress("listing", {})` after History fetch, `on_progress("downloaded", {"filename": ...})` after bytes, `on_progress("parsed", {"testcases": tc, "statuses": st})` after upserts. `list_recent`: `repository.list(...)` then `{path: ttcn_status}` dict per header via `repository.list_statuses`. `group` upper-cased in service (CLI also validates). No network on read paths.

- [ ] **Step 1: Write the failing test**

```python
def test_skip_matrix():
    from datetime import datetime, timezone
    from doc3gpp.models.testcase import TestCaseSource
    from doc3gpp.services.testcase_service import TestCaseService
    class Repo:
        def __init__(self): self.sources = {}; self.cases = []
        def get_source(self, fn): return self.sources.get(fn)
        def record_download(self, src): self.sources[src.filename] = src
        def record_parsed(self, fn, at, tc, st):
            s = self.sources[fn]; s.parsed_at = at; s.testcase_count = tc; s.status_count = st
        def upsert_many(self, cases): self.cases = cases; return len(cases)
        def replace_statuses(self, tid, rows): pass
    svc = TestCaseService(Repo())
    import doc3gpp.services.testcase_service as m
    m.list_history_files = lambda client=None: ["TTCN CR Agreement Status 2024-wk32.zip"]
    m.fetch_testcase_zip = lambda fn, client=None: b"ZIP"
    m.extract_workbook = lambda b: b"XLSX"
    m.parse_testcase_workbook = lambda b: ([], [], [])
    out1 = svc.sync()
    assert out1.status == "synced"
    out2 = svc.sync()
    assert out2.status == "skipped"
    out3 = svc.sync(force=True)
    assert out3.status == "synced"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_testcase_service.py -v`
Expected: FAIL with "No module named 'doc3gpp.services.testcase_service'".

- [ ] **Step 3: Write minimal implementation** (service per flow above; factory adds `from doc3gpp.services.testcase_service import TestCaseService`, `from doc3gpp.storage.repositories.testcase_sql import SQLAlchemyTestCaseRepository`, and `def build_testcase_service() -> TestCaseService: return TestCaseService(SQLAlchemyTestCaseRepository())`).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_testcase_service.py tests/unit/test_services_factory.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/services/testcase_service.py src/doc3gpp/services/factory.py tests/unit/test_testcase_service.py
git commit -m "feat(testcase): add TestCaseService sync/list/get and factory"
```

---

### Task 7: Settings + CLI

**Files:**
- Modify: `src/doc3gpp/settings/schema.py` (`OutputFieldsSettings.testcase`)
- Modify: `src/doc3gpp/cli.py` (`testcase_app`, `testcase sync|list|show`)
- Test: `tests/integration/test_testcase_cli.py`

**Interfaces:**
- Consumes: Task 6 service via `build_testcase_service`; `_parse_field_selection`, `_emit_records`, `_dump_show_json`, `_serialise_show_value`, `_resolve_format`, `_resolve_compact`, `create_schema` (existing CLI helpers).
- Produces: `doc3gpp testcase sync [--force]`; `testcase list` (flags below); `testcase show --testcase ID`.

Constants: `VALID_TESTCASE_GROUPS = ("5G", "LTE", "IMS", "UTRA", "POS", "MCX")`; `TESTCASE_LIST_FIELDS = ["testcase_id", "title", "ats", "feature", "release", "wis", "spec", "group", "statuses"]`; settings default `testcase: ["testcase_id", "title", "spec", "group", "release", "statuses"]`.

List JSON shape (locked): `json` format builds `payload: list[dict]` where `statuses` is a **dict** (`path → ttcn_status`), dumped with the same indent/compact rules as `_emit_json` — do NOT push the dict through `_emit_records` (its cells are strings). `table`/`markdown` use `_emit_records` with `statuses` stringified as `k=v;…` sorted by `PATH_RANK` (`-` for `None` values). `--fields` selection covers all 9 fields via `_parse_field_selection`. `--group` validated against `VALID_TESTCASE_GROUPS` → `typer.BadParameter` listing valid groups. `show` JSON: `{"testcase": {8 header fields}, "statuses": [{"path","gcf_ptcrb","ttcn_status"}, ...]}` via `_dump_show_json`; table: two `_emit_records` blocks (header, statuses) separated by `typer.echo("")`, mirroring `spec show`. Missing id → `typer.BadParameter(f"Testcase {id!r} not found")`. `sync` uses tqdm `listing → downloaded → parsed` events mirroring `spec sync` and echoes `outcome.reason`.

- [ ] **Step 1: Write the failing test**

```python
from unittest.mock import MagicMock
from typer.testing import CliRunner
from doc3gpp.cli import app
from doc3gpp.models.testcase import TestCase, TestCaseWithStatuses

runner = CliRunner()

def test_testcase_list_json_shape(monkeypatch):
    svc = MagicMock()
    svc.list_recent.return_value = [TestCaseWithStatuses(
        testcase=TestCase(testcase_id="TC_1", title="T", spec="38.523-1", group="5G", release="Rel-17"),
        statuses={"FR1": "Approved"})]
    monkeypatch.setattr("doc3gpp.cli.build_testcase_service", lambda: svc)
    result = runner.invoke(app, ["testcase", "list", "--format", "json"])
    assert result.exit_code == 0, result.stdout
    import json
    payload = json.loads(result.stdout)
    assert payload[0]["statuses"] == {"FR1": "Approved"}

def test_testcase_group_validation(monkeypatch):
    from doc3gpp.cli import build_testcase_service
    result = runner.invoke(app, ["testcase", "list", "--group", "NOPE"])
    assert result.exit_code != 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/integration/test_testcase_cli.py -v`
Expected: FAIL ("No such command 'testcase'" / module attr missing).

- [ ] **Step 3: Write minimal implementation** (settings field + `testcase_app = typer.Typer(help="testcase commands")`, `app.add_typer(testcase_app, name="testcase")` next to `spec_app`; three commands per interface above).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/integration/test_testcase_cli.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/settings/schema.py src/doc3gpp/cli.py tests/integration/test_testcase_cli.py
git commit -m "feat(testcase): add testcase sync/list/show CLI"
```

---

### Task 8: Web wiring + REST + templates

**Files:**
- Modify: `src/doc3gpp/web/state.py` (`ServiceContainer.testcase: "TestCaseService"`), `web/app.py` (`testcase=factory.build_testcase_service()`), `web/deps.py` (`get_testcase_service` + `__all__`), `web/render.py` (`testcase_rows`, `testcase_status_rows`), `web/routes/__init__.py` (mount), `web/templates/base.html` (nav link)
- Create: `src/doc3gpp/web/routes/testcases.py`, `web/templates/testcase_list.html`, `web/templates/testcase_show.html`, `web/templates/partials/testcase_filters.html`, `web/templates/partials/testcase_results.html`
- Test: `tests/unit/test_web_routes.py` (add cases) + `tests/integration/test_web_end_to_end.py` (add cases)

**Interfaces:**
- Consumes: Task 6 service via `get_testcase_service`; `parse_text_query/parse_int_query/is_htmx_request`; `TestcaseNotFoundError` (Task 9 — implement the error class first if working strictly in order, or define it in this task's first commit; plan locks the name `TestcaseNotFoundError` in `web/errors.py`).
- Produces: `GET /testcases` (filters `testcase,title,ats,feature,release,wis,spec,group,status,gcf_status,limit,offset,format`; `_LIMIT_CAP=200`, default limit 50; unknown `group` → `InvalidFilterError`; `?format=json` → bare row array byte-identical to CLI JSON via `render.testcase_rows`); `GET /testcases/{testcase_id}` (HTML or `?format=json` → `{"testcase":{...},"statuses":[...]}`; unknown → 404).

`render.testcase_rows(rows, fields)`: like `spec_rows` BUT preserves `statuses` as a dict (no `_coerce_cell` on it; coerce every other field). `render.testcase_status_rows(statuses, fields)` coerces plainly. Route JSON uses `JSONResponse(content=testcase_rows(...))` so Starlette separators match MCP `_to_json`. Templates mirror `spec_list.html`/`spec_results.html`/`spec_filters.html` with testcase columns (`TC`, `Title`, `Spec`, `Group`, `Release`, `Statuses` as `k=v` chips) + `show` link; detail page mirrors `spec_show.html` (header card + status-pairs table with `path/gcf_ptcrb/ttcn_status`).

- [ ] **Step 1: Write the failing test**

```python
def test_testcases_json_parity(client, monkeypatch):
    from doc3gpp.models.testcase import TestCase, TestCaseWithStatuses
    import doc3gpp.web.routes.testcases as r
    rows = [TestCaseWithStatuses(testcase=TestCase(testcase_id="TC_1", title="T", spec="38.523-1", group="5G"), statuses={"FR1": "Approved"})]
    monkeypatch.setattr(r, "get_testcase_service", lambda: __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock(list_recent=lambda **k: rows))
    resp = client.get("/testcases?format=json")
    assert resp.status_code == 200
    assert resp.json()[0]["statuses"] == {"FR1": "Approved"}
```

(Adapt to the repo's existing `test_web_routes.py` client fixture style; the assertion — dict preserved — is the lock.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_web_routes.py -k testcase -v`
Expected: FAIL (route 404 / missing symbol).

- [ ] **Step 3: Write minimal implementation** (state/deps/render/route/templates/nav per interfaces above).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_web_routes.py tests/integration/test_web_end_to_end.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/web/ tests/unit/test_web_routes.py tests/integration/test_web_end_to_end.py
git commit -m "feat(testcase): add testcases REST routes and web UI"
```

---

### Task 9: Jobs (kind + handler + route + sync hub)

**Files:**
- Modify: `src/doc3gpp/models/jobs.py` (`SYNC_TESTCASES = "sync_testcases"`), `web/workers/handlers.py` (`_sync_testcases` + `KIND_TO_HANDLER`), `web/routes/jobs.py` (`_SyncTestcasesBody` + `POST /jobs/sync/testcases`), `web/errors.py` (`TestcaseNotFoundError` + 3 mapping tables), `web/templates/sync.html` (tenth form `id="testcase-form"`), `web/static/js/sync_hub.js` (`"testcase-form"` body builder)
- Test: `tests/unit/test_job_worker.py` (handler case), `tests/unit/test_sync_hub_page.py` (form present), `tests/integration/test_sync_hub_end_to_end.py` (enqueue 202)

**Interfaces:**
- Consumes: `services.testcase.sync(force, on_progress)`.
- Produces: `JobKind.SYNC_TESTCASES`, handler returning `{"status","reason","synced_count"}`, `POST /jobs/sync/testcases {"force": bool}` → 202 job envelope, sync-hub panel.

Handler (paste verbatim shape, mirroring `_sync_specs` minus TSG validation):

```python
async def _sync_testcases(job, services, settings, *, progress, cancel_event):
    force = bool(job.params.get("force", False))
    progress("syncing testcases")
    def on_progress(event, data):
        if event == "listing":
            progress("fetched History listing")
        elif event == "downloaded":
            progress(f"downloaded {data.get('filename', '')}")
        elif event == "parsed":
            progress(f"parsed {data.get('testcases', 0)} testcases")
    if cancel_event.is_set():
        raise asyncio.CancelledError()
    outcome = await asyncio.to_thread(services.testcase.sync, force=force, on_progress=on_progress)
    if cancel_event.is_set():
        raise asyncio.CancelledError()
    progress(outcome.reason, force=True)
    return {"status": outcome.status, "reason": outcome.reason, "synced_count": outcome.synced_count}
```

`_SyncTestcasesBody`: `force: bool = False`. Route validates nothing else. `errors.py`: `class TestcaseNotFoundError(LookupError)` + `_MCP_RESOURCE_BY_EXC[TestcaseNotFoundError] = ("testcase", MCP_CODE_NOT_FOUND)` + `_ERROR_SLUGS[TestcaseNotFoundError] = "testcase_not_found"` + `_STATUS_BY_EXC[TestcaseNotFoundError] = 404` + `__all__`. Sync-hub form (tenth, after the spec panels):

```html
<section class="card">
  <h2>Testcase sync</h2>
  <form id="testcase-form" class="sync-form" method="post" action="/jobs/sync/testcases">
    <label class="inline-check"><input type="checkbox" name="force"> Force sync (re-parse latest file)</label>
    <button type="submit" class="btn primary">Sync testcases</button>
    <span class="sync-queued" style="display:none">Sync job queued</span>
    <div id="testcase-form-job-target"></div>
  </form>
</section>
```

JS builder: `"testcase-form": function (form) { return JSON.stringify({force: readCheckbox(form, "force")}); },`.

- [ ] **Step 1: Write the failing test**

```python
def test_sync_testcases_enqueues():
    from doc3gpp.models.jobs import JobKind
    assert JobKind.SYNC_TESTCASES.value == "sync_testcases"
    from doc3gpp.web.workers.handlers import JobHandlers
    assert JobHandlers.KIND_TO_HANDLER[JobKind.SYNC_TESTCASES].__name__ == "_sync_testcases"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_job_worker.py -k testcase -v`
Expected: FAIL (`SYNC_TESTCASES` missing).

- [ ] **Step 3: Write minimal implementation** (enum + handler + registry + jobs route + errors tables + sync.html + sync_hub.js).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_job_worker.py tests/unit/test_sync_hub_page.py tests/integration/test_sync_hub_end_to_end.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/models/jobs.py src/doc3gpp/web/ tests/unit/test_job_worker.py tests/unit/test_sync_hub_page.py tests/integration/test_sync_hub_end_to_end.py
git commit -m "feat(testcase): add SYNC_TESTCASES job, route, and sync-hub panel"
```

---

### Task 10: MCP tools

**Files:**
- Modify: `src/doc3gpp/web/mcp_server.py` (`_TESTCASE_FIELDS`, `list_testcases`, `get_testcase`, `sync_testcases`)
- Test: `tests/integration/test_mcp_end_to_end.py` (parity cases)

**Interfaces:**
- Consumes: `services.testcase`, `render.testcase_rows/testcase_status_rows`, `_to_json`, `_enqueue`, `TestcaseNotFoundError`, `JobKind.SYNC_TESTCASES`.
- Produces: `list_testcases(testcase,title,ats,feature,release,wis,spec,group,status,gcf_status,limit=50,offset=0)`, `get_testcase(testcase_id)`, `sync_testcases(force=False)`.

`_TESTCASE_FIELDS = ["testcase_id", "title", "spec", "group", "release", "statuses"]`. `list_testcases` calls `services.testcase.list_recent(...)` and returns `_to_json(render.testcase_rows(rows, _TESTCASE_FIELDS))`. `get_testcase` calls `services.testcase.get(testcase_id)`; `None` → `raise TestcaseNotFoundError(testcase_id)`; else `_to_json({"testcase": {f: getattr(detail.testcase, f) for f in ["testcase_id","title","ats","feature","release","wis","spec","group"]}, "statuses": render.testcase_status_rows(detail.statuses, ["path","gcf_ptcrb","ttcn_status"])})`. `sync_testcases` returns `_enqueue(state, JobKind.SYNC_TESTCASES, {"force": force}, "queued sync_testcases")`.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_mcp_list_testcases_parity(mcp_state):
    from doc3gpp.models.testcase import TestCase, TestCaseWithStatuses
    mcp_state.services.testcase.list_recent = lambda **k: [TestCaseWithStatuses(
        testcase=TestCase(testcase_id="TC_1", title="T", spec="38.523-1", group="5G"),
        statuses={"FR1": "Approved"})]
    result = await call_tool(mcp_state, "list_testcases", {})
    assert '"FR1":"Approved"' in result
```

(Adapt `call_tool`/fixture names to the existing `test_mcp_end_to_end.py` helpers; the byte-parity assertion is the lock.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/integration/test_mcp_end_to_end.py -k testcase -v`
Expected: FAIL (unknown tool).

- [ ] **Step 3: Write minimal implementation** (three tools per interfaces above, each under `@server.tool` + `@_mcp_error_guard`).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/integration/test_mcp_end_to_end.py -k "testcase or mcp_end_to_end" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/web/mcp_server.py tests/integration/test_mcp_end_to_end.py
git commit -m "feat(testcase): add list/get/sync MCP tools"
```

---

### Task 11: Remaining tests + online test

**Files:**
- Modify: `tests/integration/test_testcase_cli.py` (sync help/skip/sync-echo cases), `tests/unit/test_web_routes.py` (404 case), `tests/integration/test_mcp_end_to_end.py` (get 404 + sync enqueue shape)
- Create: `tests/integration/test_online_testcase_sync.py` (marked `online`)
- Test: this task IS tests; gate is the full offline suite.

**Interfaces:**
- Consumes: Tasks 1–10.
- Produces: `testcase sync --help` shows `--force`; sync with stubbed service echoes reason; `GET /testcases/NOPE?format=json` → 404 `testcase_not_found`; MCP `get_testcase` unknown → `-32004`; MCP `sync_testcases` → `{job_id,status,message,links{self,events}}`; online test syncs live History and asserts ≥1 testcase + ≥1 source row, no exact counts.

- [ ] **Step 1: Write the failing tests**

```python
def test_testcase_show_missing(monkeypatch):
    svc = MagicMock(); svc.get.return_value = None
    monkeypatch.setattr("doc3gpp.cli.build_testcase_service", lambda: svc)
    result = runner.invoke(app, ["testcase", "show", "--testcase", "NOPE"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower()
```

```python
@pytest.mark.online
def test_online_testcase_sync(sqlite_env):
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.services.factory import build_testcase_service
    from doc3gpp.storage.repositories.testcase_sql import SQLAlchemyTestCaseRepository
    create_schema()
    outcome = build_testcase_service().sync()
    assert outcome.status in ("synced", "skipped")
    repo = SQLAlchemyTestCaseRepository()
    assert len(repo.list(limit=1)) >= 1
```

- [ ] **Step 2: Run tests to verify online one is collected-but-skipped offline, others fail/pass per state**

Run: `python -m pytest tests/integration/test_testcase_cli.py tests/integration/test_online_testcase_sync.py -v`
Expected: new CLI test FAILs until `show` missing-branch message matches (fix the message, not the test); online test SKIPPED under default addopts.

- [ ] **Step 3: Fix implementation gaps** (only message/envelope tweaks; no new features).

- [ ] **Step 4: Run the full offline gate**

Run: `./scripts/test_sqlite.sh`
Expected: PASS (or only pre-existing failures — verify with `git stash` comparison if anything fails).

- [ ] **Step 5: Commit**

```bash
git add tests/
git commit -m "test(testcase): cover show-missing, 404/enqueue parity, and online sync"
```

---

### Task 12: Documentation sync (same change set)

**Files:**
- Modify: `AGENTS.md`, `docs/cli.md`, `docs/code-map.md`, `docs/architecture.md`, `docs/3gpp-knowledge.md`, `docs/web-server.md`, `README.md`

**Interfaces:**
- Consumes: final CLI `--help` text + route/tool names from Tasks 7–10.
- Produces: reviewer-verifiable docs; no code.

Content (write verbatim, keep each edit small):
- `AGENTS.md`: `testcase` row in "Where to look" (`cli.py` `testcase_app` + `services/testcase_service.py` + `storage/repositories/testcase_sql.py`); workflow one-liners for `testcase sync/list/show` incl. `--status` EXISTS semantics + `statuses`-dict projection + file-identity skip rule.
- `docs/cli.md`: full `testcase` section — every flag, default (`limit 50`, `offset 0`, fields default `testcase_id,title,spec,group,release,statuses`), `--status` (any-path `ttcn_status` EXISTS) / `--gcf-status` semantics, `statuses` dict projection, `default`-path note, examples.
- `docs/code-map.md`: new symbols under `models/`, `scraping/`, `parsers/`, `services/`, `storage/`, `web/`.
- `docs/architecture.md`: three ORM tables in schema diagram + data-flow paragraph (History → zip → workbook → upserts).
- `docs/3gpp-knowledge.md`: History URL + filename grammar + regex + sheet/column map + fixed-spec rules + `IOCAN-5Gm` typo note.
- `docs/web-server.md`: `/testcases` routes + three MCP tools + sync-hub panel + `SYNC_TESTCASES`.
- `README.md`: one-line feature mention.

- [ ] **Step 1: Draft the doc diffs** (no test to write; verify with `git diff --stat`).
- [ ] **Step 2: Verify CLI reference matches reality** — run `doc3gpp testcase sync --help`, `testcase list --help`, `testcase show --help` and paste actual flags into `docs/cli.md`.

Run: `python -m doc3gpp testcase sync --help && python -m doc3gpp testcase list --help && python -m doc3gpp testcase show --help`
Expected: all three print; every documented flag appears.

- [ ] **Step 3: Write the docs edits.**
- [ ] **Step 4: Run lint + fast tests** — Run: `ruff check . && python -m pytest tests/unit/test_testcase_parser.py tests/unit/test_testcase_service.py tests/integration/test_testcase_sql.py -q` — Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add AGENTS.md docs/cli.md docs/code-map.md docs/architecture.md docs/3gpp-knowledge.md docs/web-server.md README.md
git commit -m "docs(testcase): document testcase sync/list/show surface"
```

---

## Self-review (run before handoff)

1. Spec coverage: §2 tables → Task 2; §2.3 skip → Tasks 3/6; §3.1 listing/grammar → Task 4; §3.2 workbook/sheets → Task 5; §3.3 columns/validation → Task 5; §3.4 upserts → Task 3/6; §4 service → Task 6; §5 repo → Task 3; §6 CLI → Task 7; §7 REST/jobs/MCP → Tasks 8–10; §8 settings/bootstrap → Tasks 2/7; §9 tests → Tasks 1/3–5/11 (+ Task 8/10 parity); §10 docs → Task 12; §11 non-goals respected (no backfill flag, no disk cache, no FTS5, no FK cross-links, no per-path scoping).
2. Placeholder scan: no TBD/TODO/"similar to"; every step has file paths, signatures, code, run commands, expected output.
3. Type consistency: `TestCase*` names match Tasks 1–10; `TestcaseSourceNotFoundError`/`TestcaseWorkbookNotFoundError` casing matches spec; `TestcaseNotFoundError` (web 404) is deliberately distinct from the source error; `JobKind.SYNC_TESTCASES = "sync_testcases"`; `statuses: dict[str, str | None]` on list, `list[TestCaseStatus]` on detail; `(year, week, revision)` ordering; `default` path literal.

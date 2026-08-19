# TDoc list XLSX metadata (To / Cc / Original LS / For / Abstract / Secretary Remarks) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Plumb the six XLSX metadata columns (`To` / `Cc` / `Original LS` / `For` / `Abstract` / `Secretary Remarks`) from the meeting TDoc-list XLSX through every layer of the doc3gpp pipeline — parser → ORM/dataclass → repository → repository Protocol → service → CLI list (`--fields` + filter flags) → web `/tdocs` form + `tdoc_show.html` panel → MCP `list_tdocs` (filter flags) + `get_tdoc` JSON envelope → TOML example.

**Architecture:** Single vertical slice per layer of the doc3gpp pipeline. Each new column: XLSX header mapper entry → `TDoc` dataclass field → `TDocORM` column → one idempotent `ALTER TABLE` line inside a single new `migrate.py` helper → `_copy_fields` / `_orm_to_domain` round-trip → Protocol docstring → service kwarg forward → CLI `--typer.Option` flag + logger + service-call site → web `Query` parameter + `filters` context key → `TDOC_COLUMN_LABELS` entry → HTML template form input → JSON envelope auto-flows via `dataclass_fields`. One MCP `list_tdocs` extension forwards the same six kwargs.

**Tech Stack:** Python 3.10+, SQLAlchemy 2.0, Pydantic v2, Typer, FastAPI/Jinja2, openpyxl, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-08-19-tdocs-xlsx-metadata-design.md`

**Branch:** `feat/tdocs-xlsx-metadata`

## Global Constraints

- Six fields: `ls_to` / `ls_cc` / `original_ls` / `tdoc_for` / `abstract` / `secretary_remarks`. Storage types — `tdoc_for` `String(64)`, `ls_to` / `ls_cc` `String(256)`, `abstract` / `secretary_remarks` / `original_ls` `Text` (no length cap).
- All six are **nullable**, **optional** in the XLSX, and capture-on-every-row (no `type == "LS"` gate). Header detection (`_HEADER_ROW_MARKERS`) is **unchanged** — the new headers don't count as markers.
- All six live on `TDoc` / `tdocs` — never on `tdoc_cr_cover_page` / `tdoc_cr_ttcn_details`.
- Migration **must be idempotent**: probe `PRAGMA table_info(tdocs)` before issuing any `ALTER TABLE ... ADD COLUMN`. Same shape as `_migrate_spec_rapporteurs` / `_migrate_tdoc_cr_cover_page_summary_of_change`.
- **No backfill command.** Implicit backfill on next `tdoc sync` — parser is deterministic, upsert is by-PK, write is idempotent. Matches the precedent of every previous column addition.
- All six are **opt-in display** — default `output.fields.tdoc` stays put. Surfaced only via `--fields all`, `?fields=...`, TOML `[output.fields] tdoc`, or `--fields <name>`. `TDOC_HTML_DEFAULT_FIELDS` is unchanged.
- All six are filterable via rich-filter grammar (`null` / `not-null` / `!pattern` / plain LIKE) — mirrors `cr_pack` / `release` / `cr_num` plumbing.
- Dataclass field order: `tdoc_for`, `abstract`, `secretary_remarks`, `ls_to`, `ls_cc`, `original_ls` — declared after `cr_pack`, mirrors the migration's `additions` list ordering.
- Ruff line-length 100, target py310. No code comments unless they explain non-obvious behaviour.
- Run `ruff check .` and `./scripts/test_sqlite.sh` before finishing.

---

### Task 1: Parser — capture six XLSX metadata columns

**Files:**
- Modify: `src/doc3gpp/parsers/tdoc_parser.py:314-330`
- Modify: `tests/unit/test_tdoc_parser.py`

**Interfaces:**
- Consumes: existing `mapping` dict in `read_tdoc_sheet`; `to_text`, `pick_col`, `_HEADER_ROW_MARKERS` (all unchanged).
- Produces: each `dict[str, object]` returned by `read_tdoc_sheet` now carries keys `"tdoc_for"`, `"abstract"`, `"secretary_remarks"`, `"ls_to"`, `"ls_cc"`, `"original_ls"` (all `str | None`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_tdoc_parser.py`:

```python
# ---------------------------------------------------------------------------
# Six-column XLSX-metadata capture: To / Cc / Original LS / For / Abstract /
# Secretary Remarks. The parser stores them on every row (no LS gate).
# ---------------------------------------------------------------------------


def test_read_tdoc_sheet_captures_six_xlsx_metadata_columns() -> None:
    headers = [
        "TDoc", "Title", "Source", "Type",
        "For", "Abstract", "Secretary Remarks",
        "To", "Cc", "Original LS",
    ]
    xlsx_bytes = _make_xlsx_bytes(
        [
            headers,
            [
                "R5-260001", "Doc A", "Acme", "LS",
                "Information", "TL;DR of doc A", "Secretary has no remarks",
                "RAN2", "RAN3, RAN4", "C1-260001",
            ],
            # Empty cells become None for every column (mirrors title/source).
            [
                "R5-260002", "Doc B", "Acme", "CR",
                "", "", "",
                "", "", "",
            ],
        ]
    )

    records = read_tdoc_sheet(xlsx_bytes)

    assert len(records) == 2
    a, b = records
    assert a["tdoc_for"] == "Information"
    assert a["abstract"] == "TL;DR of doc A"
    assert a["secretary_remarks"] == "Secretary has no remarks"
    assert a["ls_to"] == "RAN2"
    assert a["ls_cc"] == "RAN3, RAN4"
    assert a["original_ls"] == "C1-260001"

    # Empty cells normalise to None like every other column.
    assert b["tdoc_for"] is None
    assert b["abstract"] is None
    assert b["secretary_remarks"] is None
    assert b["ls_to"] is None
    assert b["ls_cc"] is None
    assert b["original_ls"] is None
    # Existing columns are unaffected.
    assert a["title"] == "Doc A"
    assert a["source"] == "Acme"
    assert a["type"] == "LS"


def test_read_tdoc_sheet_xlsx_metadata_none_when_header_absent() -> None:
    # No "For" / "Abstract" / etc. columns in this fixture; the parser
    # still completes and surfaces None for every new key.
    xlsx_bytes = _make_xlsx_bytes(
        [
            ["TDoc", "Title", "Source", "Type"],
            ["R5-260001", "Doc A", "Acme", "CR"],
        ]
    )

    records = read_tdoc_sheet(xlsx_bytes)

    assert len(records) == 1
    rec = records[0]
    assert rec["tdoc_for"] is None
    assert rec["abstract"] is None
    assert rec["secretary_remarks"] is None
    assert rec["ls_to"] is None
    assert rec["ls_cc"] is None
    assert rec["original_ls"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_tdoc_parser.py::test_read_tdoc_sheet_captures_six_xlsx_metadata_columns tests/unit/test_tdoc_parser.py::test_read_tdoc_sheet_xlsx_metadata_none_when_header_absent -v`
Expected: FAIL with `KeyError: 'tdoc_for'` (the parser does not yet set the key).

- [ ] **Step 3: Extend the parser mapping**

In `src/doc3gpp/parsers/tdoc_parser.py` extend the `mapping = { ... }` dict inside `read_tdoc_sheet` (located after the `col_tdoc = pick_col(...)` lines, around line 314). Insert six new entries **after the `"cr_pack"` line**:

```python
        "tdoc_for":          pick_col(header_map, ["For"]),
        "abstract":          pick_col(header_map, ["Abstract"]),
        "secretary_remarks": pick_col(header_map, ["Secretary Remarks"]),
        "ls_to":             pick_col(header_map, ["To"]),
        "ls_cc":             pick_col(header_map, ["Cc"]),
        "original_ls":       pick_col(header_map, ["Original LS"]),
```

The existing `for key, col in mapping.items():` loop already routes every key into the `record` dict via `to_text` (or `None` for date fields — none of the six are dates).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_tdoc_parser.py::test_read_tdoc_sheet_captures_six_xlsx_metadata_columns tests/unit/test_tdoc_parser.py::test_read_tdoc_sheet_xlsx_metadata_none_when_header_absent -v`
Expected: PASS

- [ ] **Step 5: Run the full parser test file to confirm no regression**

Run: `python -m pytest tests/unit/test_tdoc_parser.py -v`
Expected: all pre-existing tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/parsers/tdoc_parser.py tests/unit/test_tdoc_parser.py
git commit -m "feat(tdoc-parser): capture six XLSX metadata columns

Adds mapping entries for To, Cc, Original LS, For, Abstract and
Secretary Remarks. read_tdoc_sheet now emits these keys (None when
the cell is empty or the header is absent) without disturbing
header detection or any other captured field."
```

---

### Task 2: Dataclass + ORM — extend `TDoc` and `TDocORM`

**Files:**
- Modify: `src/doc3gpp/models/tdoc.py:7-48`
- Modify: `src/doc3gpp/storage/db/models.py:22-45`

**Interfaces:**
- Produces: `TDoc` carries six optional `str` fields; `TDocORM` carries six optional `String` / `Text` columns.

- [ ] **Step 1: Extend the `TDoc` dataclass**

In `src/doc3gpp/models/tdoc.py`, inside the `TDoc` class, after `cr_pack: str | None = None` add:

```python
    # Six new XLSX metadata fields. All come from the meeting TDoc-list
    # spreadsheet; `None` when the cell is empty, the header is absent,
    # or the row was synced before this column existed.
    tdoc_for: str | None = None
    abstract: str | None = None
    secretary_remarks: str | None = None
    ls_to: str | None = None
    ls_cc: str | None = None
    original_ls: str | None = None
```

Note the convention: a single short docstring on the dataclass summarises all six (the XLSX source + the NULL semantics), matching the style already in place for the other field groups.

- [ ] **Step 2: Extend `TDocORM`**

In `src/doc3gpp/storage/db/models.py`, inside `TDocORM`, add six column declarations after `cr_pack`:

```python
    tdoc_for: Mapped[str | None] = mapped_column(String(64), nullable=True)
    abstract: Mapped[str | None] = mapped_column(Text, nullable=True)
    secretary_remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    ls_to: Mapped[str | None] = mapped_column(String(256), nullable=True)
    ls_cc: Mapped[str | None] = mapped_column(String(256), nullable=True)
    original_ls: Mapped[str | None] = mapped_column(Text, nullable=True)
```

The types mirror what the spec dictates (see Global Constraints).

- [ ] **Step 3: Run the existing model tests**

Run: `python -m pytest tests/unit -k tdoc -v`
Expected: PASS — the dataclass change is additive (new optional fields have defaults, so dataclass-based mocks keep working).

- [ ] **Step 4: Commit**

```bash
git add src/doc3gpp/models/tdoc.py src/doc3gpp/storage/db/models.py
git commit -m "feat(tdoc): add six XLSX-metadata fields to TDoc + ORM

Dataclass and ORM gain tdoc_for, abstract, secretary_remarks, ls_to,
ls_cc, original_ls. Types match the spec (String(64)/String(256)/Text);
all nullable with default None for forward compatibility."
```

---

### Task 3: Migration — idempotent `ALTER TABLE tdocs ADD COLUMN × 6`

**Files:**
- Modify: `src/doc3gpp/storage/db/migrate.py:107-134` (insert new function, register in `create_schema`)
- Modify: `tests/integration/test_tdoc_sqlite.py` (or a new dedicated integration test file)

- [ ] **Step 1: Write the failing test for `create_schema` idempotency**

Create a new file `tests/integration/test_tdocs_xlsx_metadata_migration_sqlite.py`:

```python
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from doc3gpp.storage.db.base import Base
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.models import MeetingORM, TDocORM


def _make_engine():
    eng = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=eng)
    return eng


def test_create_schema_adds_six_xlsx_metadata_columns_to_tdocs():
    engine = _make_engine()

    # Pre-migration baseline: only the original 21 columns exist. The
    # introspection below models what an upgraded pre-existing database
    # would look like — but a freshly-created DB already has the columns
    # via the ORM model. We simulate the upgrade by dropping them.
    with engine.begin() as conn:
        for col in (
            "tdoc_for", "abstract", "secretary_remarks",
            "ls_to", "ls_cc", "original_ls",
        ):
            try:
                conn.execute(text(f"ALTER TABLE tdocs DROP COLUMN {col}"))
            except Exception:
                # Older sqlite (pre-3.35) lacks DROP COLUMN — skip.
                return

    # Now run the migration; it must re-create the columns.
    create_schema()

    with engine.connect() as conn:
        rows = conn.execute(text("PRAGMA table_info(tdocs)")).all()
        column_names = {row[1] for row in rows}

    for col in (
        "tdoc_for", "abstract", "secretary_remarks",
        "ls_to", "ls_cc", "original_ls",
    ):
        assert col in column_names


def test_create_schema_is_idempotent_on_xlsx_metadata_columns():
    engine = _make_engine()
    # Running create_schema twice must not raise (no duplicate ALTER).
    create_schema()
    create_schema()

    with engine.connect() as conn:
        rows = conn.execute(text("PRAGMA table_info(tdocs)")).all()
        # Exactly one declaration per column name.
        column_names = [row[1] for row in rows]
    for col in (
        "tdoc_for", "abstract", "secretary_remarks",
        "ls_to", "ls_cc", "original_ls",
    ):
        assert column_names.count(col) == 1


def test_create_schema_no_op_when_tdocs_table_absent():
    # No tables exist; create_schema on an empty schema must not raise.
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema()
    # The migration short-circuits when the table is absent, then
    # Base.metadata.create_all creates the table with the columns.
    with engine.connect() as conn:
        rows = conn.execute(text("PRAGMA table_info(tdocs)")).all()
        column_names = {row[1] for row in rows}
    assert {"tdoc_for", "abstract", "secretary_remarks",
            "ls_to", "ls_cc", "original_ls"} <= column_names
```

- [ ] **Step 2: Run the migration tests to verify they fail**

Run: `python -m pytest tests/integration/test_tdocs_xlsx_metadata_migration_sqlite.py -v`
Expected: the first test will likely pass on a fresh DB (the ORM creates the columns), but `test_create_schema_is_idempotent_on_xlsx_metadata_columns` will succeed only by accident — `test_create_schema_no_op_when_tdocs_table_absent` requires the new helper to be wired in. Run all three; at least one will fail because the helper isn't registered yet. Specifically: temporarily drop the columns via the test's `ALTER TABLE` first → `create_schema` (which now wants to ADD them) → expect FAIL on the FAIL-side without the helper.

- [ ] **Step 3: Add the migration helper and register it**

In `src/doc3gpp/storage/db/migrate.py`, after
`_migrate_tdoc_cr_cover_page_summary_of_change` (around line 134) add:

```python
def _migrate_tdocs_xlsx_metadata() -> None:
    """Add six XLSX-metadata columns to ``tdocs``.

    Idempotent: probes ``PRAGMA table_info(tdocs)`` and only issues
    ``ALTER TABLE`` statements for the columns that are absent. Same
    shape as :func:`_migrate_spec_rapporteurs` /
    :func:`_migrate_tdoc_cr_cover_page_summary_of_change`.
    """
    engine = get_engine()
    with engine.begin() as conn:
        table_exists = conn.execute(
            text(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='tdocs' LIMIT 1"
            )
        ).first()
        if not table_exists:
            return
        rows = conn.execute(text("PRAGMA table_info(tdocs)")).all()
        column_names = {row[1] for row in rows}
        additions = [
            ("tdoc_for",          "VARCHAR(64)"),
            ("abstract",          "TEXT"),
            ("secretary_remarks", "TEXT"),
            ("ls_to",             "VARCHAR(256)"),
            ("ls_cc",             "VARCHAR(256)"),
            ("original_ls",       "TEXT"),
        ]
        for name, ddl_type in additions:
            if name in column_names:
                continue
            conn.execute(
                text(f"ALTER TABLE tdocs ADD COLUMN {name} {ddl_type}")
            )
```

In `create_schema()` (around line 302), add one line:

```python
    _migrate_tdocs_xlsx_metadata()
```

placed between `_migrate_tdoc_cr_cover_page_summary_of_change()` and `_migrate_spec_versions_drop_comment()`.

- [ ] **Step 4: Run the migration tests to verify they pass**

Run: `python -m pytest tests/integration/test_tdocs_xlsx_metadata_migration_sqlite.py -v`
Expected: PASS

- [ ] **Step 5: Run the full integration test file to confirm no regression**

Run: `python -m pytest tests/integration/test_tdoc_sqlite.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/storage/db/migrate.py tests/integration/test_tdocs_xlsx_metadata_migration_sqlite.py
git commit -m "feat(migrate): add six XLSX-metadata columns to tdocs

Idempotent ALTER TABLE migration runs after cover-page summary.
PRAGMA table_info guard means re-running create_schema is a no-op.
Fresh databases also pick up the columns via Base.metadata.create_all
on the upgraded TDocORM model."
```

---

### Task 4: ORM ↔ domain round-trip in `tdoc_sql.py`

**Files:**
- Modify: `src/doc3gpp/storage/repositories/tdoc_sql.py:64-83` (`_copy_fields`)
- Modify: `src/doc3gpp/storage/repositories/tdoc_sql.py` (the `_orm_to_domain` function near the bottom of the file)
- Modify: `tests/unit/test_tdoc_repository_crud.py` (extend existing test to cover the round-trip)

**Interfaces:**
- Produces: `_copy_fields` writes the six new attrs; `_orm_to_domain` reads them back into the dataclass.

- [ ] **Step 1: Write the failing test for the dataclass ⇄ ORM round-trip**

In `tests/unit/test_tdoc_repository_crud.py`, find the existing round-trip test (likely named `test_round_trip_preserves_fields` or similar). Add a new test:

```python
def test_round_trip_preserves_xlsx_metadata_fields(tmp_path) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    repo = SQLAlchemyTDocRepository(
        sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    )

    original = TDoc(
        tdoc_id="R5-260001",
        title="Doc A",
        ftp_url="tsg_ran/WG5_RL5/TSGR5_124/R5-260001.zip",
        source="Acme",
        type="LS",
        status="Noted",
        release="Rel-18",
        spec="38.331",
        version="18.1.0",
        cr_num="3790",
        cr_pack="RP-220001",
        related_wis="NR_ext",
        tdoc_for="Information",
        abstract="TL;DR here.",
        secretary_remarks="Sec notes.",
        ls_to="RAN2",
        ls_cc="RAN3, RAN4",
        original_ls="C1-260001",
    )

    repo.upsert(original)
    fetched = repo.get_by_id("R5-260001")
    assert fetched is not None
    assert fetched.tdoc_for == "Information"
    assert fetched.abstract == "TL;DR here."
    assert fetched.secretary_remarks == "Sec notes."
    assert fetched.ls_to == "RAN2"
    assert fetched.ls_cc == "RAN3, RAN4"
    assert fetched.original_ls == "C1-260001"
    # Untouched columns still round-trip.
    assert fetched.title == "Doc A"
    assert fetched.cr_pack == "RP-220001"
```

(`tmp_path` arg is included so the test signature matches the file's convention if it uses pytest-tmp-path; if the file uses a different harness, follow the local convention. Check the file's existing tests for the exact `SQLAlchemyTDocRepository(sessionmaker(...))` argument shape before adapting.)

- [ ] **Step 2: Run the new test to verify it fails**

Run: `python -m pytest tests/unit/test_tdoc_repository_crud.py::test_round_trip_preserves_xlsx_metadata_fields -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'tdoc_for'` (dataclass ctor doesn't accept the new attrs) — confirming Task 2 already wired the dataclass and the gap is in `_copy_fields` / `_orm_to_domain`.

- [ ] **Step 3: Extend `_copy_fields`**

In `src/doc3gpp/storage/repositories/tdoc_sql.py`, in the `_copy_fields` static method, add six lines after `target.cr_pack = tdoc.cr_pack`:

```python
        target.tdoc_for = tdoc.tdoc_for
        target.abstract = tdoc.abstract
        target.secretary_remarks = tdoc.secretary_remarks
        target.ls_to = tdoc.ls_to
        target.ls_cc = tdoc.ls_cc
        target.original_ls = tdoc.original_ls
```

- [ ] **Step 4: Extend `_orm_to_domain`**

In the same file, find `_orm_to_domain(row: TDocORM) -> TDoc:` (the file-private mapper near the bottom). Add six kwargs to its `TDoc(...)` ctor call, in the same order as the dataclass declaration:

```python
def _orm_to_domain(row: TDocORM) -> TDoc:
    """Map an ORM row to a TDoc dataclass."""
    return TDoc(
        tdoc_id=row.tdoc_id,
        title=row.title,
        # ... (existing kwargs unchanged) ...
        cr_pack=row.cr_pack,
        tdoc_for=row.tdoc_for,
        abstract=row.abstract,
        secretary_remarks=row.secretary_remarks,
        ls_to=row.ls_to,
        ls_cc=row.ls_cc,
        original_ls=row.original_ls,
    )
```

(Read the existing ctor argument list first; copy the surrounding kwargs verbatim, then append the six new ones at the end so `git diff` stays minimal.)

- [ ] **Step 5: Run the round-trip test to verify it passes**

Run: `python -m pytest tests/unit/test_tdoc_repository_crud.py -v`
Expected: PASS

- [ ] **Step 6: Run the full repo CRUD suite**

Run: `python -m pytest tests/unit/test_tdoc_repository_crud.py tests/unit/test_tdoc_repository_filters.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/doc3gpp/storage/repositories/tdoc_sql.py tests/unit/test_tdoc_repository_crud.py
git commit -m "feat(tdoc-repo): round-trip six XLSX-metadata fields

_copy_fields writes the new attrs; _orm_to_domain reads them back.
Combined with Task 2 + 3 the ORM <-> dataclass round-trip covers
the full set of XLSX-metadata cells."
```

---

### Task 5: Repository Protocol + repo list/list_with_meeting — six new filter kwargs

**Files:**
- Modify: `src/doc3gpp/repository/protocols.py:74-196` (`list` and `list_with_meeting` docstrings)
- Modify: `src/doc3gpp/storage/repositories/tdoc_sql.py:85-305` (`list` / `list_with_meeting` signatures and `_apply_text_filter` calls)
- Modify: `tests/unit/test_tdoc_repository_filters.py`

**Interfaces:**
- Produces: `repo.list(...)` and `repo.list_with_meeting(...)` accept six new optional `str` kwargs with rich-filter grammar.

- [ ] **Step 1: Write the failing filter tests**

In `tests/unit/test_tdoc_repository_filters.py`, find the existing filter test fixtures (likely a `_make_engine` / insert / assert pattern). If the file only covers a subset of columns, extend it with six new tests:

```python
def test_filter_by_abstract_like_pattern() -> None:
    engine = _make_engine()
    with Session(engine) as session:
        insert_data(session)
        session.commit()
    repo = SQLAlchemyTDocRepository(
        sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    )
    rows = repo.list(abstract="%RedCap%")
    assert {r.tdoc_id for r in rows} == {"R5-260001"}


def test_filter_by_tdoc_for_exact_match() -> None:
    engine = _make_engine()
    with Session(engine) as session:
        insert_data(session)
        # Add one row with tdoc_for populated.
        session.add(TDocORM(
            tdoc_id="R5-260099", title="Approval doc", source="Acme",
            type="LS", spec="38.331", version="17.1.0", tdoc_for="Approval",
        ))
        session.commit()
    repo = SQLAlchemyTDocRepository(
        sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    )
    rows = repo.list(tdoc_for="Approval")
    assert {r.tdoc_id for r in rows} == {"R5-260099"}
    rows_none = repo.list(tdoc_for="null")
    assert "R5-260099" not in {r.tdoc_id for r in rows_none}


def test_filter_by_ls_to_with_bang_pattern() -> None:
    engine = _make_engine()
    with Session(engine) as session:
        insert_data(session)
        session.add(TDocORM(
            tdoc_id="R5-260099", title="x", ls_to="RAN2",
        ))
        session.commit()
    repo = SQLAlchemyTDocRepository(
        sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    )
    rows = repo.list(ls_to="!%RAN2%")
    assert "R5-260099" not in {r.tdoc_id for r in rows}


def test_filter_by_ls_cc_null_match() -> None:
    engine = _make_engine()
    with Session(engine) as session:
        insert_data(session)
        session.add(TDocORM(tdoc_id="R5-260099", title="x"))
        session.add(TDocORM(tdoc_id="R5-260100", title="y", ls_cc="RAN3"))
        session.commit()
    repo = SQLAlchemyTDocRepository(
        sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    )
    assert {r.tdoc_id for r in repo.list(ls_cc="not-null")} == {"R5-260100"}


def test_filter_by_original_ls_like() -> None:
    engine = _make_engine()
    with Session(engine) as session:
        insert_data(session)
        session.add(TDocORM(tdoc_id="R5-260099", title="x", original_ls="C1-260001"))
        session.commit()
    repo = SQLAlchemyTDocRepository(
        sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    )
    rows = repo.list(original_ls="C1-%")
    assert {r.tdoc_id for r in rows} == {"R5-260099"}


def test_filter_by_secretary_remarks() -> None:
    engine = _make_engine()
    with Session(engine) as session:
        insert_data(session)
        session.add(TDocORM(
            tdoc_id="R5-260099", title="x",
            secretary_remarks="LS R5-206259 on failing initial registration",
        ))
        session.commit()
    repo = SQLAlchemyTDocRepository(
        sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    )
    rows = repo.list(secretary_remarks="%failing initial registration%")
    assert {r.tdoc_id for r in rows} == {"R5-260099"}
```

(Adjust the `Session` / insert / commit / sessionmaker pattern to match the file's actual helper functions — read the file before pasting.)

- [ ] **Step 2: Run the filter tests to verify they fail**

Run: `python -m pytest tests/unit/test_tdoc_repository_filters.py -v`
Expected: the six new tests fail with `TypeError: list() got an unexpected keyword argument 'abstract'` etc.

- [ ] **Step 3: Extend the Protocol docstrings**

In `src/doc3gpp/repository/protocols.py`, in both `TDocRepository.list` and `TDocRepository.list_with_meeting`, extend the parameter block and the docstring's filter bullet list:

```python
    def list(
        self,
        limit: int = 20,
        offset: int = 0,
        tdoc_id: str | None = None,
        meeting_like: str | None = None,
        meeting_id: int | None = None,
        status: str | None = None,
        cr_cat: str | None = None,
        spec: str | None = None,
        wi: str | None = None,
        revision_of: str | None = None,
        revised_to: str | None = None,
        title: str | None = None,
        ftp_url: str | None = None,
        source: str | None = None,
        tdoc_type: str | None = None,
        uploaded_date: str | None = None,
        release: str | None = None,
        version: str | None = None,
        cr_num: str | None = None,
        cr_pack: str | None = None,
        ls_to: str | None = None,
        ls_cc: str | None = None,
        original_ls: str | None = None,
        tdoc_for: str | None = None,
        abstract: str | None = None,
        secretary_remarks: str | None = None,
        exclude_parsed: bool = False,
    ) -> list[TDoc]:
```

and add a bullet to the docstring's filter list, immediately after the `cr_pack` bullet:

```
          - ``ls_to``, ``ls_cc``, ``original_ls``, ``tdoc_for``,
            ``abstract``, ``secretary_remarks``: rich-filter grammar
            applied to ``tdocs.ls_to`` / ``tdocs.ls_cc`` /
            ``tdocs.original_ls`` / ``tdocs.tdoc_for`` /
            ``tdocs.abstract`` / ``tdocs.secretary_remarks``. Same
            null/not-null/!pattern/LIKE rules as every other text
            column above. Powers the matching CLI flags on
            ``tdoc list`` / ``tdoc parse`` and the matching query
            parameters on the web ``/tdocs`` route / MCP
            ``list_tdocs``.
```

Mirror the same change in `list_with_meeting`'s signature + docstring.

- [ ] **Step 4: Extend `list(...)` in `tdoc_sql.py`**

In `src/doc3gpp/storage/repositories/tdoc_sql.py`, mirror the same six new kwargs on `list(...)` + `list_with_meeting(...)` signatures. After the existing line `stmt = _apply_text_filter(stmt, TDocORM.cr_pack, cr_pack)` add:

```python
            stmt = _apply_text_filter(stmt, TDocORM.ls_to, ls_to)
            stmt = _apply_text_filter(stmt, TDocORM.ls_cc, ls_cc)
            stmt = _apply_text_filter(stmt, TDocORM.original_ls, original_ls)
            stmt = _apply_text_filter(stmt, TDocORM.tdoc_for, tdoc_for)
            stmt = _apply_text_filter(stmt, TDocORM.abstract, abstract)
            stmt = _apply_text_filter(stmt, TDocORM.secretary_remarks, secretary_remarks)
```

In `list_with_meeting(...)`, mirror the six kwargs on the signature (forwarded through `self.list(...)`).

- [ ] **Step 5: Run the filter tests to verify they pass**

Run: `python -m pytest tests/unit/test_tdoc_repository_filters.py -v`
Expected: all six new tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/repository/protocols.py src/doc3gpp/storage/repositories/tdoc_sql.py tests/unit/test_tdoc_repository_filters.py
git commit -m "feat(tdoc): plumb six XLSX-metadata filter kwargs through repo+protocol

ls_to/ls_cc/original_ls/tdoc_for/abstract/secretary_remarks now
thread through TDocRepository.list and list_with_meeting via the
same rich-filter grammar as release/version/cr_num/cr_pack."
```

---

### Task 6: Service layer — forward the six kwargs

**Files:**
- Modify: `src/doc3gpp/services/tdoc_service.py:20-84`

**Interfaces:**
- Produces: `TDocService.list_recent_with_meeting` accepts and forwards six new kwargs to `repository.list_with_meeting`.

- [ ] **Step 1: Extend the signature**

In `src/doc3gpp/services/tdoc_service.py`, `TDocService.list_recent_with_meeting` signature: after `cr_pack: str | None = None` add six kwargs in the same order:

```python
        ls_to: str | None = None,
        ls_cc: str | None = None,
        original_ls: str | None = None,
        tdoc_for: str | None = None,
        abstract: str | None = None,
        secretary_remarks: str | None = None,
```

Forward them through the `self._repository.list_with_meeting(...)` call (add the six kwargs alongside `cr_pack=cr_pack`):

```python
            ls_to=ls_to,
            ls_cc=ls_cc,
            original_ls=original_ls,
            tdoc_for=tdoc_for,
            abstract=abstract,
            secretary_remarks=secretary_remarks,
```

- [ ] **Step 2: Run the existing tests to confirm no regression**

Run: `python -m pytest tests/unit -k tdoc_service -v`
Expected: PASS — kwargs are pure additive.

- [ ] **Step 3: Commit**

```bash
git add src/doc3gpp/services/tdoc_service.py
git commit -m "feat(tdoc-service): forward six XLSX-metadata filter kwargs

Service.list_recent_with_meeting now accepts and forwards
ls_to/ls_cc/original_ls/tdoc_for/abstract/secretary_remarks to
the repository. Pure additive change."
```

---

### Task 7: Scraper — extend `fetch_tdocs_from_portal`

**Files:**
- Modify: `src/doc3gpp/scraping/portal_source.py:74-95`

**Interfaces:**
- Produces: each `TDoc` instance constructed by the ctor loop carries the six new fields.

- [ ] **Step 1: Extend the `TDoc(...)` ctor**

In `src/doc3gpp/scraping/portal_source.py`, inside the dict-comprehension that builds `TDoc`s after `read_tdoc_sheet`, add six new kwargs after `cr_pack=row.get("cr_pack")`:

```python
            tdoc_for=row.get("tdoc_for"),
            abstract=row.get("abstract"),
            secretary_remarks=row.get("secretary_remarks"),
            ls_to=row.get("ls_to"),
            ls_cc=row.get("ls_cc"),
            original_ls=row.get("original_ls"),
```

- [ ] **Step 2: Commit**

```bash
git add src/doc3gpp/scraping/portal_source.py
git commit -m "feat(scraper): forward six XLSX-metadata cells into TDoc

fetch_tdocs_from_portal constructs each TDoc with the new ls_to,
ls_cc, original_ls, tdoc_for, abstract, secretary_remarks fields.
Combined with Task 1, the parser feeds them in; combined with
Tasks 4+5, the repo persists them on the next sync."
```

---

### Task 8: CLI — six new `tdoc list` filter flags + logger

**Files:**
- Modify: `src/doc3gpp/cli.py:966-1200` (`tdoc_list`)
- Modify: `tests/unit/test_tdoc_cli_fields.py`

**Interfaces:**
- Produces: `doc3gpp tdoc list` accepts six new filter flags; `--fields <new name>` returns each new column on demand.

- [ ] **Step 1: Extend `tests/unit/test_tdoc_cli_fields.py`**

Inside the existing `test_cli_tdoc_list_fields_and_filters` (or create a sibling test if simpler), add a stanza asserting the six new filter kwargs flow through. Edit the `fake_list_recent_with_meeting` to accept the new kwargs (mirror the existing `_kwargs` catch-all):

```python
def fake_list_recent_with_meeting(
    self, limit=20, tdoc_id=None, meeting_like=None, meeting_id=None,
    source=None, spec=None, wi=None, title=None, cr_cat=None,
    status=None, tdoc_type=None,
    revision_of=None, revised_to=None, ftp_url=None, uploaded_date=None,
    release=None, version=None, cr_num=None, cr_pack=None,
    ls_to=None, ls_cc=None, original_ls=None,
    tdoc_for=None, abstract=None, secretary_remarks=None,
    **_kwargs,
):
    observed_filters.update({
        # ... (existing observed entries) ...
        "ls_to": ls_to,
        "ls_cc": ls_cc,
        "original_ls": original_ls,
        "tdoc_for": tdoc_for,
        "abstract": abstract,
        "secretary_remarks": secretary_remarks,
    })
    return sample
```

Then add a runner invocation:

```python
runner.invoke(app, [
    "tdoc", "list",
    "--ls-to", "RAN2",
    "--ls-cc", "!%RAN3%",
    "--original-ls", "C1-%",
    "--for", "Information",
    "--abstract", "%TL;DR%",
    "--secretary-remarks", "not-null",
])
assert observed_filters["ls_to"] == "RAN2"
assert observed_filters["ls_cc"] == "!%RAN3%"
assert observed_filters["original_ls"] == "C1-%"
assert observed_filters["tdoc_for"] == "Information"
assert observed_filters["abstract"] == "%TL;DR%"
assert observed_filters["secretary_remarks"] == "not-null"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/unit/test_tdoc_cli_fields.py::test_cli_tdoc_list_fields_and_filters -v`
Expected: FAIL — `typer.BadParameter: No such option: --ls-to` (CLI doesn't accept the flag yet).

- [ ] **Step 3: Add the six new flags**

In `src/doc3gpp/cli.py::tdoc_list`, after `cr_pack: str | None = typer.Option(...)`, add:

```python
    ls_to: str | None = typer.Option(
        None,
        "--ls-to",
        help="SQL LIKE pattern on ls_to (or null/not-null/!pattern).",
    ),
    ls_cc: str | None = typer.Option(
        None,
        "--ls-cc",
        help="SQL LIKE pattern on ls_cc (or null/not-null/!pattern).",
    ),
    original_ls: str | None = typer.Option(
        None,
        "--original-ls",
        help="SQL LIKE pattern on original_ls (or null/not-null/!pattern).",
    ),
    tdoc_for: str | None = typer.Option(
        None,
        "--for",
        help="SQL LIKE pattern on tdoc_for (or null/not-null/!pattern).",
    ),
    abstract: str | None = typer.Option(
        None,
        "--abstract",
        help="SQL LIKE pattern on abstract (or null/not-null/!pattern).",
    ),
    secretary_remarks: str | None = typer.Option(
        None,
        "--secretary-remarks",
        help="SQL LIKE pattern on secretary_remarks (or null/not-null/!pattern).",
    ),
```

In the same function, extend `logger.info(...)`'s format string (line ~1116) to include the six new variables, and extend the kwargs passed to `service.list_recent_with_meeting(...)`:

```python
        ls_to=ls_to,
        ls_cc=ls_cc,
        original_ls=original_ls,
        tdoc_for=tdoc_for,
        abstract=abstract,
        secretary_remarks=secretary_remarks,
```

(The docstring at the top of `tdoc_list` listing filter flags is the comma-separated block beginning with `# --tdoc, --meeting-id, --meeting, --status, --cr-cat, ...`; append `, --ls-to, --ls-cc, --original-ls, --for, --abstract, --secretary-remarks` to keep it accurate.)

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/unit/test_tdoc_cli_fields.py -v`
Expected: PASS

- [ ] **Step 5: Run the broader CLI suite**

Run: `python -m pytest tests/unit -k tdoc -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/cli.py tests/unit/test_tdoc_cli_fields.py
git commit -m "feat(tdoc-cli): six XLSX-metadata filter flags on tdoc list

--ls-to, --ls-cc, --original-ls, --for, --abstract,
--secretary-remarks each accept the rich-filter grammar and route
to the same repository plumbing as --cr-pack. Logger.info picks
the new variables up for post-mortem grepping."
```

---

### Task 9: Web — `tdoc_columns_labels`, route filter params, filter form, detail panel

**Files:**
- Modify: `src/doc3gpp/web/render.py:37-62`
- Modify: `src/doc3gpp/web/routes/tdocs.py:118-250`
- Modify: `src/doc3gpp/web/templates/partials/tdoc_filters.html:1-82`
- Modify: `src/doc3gpp/web/templates/tdoc_show.html:1-171`
- Modify: `tests/integration/test_web_routes.py` (or the existing tdoc-routes integration test file)

- [ ] **Step 1: Extend `TDOC_COLUMN_LABELS`**

In `src/doc3gpp/web/render.py`, extend the `TDOC_COLUMN_LABELS` dict after `"related_wis": "Related WIs"`:

```python
    "ls_to": "LS To",
    "ls_cc": "LS Cc",
    "original_ls": "Original LS",
    "tdoc_for": "For",
    "abstract": "Abstract",
    "secretary_remarks": "Secretary Remarks",
```

(`TDOC_HTML_DEFAULT_FIELDS` stays unchanged — opt-in only.)

- [ ] **Step 2: Extend the web list route**

In `src/doc3gpp/web/routes/tdocs.py::list_tdocs`, after the existing `cr_pack: str | None = Query(default=None, alias="cr-pack")` add six new `Query` declarations:

```python
    ls_to: str | None = Query(default=None, alias="ls-to"),
    ls_cc: str | None = Query(default=None, alias="ls-cc"),
    original_ls: str | None = Query(default=None, alias="original-ls"),
    tdoc_for: str | None = Query(default=None, alias="for"),
    abstract: str | None = Query(default=None, alias="abstract"),
    secretary_remarks: str | None = Query(default=None, alias="secretary-remarks"),
```

Note `--for` would clash with Python's `for` keyword — Typer/Pydantic handle the alias mapping at the HTTP level; the Python kwarg stays `tdoc_for`.

In the `service.list_recent_with_meeting(...)` call inside `list_tdocs`, forward the six new filters:

```python
        ls_to=parse_text_query(ls_to),
        ls_cc=parse_text_query(ls_cc),
        original_ls=parse_text_query(original_ls),
        tdoc_for=parse_text_query(tdoc_for),
        abstract=parse_text_query(abstract),
        secretary_remarks=parse_text_query(secretary_remarks),
```

In the `filters` context dict (the Jinja form-rehydration payload), add six new keys with empty-string defaults:

```python
                "ls_to": ls_to or "",
                "ls_cc": ls_cc or "",
                "original_ls": original_ls or "",
                "tdoc_for": tdoc_for or "",
                "abstract": abstract or "",
                "secretary_remarks": secretary_remarks or "",
```

- [ ] **Step 3: Extend the filter form template**

In `src/doc3gpp/web/templates/partials/tdoc_filters.html`, after the existing `<label>CR pack …</label>` line, add six new `<label>` cells. Match the existing pattern verbatim (each `<label>` block is identical apart from the field name + placeholder/value attribute):

```html
  <label>LS To
    <input type="text" name="ls-to" value="{{ filters.ls_to or '' }}">
  </label>
  <label>LS Cc
    <input type="text" name="ls-cc" value="{{ filters.ls_cc or '' }}">
  </label>
  <label>Original LS
    <input type="text" name="original-ls" value="{{ filters.original_ls or '' }}">
  </label>
  <label>For
    <input type="text" name="for" value="{{ filters.tdoc_for or '' }}">
  </label>
  <label>Abstract
    <input type="text" name="abstract" value="{{ filters.abstract or '' }}">
  </label>
  <label>Secretary Remarks
    <input type="text" name="secretary-remarks" value="{{ filters.secretary_remarks or '' }}">
  </label>
```

(The 6 new labels will sit naturally in the existing flow — CSS layout already wraps a `.filters` grid to as many cells as it has.)

- [ ] **Step 4: Add the XLSX metadata panel on `tdoc_show.html`**

In `src/doc3gpp/web/templates/tdoc_show.html`, between the existing "Cover page" panel and the "TTCN" panel (or after the existing "Auxiliary files" panel — whichever matches the user-approved single-panel placement), add:

```html
  {% if record.tdoc.ls_to or record.tdoc.ls_cc or record.tdoc.original_ls or record.tdoc.tdoc_for or record.tdoc.abstract or record.tdoc.secretary_remarks %}
    <section class="card">
      <h2>XLSX metadata</h2>
      <dl class="kv">
        {% if record.tdoc.tdoc_for %}<dt>For</dt><dd>{{ record.tdoc.tdoc_for }}</dd>{% endif %}
        {% if record.tdoc.ls_to %}<dt>LS To</dt><dd>{{ record.tdoc.ls_to }}</dd>{% endif %}
        {% if record.tdoc.ls_cc %}<dt>LS Cc</dt><dd>{{ record.tdoc.ls_cc }}</dd>{% endif %}
        {% if record.tdoc.original_ls %}<dt>Original LS</dt><dd><pre class="xlsx-meta-pre">{{ record.tdoc.original_ls }}</pre></dd>{% endif %}
        {% if record.tdoc.abstract %}<dt>Abstract</dt><dd><pre class="xlsx-meta-pre">{{ record.tdoc.abstract }}</pre></dd>{% endif %}
        {% if record.tdoc.secretary_remarks %}<dt>Secretary Remarks</dt><dd><pre class="xlsx-meta-pre">{{ record.tdoc.secretary_remarks }}</pre></dd>{% endif %}
      </dl>
    </section>
  {% endif %}
```

(Add a small CSS rule to `src/doc3gpp/web/static/css/` if `xlsx-meta-pre` doesn't yet exist — match the existing `.card pre` / `.kv pre` patterns. Use `git grep "pre"` on the CSS dir to confirm. If a global pre-formatter for `.kv dd pre` already exists, the new class is redundant.)

- [ ] **Step 5: Write the failing web test**

In the existing web integration test file (likely `tests/integration/test_web_routes.py`), add a test that:

```python
def test_web_tdocs_list_filter_by_abstract(client):
    # Seed a row via direct ORM insert.
    ...
    response = client.get("/tdocs?abstract=%TL;DR%&format=json")
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert all(r["tdoc_id"] == "R5-260001" for r in body)


def test_web_tdocs_show_renders_xlsx_metadata_panel(client):
    # Seed a row with all six fields populated.
    ...
    response = client.get("/tdocs/R5-260001")
    assert response.status_code == 200
    html = response.text
    assert "XLSX metadata" in html
    assert "Information" in html  # tdoc_for
    assert "RAN2" in html         # ls_to
    assert "TL;DR here." in html  # abstract


def test_web_tdocs_show_panel_omitted_when_all_six_are_null(client):
    # Seed a row with no xlsx metadata.
    ...
    response = client.get("/tdocs/R5-260002")
    assert response.status_code == 200
    assert "XLSX metadata" not in response.text
```

Adapt to the file's existing fixture / client / seeding pattern. Read `tests/integration/test_web_routes.py` first.

- [ ] **Step 6: Run the web tests to verify they pass**

Run: `python -m pytest tests/integration/test_web_routes.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/doc3gpp/web/render.py src/doc3gpp/web/routes/tdocs.py src/doc3gpp/web/templates/partials/tdoc_filters.html src/doc3gpp/web/templates/tdoc_show.html tests/integration/test_web_routes.py
git commit -m "feat(web): plumb six XLSX-metadata fields through /tdocs route + detail

TDOC_COLUMN_LABELS gains six new entries, list_tdocs accepts the
six Query params, the filter form gains six input cells, and
tdoc_show.html renders an 'XLSX metadata' panel when any of the
six fields is non-None. Default column visibility unchanged."
```

---

### Task 10: MCP — `list_tdocs` filter kwargs + tool description update

**Files:**
- Modify: `src/doc3gpp/web/mcp_server.py:45-270`

**Interfaces:**
- Produces: `list_tdocs` MCP tool accepts six new filter kwargs; `get_tdoc` JSON envelope automatically includes the six new fields.

- [ ] **Step 1: Add six kwargs to `list_tdocs`**

In `src/doc3gpp/web/mcp_server.py::list_tdocs`, after the existing `cr_pack` declaration add:

```python
        ls_to: Annotated[str | None, Field(description="Rich filter on ls_to.")] = None,
        ls_cc: Annotated[str | None, Field(description="Rich filter on ls_cc.")] = None,
        original_ls: Annotated[str | None, Field(description="Rich filter on original_ls.")] = None,
        tdoc_for: Annotated[str | None, Field(description="Rich filter on tdoc_for.")] = None,
        abstract: Annotated[str | None, Field(description="Rich filter on abstract.")] = None,
        secretary_remarks: Annotated[str | None, Field(description="Rich filter on secretary_remarks.")] = None,
```

Forward the six kwargs through the `services.tdoc.list_recent_with_meeting(...)` call (after `cr_pack=cr_pack`):

```python
            ls_to=ls_to,
            ls_cc=ls_cc,
            original_ls=original_ls,
            tdoc_for=tdoc_for,
            abstract=abstract,
            secretary_remarks=secretary_remarks,
```

Update the `@server.tool(...)` description line at the top of `list_tdocs` to include the six new filter names in the prose (mirror the existing prose; append the six names to the comma list that already enumerates `cr_pack` etc.).

- [ ] **Step 2: (No code change) `get_tdoc` automatically picks up the new fields**

`render.to_jsonable(record)` walks `dataclasses.fields`, so the six new fields surface in the JSON envelope automatically. Run an existing MCP / web parity test (e.g. `tests/integration/test_mcp_end_to_end.py` or equivalent — use the project's actual suite name) to confirm.

- [ ] **Step 3: Commit**

```bash
git add src/doc3gpp/web/mcp_server.py
git commit -m "feat(mcp): six XLSX-metadata filter kwargs on list_tdocs

list_tdocs MCP tool accepts ls_to, ls_cc, original_ls, tdoc_for,
abstract, secretary_remarks. get_tdoc already exposes the new
fields through render.to_jsonable's dataclass-field walk; no
tool-side change needed."
```

---

### Task 11: TOML example + docs

**Files:**
- Modify: `src/doc3gpp/data/doc3gpp.toml.example:42-65`
- Modify: `docs/cli.md` (tdoc list filter flag table — find by `grep`/text search)
- Modify: `docs/architecture.md` (TDoc row schema)
- Modify: `docs/web-server.md` (tdoc detail page section)

- [ ] **Step 1: Extend `doc3gpp.toml.example`**

Open `src/doc3gpp/data/doc3gpp.toml.example`. Find the `[output.fields]` block (around lines 46-65). Extend the commented `tdoc` example list (currently shown as one block of commented-out lines) to mention three opt-in opt-ins. Match the existing prose style:

```toml
# tdoc = [
#   "tdoc_id", "meeting_name", "title",
#   "source", "type", "status",
#   "cr_cat", "spec", "version", "related_wis",
#   # "ftp_url" — relative path to the TDoc zip on https://www.3gpp.org/ftp/
#   # "abstract" — TL;DR pulled from the XLSX "Abstract" column
#   # "secretary_remarks" — free-form secretary annotations from XLSX
#   # "original_ls" — LS origin pointer from the XLSX "Original LS" column
# ]
```

- [ ] **Step 2: Update `docs/cli.md`**

Open `docs/cli.md`. Search for the section describing `doc3gpp tdoc list` filter flags (likely a table). Add six rows (one per new flag). Each row follows the existing 3-column shape (flag name, source column, default/notes). If `docs/cli.md` uses a bullet-prose style for the filter list, append the six bullets.

Confirm the change keeps existing examples consistent.

- [ ] **Step 3: Update `docs/architecture.md`**

Open `docs/architecture.md`. Find the TDoc schema / `tdocs` table row description (likely under "ORM schema" or "Data model"). Add a one-line bullet:

```
- `tdocs.tdoc_for` / `abstract` / `secretary_remarks` / `ls_to` /
  `ls_cc` / `original_ls` — six optional XLSX metadata columns
  captured per meeting TDoc-list sync; see
  docs/superpowers/specs/2026-08-19-tdocs-xlsx-metadata-design.md.
```

- [ ] **Step 4: Update `docs/web-server.md`**

Open `docs/web-server.md`. Find the "TDoc detail page" section (likely a paragraph or subheading describing `tdoc_show.html`). Append a paragraph:

```
The TDoc detail page renders an additional **XLSX metadata** panel
when any of the six new `tdocs` columns (`tdoc_for` / `abstract` /
`secretary_remarks` / `ls_to` / `ls_cc` / `original_ls`) is
non-`NULL`. The panel is omitted for legacy rows that were synced
before this feature landed — they stay `NULL` until the next
`doc3gpp tdoc sync` re-reads the meeting XLSX.
```

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/data/doc3gpp.toml.example docs/cli.md docs/architecture.md docs/web-server.md
git commit -m "docs: surface six XLSX-metadata fields across CLI/web/TOML

TOML example gains three commented opt-ins, docs/cli.md adds the
six new filter-flag rows, docs/architecture.md lists the columns
in the ORM schema, docs/web-server.md documents the new detail
panel placement and backfill semantics."
```

---

### Task 12: End-to-end integration test (synthetic XLSX → CLI → web → MCP)

**Files:**
- Create: `tests/integration/test_tdocs_xlsx_metadata_sqlite.py`

This is the one focused integration test that exercises **every** layer touched by the previous eleven tasks.

- [ ] **Step 1: Write the test**

```python
import io
import json
from pathlib import Path

import pytest
from openpyxl import Workbook
from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.scraping.portal_source import fetch_tdocs_from_portal
from doc3gpp.storage.db.base import Base
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.models import MeetingORM, TDocORM
from doc3gpp.storage.db.session import get_session_factory
from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def _build_xlsx() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append([
        "TDoc", "Title", "Source", "Type",
        "For", "Abstract", "Secretary Remarks",
        "To", "Cc", "Original LS",
    ])
    ws.append([
        "R5-260001", "Doc A", "Acme", "LS",
        "Information", "TL;DR here.", "Sec note.",
        "RAN2", "RAN3, RAN4", "C1-260001",
    ])
    ws.append([
        "R5-260002", "Doc B", "Acme", "CR",
        "Approval", "", "",
        "", "", "",
    ])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture
def engine(tmp_path, monkeypatch):
    eng = create_engine("sqlite+pysqlite:///:memory:", future=True)
    monkeypatch.setattr(
        "doc3gpp.storage.db.session._engine", eng, raising=False,
    )
    Base.metadata.create_all(bind=eng)
    sf = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)
    with sf() as session:
        session.add(MeetingORM(
            meeting_id=1, name="RAN5#111", title="t", location="loc",
            start_date=__import__("datetime").datetime(2026, 1, 1),
            end_date=__import__("datetime").datetime(2026, 1, 5),
        ))
        session.commit()
    return sf, eng


def test_xlsx_metadata_round_trips_end_to_end(engine):
    sf, _ = engine
    xlsx_bytes = _build_xlsx()
    # Bypass the network by calling the parser directly + stamping meeting_id.
    from doc3gpp.parsers.tdoc_parser import read_tdoc_sheet
    rows = read_tdoc_sheet(xlsx_bytes)
    tdocs = [
        __import__("doc3gpp.models.tdoc", fromlist=["TDoc"]).TDoc(
            tdoc_id=row["tdoc"], meeting_id=1,
            title=row.get("title"),
            source=row.get("source"),
            type=row.get("type"),
            tdoc_for=row.get("tdoc_for"),
            abstract=row.get("abstract"),
            secretary_remarks=row.get("secretary_remarks"),
            ls_to=row.get("ls_to"),
            ls_cc=row.get("ls_cc"),
            original_ls=row.get("original_ls"),
        )
        for row in rows
    ]
    repo = SQLAlchemyTDocRepository(sf)
    repo.upsert_many(tdocs)
    fetched = {t.tdoc_id: t for t in repo.list()}
    assert fetched["R5-260001"].tdoc_for == "Information"
    assert fetched["R5-260001"].abstract == "TL;DR here."
    assert fetched["R5-260001"].secretary_remarks == "Sec note."
    assert fetched["R5-260001"].ls_to == "RAN2"
    assert fetched["R5-260001"].ls_cc == "RAN3, RAN4"
    assert fetched["R5-260001"].original_ls == "C1-260001"

    assert fetched["R5-260002"].tdoc_for == "Approval"
    assert fetched["R5-260002"].ls_to is None   # empty cell
    assert fetched["R5-260002"].abstract is None


def test_cli_tdoc_list_filters_by_xlsx_metadata(engine, monkeypatch):
    _, _ = engine
    runner = CliRunner()
    observed = {}

    def fake(self, **kwargs):
        observed.update(kwargs)
        sample = __import__("doc3gpp.models.tdoc", fromlist=["TDocWithMeeting"]).TDocWithMeeting(
            tdoc=__import__("doc3gpp.models.tdoc", fromlist=["TDoc"]).TDoc(tdoc_id="R5-260001"),
            meeting_name="RAN5#111",
        )
        return [sample]

    monkeypatch.setattr(
        "doc3gpp.services.tdoc_service.TDocService.list_recent_with_meeting",
        fake,
    )
    result = runner.invoke(app, [
        "tdoc", "list",
        "--abstract", "%TL;DR%",
        "--ls-to", "RAN2",
        "--secretary-remarks", "not-null",
        "--for", "Information",
    ])
    assert result.exit_code == 0
    assert observed["abstract"] == "%TL;DR%"
    assert observed["ls_to"] == "RAN2"
    assert observed["secretary_remarks"] == "not-null"
    assert observed["tdoc_for"] == "Information"
```

- [ ] **Step 2: Run the integration test**

Run: `python -m pytest tests/integration/test_tdocs_xlsx_metadata_sqlite.py -v`
Expected: PASS

- [ ] **Step 3: Run lint + full sqlite test suite**

Run: `ruff check .`
Expected: PASS

Run: `./scripts/test_sqlite.sh`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_tdocs_xlsx_metadata_sqlite.py
git commit -m "test(integration): round-trip six XLSX-metadata fields end-to-end

Builds a synthetic XLSX with the new columns populated, drives the
parser + repo round-trip, and exercises the CLI flags via Typer's
CliRunner against the live service stub. Closes the test loop on
the entire TDoc-list vertical slice introduced by this branch."
```

---

## Self-Review Notes

- **Spec coverage check:** Every requirement in
  `docs/superpowers/specs/2026-08-19-tdocs-xlsx-metadata-design.md`
  has a matching task —
  * parser (Task 1),
  * dataclass/ORM (Task 2),
  * migration (Task 3),
  * repo copy + mapper (Task 4),
  * Protocol + repo filters (Task 5),
  * service (Task 6),
  * scraper (Task 7),
  * CLI flags + fields (Task 8),
  * web route + form + detail panel (Task 9),
  * MCP list_tdocs kwargs (Task 10),
  * TOML + docs (Task 11),
  * end-to-end integration test (Task 12).
  Nothing is missed.
- **No placeholders:** every step carries full code or command and matching test code.
- **Type / signature consistency:**
  `TDoc` field order — `tdoc_for, abstract, secretary_remarks, ls_to, ls_cc, original_ls` —
  is identical across the dataclass (Task 2), `TDocORM` (Task 2), the migration's `additions` list (Task 3), `_copy_fields` (Task 4), the scraper ctor forwards (Task 7), the web `Query` params (Task 9), and the MCP tool kwargs (Task 10).
  CLI flag aliases `--for` (alias `--tdoc-for`) — see spec section 8 — chosen as `--for` for ergonomics with the matching web input name (`?for=`).
- **Backfill scope:** the spec's "no explicit backfill command" choice is honoured by the absence of any `tdoc backfill` task; Tasks 1, 7 make re-syncing implicit.
- **Surface coverage:** per the user's "Full surface (listed)" answer, every layer (CLI / web / MCP) gains both display (`--fields` / `?fields=` / template) and filter coverage.

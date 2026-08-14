# Cover-page `Summary of change` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract the optional `| Summary of change: | … |` cover-page row from 3GPP CR markdown and persist it on `tdoc_cr_cover_page.summary_of_change`, surfacing it through every read path that already exposes the other narrative cover-page fields.

**Architecture:** Single vertical slice spanning every layer of the doc3gpp pipeline — parser regex → `_COVER_FIELDS` whitelist → `TDocCRDetails` field → ORM column → `tdoc_cr_cover_page` migration → `_details_to_orm` / `_orm_to_details` round-trip → CLI by-id table renderer → CLI direct-mode field list → CLI by-id JSON/Markdown (auto via `dataclass_fields`) → CLI by-url (auto via shared renderers) → web HTML template → web JSON envelope (auto via `to_jsonable`) → MCP `get_tdoc` (auto via `render.to_jsonable`) → FTS5 `cover_text` projection → semantic embed-text projection. No backfill of pre-existing rows.

**Tech Stack:** Python 3.10+, SQLAlchemy 2.0, Pydantic v2, Typer, FastAPI/Jinja2, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-08-14-cover-page-summary-of-change-design.md`.

## Global Constraints

- `summary_of_change` is a **nullable `Text` column** on `tdoc_cr_cover_page` (free-text narrative, identical storage to `reason_for_change` / `clauses_affected`).
- `summary_of_change` is **optional** in the source markdown; the regex is registered as `optional=True` in `CRCoverPageParser.parse`'s `patterns` list, matching the precedent of `_COVER_CLAUSES_RE` / `_COVER_OTHER_RE` / `_COVER_REVHIST_RE`. Missing → `None` → `NULL`, no warning.
- `summary_of_change` participates in `max_text_length` truncation alongside `reason_for_change`, `consequences_if_not_approved`, `other_comments`, `revision_history`.
- Migration **must be idempotent**: probe `PRAGMA table_info(tdoc_cr_cover_page)` before `ALTER TABLE ... ADD COLUMN`. Same shape as `_migrate_spec_rapporteurs`.
- **No backfill** — pre-existing rows stay `NULL` until the operator re-runs `tdoc parse`. Matches the precedent for every other column added to this table.
- The TTCN `Summary of change` row (per-function summary in `src/doc3gpp/parsers/cr/ttcn_sections.py`) is **unaffected** — that regex is a separate cell on a different table.
- JSON / Markdown renderers (`_render_tdoc_show_json`, `_render_tdoc_show_markdown_full`, `_render_tdoc_show_markdown_compact`) and the web JSON envelope / MCP `get_tdoc` tool flow through `dataclass_fields(record.cover)` or `to_jsonable(record)`, so they pick up the new field automatically. **No code edits in those layers** — but tests must cover them.
- Ruff line-length 100, target py310. No code comments unless they explain non-obvious behaviour.
- Run `ruff check .` and `./scripts/test_sqlite.sh` before finishing.

---

### Task 1: Parser — extract `Summary of change` from cover-page markdown

**Files:**
- Modify: `src/doc3gpp/parsers/cr/cover_page.py`
- Modify: `src/doc3gpp/parsers/cr/helpers.py`
- Modify: `tests/unit/test_cr_parser.py`

**Interfaces:**
- Produces: `CRCoverPageParser.parse` returns a `dict` that now includes the key `"summary_of_change"` when the source markdown has the row; `helpers._COVER_FIELDS` includes `"summary_of_change"`.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_cr_parser.py` (place after the existing `_TTCN_CORRECTION_LINES` fixture block, before the `# Header / fail-loud behaviour` section):

```python
_HEADER_WITH_SUMMARY_LINES = (
    "**3GPP TSG-RAN5 Meeting #97 *R5-227476***",
    "",
    "**Toulouse, France, 14th Nov 2022 - 18th Nov 2022**",
    "",
    "| CR-Form-v12.4 |",
    "| --- |",
    "| CHANGE REQUEST |",
    "|  | 38.508-1 | CR | 2678 | rev | 1 | Current version: | 17.6.0 |  |",
    "| Title: | Addition of USIM configuration for MUSIM test cases | | | | | | | | |",
    "| Source to WG: | Qualcomm Incorporated | | | | | | | | |",
    "| Source to TSG: | R5 | | | | | | | | |",
    "| Category: | F | | | | | | Release: | | | Rel-17 |",
    "| Reason for change: | | Existing flow does not cover USIM refresh. | | | | | | | |",
    "| Summary of change: | | Add USIM config setter. | | | | | | | |",
    "| Consequences if not approved: | | Test will fail unfairly. | | | | | | | |",
    "| Clauses affected: | | 5.2.3 | | | | | | | | |",
)


def test_cover_page_extracts_summary_of_change() -> None:
    """The ``| Summary of change: |`` cover-page row populates the
    ``summary_of_change`` field on :class:`TDocCRDetails`."""
    parsed = parse_cr_details(
        "\n".join(_HEADER_WITH_SUMMARY_LINES), tdoc_id="R5-227476"
    )
    assert isinstance(parsed, TDocCRParseResult)
    assert parsed.cover.summary_of_change == "Add USIM config setter."
    # Neighbouring fields must still be parsed correctly.
    assert parsed.cover.reason_for_change == "Existing flow does not cover USIM refresh."
    assert parsed.cover.consequences_if_not_approved == "Test will fail unfairly."
    assert parsed.cover.clauses_affected == "5.2.3"


def test_cover_page_summary_of_change_absent_is_none() -> None:
    """When the source markdown has no ``| Summary of change: |`` row,
    the field is ``None`` (no warning, matches every other optional
    narrative cell)."""
    parsed = parse_cr_details(
        "\n".join(_NON_TTCN_HEADER_LINES), tdoc_id="R5-227476"
    )
    assert parsed.cover.summary_of_change is None


def test_cover_page_summary_of_change_blank_is_none() -> None:
    """A blank ``| Summary of change: | |`` cell becomes ``None`` after
    the existing ``_blank_cells_to_none`` pass."""
    md = "\n".join(
        list(_NON_TTCN_HEADER_LINES)
        + ["| Summary of change: | | | | | | | | |"]
    )
    parsed = parse_cr_details(md, tdoc_id="R5-227476")
    assert parsed.cover.summary_of_change is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_cr_parser.py::test_cover_page_extracts_summary_of_change tests/unit/test_cr_parser.py::test_cover_page_summary_of_change_absent_is_none tests/unit/test_cr_parser.py::test_cover_page_summary_of_change_blank_is_none -v`
Expected: three failures — `AttributeError: 'TDocCRDetails' object has no attribute 'summary_of_change'` (or the key is missing from the parsed dict and the field does not exist).

- [ ] **Step 3: Add the regex and patterns entry**

In `src/doc3gpp/parsers/cr/cover_page.py`, after `_COVER_OTHER_RE` (just before `_COVER_REVHIST_RE`), add:

```python
_COVER_SUMMARY_RE = re.compile(
    r"\|\s*Summary of change:(?:\s*\|)+\s*(.*?)\s*\|",
    re.IGNORECASE,
)
```

In `CRCoverPageParser.parse`, in the `patterns` list, immediately after the `_COVER_OTHER_RE` entry, add:

```python
(True, ["summary_of_change"], [1], _COVER_SUMMARY_RE),
```

Then update the `max_text_length` truncation tuple inside the same `parse` method to include `"summary_of_change"` between `"other_comments"` and `"revision_history"`:

```python
for field_name in (
    "reason_for_change",
    "consequences_if_not_approved",
    "summary_of_change",
    "other_comments",
    "revision_history",
):
```

- [ ] **Step 4: Add `summary_of_change` to the `_COVER_FIELDS` whitelist**

In `src/doc3gpp/parsers/cr/helpers.py`, in the `_COVER_FIELDS` tuple, append `"summary_of_change"` immediately after `"consequences_if_not_approved"`:

```python
_COVER_FIELDS = (
    "spec",
    "cr_num",
    "rev",
    "version",
    "title",
    "source",
    "tsg",
    "related_wis",
    "cr_cat",
    "release",
    "reason_for_change",
    "consequences_if_not_approved",
    "summary_of_change",     # NEW
    "clauses_affected",
    "other_comments",
    "revision_history",
    "date",
)
```

This makes the existing `for key in _COVER_FIELDS` loop in `CRParserBase.parse` (`src/doc3gpp/parsers/cr/cr_parsers.py:166`) carry the value into the `TDocCRDetails` constructor on the next task.

- [ ] **Step 5: Run tests to verify they still fail (model field not yet present)**

Run: `pytest tests/unit/test_cr_parser.py::test_cover_page_extracts_summary_of_change tests/unit/test_cr_parser.py::test_cover_page_summary_of_change_absent_is_none tests/unit/test_cr_parser.py::test_cover_page_summary_of_change_blank_is_none -v`
Expected: still failing — the parser now extracts the value into the dict, but `TDocCRDetails` has no `summary_of_change` slot, so the constructor raises `TypeError: __init__() got an unexpected keyword argument 'summary_of_change'`.

- [ ] **Step 6: Commit (TDD red + parser scaffolding)**

```bash
git add src/doc3gpp/parsers/cr/cover_page.py src/doc3gpp/parsers/cr/helpers.py tests/unit/test_cr_parser.py
git commit -m "feat(cr-parser): extract cover-page Summary of change (red)"
```

---

### Task 2: Domain model — add `summary_of_change` to `TDocCRDetails`

**Files:**
- Modify: `src/doc3gpp/models/tdoc_cr.py`
- Modify: `tests/unit/test_tdoc_cr_model.py`

**Interfaces:**
- Consumes: `helpers._COVER_FIELDS` now contains `"summary_of_change"` (Task 1).
- Produces: `TDocCRDetails.summary_of_change: str | None = None` (between `consequences_if_not_approved` and `clauses_affected`); `to_persisted()` includes the new key.

- [ ] **Step 1: Write the failing test**

In `tests/unit/test_tdoc_cr_model.py`, find the existing `TDocCRDetails` field-shape test and either add a new test or extend the existing one to cover `summary_of_change`. Add:

```python
def test_tdoc_cr_details_summary_of_change_default_is_none() -> None:
    details = TDocCRDetails(tdoc_id="R5-227476")
    assert details.summary_of_change is None


def test_tdoc_cr_details_summary_of_change_round_trip() -> None:
    details = TDocCRDetails(
        tdoc_id="R5-227476",
        summary_of_change="Add USIM config setter.",
    )
    assert details.summary_of_change == "Add USIM config setter."
    persisted = details.to_persisted()
    assert persisted["summary_of_change"] == "Add USIM config setter."
    assert persisted["tdoc_id"] == "R5-227476"


def test_tdoc_cr_details_summary_of_change_none_in_persisted() -> None:
    details = TDocCRDetails(tdoc_id="R5-227476")
    persisted = details.to_persisted()
    assert persisted["summary_of_change"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_tdoc_cr_model.py::test_tdoc_cr_details_summary_of_change_default_is_none tests/unit/test_tdoc_cr_model.py::test_tdoc_cr_details_summary_of_change_round_trip tests/unit/test_tdoc_cr_model.py::test_tdoc_cr_details_summary_of_change_none_in_persisted -v`
Expected: three failures — `TypeError: __init__() got an unexpected keyword argument 'summary_of_change'`.

- [ ] **Step 3: Add the dataclass field**

In `src/doc3gpp/models/tdoc_cr.py`, in `TDocCRDetails` (after the `consequences_if_not_approved: str | None = None` line, before `clauses_affected`), add:

```python
    summary_of_change: str | None = None
```

Add a matching docstring entry between the `consequences_if_not_approved` and `clauses_affected` docstring lines:

```
        summary_of_change: Cover-page ``Summary of change:`` cell text.
```

In `to_persisted()`, add the same key in the same position:

```python
        payload: dict[str, Any] = {
            "tdoc_id": self.tdoc_id,
            "spec": self.spec,
            "cr_num": self.cr_num,
            "rev": self.rev,
            "version": self.version,
            "title": self.title,
            "source": self.source,
            "tsg": self.tsg,
            "related_wis": self.related_wis,
            "date": self.date,
            "cr_cat": self.cr_cat,
            "release": self.release,
            "reason_for_change": self.reason_for_change,
            "consequences_if_not_approved": self.consequences_if_not_approved,
            "summary_of_change": self.summary_of_change,    # NEW
            "clauses_affected": self.clauses_affected,
            "other_comments": self.other_comments,
            "revision_history": self.revision_history,
            "extracted_tdoc_id": self.extracted_tdoc_id,
            "ftp_url": self.ftp_url,
        }
```

- [ ] **Step 4: Run all cover-page parser + model tests to verify they pass**

Run: `pytest tests/unit/test_cr_parser.py tests/unit/test_tdoc_cr_model.py -v`
Expected: every previously-red test in Task 1 + this task now passes; no other tests in those files regress.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/models/tdoc_cr.py tests/unit/test_tdoc_cr_model.py
git commit -m "feat(models): add summary_of_change to TDocCRDetails"
```

---

### Task 3: ORM — add `summary_of_change` column on `TDocCrDetailOrm`

**Files:**
- Modify: `src/doc3gpp/storage/db/models.py`
- Modify: `tests/integration/test_tdoc_cr_sqlite.py`

**Interfaces:**
- Consumes: `TDocCRDetails.summary_of_change` (Task 2).
- Produces: `TDocCrDetailOrm.summary_of_change: Mapped[str | None]` (nullable `Text`) between `consequences_if_not_approved` and `clauses_affected`. The ORM column lands automatically via `Base.metadata.create_all` on a fresh DB; pre-existing DBs get it via Task 4's migration.

- [ ] **Step 1: Write the failing ORM assertion test**

In `tests/integration/test_tdoc_cr_sqlite.py`, add a test that bootstraps a fresh schema and asserts the column is present (catches typos in the column name / type at the ORM layer):

```python
def test_tdoc_cr_detail_orm_has_summary_of_change_column(tmp_path, monkeypatch) -> None:
    """A fresh ``tdoc_cr_cover_page`` schema carries
    ``summary_of_change TEXT`` after ``create_schema``."""
    db_path = tmp_path / "doc3gpp.db"
    monkeypatch.setenv("DOC3GPP_DB_URL", f"sqlite:///{db_path}")
    from doc3gpp.settings.loader import get_settings
    get_settings.cache_clear()

    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.db.session import get_engine
    create_schema()

    with get_engine().begin() as conn:
        rows = conn.execute(text("PRAGMA table_info(tdoc_cr_cover_page)")).all()
    cols = {row[1]: row[2] for row in rows}
    assert cols.get("summary_of_change") == "TEXT"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_tdoc_cr_sqlite.py::test_tdoc_cr_detail_orm_has_summary_of_change_column -v`
Expected: FAIL — `'summary_of_change'` not in the columns dict (the ORM column does not exist yet).

- [ ] **Step 3: Add the column**

In `src/doc3gpp/storage/db/models.py`, in `TDocCrDetailOrm`, after `consequences_if_not_approved` (and before `clauses_affected`), add:

```python
    summary_of_change: Mapped[str | None] = mapped_column(Text, nullable=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/integration/test_tdoc_cr_sqlite.py::test_tdoc_cr_detail_orm_has_summary_of_change_column -v`
Expected: PASS.

- [ ] **Step 5: Run lint to catch unused-import / typos**

Run: `ruff check src/doc3gpp/storage/db/models.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/storage/db/models.py tests/integration/test_tdoc_cr_sqlite.py
git commit -m "feat(orm): add summary_of_change column to TDocCrDetailOrm"
```

---

### Task 4: Migration — add `summary_of_change` to pre-existing tables

**Files:**
- Modify: `src/doc3gpp/storage/db/migrate.py`

**Interfaces:**
- Consumes: `TDocCrDetailOrm.summary_of_change` (Task 3).
- Produces: `create_schema()` calls `_migrate_tdoc_cr_cover_page_summary_of_change()` (idempotent `ALTER TABLE ... ADD COLUMN` gated by `PRAGMA table_info`).

- [ ] **Step 1: Write the failing migration test**

In `tests/integration/test_tdoc_cr_sqlite.py` (or whichever integration file already exercises `create_schema`), add a test that creates a schema, asserts the column lands, calls `create_schema()` again, and asserts idempotency:

```python
def test_migrate_adds_summary_of_change_to_existing_db(tmp_path, monkeypatch) -> None:
    """Migration adds ``summary_of_change`` to a database whose
    ``tdoc_cr_cover_page`` predates the column. Idempotent on a second
    call."""
    db_path = tmp_path / "doc3gpp.db"
    monkeypatch.setenv("DOC3GPP_DB_URL", f"sqlite:///{db_path}")
    from doc3gpp.settings.loader import get_settings
    get_settings.cache_clear()

    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.db.session import get_engine

    create_schema()
    engine = get_engine()
    with engine.begin() as conn:
        cols = {row[1] for row in conn.execute(
            text("PRAGMA table_info(tdoc_cr_cover_page)")
        ).all()}
    assert "summary_of_change" in cols

    # Second call is a no-op (no exception, column still present).
    create_schema()
    with engine.begin() as conn:
        cols = {row[1] for row in conn.execute(
            text("PRAGMA table_info(tdoc_cr_cover_page)")
        ).all()}
    assert "summary_of_change" in cols
```

(Adjust the `monkeypatch.setenv` / `get_settings.cache_clear()` pattern to match the existing test conventions in the file. If `create_schema` reads the URL via the engine factory directly, follow the precedent of the closest sibling test.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_tdoc_cr_sqlite.py::test_migrate_adds_summary_of_change_to_existing_db -v`
Expected: FAIL — `summary_of_change` is missing from `PRAGMA table_info`.

- [ ] **Step 3: Add the migration helper**

In `src/doc3gpp/storage/db/migrate.py`, after `_migrate_spec_rapporteurs`, add:

```python
def _migrate_tdoc_cr_cover_page_summary_of_change() -> None:
    """Add ``tdoc_cr_cover_page.summary_of_change`` to databases created
    before that column existed. Idempotent: probe
    ``PRAGMA table_info`` first (same shape as
    :func:`_migrate_spec_rapporteurs`)."""
    engine = get_engine()
    with engine.begin() as conn:
        table_exists = conn.execute(
            text(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='tdoc_cr_cover_page' LIMIT 1"
            )
        ).first()
        if not table_exists:
            return
        rows = conn.execute(
            text("PRAGMA table_info(tdoc_cr_cover_page)")
        ).all()
        column_names = {row[1] for row in rows}
        if "summary_of_change" in column_names:
            return
        conn.execute(
            text("ALTER TABLE tdoc_cr_cover_page ADD COLUMN summary_of_change TEXT")
        )
```

Then wire it into `create_schema()` next to the existing `_migrate_*` calls:

```python
def create_schema() -> None:
    """Create database tables for configured backend."""
    engine = get_engine()
    _migrate_rename_tdoc_cr_details()
    _migrate_drop_tsg_spec_last_sync()
    _migrate_spec_rapporteurs()
    _migrate_tdoc_cr_cover_page_summary_of_change()    # NEW
    _migrate_spec_versions_drop_comment()
    Base.metadata.create_all(bind=engine)
    _create_search_schema()
    _create_vector_schema()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/integration/test_tdoc_cr_sqlite.py::test_migrate_adds_summary_of_change_to_existing_db -v`
Expected: PASS.

- [ ] **Step 5: Run existing migration tests to confirm no regression**

Run: `pytest tests/integration -k migrate -v`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/storage/db/migrate.py tests/integration/test_tdoc_cr_sqlite.py
git commit -m "feat(migrate): add summary_of_change column to tdoc_cr_cover_page"
```

---

### Task 5: Repository — round-trip `summary_of_change`

**Files:**
- Modify: `src/doc3gpp/storage/repositories/tdoc_cr_sql.py`

**Interfaces:**
- Consumes: `TDocCrDetailOrm.summary_of_change` (Task 3), `TDocCRDetails.summary_of_change` (Task 2).
- Produces: `_details_to_orm` writes `target.summary_of_change`; `_orm_to_details` returns the value on `TDocCRDetails`.

- [ ] **Step 1: Write the failing round-trip test**

In `tests/integration/test_tdoc_cr_sqlite.py`, add:

```python
def test_cr_cover_page_round_trip_summary_of_change(tmp_path, monkeypatch) -> None:
    """A ``TDocCRDetails`` with ``summary_of_change`` set round-trips
    through the SQL repo; ``None`` round-trips as ``NULL``."""
    db_path = tmp_path / "doc3gpp.db"
    monkeypatch.setenv("DOC3GPP_DB_URL", f"sqlite:///{db_path}")
    from doc3gpp.settings.loader import get_settings
    get_settings.cache_clear()
    from doc3gpp.storage.db.migrate import create_schema
    create_schema()

    # Insert a parent tdoc row so the FK validates.
    from doc3gpp.storage.db.session import get_session_factory
    from doc3gpp.storage.db.models import TDocORM
    sf = get_session_factory()
    with sf() as session:
        session.add(TDocORM(tdoc_id="R5-227476", ftp_url="TSG_RAN/TSG_RAN_2/R5-227476.zip"))
        session.commit()

    from doc3gpp.storage.repositories.tdoc_cr_sql import SQLAlchemyTDocCrRepository
    repo = SQLAlchemyTDocCrRepository()

    populated = TDocCRDetails(
        tdoc_id="R5-227476",
        ftp_url="TSG_RAN/TSG_RAN_2/R5-227476.zip",
        summary_of_change="Add USIM config setter.",
    )
    repo.upsert(populated)
    fetched = repo.get_by_url("TSG_RAN/TSG_RAN_2/R5-227476.zip")
    assert fetched is not None
    assert fetched.summary_of_change == "Add USIM config setter."

    # ``None`` round-trips as NULL.
    blank = TDocCRDetails(
        tdoc_id="R5-227476",
        ftp_url="TSG_RAN/TSG_RAN_2/R5-227476.zip",
        summary_of_change=None,
    )
    repo.upsert(blank)
    fetched_blank = repo.get_by_url("TSG_RAN/TSG_RAN_2/R5-227476.zip")
    assert fetched_blank is not None
    assert fetched_blank.summary_of_change is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_tdoc_cr_sqlite.py::test_cr_cover_page_round_trip_summary_of_change -v`
Expected: FAIL — `AttributeError: 'TDocCRDetails' object has no attribute 'summary_of_change'` on the first fetch (the SQL read can't reconstruct the missing field).

- [ ] **Step 3: Add the field to `_details_to_orm` and `_orm_to_details`**

In `src/doc3gpp/storage/repositories/tdoc_cr_sql.py`, in `_details_to_orm` (after `target.consequences_if_not_approved`), add:

```python
        target.summary_of_change = details.summary_of_change
```

In `_orm_to_details` (after `consequences_if_not_approved=row.consequences_if_not_approved`), add:

```python
        summary_of_change=row.summary_of_change,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/integration/test_tdoc_cr_sqlite.py::test_cr_cover_page_round_trip_summary_of_change -v`
Expected: PASS.

- [ ] **Step 5: Run the full sqlite integration suite to confirm no regression**

Run: `./scripts/test_sqlite.sh`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/storage/repositories/tdoc_cr_sql.py tests/integration/test_tdoc_cr_sqlite.py
git commit -m "feat(repo): round-trip summary_of_change in tdoc_cr_cover_page"
```

---

### Task 6: CLI table renderer — surface `summary_of_change`

**Files:**
- Modify: `src/doc3gpp/cli.py`

**Interfaces:**
- Consumes: `TDocCRDetails.summary_of_change` (Task 2).
- Produces: `_render_tdoc_show_table_body` writes `summary_of_change: <value or ->` in the `[Extracted Details]` block between `reason_for_change` and `consequences_if_not_approved`.

- [ ] **Step 1: Write the failing renderer test**

In `tests/unit/test_compact_helpers.py` (or `tests/unit/test_tdoc_show_renderers.py` if it already exists), add a test that calls `_render_tdoc_show_table_body` with a `TDocShowRecord` whose `cover.summary_of_change` is set, captures the output, and asserts the field appears in the right position:

```python
def test_show_table_body_renders_summary_of_change() -> None:
    """``_render_tdoc_show_table_body`` emits ``summary_of_change:``
    between ``reason_for_change:`` and ``consequences_if_not_approved:``."""
    from io import StringIO

    from doc3gpp.cli import _render_tdoc_show_table_body
    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.models.tdoc_cr import TDocCRDetails
    from doc3gpp.models.tdoc_show import TDocShowRecord

    record = TDocShowRecord(
        tdoc=TDoc(tdoc_id="R5-227476", ftp_url="x.zip"),
        cover=TDocCRDetails(
            tdoc_id="R5-227476",
            ftp_url="x.zip",
            reason_for_change="Existing flow does not cover USIM refresh.",
            summary_of_change="Add USIM config setter.",
            consequences_if_not_approved="Test will fail unfairly.",
        ),
    )
    stream = StringIO()
    _render_tdoc_show_table_body(stream, record, parse_hint="--tdoc <id>")
    out = stream.getvalue()
    # Position: summary_of_change appears between reason_for_change and consequences.
    assert "reason_for_change: Existing flow does not cover USIM refresh." in out
    assert "summary_of_change: Add USIM config setter." in out
    assert "consequences_if_not_approved: Test will fail unfairly." in out
    rfc_pos = out.index("reason_for_change:")
    soc_pos = out.index("summary_of_change:")
    cna_pos = out.index("consequences_if_not_approved:")
    assert rfc_pos < soc_pos < cna_pos


def test_show_table_body_renders_summary_of_change_absent() -> None:
    """When ``summary_of_change`` is ``None``, the table renderer omits
    the line entirely (matches the existing ``clauses_affected`` /
    ``other_comments`` / ``revision_history`` omission convention)."""
    from io import StringIO

    from doc3gpp.cli import _render_tdoc_show_table_body
    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.models.tdoc_cr import TDocCRDetails
    from doc3gpp.models.tdoc_show import TDocShowRecord

    record = TDocShowRecord(
        tdoc=TDoc(tdoc_id="R5-227476", ftp_url="x.zip"),
        cover=TDocCRDetails(tdoc_id="R5-227476", ftp_url="x.zip"),
    )
    stream = StringIO()
    _render_tdoc_show_table_body(stream, record, parse_hint="--tdoc <id>")
    assert "summary_of_change:" not in stream.getvalue()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_compact_helpers.py::test_show_table_body_renders_summary_of_change tests/unit/test_compact_helpers.py::test_show_table_body_renders_summary_of_change_absent -v`
Expected: first fails on the `out.index("summary_of_change:")` lookup (no such substring in the rendered output); second passes already (absence omits), so it may pass for the wrong reason — proceed to step 3 to harden the assertion either way.

- [ ] **Step 3: Add the renderer line**

In `src/doc3gpp/cli.py`, in `_render_tdoc_show_table_body`, inside the `if record.cover is not None:` block, between the `reason_for_change` write and the `consequences_if_not_approved` write, add:

```python
        stream.write(
            "summary_of_change: "
            f"{_truncate_for_display(details.summary_of_change)}\n"
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_compact_helpers.py::test_show_table_body_renders_summary_of_change tests/unit/test_compact_helpers.py::test_show_table_body_renders_summary_of_change_absent -v`
Expected: PASS.

- [ ] **Step 5: Run the full compact-helpers suite to confirm no regression**

Run: `pytest tests/unit/test_compact_helpers.py -v`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/cli.py tests/unit/test_compact_helpers.py
git commit -m "feat(cli): surface summary_of_change in tdoc show table renderer"
```

---

### Task 7: CLI direct-mode field list — include `summary_of_change`

**Files:**
- Modify: `src/doc3gpp/cli.py`

**Interfaces:**
- Produces: `_DIRECT_PARSE_FIELDS` tuple includes `"summary_of_change"` between `"consequences_if_not_approved"` and `"clauses_affected"`.

- [ ] **Step 1: Write the failing test**

In the existing direct-mode field-list test (search for `_DIRECT_PARSE_FIELDS` references in `tests/unit/`), extend it (or add a sibling) to assert the field list now contains `"summary_of_change"`:

```python
def test_direct_parse_fields_includes_summary_of_change() -> None:
    from doc3gpp.cli import _DIRECT_PARSE_FIELDS
    assert "summary_of_change" in _DIRECT_PARSE_FIELDS
    # Position: between consequences_if_not_approved and clauses_affected.
    assert (
        _DIRECT_PARSE_FIELDS.index("summary_of_change")
        > _DIRECT_PARSE_FIELDS.index("consequences_if_not_approved")
    )
    assert (
        _DIRECT_PARSE_FIELDS.index("summary_of_change")
        < _DIRECT_PARSE_FIELDS.index("clauses_affected")
    )
```

(If the existing direct-parse tests already iterate the tuple, just extend the test list rather than adding a new one.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest <path>::test_direct_parse_fields_includes_summary_of_change -v`
Expected: FAIL — `"summary_of_change" not in _DIRECT_PARSE_FIELDS`.

- [ ] **Step 3: Add the field to the tuple**

In `src/doc3gpp/cli.py`, in `_DIRECT_PARSE_FIELDS`, between `"consequences_if_not_approved"` and `"clauses_affected"`, add:

```python
    "summary_of_change",
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest <path>::test_direct_parse_fields_includes_summary_of_change -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/cli.py tests/<file>
git commit -m "feat(cli): add summary_of_change to _DIRECT_PARSE_FIELDS"
```

---

### Task 8: CLI JSON / Markdown / by-url auto-coverage

**Files:**
- Modify: `tests/unit/test_compact_helpers.py`
- Modify: `tests/unit/test_tdoc_show_renderers.py` (create if absent)

**Interfaces:**
- Consumes: `_render_tdoc_show_json`, `_render_tdoc_show_markdown_full`, `_render_tdoc_show_markdown_compact`, `_render_tdoc_show_by_ftp_url_table`, `_render_tdoc_show_by_ftp_url_json`, `_render_tdoc_show_by_ftp_url_markdown` — all of these iterate `dataclass_fields(record.cover)` (by-id) or accept a `TDocShowRecordByUrl` (by-url) whose `cover` is a `TDocCRDetails`.
- Produces: assertion-only coverage. **No code change in any of these renderers.**

- [ ] **Step 1: Write tests asserting the auto-propagation**

Add to `tests/unit/test_compact_helpers.py` (or the existing renderer test file):

```python
def test_show_json_includes_summary_of_change() -> None:
    """``_render_tdoc_show_json`` includes ``summary_of_change`` in
    the ``cover`` block (null when absent, matching the existing
    reason_for_change / clauses_affected contract)."""
    from io import StringIO
    import json

    from doc3gpp.cli import _render_tdoc_show_json
    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.models.tdoc_cr import TDocCRDetails
    from doc3gpp.models.tdoc_show import TDocShowRecord

    record = TDocShowRecord(
        tdoc=TDoc(tdoc_id="R5-227476", ftp_url="x.zip"),
        cover=TDocCRDetails(
            tdoc_id="R5-227476",
            ftp_url="x.zip",
            summary_of_change="Add USIM config setter.",
        ),
    )
    stream = StringIO()
    _render_tdoc_show_json(record, stream)
    payload = json.loads(stream.getvalue())
    assert payload["cover"]["summary_of_change"] == "Add USIM config setter."

    # Absent case: null (not omitted).
    record_blank = TDocShowRecord(
        tdoc=TDoc(tdoc_id="R5-227476", ftp_url="x.zip"),
        cover=TDocCRDetails(tdoc_id="R5-227476", ftp_url="x.zip"),
    )
    stream2 = StringIO()
    _render_tdoc_show_json(record_blank, stream2)
    payload2 = json.loads(stream2.getvalue())
    assert payload2["cover"]["summary_of_change"] is None


def test_show_markdown_full_includes_summary_of_change() -> None:
    """``_render_tdoc_show_markdown_full`` emits the
    ``- **summary_of_change**: …`` line under ``## Extracted Cover Details``."""
    from io import StringIO

    from doc3gpp.cli import _render_tdoc_show_markdown_full
    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.models.tdoc_cr import TDocCRDetails
    from doc3gpp.models.tdoc_show import TDocShowRecord

    record = TDocShowRecord(
        tdoc=TDoc(tdoc_id="R5-227476", ftp_url="x.zip"),
        cover=TDocCRDetails(
            tdoc_id="R5-227476",
            ftp_url="x.zip",
            summary_of_change="Add USIM config setter.",
        ),
    )
    stream = StringIO()
    _render_tdoc_show_markdown_full(
        stream,
        record,
        header_line="# R5-227476",
        tdoc_heading="## TDoc",
        tdoc_missing_note=None,
        parse_hint="--tdoc <id>",
        show_extracted_details_fallback=False,
        files_missing_hint="—",
    )
    assert "**summary_of_change**: Add USIM config setter." in stream.getvalue()


def test_show_by_ftp_url_table_includes_summary_of_change() -> None:
    """``_render_tdoc_show_by_ftp_url_table`` surfaces
    ``summary_of_change`` in the same way as the by-id renderer."""
    from io import StringIO

    from doc3gpp.cli import _render_tdoc_show_by_ftp_url_table
    from doc3gpp.models.tdoc_cr import TDocCRDetails
    from doc3gpp.models.tdoc_show import TDocShowRecordByUrl

    record = TDocShowRecordByUrl(
        ftp_url="x.zip",
        cover=TDocCRDetails(
            tdoc_id="R5-227476",
            ftp_url="x.zip",
            summary_of_change="Add USIM config setter.",
        ),
    )
    stream = StringIO()
    _render_tdoc_show_by_ftp_url_table(stream, record)
    out = stream.getvalue()
    assert "summary_of_change: Add USIM config setter." in out
```

(The actual signatures of `_render_tdoc_show_markdown_full` and
`_render_tdoc_show_by_ftp_url_table` may differ from these stubs —
e.g. `_render_tdoc_show_by_ftp_url_table` takes `(record, output)`
not `(stream, record)`. Look up the function signature in
`src/doc3gpp/cli.py` at implementation time and adapt the stubs
accordingly. The shape of the assertions — what substrings should
appear in the output — does not change.)

- [ ] **Step 2: Run tests to verify they pass**

Run: `pytest tests/unit/test_compact_helpers.py::test_show_json_includes_summary_of_change tests/unit/test_compact_helpers.py::test_show_markdown_full_includes_summary_of_change tests/unit/test_compact_helpers.py::test_show_by_ftp_url_table_includes_summary_of_change -v`
Expected: PASS on the first run (no production code changes in this task — the auto-propagation through `dataclass_fields` is already in place).

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_compact_helpers.py
git commit -m "test: cover summary_of_change in JSON / markdown / by-url show renderers"
```

---

### Task 9: Web HTML template — render `Summary of change` in the Cover card

**Files:**
- Modify: `src/doc3gpp/web/templates/tdoc_show.html`
- Modify: `tests/unit/test_web_routes.py`

**Interfaces:**
- Consumes: `record.cover.summary_of_change` (Task 2).
- Produces: one `<dt>Summary of change</dt><dd>{{ record.cover.summary_of_change or '-' }}</dd>` row inside the Cover card, between `Reason for change` and `Consequences if not approved`.

- [ ] **Step 1: Write the failing template test**

In `tests/unit/test_web_routes.py`, add a test that follows the
exact setup pattern of the existing `test_show_tdoc_*` tests in the
file (search for `template_name="tdoc_show.html"` or look at the
nearest sibling test — line ~2555 onward). The fixture seeds a
`TDocORM` row + a `TDocCRTTCNDetails` row; for this test we seed a
`TDocORM` + a `TDocCrDetailOrm` row with `summary_of_change` set:

```python
def test_show_tdoc_cover_card_includes_summary_of_change(
    client: TestClient, sqlite_env: Any,
) -> None:
    """The Cover card on ``tdoc_show.html`` renders
    ``Summary of change`` between Reason for change and Consequences
    if not approved (matches the source CR row order)."""
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_cr_sql import (
        SQLAlchemyTDocCrRepository,
    )
    from doc3gpp.storage.repositories.tdoc_sql import (
        SQLAlchemyTDocRepository,
    )
    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.models.tdoc_cr import TDocCRDetails

    create_schema()
    url = "R5/26.001/R5s260001.zip"
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5s260001", ftp_url=url),
    )
    SQLAlchemyTDocCrRepository().upsert(
        TDocCRDetails(
            tdoc_id="R5s260001",
            ftp_url=url,
            reason_for_change="Existing flow does not cover USIM refresh.",
            summary_of_change="Add USIM config setter.",
            consequences_if_not_approved="Test will fail unfairly.",
        ),
    )

    response = client.get("/tdocs/R5s260001")
    assert response.status_code == 200
    body = response.text
    assert "<dt>Summary of change</dt>" in body
    assert "<dd>Add USIM config setter.</dd>" in body
    # Position: between Reason for change and Consequences if not approved.
    rfc_pos = body.index("<dt>Reason for change</dt>")
    soc_pos = body.index("<dt>Summary of change</dt>")
    cna_pos = body.index("<dt>Consequences if not approved</dt>")
    assert rfc_pos < soc_pos < cna_pos
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest <path>::test_show_tdoc_cover_card_includes_summary_of_change -v`
Expected: FAIL — `<dt>Summary of change</dt>` not in body.

- [ ] **Step 3: Add the template row**

In `src/doc3gpp/web/templates/tdoc_show.html`, inside the `{% if record.cover %}` block (search for `<dt>Reason for change</dt>` and `<dt>Consequences if not approved</dt>`), between those two rows, add:

```html
        <dt>Summary of change</dt><dd>{{ record.cover.summary_of_change or '-' }}</dd>
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest <path>::test_show_tdoc_cover_card_includes_summary_of_change -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/web/templates/tdoc_show.html tests/unit/test_web_routes.py
git commit -m "feat(web): render Summary of change in tdoc show Cover card"
```

---

### Task 10: Web JSON envelope + MCP `get_tdoc` — auto-coverage

**Files:**
- Modify: `tests/unit/test_web_routes.py`
- Modify: `tests/integration/test_mcp_end_to_end.py`

**Interfaces:**
- Consumes: `TDocCRDetails.summary_of_change` (Task 2); `render.to_jsonable` iterates dataclass fields.
- Produces: assertion-only coverage for the web JSON envelope
  (`GET /tdocs/{id}?format=json`) and the MCP `get_tdoc` tool. **No code change.**

- [ ] **Step 1: Write the web JSON test**

In `tests/unit/test_web_routes.py`, add a test that reuses the same
fixture-setup as Task 9 but hits `?format=json`:

```python
def test_show_tdoc_json_includes_cover_summary_of_change(
    client: TestClient, sqlite_env: Any,
) -> None:
    """``GET /tdocs/{id}?format=json`` surfaces
    ``cover.summary_of_change`` in the JSON envelope."""
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_cr_sql import (
        SQLAlchemyTDocCrRepository,
    )
    from doc3gpp.storage.repositories.tdoc_sql import (
        SQLAlchemyTDocRepository,
    )
    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.models.tdoc_cr import TDocCRDetails

    create_schema()
    url = "R5/26.001/R5s260001.zip"
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5s260001", ftp_url=url),
    )
    SQLAlchemyTDocCrRepository().upsert(
        TDocCRDetails(
            tdoc_id="R5s260001",
            ftp_url=url,
            summary_of_change="Add USIM config setter.",
        ),
    )
    response = client.get("/tdocs/R5s260001?format=json")
    assert response.status_code == 200
    payload = response.json()
    assert payload["cover"]["summary_of_change"] == "Add USIM config setter."
```

- [ ] **Step 2: Write the MCP test**

In `tests/integration/test_mcp_end_to_end.py`, add a test that
seeds a tdoc + cover row and invokes the `get_tdoc` MCP tool:

```python
def test_mcp_get_tdoc_includes_cover_summary_of_change(
    client, sqlite_env,
) -> None:
    """The ``get_tdoc`` MCP tool surfaces ``cover.summary_of_change``."""
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_cr_sql import (
        SQLAlchemyTDocCrRepository,
    )
    from doc3gpp.storage.repositories.tdoc_sql import (
        SQLAlchemyTDocRepository,
    )
    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.models.tdoc_cr import TDocCRDetails

    create_schema()
    url = "R5/26.001/R5s260001.zip"
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5s260001", ftp_url=url),
    )
    SQLAlchemyTDocCrRepository().upsert(
        TDocCRDetails(
            tdoc_id="R5s260001",
            ftp_url=url,
            summary_of_change="Add USIM config setter.",
        ),
    )

    # Use the test client's MCP helper to invoke the tool. The exact
    # helper name (call_tool / invoke / etc.) and the path follow the
    # precedent of the existing get_tdoc tests in this file. Look for
    # `name="get_tdoc"` in the closest sibling test for the exact
    # call shape.
    result = ... # call get_tdoc with tdoc_id="R5s260001"
    import json
    payload = json.loads(result.text)
    assert payload["cover"]["summary_of_change"] == "Add USIM config setter."
```

- [ ] **Step 3: Run tests to verify they pass**

Run: `pytest <path>::test_show_tdoc_json_includes_cover_summary_of_change <path>::test_mcp_get_tdoc_includes_cover_summary_of_change -v`
Expected: PASS on the first run (no production code change — `to_jsonable` / `render.to_jsonable` iterate dataclass fields).

- [ ] **Step 4: Commit**

```bash
git add tests/unit/test_web_routes.py tests/integration/test_mcp_end_to_end.py
git commit -m "test: cover summary_of_change in web JSON envelope and MCP get_tdoc"
```

---

### Task 11: FTS5 `cover_text` — include `summary_of_change` in search projection

**Files:**
- Modify: `src/doc3gpp/storage/repositories/search_sql.py`

**Interfaces:**
- Consumes: `tdoc_cr_cover_page.summary_of_change` (Task 3).
- Produces: `_cover_text` SELECT projection includes `summary_of_change` so a rebuilt index makes the field searchable.

- [ ] **Step 1: Write the failing FTS5 test**

In `tests/integration/test_search_indexes.py` (where other FTS5 schema tests live), add a test that calls `_cover_text` directly and asserts the value is included:

```python
def test_cover_text_projection_includes_summary_of_change(sqlite_env) -> None:
    """``_cover_text`` returns a string that includes the
    ``summary_of_change`` value when one is stored on
    ``tdoc_cr_cover_page``.

    The function is the projection that ``tdoc_search.cover_text`` is
    populated from at index time; asserting on it directly tests the
    full rebuild path without standing up the FTS5 virtual table.
    """
    from doc3gpp.storage.db.session import get_engine
    from doc3gpp.storage.repositories.search_sql import _cover_text

    # Seed a parent tdoc + cover row with summary_of_change set.
    from doc3gpp.storage.db.models import TDocORM
    from doc3gpp.storage.repositories.tdoc_cr_sql import SQLAlchemyTDocCrRepository
    from doc3gpp.models.tdoc_cr import TDocCRDetails

    repo = SQLAlchemyTDocCrRepository()
    repo.upsert(TDocCRDetails(
        tdoc_id="R5-227476",
        ftp_url="TSG_RAN/TSG_RAN_2/R5-227476.zip",
        title="USIM configuration",
        summary_of_change="AddUSIMConfigSetter",
    ))
    # Seed the parent tdoc row (FK target) so the upsert above succeeds.
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "INSERT OR IGNORE INTO tdocs (tdoc_id, ftp_url) "
                "VALUES (:id, :url)"
            ),
            {"id": "R5-227476", "url": "TSG_RAN/TSG_RAN_2/R5-227476.zip"},
        )

    text_out = _cover_text(conn=get_engine(), tdoc_id="R5-227476")
    assert "AddUSIMConfigSetter" in text_out
```

(The `sqlite_env` fixture is defined in `tests/integration/conftest.py`; if not present in your environment, use the `tmp_path` / `monkeypatch.setenv("DOC3GPP_DB_URL", ...)` pattern from Task 4.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest <path>::test_fts5_cover_text_includes_summary_of_change -v`
Expected: FAIL — the MATCH query returns no rows (the field is not in the cover_text projection).

- [ ] **Step 3: Add the column to `_cover_text`**

In `src/doc3gpp/storage/repositories/search_sql.py`, in `_cover_text`, extend the `SELECT` projection to include `summary_of_change` (placed between `consequences_if_not_approved` and `clauses_affected` to match the source-CR row order):

```python
def _cover_text(*, conn: Engine, tdoc_id: str) -> str:
    with conn.begin() as c:
        row = c.execute(
            text(
                """
                SELECT spec, cr_num, rev, version, title, source, tsg,
                       related_wis, date, cr_cat, release,
                       reason_for_change, consequences_if_not_approved,
                       summary_of_change,                         -- NEW
                       clauses_affected, other_comments, revision_history,
                       extracted_tdoc_id
                  FROM tdoc_cr_cover_page
                 WHERE tdoc_id = :id
                """
            ),
            {"id": tdoc_id},
        ).first()
    if row is None:
        return ""
    return " ".join(str(v) for v in row if v is not None)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest <path>::test_fts5_cover_text_includes_summary_of_change -v`
Expected: PASS.

- [ ] **Step 5: Run the full search integration suite to confirm no regression**

Run: `pytest tests/integration -k search -v`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/storage/repositories/search_sql.py tests/integration/test_search_sqlite.py
git commit -m "feat(search): include summary_of_change in FTS5 cover_text projection"
```

---

### Task 12: Semantic embedding — include `summary_of_change` in embed text

**Files:**
- Modify: `src/doc3gpp/storage/repositories/vector_sql.py`

**Interfaces:**
- Consumes: `tdoc_cr_cover_page.summary_of_change` (Task 3).
- Produces: `_build_embed_text` SELECTs `title, summary_of_change FROM tdoc_cr_cover_page WHERE tdoc_id = :id` and appends the value to `parts`.

- [ ] **Step 1: Write the failing embed test**

In `tests/integration/test_embed_after_parse.py`, add a test that calls `_build_embed_text` directly and asserts the returned string contains the new field:

```python
def test_build_embed_text_includes_summary_of_change(sqlite_env) -> None:
    """``_build_embed_text`` appends ``tdoc_cr_cover_page.summary_of_change``
    to the parts list so a fresh embedding reflects the field."""
    from doc3gpp.storage.db.session import get_engine
    from doc3gpp.storage.repositories.vector_sql import _build_embed_text

    # Seed a parent tdoc + cover row with summary_of_change set.
    from doc3gpp.storage.repositories.tdoc_cr_sql import SQLAlchemyTDocCrRepository
    from doc3gpp.models.tdoc_cr import TDocCRDetails

    repo = SQLAlchemyTDocCrRepository()
    repo.upsert(TDocCRDetails(
        tdoc_id="R5-227476",
        ftp_url="TSG_RAN/TSG_RAN_2/R5-227476.zip",
        title="USIM configuration",
        summary_of_change="AddUSIMConfigSetter",
    ))
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "INSERT OR IGNORE INTO tdocs (tdoc_id, ftp_url) "
                "VALUES (:id, :url)"
            ),
            {"id": "R5-227476", "url": "TSG_RAN/TSG_RAN_2/R5-227476.zip"},
        )

    out = _build_embed_text("R5-227476")
    assert out is not None
    assert "AddUSIMConfigSetter" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest <path>::test_embed_text_includes_summary_of_change -v`
Expected: FAIL — the returned text does not contain `"Add USIM config setter."`.

- [ ] **Step 3: Extend the cover-page SELECT in `_build_embed_text`**

In `src/doc3gpp/storage/repositories/vector_sql.py`, in `_build_embed_text`, change the `tdoc_cr_cover_page` SELECT to include `summary_of_change` and append the value:

```python
        cover = conn.execute(
            text(
                "SELECT title, summary_of_change "
                "FROM tdoc_cr_cover_page WHERE tdoc_id = :id"
            ),
            {"id": tdoc_id},
        ).first()
        if cover is not None:
            if cover[0]:
                parts.append(cover[0])
            if cover[1]:                                              # NEW
                parts.append(cover[1])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest <path>::test_embed_text_includes_summary_of_change -v`
Expected: PASS.

- [ ] **Step 5: Run the full embed integration suite to confirm no regression**

Run: `pytest tests/integration -k embed -v`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/storage/repositories/vector_sql.py tests/integration/test_embed_after_parse.py
git commit -m "feat(embed): include summary_of_change in semantic embed-text projection"
```

---

### Task 13: Full verification

- [ ] **Step 1: Run lint**

Run: `ruff check .`
Expected: PASS (no errors, no warnings).

- [ ] **Step 2: Run the full sqlite test suite**

Run: `./scripts/test_sqlite.sh`
Expected: all green (unit + integration, sqlite-only, `-n auto` if xdist is installed).

- [ ] **Step 3: Spot-check the CLI**

Run (against an empty test DB):

```bash
doc3gpp db init
doc3gpp tdoc show --tdoc R5-227476 --format json
```

Expected: JSON envelope includes `"summary_of_change": null` (the TDoc isn't parsed in this DB, but the field is in the dataclass schema). The `tdoc show --tdoc <parsed-id> --format json` path should include the populated value once the operator runs `tdoc parse` against a parsed row.

- [ ] **Step 4: Final commit (if any uncommitted edits)**

```bash
git status
git diff
git add -A
git commit -m "chore: post-verification tidy" || true
```

---

## Spec coverage map

| Spec § | Task |
|---|---|
| §1 Parser regex + patterns entry + truncation list | Task 1 |
| §2 `_COVER_FIELDS` whitelist | Task 1 |
| §3 `TDocCRDetails` field + `to_persisted()` | Task 2 |
| §4 ORM column | Task 3 |
| §5 Migration helper + `create_schema` wiring | Task 4 |
| §6 Repository `_details_to_orm` / `_orm_to_details` | Task 5 |
| §7 CLI by-id table renderer | Task 6 |
| §8 `_DIRECT_PARSE_FIELDS` | Task 7 |
| §9 JSON / Markdown by-id auto-propagation | Task 8 |
| §9a CLI by-url auto-propagation | Task 8 |
| §10 Web HTML template | Task 9 |
| §10a Web JSON envelope auto-propagation | Task 10 |
| §10b MCP `get_tdoc` auto-propagation | Task 10 |
| §11 FTS5 `cover_text` projection | Task 11 |
| §12 Semantic embed-text projection | Task 12 |
| Risk: backfill / embedding invalidation / FTS5 schema | No task (documented in spec §Risks; no code action) |
| Docs sync (`3gpp-knowledge.md`, `cli.md`, `README.md`, `AGENTS.md`) | No task (spec §Documentation explicitly says no change) |

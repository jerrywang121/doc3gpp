# Spec rapporteurs + drop comment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `rapporteurs` field to the `specs` table (extracted from the detail page rapporteurs grid) and remove the unused `comment` field from `spec_versions`.

**Architecture:** The change spans the layered pipeline: domain model (`models/spec.py`) → parser (`parsers/spec_parser.py`) → ORM (`storage/db/models.py`) → repository (`storage/repositories/spec_sql.py`) → migration (`storage/db/migrate.py`) → output surfaces (CLI, web routes, MCP, settings, template). `rapporteurs` mirrors the existing `wis` comma-joined-text pattern; `comment` is removed end-to-end.

**Tech Stack:** Python 3.10+, SQLAlchemy 2.0, BeautifulSoup4/lxml, Typer, FastAPI, pytest.

## Global Constraints

- `rapporteurs` column type is `String(128)` (normally a single company).
- `comment` is removed from `spec_versions` entirely (model, ORM, parser, repo, output surfaces).
- Migration must be idempotent: probe `PRAGMA table_info` before `ALTER TABLE`.
- `_migrate_spec_versions_drop_comment` uses `ALTER TABLE ... DROP COLUMN` (SQLite 3.35+; runtime is 3.45.1) wrapped in `try/except` so an older sqlite degrades to leaving the orphan column.
- Ruff line-length 100, target py310. No code comments unless they explain non-obvious behavior.
- Run `ruff check .` and `./scripts/test_sqlite.sh` before finishing.

---

### Task 1: Domain model — add `rapporteurs`, remove `comment`

**Files:**
- Modify: `src/doc3gpp/models/spec.py`

**Interfaces:**
- Produces: `Spec.rapporteurs: str | None = None` (after `wis`); `SpecVersion` no longer has `comment`.

- [ ] **Step 1: Write the failing test**

Modify `tests/unit/test_spec_model.py`:

```python
def test_spec_fields() -> None:
    spec = Spec(
        spec_id="36.579-5", type="TS", title="T", status="Under change control",
        radio_tech="2G,3G,LTE", initial_release="Rel-20", tsg="R5", wis="A,B",
        rapporteurs="Ericsson LM",
    )
    assert spec.spec_id == "36.579-5"
    assert spec.type == "TS"
    assert spec.tsg == "R5"
    assert spec.rapporteurs == "Ericsson LM"


def test_spec_defaults() -> None:
    spec = Spec(spec_id="36.579-5", type="TS", title="T")
    assert spec.status is None
    assert spec.radio_tech is None
    assert spec.initial_release is None
    assert spec.tsg is None
    assert spec.wis is None
    assert spec.rapporteurs is None
    assert spec.last_synced_at is None


def test_spec_version_optional_fields() -> None:
    v = SpecVersion(spec_id="s", version="1.0.0", ftp_url="ftp://x")
    assert v.pdf_url is None
    assert v.crs is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_spec_model.py -v`
Expected: FAIL — `Spec.__init__()` got an unexpected keyword argument `rapporteurs`; `SpecVersion` still has `comment`.

- [ ] **Step 3: Implement the model change**

In `src/doc3gpp/models/spec.py`:

- Add `rapporteurs: str | None = None` to `Spec` after `wis`, and a docstring line:
  ```python
  rapporteurs: Comma-joined company names from the detail page rapporteurs grid (e.g. ``Ericsson LM``).
  ```
- Remove `comment: str | None = None` from `SpecVersion` and its docstring line `comment: From the row's ``Comment`` cell (nullable).`

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_spec_model.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/models/spec.py tests/unit/test_spec_model.py
git commit -m "feat(spec): add rapporteurs field, drop comment from SpecVersion"
```

---

### Task 2: Parser — extract `rapporteurs`, stop parsing `comment`

**Files:**
- Modify: `src/doc3gpp/parsers/spec_parser.py`
- Modify: `tests/fixtures/spec_pages/R5_detail_portal.html`
- Modify: `tests/unit/test_spec_parser.py`

**Interfaces:**
- Consumes: `Spec.rapporteurs` (Task 1).
- Produces: `parse_spec_detail(html, spec_id, tsg) -> tuple[Spec, list[SpecVersion]]` sets `header.rapporteurs`; `_parse_version_row` no longer sets `comment`.

- [ ] **Step 1: Add a rapporteurs grid to the portal fixture**

Append to `tests/fixtures/spec_pages/R5_detail_portal.html` (before `</body></html>`):

```html
<table class="rgMasterTable rgClipCells" id="specificationRapporteurs_rdGridRapporteurs_ctl00">
  <tbody>
    <tr class="rgRow" id="specificationRapporteurs_rdGridRapporteurs_ctl00__0">
      <td class="hyperlinkStyleColumn"><a href="https://portal.etsi.org/webapp/teldir/ListPersDetails.asp?PersId=76116" target="_blank"><strong>Bo</strong> <strong>Jönsson</strong></a></td>
      <td class="companyStyleColumn"><div>Ericsson LM</div></td>
      <td style="display:none;">76116</td>
    </tr>
  </tbody>
</table>
```

- [ ] **Step 2: Write the failing test**

In `tests/unit/test_spec_parser.py`, add to `test_parse_spec_detail_header_wis_telerik_grid`:

```python
    assert header.rapporteurs == "Ericsson LM"
```

And in `test_parse_spec_detail_versions`, remove the line:

```python
    assert v0.comment == "Some comment here"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/unit/test_spec_parser.py -v`
Expected: FAIL — `header.rapporteurs` is `None` (attribute missing).

- [ ] **Step 4: Implement the parser change**

In `src/doc3gpp/parsers/spec_parser.py`:

- In `parse_spec_detail`, after `wis = _extract_related_wis(soup)`, add:
  ```python
  rapporteurs = _extract_rapporteurs(soup)
  ```
- Add `rapporteurs=rapporteurs,` to the `Spec(...)` constructor.
- Add a new helper (place after `_extract_related_wis`):
  ```python
  def _extract_rapporteurs(soup: BeautifulSoup) -> str | None:
      grid = soup.find(id="specificationRapporteurs_rdGridRapporteurs_ctl00")
      if grid is None:
          return None
      companies: list[str] = []
      for row in grid.find_all(
          "tr", class_=lambda c: c and ("rgRow" in c or "rgAltRow" in c)
      ):
          cell = row.find("td", class_="companyStyleColumn")
          if cell is None:
              continue
          text = _normalize(cell.get_text())
          if text and text not in companies:
              companies.append(text)
      return ",".join(companies) if companies else None
  ```
- In `_parse_version_row`, remove the `comment` extraction block:
  ```python
  comment: str | None = None
  remark = row.find(class_="lblRemarkText")
  if remark is not None:
      comment = _normalize(remark.get_text())[:256] or None
  ```
  and remove `comment=comment,` from the `SpecVersion(...)` constructor.

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/unit/test_spec_parser.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/parsers/spec_parser.py tests/fixtures/spec_pages/R5_detail_portal.html tests/unit/test_spec_parser.py
git commit -m "feat(spec): parse rapporteurs from detail page, drop comment parsing"
```

---

### Task 3: ORM + repository — persist `rapporteurs`, drop `comment`

**Files:**
- Modify: `src/doc3gpp/storage/db/models.py`
- Modify: `src/doc3gpp/storage/repositories/spec_sql.py`
- Modify: `tests/integration/test_spec_sql.py`

**Interfaces:**
- Consumes: `Spec.rapporteurs`, `SpecVersion` without `comment` (Task 1).
- Produces: `SpecORM.rapporteurs` column; `SpecVersionORM` without `comment`; `SQLAlchemySpecRepository.upsert` persists `rapporteurs`; `_orm_to_spec` maps it.

- [ ] **Step 1: Write the failing test**

In `tests/integration/test_spec_sql.py`:

- In `test_upsert_and_get`, add `rapporteurs="Ericsson LM"` to the `Spec(...)` and assert `got.rapporteurs == "Ericsson LM"`.
- In `test_upsert_versions_dedupes_within_single_batch`, remove `comment="7-99-329,325/96"` and `comment="re-upload"` from the two `SpecVersion(...)` constructors, and remove `assert rows[0].comment == "re-upload"`.
- In `test_upsert_versions_dedupes_when_upload_date_is_none`, remove `comment="first"` / `comment="second"` and the `assert rows[0].comment == "second"` line.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_spec_sql.py -v`
Expected: FAIL — `SpecVersion` no longer accepts `comment`; `got.rapporteurs` missing.

- [ ] **Step 3: Implement ORM change**

In `src/doc3gpp/storage/db/models.py`:

- In `SpecORM`, add after `wis`:
  ```python
  rapporteurs: Mapped[str | None] = mapped_column(String(128), nullable=True)
  ```
- In `SpecVersionORM`, remove:
  ```python
  comment: Mapped[str | None] = mapped_column(String(256), nullable=True)
  ```

- [ ] **Step 4: Implement repository change**

In `src/doc3gpp/storage/repositories/spec_sql.py`:

- In `upsert` update branch, add `existing.rapporteurs = spec.rapporteurs`.
- In `upsert` insert branch, add `rapporteurs=spec.rapporteurs,` to the `SpecORM(...)` constructor.
- In `_orm_to_spec`, add `rapporteurs=row.rapporteurs,`.
- In `upsert_versions`, remove `existing.comment = v.comment` and `comment=v.comment,`.
- In `_orm_to_version`, remove `comment=row.comment,`.

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/integration/test_spec_sql.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/storage/db/models.py src/doc3gpp/storage/repositories/spec_sql.py tests/integration/test_spec_sql.py
git commit -m "feat(spec): persist rapporteurs, drop comment column"
```

---

### Task 4: Migration — add `rapporteurs`, drop `comment`

**Files:**
- Modify: `src/doc3gpp/storage/db/migrate.py`

**Interfaces:**
- Consumes: `SpecORM.rapporteurs`, `SpecVersionORM` without `comment` (Task 3).
- Produces: `_migrate_spec_rapporteurs()` and `_migrate_spec_versions_drop_comment()`, both called from `create_schema()`.

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_spec_migration.py` following the `test_tdoc_cr_rename_migration.py` pattern (uses the `sqlite_env` fixture and `get_engine()`):

```python
"""Integration tests for the spec rapporteurs / comment migrations."""

from __future__ import annotations

from sqlalchemy import text

from doc3gpp.storage.db.base import Base
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.session import get_engine


def _seed_legacy_spec_db() -> None:
    """Build a legacy-shape ``specs`` (no rapporteurs) + ``spec_versions``
    (with comment) on the active engine."""
    engine = get_engine()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS spec_versions"))
        conn.execute(text("DROP TABLE IF EXISTS specs"))
        conn.execute(
            text(
                """
                CREATE TABLE specs (
                    spec_id VARCHAR(32) PRIMARY KEY,
                    type VARCHAR(8),
                    title TEXT,
                    status VARCHAR(32),
                    radio_tech VARCHAR(64),
                    initial_release VARCHAR(16),
                    tsg VARCHAR(16),
                    wis VARCHAR(512),
                    last_synced_at DATETIME
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE spec_versions (
                    spec_id VARCHAR(32) NOT NULL,
                    version VARCHAR(16) NOT NULL,
                    ftp_url VARCHAR(1024) NOT NULL,
                    release VARCHAR(16),
                    meeting_id INTEGER,
                    meeting_name VARCHAR(64),
                    upload_date DATE,
                    version_id INTEGER,
                    pdf_url VARCHAR(1024),
                    crs TEXT,
                    comment VARCHAR(256),
                    PRIMARY KEY (spec_id, version),
                    FOREIGN KEY (spec_id) REFERENCES specs(spec_id) ON DELETE CASCADE
                )
                """
            )
        )


def test_create_schema_adds_rapporteurs_and_drops_comment(sqlite_env) -> None:
    _seed_legacy_spec_db()
    create_schema()
    with get_engine().begin() as conn:
        spec_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(specs)")).all()}
        assert "rapporteurs" in spec_cols
        version_cols = {
            r[1] for r in conn.execute(text("PRAGMA table_info(spec_versions)")).all()
        }
        assert "comment" not in version_cols


def test_migration_is_idempotent_on_fresh_schema(sqlite_env) -> None:
    engine = get_engine()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    create_schema()
    create_schema()
    with get_engine().begin() as conn:
        spec_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(specs)")).all()}
        assert "rapporteurs" in spec_cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_spec_migration.py -v`
Expected: FAIL — `create_schema()` does not add `rapporteurs` / drop `comment`.

- [ ] **Step 3: Implement the migrations**

In `src/doc3gpp/storage/db/migrate.py`, add two functions following the `_migrate_tsg_spec_last_sync` probe pattern:

```python
def _migrate_spec_rapporteurs() -> None:
    """Add ``specs.rapporteurs`` to databases created before that column
    existed. Idempotent: probe ``PRAGMA table_info`` first."""
    engine = get_engine()
    with engine.begin() as conn:
        table_exists = conn.execute(
            text(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='specs' LIMIT 1"
            )
        ).first()
        if not table_exists:
            return
        rows = conn.execute(text("PRAGMA table_info(specs)")).all()
        column_names = {row[1] for row in rows}
        if "rapporteurs" in column_names:
            return
        conn.execute(
            text("ALTER TABLE specs ADD COLUMN rapporteurs VARCHAR(128)")
        )


def _migrate_spec_versions_drop_comment() -> None:
    """Drop the unused ``spec_versions.comment`` column. Idempotent;
    degrades to leaving the orphan column on sqlite < 3.35 (no
    ``DROP COLUMN``)."""
    engine = get_engine()
    with engine.begin() as conn:
        table_exists = conn.execute(
            text(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='spec_versions' LIMIT 1"
            )
        ).first()
        if not table_exists:
            return
        rows = conn.execute(text("PRAGMA table_info(spec_versions)")).all()
        column_names = {row[1] for row in rows}
        if "comment" not in column_names:
            return
        try:
            conn.execute(text("ALTER TABLE spec_versions DROP COLUMN comment"))
        except Exception:  # noqa: BLE001 - older sqlite lacks DROP COLUMN
            return
```

In `create_schema()`, add both calls before `Base.metadata.create_all(bind=engine)`:

```python
    _migrate_spec_rapporteurs()
    _migrate_spec_versions_drop_comment()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/integration/test_spec_migration.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/storage/db/migrate.py tests/integration/test_spec_migration.py
git commit -m "feat(spec): migrate specs.rapporteurs add + spec_versions.comment drop"
```

---

### Task 5: CLI + settings — surface `rapporteurs`, drop `comment`

**Files:**
- Modify: `src/doc3gpp/cli.py` (`spec_show` header_fields / version_fields)
- Modify: `src/doc3gpp/settings/schema.py` (`OutputSettings.fields.spec` default)
- Modify: `src/doc3gpp/data/doc3gpp.toml.example` (`[output.fields]` spec block)
- Modify: `tests/integration/test_spec_cli.py`
- Modify: `tests/unit/test_settings.py`

**Interfaces:**
- Consumes: `Spec.rapporteurs`, `SpecVersion` without `comment` (Task 1).

- [ ] **Step 1: Write the failing test**

In `tests/integration/test_spec_cli.py` `test_spec_show_json`, remove `comment="-"` from the `SpecVersion(...)` constructor. In `tests/unit/test_settings.py` `test_output_fields_default_spec`, change the expected list to:

```python
    assert s.output.fields.spec == [
        "spec_id", "type", "title", "status",
        "radio_tech", "initial_release", "tsg", "wis", "rapporteurs",
    ]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_spec_cli.py tests/unit/test_settings.py -v`
Expected: FAIL — `SpecVersion` rejects `comment`; settings default lacks `rapporteurs`.

- [ ] **Step 3: Implement CLI + settings change**

In `src/doc3gpp/cli.py` `spec_show`:

- `header_fields`: add `"rapporteurs"` after `"wis"`.
- `version_fields`: remove `"comment"`.

In `src/doc3gpp/settings/schema.py` `OutputSettings.fields.spec` default, add `"rapporteurs"` after `"wis"`.

In `src/doc3gpp/data/doc3gpp.toml.example`, add a commented `spec` block under `[output.fields]` (after the `wi` line):

```toml
# spec = [
#   "spec_id", "type", "title", "status",
#   "radio_tech", "initial_release", "tsg", "wis", "rapporteurs",
# ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/integration/test_spec_cli.py tests/unit/test_settings.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/cli.py src/doc3gpp/settings/schema.py src/doc3gpp/data/doc3gpp.toml.example tests/integration/test_spec_cli.py tests/unit/test_settings.py
git commit -m "feat(spec): surface rapporteurs in CLI + settings, drop comment"
```

---

### Task 6: Web + MCP + template — surface `rapporteurs`, drop `comment`

**Files:**
- Modify: `src/doc3gpp/web/routes/specs.py`
- Modify: `src/doc3gpp/web/mcp_server.py`
- Modify: `src/doc3gpp/web/templates/spec_show.html`
- Modify: `tests/unit/test_web_routes.py`

**Interfaces:**
- Consumes: `Spec.rapporteurs`, `SpecVersion` without `comment` (Task 1).

- [ ] **Step 1: Write the failing test**

In `tests/unit/test_web_routes.py` `FakeSpecService.__init__`, add `rapporteurs="Ericsson LM"` to the first `Spec(...)`. In `test_get_spec_show_json`, add `assert body["spec"]["rapporteurs"] == "Ericsson LM"`.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_web_routes.py -k spec -v`
Expected: FAIL — `body["spec"]` lacks `rapporteurs`.

- [ ] **Step 3: Implement web + MCP + template change**

In `src/doc3gpp/web/routes/specs.py`:

- `_SPEC_DEFAULT_FIELDS`: add `"rapporteurs"` after `"wis"`.
- `_VERSION_FIELDS`: remove `"comment"`.

In `src/doc3gpp/web/mcp_server.py`:

- `_SPEC_FIELDS`: add `"rapporteurs"` after `"wis"`.
- `_VERSION_FIELDS`: remove `"comment"`.

In `src/doc3gpp/web/templates/spec_show.html`:

- Add a Rapporteurs row in the `<dl class="kv">` block after the WIs row:
  ```html
  {% if spec.rapporteurs %}<dt>Rapporteurs</dt><dd>{{ spec.rapporteurs }}</dd>{% endif %}
  ```
- Remove the `<th>Comment</th>` header and the `<td>{{ v.comment or '-' }}</td>` cell from the versions table.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_web_routes.py -k spec -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/web/routes/specs.py src/doc3gpp/web/mcp_server.py src/doc3gpp/web/templates/spec_show.html tests/unit/test_web_routes.py
git commit -m "feat(spec): surface rapporteurs in web + MCP, drop comment"
```

---

### Task 7: Docs sync

**Files:**
- Modify: `docs/architecture.md`
- Modify: `docs/cli.md`
- Modify: `docs/3gpp-knowledge.md`
- Modify: `docs/code-map.md`
- Modify: `docs/superpowers/specs/2026-08-10-spec-sync-list-show-design.md`

**Interfaces:**
- Consumes: the field names from Tasks 1–6.

- [ ] **Step 1: Update `docs/architecture.md`**

In the `specs` bullet (line ~590), add `rapporteurs` to the field list. In the `spec_versions` bullet (line ~598), remove `comment`.

- [ ] **Step 2: Update `docs/3gpp-knowledge.md`**

In the `Spec` header fields section (line ~370), add a `rapporteurs` bullet after `wis`. In the `SpecVersion` fields section (line ~389), remove the `comment` bullet.

- [ ] **Step 3: Update `docs/cli.md`**

In the `spec list` output-columns line (~1884), add `rapporteurs`. In the `spec show` behavior section, note the header now includes `rapporteurs` and version rows no longer include `comment`.

- [ ] **Step 4: Update `docs/code-map.md`**

In the `SQLAlchemySpecRepository` row (~107), mention `rapporteurs` on the header row.

- [ ] **Step 5: Update the historical spec**

In `docs/superpowers/specs/2026-08-10-spec-sync-list-show-design.md`, amend the `specs` table section to add `rapporteurs` and the `spec_versions` table section to remove `comment` (per the AGENTS.md doc-sync convention).

- [ ] **Step 6: Commit**

```bash
git add docs/architecture.md docs/cli.md docs/3gpp-knowledge.md docs/code-map.md docs/superpowers/specs/2026-08-10-spec-sync-list-show-design.md
git commit -m "docs(spec): rapporteurs field + comment removal"
```

---

### Task 8: Full verification

**Files:**
- None (verification only).

- [ ] **Step 1: Run the linter**

Run: `ruff check .`
Expected: no errors.

- [ ] **Step 2: Run the full sqlite test suite**

Run: `./scripts/test_sqlite.sh`
Expected: all tests pass.

- [ ] **Step 3: Grep for stale references**

Run: `rg -n "\.comment|comment=" src/doc3gpp tests --glob '!**/tdoc_cr*' --glob '!**/search_sql.py' --glob '!**/cr/*'`
Expected: no matches in the spec pipeline (remaining `comment` references are unrelated TDoc/CR fields).

- [ ] **Step 4: Commit any stragglers**

```bash
git add -A
git commit -m "chore(spec): final verification" || echo "nothing to commit"
```

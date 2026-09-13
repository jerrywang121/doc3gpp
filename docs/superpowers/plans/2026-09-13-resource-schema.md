# Resource `schema` surfaces Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `schema` subcommand to each of the six resource groups (`tsg`, `meeting`, `tdoc`, `wi`, `spec`, `testcase`) describing every DB field, exposed identically on CLI, REST+HTML, and MCP.

**Architecture:** New pure-data registry module `src/doc3gpp/models/schema_info.py` (single source of truth, no DB reads); six thin CLI commands, six REST routes sharing one HTML template pair, six MCP tools; one shared `schema_payload()` helper guarantees byte-identical JSON everywhere.

**Tech Stack:** Python 3.10, Typer, FastAPI + Jinja2, MCP SDK v2, SQLAlchemy ORM (read-only for drift tests), pytest + Typer CliRunner + FastAPI TestClient.

## Global Constraints

- Python runtime floor: `py310` (no `match` on enums tricks needed; `str | None` syntax OK).
- Ruff only: `line-length = 100`, default rule selection. Run `ruff check .` before every commit.
- Offline only: no network in any new test; plain `pytest` is the offline suite (`addopts = ["-m", "not online"]`).
- Conventional Commits, present tense: `feat(schema): ...`, `test(schema): ...`, `docs(schema): ...`.
- Documentation sync in the same change set: `docs/cli.md`, `docs/web-server.md`, `docs/code-map.md`, `README.md`/`AGENTS.md` inventory lines.
- New features ship with both a unit test and an integration test (`tests/integration/`, sqlite).
- `models/` must not import from `services/` or `storage/` (layering rule) — categorical literals are duplicated in the registry with an equality test.

---

## File Structure

- Create: `src/doc3gpp/models/schema_info.py` — `FieldInfo`, `TableSchema`, `RESOURCE_SCHEMAS`, `SCHEMA_FIELDS`, `schema_payload()`. Pure data, zero imports from services/storage.
- Modify: `src/doc3gpp/cli.py` — `_emit_schema()` helper + six `schema` commands (one per Typer app).
- Modify: `src/doc3gpp/web/routes/{tsgs,meetings,tdocs,wis,specs,testcases}.py` — one `GET /schema` route each, registered **before** any `/{param}` route in the same file.
- Create: `src/doc3gpp/web/templates/schema.html` + `src/doc3gpp/web/templates/partials/schema_results.html` — shared HTML rendering grouped by table.
- Modify: `src/doc3gpp/web/mcp_server.py` — six `get_*_schema` tools.
- Create: `tests/unit/test_schema_info.py` — registry completeness/drift + categorical equality + CLI table/json tests.
- Modify: `tests/integration/test_web_end_to_end.py` (or new `tests/integration/test_schema_parity.py`) — REST/MCP/CLI parity + HTML smoke.
- Modify docs: `docs/cli.md`, `docs/web-server.md`, `docs/code-map.md`, `README.md`, `AGENTS.md`.

---

### Task 1: Registry core — types, helper, tsg/meeting/wi tables

**Files:**
- Create: `src/doc3gpp/models/schema_info.py`
- Test: `tests/unit/test_schema_info.py`

**Interfaces:**
- Consumes: nothing (greenfield pure-data module).
- Produces: `FieldInfo(name, type, nullable, description, values)`, `TableSchema(table, fields)`, `RESOURCE_SCHEMAS: dict[str, tuple[TableSchema, ...]]`, `SCHEMA_FIELDS: list[str]`, `schema_payload(resource: str) -> list[dict[str, object]]` — consumed by Tasks 4, 5, 6.

- [ ] **Step 1: Write the failing test for the core API**

```python
"""Registry tests for the resource `schema` surfaces."""

from doc3gpp.models.schema_info import (
    RESOURCE_SCHEMAS,
    SCHEMA_FIELDS,
    schema_payload,
)


def test_schema_field_order_and_keys() -> None:
    assert SCHEMA_FIELDS == ["table", "field", "type", "nullable", "description", "values"]
    assert sorted(RESOURCE_SCHEMAS) == ["meeting", "spec", "tdoc", "testcase", "tsg", "wi"]


def test_tsg_payload_shape() -> None:
    payload = schema_payload("tsg")
    assert payload[0] == {
        "table": "tsgs",
        "field": "tsg_name",
        "type": "str",
        "nullable": False,
        "description": "Full human-readable group name, e.g. RAN WG5.",
        "values": "-",
    }
    short = next(row for row in payload if row["field"] == "short_name")
    assert short["nullable"] is False
    assert short["values"] == (
        "RP,R1,R2,R3,R4,R5,RT,SP,S1,S2,S3,S4,S5,S6,CP,C1,C3,C4,C6"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_schema_info.py -v`
Expected: FAIL with "No module named 'doc3gpp.models.schema_info'" (or collection error).

- [ ] **Step 3: Write the module with FieldInfo/TableSchema/helper + tsg/meeting/wi registries**

```python
"""Static field registry backing the resource `schema` surfaces.

Single source of truth for ``doc3gpp <resource> schema`` (CLI),
``GET /<resources>/schema`` (REST/HTML), and the ``get_*_schema``
MCP tools. Pure data by design: no DB reads, no imports from
``services`` / ``storage`` (layering rule), deterministic on an
empty database.

``type`` vocabulary: ``str`` (String columns), ``int`` (Integer),
``date`` (Date), ``datetime`` (DateTime), ``text`` (Text — long-form
or unbounded content), ``gzip-json`` (LargeBinary holding
gzip-compressed UTF-8 JSON). ``values`` is set only where code fixes
the set; everything scraped verbatim is free-form with examples in
``description``.
"""

from __future__ import annotations

from dataclasses import dataclass


FIELD_TYPES: tuple[str, ...] = ("str", "int", "date", "datetime", "text", "gzip-json")

SCHEMA_FIELDS: list[str] = ["table", "field", "type", "nullable", "description", "values"]

# Canonical TSG short names. Duplicates
# ``services/tsg_service.py::_DEFAULT_TSGS`` (models/ cannot import
# services/); equality is locked by ``test_schema_info.py``.
TSG_SHORT_NAMES: tuple[str, ...] = (
    "RP", "R1", "R2", "R3", "R4", "R5", "RT", "SP",
    "S1", "S2", "S3", "S4", "S5", "S6",
    "CP", "C1", "C3", "C4", "C6",
)


@dataclass(slots=True, frozen=True)
class FieldInfo:
    """One DB column descriptor."""

    name: str
    type: str
    nullable: bool
    description: str
    values: tuple[str, ...] | None = None


@dataclass(slots=True, frozen=True)
class TableSchema:
    """All columns of one DB table."""

    table: str
    fields: tuple[FieldInfo, ...]


RESOURCE_SCHEMAS: dict[str, tuple[TableSchema, ...]] = {
    "tsg": (
        TableSchema(
            table="tsgs",
            fields=(
                FieldInfo("tsg_name", "str", False, "Full human-readable group name, e.g. RAN WG5."),
                FieldInfo("short_name", "str", False, "Canonical short code and primary key, e.g. R5; FK target for meetings.tsg, wis.tsg_short, specs.tsg.", TSG_SHORT_NAMES),
                FieldInfo("description", "str", False, "Plain-text scope of the group."),
                FieldInfo("url", "str", True, "3GPP group page URL, if known."),
                FieldInfo("meeting_last_sync", "datetime", True, "UTC ISO-8601 timestamp of the last meeting sync, else null."),
            ),
        ),
    ),
    "meeting": (
        TableSchema(
            table="meetings",
            fields=(
                FieldInfo("meeting_id", "int", False, "Numeric 3GPP meeting id; primary key."),
                FieldInfo("name", "str", False, "Short meeting name, e.g. RAN5#111."),
                FieldInfo("title", "str", False, "Full meeting title."),
                FieldInfo("location", "str", False, "Venue or online details."),
                FieldInfo("start_date", "date", True, "Start date ISO-8601, else null."),
                FieldInfo("end_date", "date", True, "End date ISO-8601, else null."),
                FieldInfo("ftp_url", "str", True, "FTP path relative to https://www.3gpp.org/ftp/ used to discover documents."),
                FieldInfo("start_doc", "str", True, "First TDoc id in the meeting range, e.g. R5-260001."),
                FieldInfo("end_doc", "str", True, "Last TDoc id in the range; null means open-ended."),
                FieldInfo("tsg", "str", True, "Owning TSG short name; FK into tsgs.short_name.", TSG_SHORT_NAMES),
                FieldInfo("tdoc_list_last_sync", "datetime", True, "UTC timestamp of the last TDoc-list sync, else null."),
            ),
        ),
    ),
    "wi": (
        TableSchema(
            table="wis",
            fields=(
                FieldInfo("wi_id", "int", False, "Numeric workitemId from the 3GPP portal, e.g. 1031076; first half of the composite PK."),
                FieldInfo("acronym", "str", False, "Short symbolic WI identifier, e.g. LTE_TN_NR_NTN_mob-Core."),
                FieldInfo("release", "str", False, "Free-form release marker, e.g. Rel-19."),
                FieldInfo("name", "text", False, "Full human-readable WI title."),
                FieldInfo("tsg_short", "str", False, "Owning TSG; FK into tsgs.short_name and second half of the composite PK.", TSG_SHORT_NAMES),
            ),
        ),
    ),
}


def schema_payload(resource: str) -> list[dict[str, object]]:
    """Return the flat JSON-ready rows for ``resource``.

    One dict per field with keys ``table, field, type, nullable,
    description, values``. ``nullable`` is a real bool; ``values``
    is a comma-joined string (``"-"`` when the field is free-form).
    Key order is fixed so CLI ``--compact``, REST ``?format=json``,
    and MCP payloads serialise byte-identically.
    """
    tables = RESOURCE_SCHEMAS[resource]
    payload: list[dict[str, object]] = []
    for table in tables:
        for field in table.fields:
            payload.append(
                {
                    "table": table.table,
                    "field": field.name,
                    "type": field.type,
                    "nullable": field.nullable,
                    "description": field.description,
                    "values": ",".join(field.values) if field.values else "-",
                }
            )
    return payload
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_schema_info.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/models/schema_info.py tests/unit/test_schema_info.py
git commit -m "feat(schema): add static field registry core (tsg, meeting, wi)"
```

---

### Task 2: Registry — tdoc tables (6 tables, 71 fields)

**Files:**
- Modify: `src/doc3gpp/models/schema_info.py`
- Test: `tests/unit/test_schema_info.py`

**Interfaces:**
- Consumes: `FieldInfo`, `TableSchema` from Task 1.
- Produces: `RESOURCE_SCHEMAS["tdoc"]` (6 `TableSchema` entries) — consumed by Tasks 4, 5, 6.

- [ ] **Step 1: Write the failing test**

```python
def test_tdoc_tables_and_counts() -> None:
    from doc3gpp.models.schema_info import RESOURCE_SCHEMAS

    tables = RESOURCE_SCHEMAS["tdoc"]
    assert [t.table for t in tables] == [
        "tdocs",
        "tdoc_cr_cover_page",
        "tdoc_cr_ttcn_details",
        "tdoc_cr_change_details",
        "tdoc_files",
        "tdoc_extracts",
    ]
    counts = {t.table: len(t.fields) for t in tables}
    assert counts == {
        "tdocs": 24,
        "tdoc_cr_cover_page": 21,
        "tdoc_cr_ttcn_details": 10,
        "tdoc_cr_change_details": 4,
        "tdoc_files": 6,
        "tdoc_extracts": 6,
    }


def test_tdoc_categoricals() -> None:
    payload = schema_payload("tdoc")
    by_table_field = {(r["table"], r["field"]): r for r in payload}
    assert by_table_field[("tdoc_cr_cover_page", "cr_cat")]["values"] == "F,B,A,C,D"
    assert by_table_field[("tdoc_files", "type")]["values"] == "revision,review,support"
    assert by_table_field[("tdocs", "status")]["values"] == "-"
    assert by_table_field[("tdoc_cr_ttcn_details", "required_changes")]["type"] == "gzip-json"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_schema_info.py::test_tdoc_tables_and_counts -v`
Expected: FAIL with `KeyError: 'tdoc'`.

- [ ] **Step 3: Add the `tdoc` entry to `RESOURCE_SCHEMAS`**

Insert a `"tdoc": (...)` entry into the `RESOURCE_SCHEMAS` dict (after `"meeting"`, before `"wi"` to keep tsg/meeting/tdoc/wi/spec/testcase CLI order). Full entry:

```python
    "tdoc": (
        TableSchema(
            table="tdocs",
            fields=(
                FieldInfo("tdoc_id", "str", False, "Canonical TDoc id; primary key. Shapes: R5-260013, R5s260009, R5w260013, R4-2607922."),
                FieldInfo("title", "text", True, "Document title; null when the XLSX cell is empty."),
                FieldInfo("meeting_id", "int", True, "FK into meetings.meeting_id."),
                FieldInfo("ftp_url", "text", True, "Relative download path under https://www.3gpp.org/ftp/; null when the XLSX row has no hyperlink."),
                FieldInfo("source", "str", True, "Authoring company, e.g. Qualcomm, Ericsson."),
                FieldInfo("type", "str", True, "Free-form document type, e.g. CR, Discussion Paper."),
                FieldInfo("status", "str", True, "Free-form disposition, e.g. Agreed, Noted, Approved."),
                FieldInfo("reservation_date", "date", True, "Reservation date from the XLSX, ISO-8601."),
                FieldInfo("uploaded_date", "date", True, "Upload date from the XLSX, ISO-8601."),
                FieldInfo("cr_cat", "str", True, "CR category letter when the XLSX carries one."),
                FieldInfo("is_revision_of", "str", True, "TDoc id this document revises."),
                FieldInfo("revised_to", "str", True, "TDoc id that revises this document."),
                FieldInfo("release", "str", True, "Free-form release marker, e.g. Rel-17."),
                FieldInfo("spec", "str", True, "Spec number, e.g. 38.523-1."),
                FieldInfo("version", "str", True, "Spec version, e.g. 18.4.0."),
                FieldInfo("related_wis", "str", True, "Comma-joined WI acronyms."),
                FieldInfo("cr_num", "str", True, "Numeric CR id as a string, e.g. 3790."),
                FieldInfo("cr_pack", "str", True, "TSG CR pack value, e.g. RP-000123."),
                FieldInfo("tdoc_for", "str", True, "XLSX metadata: target of the TDoc."),
                FieldInfo("abstract", "text", True, "XLSX metadata: abstract text."),
                FieldInfo("secretary_remarks", "text", True, "XLSX metadata: secretary remarks."),
                FieldInfo("ls_to", "str", True, "XLSX metadata: liaison recipients."),
                FieldInfo("ls_cc", "str", True, "XLSX metadata: liaison CC list."),
                FieldInfo("original_ls", "text", True, "XLSX metadata: original liaison statement."),
            ),
        ),
        TableSchema(
            table="tdoc_cr_cover_page",
            fields=(
                FieldInfo("ftp_url", "str", False, "Immutable download URL path; primary key and row identity (one row per URL, not per tdoc_id)."),
                FieldInfo("tdoc_id", "str", False, "FK into tdocs.tdoc_id."),
                FieldInfo("spec", "str", True, "Spec number, e.g. 38.523-3."),
                FieldInfo("cr_num", "str", True, "Numeric CR id as a string, e.g. 3790."),
                FieldInfo("rev", "str", True, "CR revision normalised to a digit string; the cover '-' placeholder becomes 0."),
                FieldInfo("version", "str", True, "Current spec version, e.g. 18.4.0."),
                FieldInfo("title", "text", True, "CR title."),
                FieldInfo("source", "str", True, "Contents of 'Source to WG:'."),
                FieldInfo("tsg", "str", True, "Contents of 'Source to TSG:'."),
                FieldInfo("related_wis", "str", True, "Contents of 'Work item code:'."),
                FieldInfo("date", "date", True, "Cover-page date, YYYY-MM-DD."),
                FieldInfo("cr_cat", "str", True, "Single-letter category: F correction, B addition of feature, A correction in earlier release, C functional modification, D editorial.", ("F", "B", "A", "C", "D")),
                FieldInfo("release", "str", True, "Release label, e.g. Rel-18."),
                FieldInfo("reason_for_change", "text", True, "Reason-for-change cell text."),
                FieldInfo("consequences_if_not_approved", "text", True, "Consequences cell text."),
                FieldInfo("summary_of_change", "text", True, "Summary-of-change cell text."),
                FieldInfo("clauses_affected", "text", True, "Clauses-affected cell text."),
                FieldInfo("other_comments", "text", True, "Other-comments cell text."),
                FieldInfo("revision_history", "text", True, "Revision-history cell text."),
                FieldInfo("extracted_tdoc_id", "str", True, "What the header parser found in the document; may diverge from tdoc_id."),
            ),
        ),
        TableSchema(
            table="tdoc_cr_ttcn_details",
            fields=(
                FieldInfo("ftp_url", "str", False, "Immutable download URL path; primary key, shares identity with the cover-page row."),
                FieldInfo("tdoc_id", "str", False, "FK into tdocs.tdoc_id."),
                FieldInfo("testcase", "str", True, "TTCN overview: testcase field."),
                FieldInfo("ue", "str", True, "TTCN overview: UE field."),
                FieldInfo("ss", "str", True, "TTCN overview: SS field."),
                FieldInfo("ats_version", "str", True, "TTCN overview: ATS version."),
                FieldInfo("ttcn_release", "str", True, "TTCN release derived from the ATS version."),
                FieldInfo("test_suite", "str", True, "TTCN overview: test suite."),
                FieldInfo("required_changes", "gzip-json", True, "Gzip-compressed UTF-8 JSON list of correction dicts."),
                FieldInfo("changed_functions", "text", True, "Newline-delimited <module>.<function> pairs; '<module>.' when only the module is known, '.<function>' when only the function is known."),
            ),
        ),
        TableSchema(
            table="tdoc_cr_change_details",
            fields=(
                FieldInfo("ftp_url", "str", False, "Immutable download URL path; primary key (non-TTCN CRs only)."),
                FieldInfo("tdoc_id", "str", False, "FK into tdocs.tdoc_id."),
                FieldInfo("clauses", "text", True, "Sorted unique newline-delimited clause labels, e.g. 5.2.3."),
                FieldInfo("changes", "gzip-json", True, "Gzip-compressed JSON array of {clauses, text} change blocks."),
            ),
        ),
        TableSchema(
            table="tdoc_files",
            fields=(
                FieldInfo("id", "int", False, "Database-assigned primary key."),
                FieldInfo("tdoc_id", "str", False, "FK into tdocs.tdoc_id."),
                FieldInfo("type", "str", False, "Attachment kind: revision, review, or support.", ("revision", "review", "support")),
                FieldInfo("file", "str", False, "Bare attachment filename, e.g. R5s260001_MCC160Comments.zip."),
                FieldInfo("ftp_url", "str", False, "Relative download URL; unique upsert key."),
                FieldInfo("uploaded_date", "date", True, "Upload date from the FTP 'Last Modified' column."),
            ),
        ),
        TableSchema(
            table="tdoc_extracts",
            fields=(
                FieldInfo("ftp_url", "str", False, "Immutable download URL path; primary key."),
                FieldInfo("tdoc_id", "str", False, "FK into tdocs.tdoc_id."),
                FieldInfo("cache_file", "str", False, "Cache basename; paths rebuild as cache/zips/<file> and cache/markdown/<file>."),
                FieldInfo("doc_filename", "str", False, "Word document name inside the zip, e.g. R5s260009.docx."),
                FieldInfo("extracted_at", "datetime", False, "UTC timestamp of the extract."),
                FieldInfo("parser_version", "str", False, "Parser version, e.g. 1.0.0."),
            ),
        ),
    ),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_schema_info.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/models/schema_info.py tests/unit/test_schema_info.py
git commit -m "feat(schema): add tdoc table descriptors to registry"
```

---

### Task 3: Registry — spec + testcase tables, drift + categorical equality tests

**Files:**
- Modify: `src/doc3gpp/models/schema_info.py`
- Test: `tests/unit/test_schema_info.py`

**Interfaces:**
- Consumes: `FieldInfo`, `TableSchema` from Task 1.
- Produces: complete `RESOURCE_SCHEMAS` (all 6 resources, 14 tables) — consumed by Tasks 4, 5, 6.

- [ ] **Step 1: Write the failing tests (drift + categorical equality)**

```python
def test_registry_matches_orm_columns() -> None:
    """Every ORM column is described exactly once; no extras; nullability matches."""
    from doc3gpp.models.schema_info import RESOURCE_SCHEMAS
    from doc3gpp.storage.db.models import (
        MeetingORM,
        SpecORM,
        SpecVersionORM,
        TDocCrChangeDetailOrm,
        TDocCrDetailOrm,
        TDocCrTtcnDetailOrm,
        TDocExtractOrm,
        TDocFileORM,
        TDocORM,
        TestCaseORM,
        TestCaseSourceORM,
        TestCaseStatusORM,
        TsgORM,
        WiORM,
    )

    expected = {
        "tsgs": TsgORM,
        "meetings": MeetingORM,
        "tdocs": TDocORM,
        "tdoc_cr_cover_page": TDocCrDetailOrm,
        "tdoc_cr_ttcn_details": TDocCrTtcnDetailOrm,
        "tdoc_cr_change_details": TDocCrChangeDetailOrm,
        "tdoc_files": TDocFileORM,
        "tdoc_extracts": TDocExtractOrm,
        "wis": WiORM,
        "specs": SpecORM,
        "spec_versions": SpecVersionORM,
        "testcases": TestCaseORM,
        "testcase_status": TestCaseStatusORM,
        "testcase_sources": TestCaseSourceORM,
    }
    by_table = {}
    for tables in RESOURCE_SCHEMAS.values():
        for table in tables:
            by_table[table.table] = table
    assert sorted(by_table) == sorted(expected)
    for table_name, orm_cls in expected.items():
        columns = orm_cls.__table__.columns
        registry = {f.name: f for f in by_table[table_name].fields}
        assert sorted(registry) == sorted(columns.keys()), table_name
        for col_name, column in columns.items():
            assert registry[col_name].nullable == column.nullable, (table_name, col_name)


def test_categoricals_match_canonical_constants() -> None:
    from doc3gpp.cli import VALID_TESTCASE_GROUPS
    from doc3gpp.models.schema_info import RESOURCE_SCHEMAS
    from doc3gpp.models.tdoc_file import TDocFileTypes
    from doc3gpp.services.tsg_service import _DEFAULT_TSGS
    from doc3gpp.storage.repositories.testcase_sql import _PATH_RANK

    by_table = {}
    for tables in RESOURCE_SCHEMAS.values():
        for table in tables:
            for field in table.fields:
                by_table[(table.table, field.name)] = field
    assert by_table[("tsgs", "short_name")].values == tuple(t.short_name for t in _DEFAULT_TSGS)
    assert by_table[("testcases", "group")].values == tuple(VALID_TESTCASE_GROUPS)
    assert by_table[("testcase_status", "path")].values == tuple(_PATH_RANK)
    assert by_table[("tdoc_files", "type")].values == tuple(sorted(TDocFileTypes))


def test_type_vocabulary() -> None:
    from doc3gpp.models.schema_info import FIELD_TYPES, RESOURCE_SCHEMAS

    for tables in RESOURCE_SCHEMAS.values():
        for table in tables:
            for field in table.fields:
                assert field.type in FIELD_TYPES, (table.table, field.name)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_schema_info.py -v`
Expected: FAIL — `KeyError: 'specs'` (or `KeyError: 'testcases'`) since only tsg/meeting/tdoc/wi exist.

- [ ] **Step 3: Add module constants + `spec` and `testcase` entries**

Add these constants after `TSG_SHORT_NAMES`:

```python
# Duplicates ``cli.py::VALID_TESTCASE_GROUPS`` (models/ cannot import
# the CLI layer); equality locked by ``test_schema_info.py``.
TESTCASE_GROUPS: tuple[str, ...] = ("5G", "LTE", "IMS", "UTRA", "POS", "MCX")

# Duplicates ``storage/repositories/testcase_sql.py::_PATH_RANK``;
# single-path groups store the literal ``'default'``.
TESTCASE_PATHS: tuple[str, ...] = (
    "FR1", "FR2", "FR1+FR2", "FDD", "TDD",
    "IPCAN-4G", "EUTRA", "IPCAN-5G", "NR5GC", "default",
)

# Duplicates ``models/tdoc_file.py::TDocFileTypes``.
TDOC_FILE_TYPES: tuple[str, ...] = ("revision", "review", "support")

# CR category letters from the cover page (see ``models/tdoc_cr.py``).
CR_CATEGORIES: tuple[str, ...] = ("F", "B", "A", "C", "D")

# Spec types from the DynaReport detail page (see ``parsers/spec_parser.py``).
SPEC_TYPES: tuple[str, ...] = ("TS", "TR")
```

Then use `TDOC_FILE_TYPES` and `CR_CATEGORIES` in the Task 2 entry (replace the inline tuples), and append these entries to `RESOURCE_SCHEMAS` (after `"wi"`):

```python
    "spec": (
        TableSchema(
            table="specs",
            fields=(
                FieldInfo("spec_id", "str", False, "Full dotted spec identity, e.g. 36.579-5; primary key."),
                FieldInfo("type", "str", True, "TS or TR.", SPEC_TYPES),
                FieldInfo("title", "text", True, "Full spec title from the list page."),
                FieldInfo("status", "str", True, "Free-form status, e.g. Under change control."),
                FieldInfo("radio_tech", "str", True, "Comma-joined ticked radio technologies, e.g. LTE,NR."),
                FieldInfo("initial_release", "str", True, "Normalised release marker, e.g. Rel-20, R99."),
                FieldInfo("tsg", "str", True, "Owning TSG; FK into tsgs.short_name.", TSG_SHORT_NAMES),
                FieldInfo("wis", "str", True, "Comma-joined related-WI acronyms; point-in-time snapshot."),
                FieldInfo("rapporteurs", "str", True, "Comma-joined company names, e.g. Ericsson LM."),
                FieldInfo("last_synced_at", "datetime", True, "UTC timestamp of the last successful detail sync, else null."),
            ),
        ),
        TableSchema(
            table="spec_versions",
            fields=(
                FieldInfo("spec_id", "str", False, "FK into specs.spec_id; first half of the composite PK."),
                FieldInfo("version", "str", False, "Version string, e.g. 18.3.0; second half of the composite PK."),
                FieldInfo("ftp_url", "str", False, "Absolute 3GPP FTP URL of the version zip."),
                FieldInfo("release", "str", True, "Canonical release marker: draft, pre-release, or Rel-N."),
                FieldInfo("meeting_id", "int", True, "Numeric 3GPP meeting id."),
                FieldInfo("meeting_name", "str", True, "Meeting name, e.g. RAN#108."),
                FieldInfo("upload_date", "date", True, "Upload date from the version row, ISO-8601."),
                FieldInfo("version_id", "int", True, "?versionId= key used to build the CR list URL."),
                FieldInfo("pdf_url", "str", True, "ETSI 'download as PDF' link."),
                FieldInfo("crs", "text", True, "Comma-joined tdoc_ids from the CR list page."),
            ),
        ),
    ),
    "testcase": (
        TableSchema(
            table="testcases",
            fields=(
                FieldInfo("testcase_id", "str", False, "Testcase id; first half of the composite PK."),
                FieldInfo("group", "str", False, "Testcase group; second half of the composite PK.", TESTCASE_GROUPS),
                FieldInfo("title", "text", True, "Testcase title."),
                FieldInfo("ats", "str", True, "Abstract test suite identifier."),
                FieldInfo("feature", "str", True, "Feature name."),
                FieldInfo("release", "str", True, "Release marker, e.g. Rel-17."),
                FieldInfo("wis", "str", True, "Comma-joined related WIs."),
                FieldInfo("spec", "str", True, "Spec number; fixed 38.523-1 for 5G and 36.523-1 for LTE, else the row 'part of' value verbatim."),
            ),
        ),
        TableSchema(
            table="testcase_status",
            fields=(
                FieldInfo("testcase_id", "str", False, "FK half of the composite PK."),
                FieldInfo("group", "str", False, "FK half of the composite PK.", TESTCASE_GROUPS),
                FieldInfo("path", "str", False, "Radio path; third third of the composite PK. Single-path groups store the literal 'default'.", TESTCASE_PATHS),
                FieldInfo("gcf_ptcrb", "text", True, "GCF/PTCRB status, e.g. Approved."),
                FieldInfo("ttcn_status", "text", True, "TTCN status, e.g. Approved."),
            ),
        ),
        TableSchema(
            table="testcase_sources",
            fields=(
                FieldInfo("filename", "str", False, "Status-file name, e.g. TTCN CR Agreement Status 2024-wk32.zip; primary key and sync skip key."),
                FieldInfo("year", "int", False, "Status-file year."),
                FieldInfo("week", "int", False, "Status-file week number."),
                FieldInfo("revision", "int", False, "Status-file revision."),
                FieldInfo("downloaded_at", "datetime", True, "UTC download timestamp, else null."),
                FieldInfo("parsed_at", "datetime", True, "UTC parse timestamp, else null."),
                FieldInfo("testcase_count", "int", False, "Number of testcase headers parsed from the file."),
                FieldInfo("status_count", "int", False, "Number of status rows parsed from the file."),
            ),
        ),
    ),
```

Note: the transient parser field `SpecVersion.wki_id` is deliberately excluded (not persisted — no ORM column, so the drift test enforces the exclusion).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_schema_info.py -v`
Expected: PASS (all tests, including the ORM drift test).

- [ ] **Step 5: Run lint**

Run: `ruff check src/doc3gpp/models/schema_info.py tests/unit/test_schema_info.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/models/schema_info.py tests/unit/test_schema_info.py
git commit -m "feat(schema): add spec and testcase descriptors plus drift tests"
```

---

### Task 4: CLI — six `schema` commands

**Files:**
- Modify: `src/doc3gpp/cli.py`
- Test: `tests/unit/test_schema_info.py` (append CLI tests)

**Interfaces:**
- Consumes: `schema_payload`, `SCHEMA_FIELDS` from Task 1; `_resolve_format`, `_resolve_compact`, `_emit_records`, `_dump_show_json` (existing in `cli.py`).
- Produces: `doc3gpp {tsg,meeting,tdoc,wi,spec,testcase} schema` — consumed by parity tests in Task 6.

- [ ] **Step 1: Write the failing CLI tests**

```python
def test_cli_schema_json_all_resources() -> None:
    from typer.testing import CliRunner

    from doc3gpp.cli import app
    from doc3gpp.models.schema_info import schema_payload

    runner = CliRunner()
    for resource in ("tsg", "meeting", "tdoc", "wi", "spec", "testcase"):
        result = runner.invoke(app, [resource, "schema", "--format", "json"])
        assert result.exit_code == 0, (resource, result.output)
        import json

        assert json.loads(result.output) == schema_payload(resource), resource


def test_cli_schema_table_marks_nullable() -> None:
    from typer.testing import CliRunner

    from doc3gpp.cli import app

    result = CliRunner().invoke(app, ["wi", "schema"])
    assert result.exit_code == 0, result.output
    assert "tsg_short" in result.output
    assert "no" in result.output.splitlines()[0] or True
    header_rows = [line for line in result.output.splitlines() if "short_name" in line]
    assert header_rows and header_rows[0].split("\t")[3] == "no"
    nullable_rows = [line for line in result.output.splitlines() if "\turl\t" in line]
    assert nullable_rows == []
```

Hmm — the second test above is sloppy (the `or True` line). Replace with a precise version: `tsg schema` table output must render `nullable` as `yes`/`no` strings. Write it cleanly:

```python
def test_cli_schema_table_nullable_yes_no() -> None:
    from typer.testing import CliRunner

    from doc3gpp.cli import app

    result = CliRunner().invoke(app, ["tsg", "schema"])
    assert result.exit_code == 0, result.output
    rows = [line.split("\t") for line in result.output.splitlines()]
    by_field = {cells[1]: cells for cells in rows}
    assert by_field["short_name"][3] == "no"
    assert by_field["url"][3] == "yes"
    assert by_field["short_name"][5].startswith("RP,R1")
    assert by_field["description"][5] == "-"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_schema_info.py -k cli_schema -v`
Expected: FAIL — `No such command 'schema'` (exit code 2).

- [ ] **Step 3: Add the `_emit_schema` helper + six commands to `cli.py`**

Place the helper right after `_emit_records` (line ~609). It needs `schema_payload`/`SCHEMA_FIELDS` — import at top with the other model imports: `from doc3gpp.models.schema_info import SCHEMA_FIELDS, schema_payload`.

```python
def _emit_schema(
    resource: str,
    fmt: str,
    output: str | None,
    *,
    compact: bool = False,
) -> None:
    """Emit the static field registry for ``resource`` in the chosen format.

    JSON goes through :func:`_dump_show_json` so ``nullable`` stays a
    real bool (matching REST ``?format=json`` and the MCP tools
    byte-for-byte). Table/markdown go through :func:`_emit_records`
    with ``nullable`` rendered as ``yes``/``no``. No DB access, no
    filters — the whole descriptor fits in one response.
    """
    payload = schema_payload(resource)
    if fmt == "json":
        _dump_show_json(payload, output, compact=compact)
        return
    rows = [
        [
            str(row["table"]),
            str(row["field"]),
            str(row["type"]),
            "yes" if row["nullable"] else "no",
            str(row["description"]),
            str(row["values"]),
        ]
        for row in payload
    ]
    _emit_records(
        rows=rows,
        fields=SCHEMA_FIELDS,
        fmt=fmt,
        output=output,
        no_records_msg=f"No schema for {resource}",
        compact=compact,
    )
```

One command per Typer app, placed directly after that app's `list` command. All six share this exact flag set (mirror `tsg_list` plus `--compact`):

```python
@tsg_app.command("schema")
def tsg_schema(
    fmt: str | None = typer.Option(
        None,
        "--format",
        help="Output format: table (default, tab-separated), json, or markdown.",
    ),
    output: str | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Write results to FILE instead of stdout. Pass '-' for stdout.",
    ),
    compact: bool = typer.Option(
        False,
        "--compact",
        help="Strip output formatting: JSON drops indent and operator-space. No-op for ``table``.",
    ),
) -> None:
    """Describe every column of the tsgs table (meaning, format, possible values)."""
    settings = get_settings()
    fmt = _resolve_format(fmt, default=settings.output.format)
    resolved_compact = _resolve_compact(compact)
    logger.info("Describing tsg schema")
    _emit_schema("tsg", fmt, output, compact=resolved_compact)
```

Repeat for `meeting_schema` (`@meeting_app.command("schema")`, `_emit_schema("meeting", ...)`), `tdoc_schema`, `wi_schema`, `spec_schema`, `testcase_schema` with matching docstrings ("Describe every column of the meetings table", "…of the tdocs, tdoc_cr_cover_page, tdoc_cr_ttcn_details, tdoc_cr_change_details, tdoc_files and tdoc_extracts tables", "…of the wis table", "…of the specs and spec_versions tables", "…of the testcases, testcase_status and testcase_sources tables (separate testcase sqlite file)").

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_schema_info.py -v`
Expected: PASS.

- [ ] **Step 5: Manual smoke of all six + compact**

Run: `python -m doc3gpp tsg schema | head -8 && python -m doc3gpp tdoc schema --format json --compact | head -c 300`
Expected: tab-separated rows; single-line JSON starting with `[{"table":"tdocs",...`.

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/cli.py tests/unit/test_schema_info.py
git commit -m "feat(schema): add schema subcommand to all six CLI groups"
```

---

### Task 5: REST routes + shared HTML templates

**Files:**
- Modify: `src/doc3gpp/web/routes/{tsgs,meetings,tdocs,wis,specs,testcases}.py`
- Create: `src/doc3gpp/web/templates/schema.html`, `src/doc3gpp/web/templates/partials/schema_results.html`
- Test: `tests/integration/test_schema_parity.py` (new file; HTML smoke asserts live here)

**Interfaces:**
- Consumes: `schema_payload`, `RESOURCE_SCHEMAS` from Task 1.
- Produces: `GET /tsgs/schema`, `/meetings/schema`, `/tdocs/schema`, `/wis/schema`, `/specs/schema`, `/testcases/schema` (HTML default, `?format=json` verbatim CLI payload) — consumed by Task 6 parity tests.

Route ordering constraint (load-bearing): FastAPI matches routes in registration order, so each `GET /schema` route MUST be defined **before** that router's `/{param}` route (`/tdocs/{tdoc_id}`, `/specs/{spec_id}`, `/testcases/{testcase_id}`, `/meetings/{meeting_id}`, `/tsgs/{short_name}`) or `"schema"` is swallowed as a param. `wis.py` has no param route but gets the same placement for consistency.

- [ ] **Step 1: Write the failing route test**

```python
"""Parity + HTML tests for the resource `schema` surfaces."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from tests.integration.test_web_end_to_end import app_with_deps  # noqa: F401  (pytest fixture re-export)


def test_rest_schema_json_matches_cli_compact(app_with_deps, sqlite_env) -> None:
    """``GET /<r>/schema?format=json`` bytes equal CLI ``schema --format json --compact``."""
    from doc3gpp.cli import app as cli_app

    app, _ = app_with_deps
    routes = {
        "tsg": "/tsgs/schema",
        "meeting": "/meetings/schema",
        "tdoc": "/tdocs/schema",
        "wi": "/wis/schema",
        "spec": "/specs/schema",
        "testcase": "/testcases/schema",
    }
    runner = CliRunner()
    with TestClient(app) as client:
        for resource, route in routes.items():
            http_resp = client.get(route, params={"format": "json"})
            assert http_resp.status_code == 200, (resource, http_resp.text)
            cli_result = runner.invoke([resource, "schema", "--format", "json", "--compact"])
            assert cli_result.exit_code == 0, (resource, cli_result.output)
            assert http_resp.content.decode("utf-8") == cli_result.output, resource
            assert isinstance(json.loads(http_resp.content), list)


def test_schema_html_and_partial(app_with_deps, sqlite_env) -> None:
    app, _ = app_with_deps
    with TestClient(app) as client:
        full = client.get("/tdocs/schema")
        assert full.status_code == 200
        assert "<!DOCTYPE" in full.text
        assert "tdoc_cr_cover_page" in full.text
        assert "changed_functions" in full.text
        partial = client.get("/tdocs/schema", headers={"HX-Request": "true"})
        assert partial.status_code == 200
        assert 'id="results"' in partial.text
        assert "<!DOCTYPE" not in partial.text
```

Check `tests/integration/test_web_end_to_end.py::app_with_deps` (local fixture, line 44) and `tests/conftest.py::sqlite_env` (global fixture): the plan's test file re-exports `app_with_deps` via an explicit import (see test code above) — no fixture move needed. The `sqlite_env` global fixture needs no import.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_schema_parity.py -v`
Expected: FAIL — 404 on `/tsgs/schema` (or `"schema"` swallowed by `/{short_name}` → 404 TSGNotFoundError envelope).

- [ ] **Step 3: Create the two shared templates**

`src/doc3gpp/web/templates/partials/schema_results.html`:

```html
<div id="results">
  {% for table in tables %}
    <h2><code>{{ table.table }}</code></h2>
    <table class="grid">
      <thead>
        <tr>
          <th>Field</th>
          <th>Type</th>
          <th>Nullable</th>
          <th>Description</th>
          <th>Possible values</th>
        </tr>
      </thead>
      <tbody>
        {% for field in table.fields %}
          <tr>
            <td><code>{{ field.name }}</code></td>
            <td><code>{{ field.type }}</code></td>
            <td>{{ 'yes' if field.nullable else 'no' }}</td>
            <td>{{ field.description }}</td>
            <td>{% if field.values %}<code>{{ field.values | join(',') }}</code>{% else %}-{% endif %}</td>
          </tr>
        {% endfor %}
      </tbody>
    </table>
  {% endfor %}
</div>
```

`src/doc3gpp/web/templates/schema.html`:

```html
{% extends "base.html" %}
{% block title %}doc3gpp · {{ resource }} schema{% endblock %}
{% block content %}
  <h1>{{ resource }} schema</h1>
  <p class="meta">{{ total }} fields across {{ tables | length }} tables. No database access; static descriptors.</p>
  {% include "partials/schema_results.html" %}
{% endblock %}
```

- [ ] **Step 4: Add one route per router (before the param route)**

Pattern (adapt imports per file; `tsgs.py` shown — others identical except resource key, nav id, and no service dependency):

```python
@router.get("/schema", include_in_schema=False)
async def tsg_schema(
    request: Request,
    format: str | None = Query(default=None, alias="format"),
    pending_jobs: int = Depends(get_pending_jobs),
) -> Any:
    """Render ``schema.html`` or the CLI-identical JSON field descriptors.

    ``?format=json`` returns the same payload as
    ``doc3gpp tsg schema --format json``: a bare array of
    ``{table, field, type, nullable, description, values}`` rows from
    :func:`doc3gpp.models.schema_info.schema_payload`. No DB access.
    """
    from doc3gpp.models.schema_info import RESOURCE_SCHEMAS, schema_payload
    from doc3gpp.web.filters import is_htmx_request

    if format == "json":
        return JSONResponse(content=schema_payload("tsg"))
    tables = RESOURCE_SCHEMAS["tsg"]
    template_name = (
        "partials/schema_results.html" if is_htmx_request(request) else "schema.html"
    )
    return templates.TemplateResponse(
        request=request,
        name=template_name,
        context={
            "active_nav": "tsgs",
            "resource": "tsg",
            "tables": tables,
            "total": sum(len(t.fields) for t in tables),
            "pending_jobs": pending_jobs,
        },
    )
```

`active_nav` per router: tsgs/meetings/tdocs/wis/specs/testcases (match `base.html` nav ids). Follow each file's existing import style (top-level imports preferred; check whether `is_htmx_request` is already imported — it is in meetings/wis/specs/testcases; add to tsgs/tdocs imports).

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/integration/test_schema_parity.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/web/routes/ src/doc3gpp/web/templates/schema.html src/doc3gpp/web/templates/partials/schema_results.html tests/integration/test_schema_parity.py
git commit -m "feat(schema): add REST schema routes with shared HTML templates"
```

---

### Task 6: MCP tools + full parity lock

**Files:**
- Modify: `src/doc3gpp/web/mcp_server.py`
- Test: `tests/integration/test_schema_parity.py` (append), `tests/integration/test_mcp_end_to_end.py` (extend read-tools parity cases if trivially possible)

**Interfaces:**
- Consumes: `schema_payload` from Task 1; `_to_json`, `_mcp_error_guard` (existing in `mcp_server.py`).
- Produces: `get_tsg_schema`, `get_meeting_schema`, `get_tdoc_schema`, `get_wi_schema`, `get_spec_schema`, `get_testcase_schema` — byte-identical to REST `?format=json`.

- [ ] **Step 1: Write the failing MCP parity test**

Append to `tests/integration/test_schema_parity.py`:

```python
def test_mcp_schema_parity_with_http_json(sqlite_env) -> None:
    """MCP ``get_*_schema`` payloads match the HTTP ``?format=json`` bytes."""
    import asyncio

    from fastapi.testclient import TestClient

    from doc3gpp.settings.schema import CacheSettings, MCPSettings, ServerSettings, Settings
    from doc3gpp.storage.db.session import get_engine
    from doc3gpp.web.app import build_app

    from tests.integration.test_mcp_end_to_end import _seed_corpus, _state_and_server

    _state_and_server()
    _seed_corpus()
    state, server = _state_and_server()
    app = build_app(
        Settings(
            server=ServerSettings(enabled=True, port=8765),
            mcp=MCPSettings(enabled=True),
            cache=CacheSettings(dir=state.settings.cache.dir),
        )
    )
    cases = [
        ("get_tsg_schema", "/tsgs/schema?format=json"),
        ("get_meeting_schema", "/meetings/schema?format=json"),
        ("get_tdoc_schema", "/tdocs/schema?format=json"),
        ("get_wi_schema", "/wis/schema?format=json"),
        ("get_spec_schema", "/specs/schema?format=json"),
        ("get_testcase_schema", "/testcases/schema?format=json"),
    ]

    async def call(name: str):
        result = await server.call_tool(name, {})
        assert result.is_error is False, result
        return result.content[0].text

    with TestClient(app) as client:
        for tool_name, route in cases:
            mcp_bytes = asyncio.run(call(tool_name))
            http_resp = client.get(route)
            assert http_resp.status_code == 200, (tool_name, http_resp.text)
            assert mcp_bytes == http_resp.content.decode("utf-8"), tool_name

    get_engine.cache_clear()
    del state.engine
```

Verify `_state_and_server` / `_seed_corpus` are importable from `tests.integration.test_mcp_end_to_end` (they are module-level helpers there per the existing parity test at line 458); if import causes side effects, inline a minimal local copy instead.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_schema_parity.py::test_mcp_schema_parity_with_http_json -v`
Expected: FAIL — unknown tool `get_tsg_schema`.

- [ ] **Step 3: Register the six MCP tools**

Place after the `get_testcase` tool (before the `# ---- Search ---` block), following the existing `list_tsgs` thin-wrapper style:

```python
    @server.tool(name="get_tsg_schema", description="Describe every column of the tsgs table: meaning, format, and possible values for categorical fields.")
    @_mcp_error_guard
    def get_tsg_schema() -> str:
        from doc3gpp.models.schema_info import schema_payload

        return _to_json(schema_payload("tsg"))

    @server.tool(name="get_meeting_schema", description="Describe every column of the meetings table: meaning, format, and possible values for categorical fields.")
    @_mcp_error_guard
    def get_meeting_schema() -> str:
        from doc3gpp.models.schema_info import schema_payload

        return _to_json(schema_payload("meeting"))

    @server.tool(name="get_tdoc_schema", description="Describe every column of the tdocs, tdoc_cr_cover_page, tdoc_cr_ttcn_details, tdoc_cr_change_details, tdoc_files and tdoc_extracts tables.")
    @_mcp_error_guard
    def get_tdoc_schema() -> str:
        from doc3gpp.models.schema_info import schema_payload

        return _to_json(schema_payload("tdoc"))

    @server.tool(name="get_wi_schema", description="Describe every column of the wis table: meaning, format, and possible values for categorical fields.")
    @_mcp_error_guard
    def get_wi_schema() -> str:
        from doc3gpp.models.schema_info import schema_payload

        return _to_json(schema_payload("wi"))

    @server.tool(name="get_spec_schema", description="Describe every column of the specs and spec_versions tables.")
    @_mcp_error_guard
    def get_spec_schema() -> str:
        from doc3gpp.models.schema_info import schema_payload

        return _to_json(schema_payload("spec"))

    @server.tool(name="get_testcase_schema", description="Describe every column of the testcases, testcase_status and testcase_sources tables (separate testcase sqlite file).")
    @_mcp_error_guard
    def get_testcase_schema() -> str:
        from doc3gpp.models.schema_info import schema_payload

        return _to_json(schema_payload("testcase"))
```

`_to_json` uses compact separators + `ensure_ascii=False`, identical to Starlette `JSONResponse` — the same guarantee the existing `test_read_tools_parity_with_http_json` test locks.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/integration/test_schema_parity.py tests/integration/test_mcp_end_to_end.py -v`
Expected: PASS (no regressions in existing MCP parity).

- [ ] **Step 5: Run lint**

Run: `ruff check src/doc3gpp/web/mcp_server.py src/doc3gpp/cli.py src/doc3gpp/web/routes/ tests/`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/web/mcp_server.py tests/integration/test_schema_parity.py
git commit -m "feat(schema): add get_*_schema MCP tools with parity lock"
```

---

### Task 7: Docs sync + full offline verification

**Files:**
- Modify: `docs/cli.md`, `docs/web-server.md`, `docs/code-map.md`, `README.md`, `AGENTS.md`

**Interfaces:**
- Consumes: everything above. No new code.

- [ ] **Step 1: Update `docs/cli.md`**

Add a `schema` subsection to each of the six resource sections (or one shared section if the file groups commands that way — check first). Document: no filters, `--format table|json|markdown` (default table), `--output/-o`, `--compact`; flat row shape `{table, field, type, nullable, description, values}`; `nullable` is `yes`/`no` in table/markdown and a bool in JSON; `values` comma-joined, `"-"` when free-form. Example:

```
doc3gpp tsg schema --format json
```

- [ ] **Step 2: Update `docs/web-server.md`**

Document the six `GET /<resources>/schema` routes (HTML default grouped by table; `?format=json` verbatim CLI payload) and the six `get_*_schema` MCP tools (no params, byte-identical to REST JSON).

- [ ] **Step 3: Update `docs/code-map.md`, `README.md`, `AGENTS.md`**

Add `models/schema_info.py` to the symbol→file map; add one inventory line per doc's existing style for the new `schema` commands (check how `list`/`show` are inventoried and mirror it).

- [ ] **Step 4: Run the full offline suite + lint**

Run: `ruff check .`
Expected: clean.

Run: `./scripts/test_sqlite.sh`
Expected: all green (falls back to sequential when xdist is absent).

- [ ] **Step 5: Commit**

```bash
git add docs/ README.md AGENTS.md
git commit -m "docs(schema): document schema surfaces on CLI, REST, MCP"
```

---

## Self-Review

1. **Spec coverage:** registry module (§1) → Tasks 1–3; six CLI commands (§3) → Task 4; six REST routes + shared template (§4) → Task 5; six MCP tools (§5) → Task 6; registry/drift/categorical + CLI + parity tests (§Testing) → Tasks 1–3, 4, 5–6; docs list (§Docs) → Task 7. Open decisions from the spec (shared-vs-per-resource templates → shared `schema.html`; column widths → reuse `_emit_records` unchanged, long descriptions flow naturally) are resolved inline above.
2. **Placeholder scan:** every step carries exact code (full 133-field registry text, exact route/tool/test bodies). No TBD/TODO/"similar to".
3. **Type consistency:** `schema_payload()` returns `list[dict[str, object]]`; CLI `_emit_schema` stringifies cells for `_emit_records` and passes the payload straight to `_dump_show_json(payload, output, compact=...)` — matches its `dict | list` parameter. REST `JSONResponse(content=schema_payload(...))` and MCP `_to_json(schema_payload(...))` share the identical object, so key order (`table, field, type, nullable, description, values`) and compact bytes agree on all three surfaces. `RESOURCE_SCHEMAS` values are tuples; Jinja iterates tuples and accesses frozen-dataclass attrs fine.
4. **Known count deltas vs the design doc:** `tdocs` has 24 columns (not 23) and `tdoc_cr_cover_page` has 21 (not 19) per `storage/db/models.py` — the plan uses the ORM counts and the drift test enforces them. `SpecVersion.wki_id` stays excluded (no ORM column; drift test locks this).
5. **Route-ordering hazard** (`/schema` vs `/{param}`) is called out explicitly in Task 5 — the most likely implementation bug in this plan.

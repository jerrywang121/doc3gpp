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
    # Brief/plan claim 21 for tdoc_cr_cover_page, but the verbatim
    # entry lists 20 and the ORM (TDocCrDetailOrm) has 20 columns —
    # registry matches ORM; see task-2 report.
    assert counts == {
        "tdocs": 24,
        "tdoc_cr_cover_page": 20,
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
    # Brief verbatim was `...values == tuple(sorted(TDocFileTypes))`, but
    # sorted(TDocFileTypes) is ("review", "revision", "support") ("review" <
    # "revision": 'e' < 's' at index 4) while the registry (and Task 2's
    # locked "revision,review,support" payload) uses logical order
    # ("revision", "review", "support"). Compare sorted-vs-sorted so the
    # test still locks set equality without forcing alphabetical output.
    assert sorted(by_table[("tdoc_files", "type")].values or ()) == sorted(TDocFileTypes)


def test_type_vocabulary() -> None:
    from doc3gpp.models.schema_info import FIELD_TYPES, RESOURCE_SCHEMAS

    for tables in RESOURCE_SCHEMAS.values():
        for table in tables:
            for field in table.fields:
                assert field.type in FIELD_TYPES, (table.table, field.name)


def test_cli_schema_json_all_resources() -> None:
    import json

    from typer.testing import CliRunner

    from doc3gpp.cli import app
    from doc3gpp.models.schema_info import schema_payload

    runner = CliRunner()
    for resource in ("tsg", "meeting", "tdoc", "wi", "spec", "testcase"):
        result = runner.invoke(app, [resource, "schema", "--format", "json"])
        assert result.exit_code == 0, (resource, result.output)
        assert json.loads(result.output) == schema_payload(resource), resource


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

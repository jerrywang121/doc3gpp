"""Registry tests for the resource `schema` surfaces."""

from doc3gpp.models.schema_info import (
    RESOURCE_SCHEMAS,
    SCHEMA_FIELDS,
    schema_payload,
)


def test_schema_field_order_and_keys() -> None:
    assert SCHEMA_FIELDS == ["table", "field", "type", "nullable", "description", "values"]
    # Task 2 scope: tsg/meeting/tdoc/wi. Task 3 extends the key list to
    # ["meeting", "spec", "tdoc", "testcase", "tsg", "wi"] as each
    # resource entry lands (brief Step 1 shows the end-state assertion).
    assert sorted(RESOURCE_SCHEMAS) == ["meeting", "tdoc", "tsg", "wi"]


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

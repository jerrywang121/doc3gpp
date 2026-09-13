"""Registry tests for the resource `schema` surfaces."""

from doc3gpp.models.schema_info import (
    RESOURCE_SCHEMAS,
    SCHEMA_FIELDS,
    schema_payload,
)


def test_schema_field_order_and_keys() -> None:
    assert SCHEMA_FIELDS == ["table", "field", "type", "nullable", "description", "values"]
    # Task 1 scope: tsg/meeting/wi only. Tasks 2-3 extend the key list to
    # ["meeting", "spec", "tdoc", "testcase", "tsg", "wi"] as each
    # resource entry lands (brief Step 1 shows the end-state assertion).
    assert sorted(RESOURCE_SCHEMAS) == ["meeting", "tsg", "wi"]


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

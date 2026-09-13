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

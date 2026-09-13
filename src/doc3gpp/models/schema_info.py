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

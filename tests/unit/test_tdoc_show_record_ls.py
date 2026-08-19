"""Unit tests for the LS sidecar on the tdoc show DTOs and renderers.

Task 11: ``TDocShowRecord`` / ``TDocShowRecordByUrl`` gain the
``ls: TDocLSDetails | None`` field (omit-when-null), ``TDocShowRepos``
gains the LS repository, and the CLI renderers emit an ``ls`` block in
JSON / Markdown / table modes.
"""
from __future__ import annotations

import io
import json

from doc3gpp.models.tdoc import TDoc
from doc3gpp.models.tdoc_ls import TDocLSDetails
from doc3gpp.models.tdoc_show import TDocShowRecord, TDocShowRecordByUrl


def _make_ls_details(**overrides: object) -> TDocLSDetails:
    base = dict(
        ftp_url="tsg/ls/R5-240001.doc",
        tdoc_id="R5-240001",
        variant="3gpp",
        title="LS on foo",
        response_to="LS R5-234567 on foo from RAN WG3",
        release="Rel-17",
        work_item_name="5G_eHealth",
        work_item_code="WI-123456",
        source="3GPP TSG RAN WG2",
        to_groups="RAN WG3\nRAN WG4",
        cc_groups="SA WG2",
        attachments=(
            {"doc_number": "TR 38.901 v0.1.0 [draft]", "description": "draft TR"},
            {"doc_number": "TS 38.300 v17.1.0", "description": ""},
        ),
    )
    base.update(overrides)
    return TDocLSDetails(**base)


def test_show_record_carries_ls_field() -> None:
    rec = TDocShowRecord(tdoc=None, cover=None, ttcn=None, changes=None, files=())
    assert rec.ls is None


def test_show_record_ls_can_be_set() -> None:
    details = TDocLSDetails(
        tdoc_id="R5-240001", ftp_url="tsg/ls/R5-240001.doc",
        variant="3gpp", title="LS on foo",
    )
    rec = TDocShowRecord(tdoc=None, cover=None, ttcn=None, changes=None, files=(), ls=details)
    assert rec.ls.title == "LS on foo"


def test_show_record_by_url_carries_ls_field() -> None:
    rec = TDocShowRecordByUrl(ftp_url="tsg/ls/R5-240001.doc")
    assert rec.ls is None


# ---------------------------------------------------------------------------
# JSON renderers
# ---------------------------------------------------------------------------


def test_show_json_includes_ls_block() -> None:
    """``_render_tdoc_show_json`` emits the ``ls`` block with every
    dataclass field, including the attachments array."""
    from doc3gpp.cli import _render_tdoc_show_json

    record = TDocShowRecord(
        tdoc=TDoc(tdoc_id="R5-240001", ftp_url="tsg/ls/R5-240001.doc"),
        ls=_make_ls_details(),
    )
    stream = io.StringIO()
    _render_tdoc_show_json(record, stream)
    payload = json.loads(stream.getvalue())
    assert payload["ls"]["title"] == "LS on foo"
    assert payload["ls"]["to_groups"] == "RAN WG3\nRAN WG4"
    assert payload["ls"]["attachments"] == [
        {"doc_number": "TR 38.901 v0.1.0 [draft]", "description": "draft TR"},
        {"doc_number": "TS 38.300 v17.1.0", "description": ""},
    ]


def test_show_json_omits_ls_when_null() -> None:
    """The ``ls`` key is omitted (not null) when no sidecar row exists."""
    from doc3gpp.cli import _render_tdoc_show_json

    record = TDocShowRecord(tdoc=TDoc(tdoc_id="R5-240001", ftp_url="x"))
    stream = io.StringIO()
    _render_tdoc_show_json(record, stream)
    assert "ls" not in json.loads(stream.getvalue())


def test_show_by_url_json_includes_ls_block() -> None:
    """``_render_tdoc_show_by_url_json`` emits the ``ls`` block anchored
    on the URL."""
    from doc3gpp.cli import _render_tdoc_show_by_url_json

    record = TDocShowRecordByUrl(
        ftp_url="tsg/ls/R5-240001.doc",
        ls=_make_ls_details(),
    )
    stream = io.StringIO()
    _render_tdoc_show_by_url_json(record, stream)
    payload = json.loads(stream.getvalue())
    assert payload["ftp_url"] == "tsg/ls/R5-240001.doc"
    assert payload["ls"]["title"] == "LS on foo"


def test_ls_block_matches_web_to_jsonable() -> None:
    """The CLI ``_build_show_payload`` ``ls`` block is byte-equivalent to
    the web/MCP ``to_jsonable`` envelope (the Tasks 12-13 shape)."""
    from doc3gpp.cli import _build_show_payload
    from doc3gpp.web.render import to_jsonable

    record = TDocShowRecord(
        tdoc=TDoc(tdoc_id="R5-240001", ftp_url="tsg/ls/R5-240001.doc"),
        ls=_make_ls_details(),
    )
    cli_bytes = json.dumps(_build_show_payload(record), sort_keys=True, default=str)
    http_bytes = json.dumps(to_jsonable(record), sort_keys=True, default=str)
    assert cli_bytes == http_bytes


# ---------------------------------------------------------------------------
# Markdown renderers
# ---------------------------------------------------------------------------


def test_show_markdown_full_emits_ls_section() -> None:
    """Full-mode markdown renders ``## LS`` with ``- **key**: value``
    bullets; multi-line group fields are comma-joined; attachments are
    bulleted."""
    from doc3gpp.cli import _render_tdoc_show_markdown

    record = TDocShowRecord(
        tdoc=TDoc(tdoc_id="R5-240001", ftp_url="x"),
        ls=_make_ls_details(),
    )
    stream = io.StringIO()
    _render_tdoc_show_markdown(record, stream)
    text = stream.getvalue()
    assert "## LS" in text
    assert "- **title**: LS on foo" in text
    assert "- **to_groups**: RAN WG3, RAN WG4" in text
    assert "- **attachments**:" in text
    assert "  * TR 38.901 v0.1.0 [draft] — draft TR" in text
    assert "  * TS 38.300 v17.1.0" in text


def test_show_markdown_compact_emits_ls_key_value_lines() -> None:
    """Compact markdown drops decorators and emits ``key: value`` lines
    for the LS block; attachments become a single-line JSON literal."""
    from doc3gpp.cli import _render_tdoc_show_markdown

    record = TDocShowRecord(
        tdoc=TDoc(tdoc_id="R5-240001", ftp_url="x"),
        ls=_make_ls_details(),
    )
    stream = io.StringIO()
    _render_tdoc_show_markdown(record, stream, compact=True)
    text = stream.getvalue()
    assert "##" not in text
    assert "**" not in text
    assert "title: LS on foo" in text
    assert "to_groups: RAN WG3, RAN WG4" in text
    assert "attachments: " in text
    assert '"doc_number":"TR 38.901 v0.1.0 [draft]"' in text


def test_show_markdown_ls_only_does_not_emit_no_extracted_details() -> None:
    """A record with ONLY the LS sidecar must not regress to the
    'no extracted details' placeholder in either markdown mode."""
    from doc3gpp.cli import _render_tdoc_show_markdown

    record = TDocShowRecord(
        tdoc=TDoc(tdoc_id="R5-240001", ftp_url="x"),
        ls=_make_ls_details(),
    )
    full = io.StringIO()
    _render_tdoc_show_markdown(record, full)
    assert "No extracted details" not in full.getvalue()
    compact = io.StringIO()
    _render_tdoc_show_markdown(record, compact, compact=True)
    assert "note: No extracted details" not in compact.getvalue()


def test_show_by_url_markdown_full_emits_ls_section() -> None:
    """``_render_tdoc_show_by_url_markdown`` renders the ``## LS``
    section under the FTP URL anchor."""
    from doc3gpp.cli import _render_tdoc_show_by_url_markdown

    record = TDocShowRecordByUrl(
        ftp_url="tsg/ls/R5-240001.doc",
        ls=_make_ls_details(),
    )
    stream = io.StringIO()
    _render_tdoc_show_by_url_markdown(record, stream)
    text = stream.getvalue()
    assert "## LS" in text
    assert "- **title**: LS on foo" in text


# ---------------------------------------------------------------------------
# Table renderer
# ---------------------------------------------------------------------------


def test_show_table_emits_ls_cover_block() -> None:
    """The table renderer emits a ``[LS Cover]`` block with the header
    fields and an attachments count."""
    from doc3gpp.cli import _render_tdoc_show_table

    record = TDocShowRecord(
        tdoc=TDoc(tdoc_id="R5-240001", ftp_url="x"),
        ls=_make_ls_details(),
    )
    stream = io.StringIO()
    _render_tdoc_show_table(record, stream)
    text = stream.getvalue()
    assert "[LS Cover]" in text
    assert "title: LS on foo" in text
    assert "to_groups: RAN WG3, RAN WG4" in text
    assert "attachments: 2 item(s)" in text


def test_show_by_url_table_emits_ls_cover_block() -> None:
    """``_render_tdoc_show_by_url_table`` emits the ``[LS Cover]`` block
    after the ``[FTP URL]`` anchor."""
    from doc3gpp.cli import _render_tdoc_show_by_url_table

    record = TDocShowRecordByUrl(
        ftp_url="tsg/ls/R5-240001.doc",
        ls=_make_ls_details(),
    )
    stream = io.StringIO()
    _render_tdoc_show_by_url_table(record, stream)
    text = stream.getvalue()
    assert "[FTP URL]" in text
    assert "[LS Cover]" in text
    assert "title: LS on foo" in text

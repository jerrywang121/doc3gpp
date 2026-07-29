"""Tests for ``_resolve_compact`` (the CLI → settings precedence helper)
and the ``_emit_json`` / ``_emit_records`` compact seam (Task 3)."""

from __future__ import annotations


def test_resolve_compact_cli_true_wins_over_setting_false(monkeypatch) -> None:
    """``--compact`` on the command line forces ``True`` even when the
    setting is ``False`` (the CLI is the highest-precedence layer)."""
    from doc3gpp.cli import _resolve_compact
    from doc3gpp.settings.loader import get_settings

    monkeypatch.setattr(get_settings(), "output",
                        type(get_settings().output)(format="table", compact=False))
    assert _resolve_compact(True) is True


def test_resolve_compact_cli_false_setting_true(monkeypatch) -> None:
    """When the CLI flag is absent (``False``) the setting can still opt in."""
    from doc3gpp.cli import _resolve_compact
    from doc3gpp.settings.loader import get_settings

    monkeypatch.setattr(get_settings(), "output",
                        type(get_settings().output)(format="table", compact=True))
    assert _resolve_compact(False) is True


def test_resolve_compact_default_false(monkeypatch) -> None:
    """Default (no CLI flag, default setting) yields ``False``."""
    from doc3gpp.cli import _resolve_compact
    from doc3gpp.settings.loader import get_settings

    monkeypatch.setattr(get_settings(), "output",
                        type(get_settings().output)(format="table", compact=False))
    assert _resolve_compact(False) is False


def test_emit_json_compact_single_line_no_trailing_newline() -> None:
    """Compact JSON is one line, no operator-space, no trailing newline."""
    import io
    import json

    from doc3gpp.cli import _emit_json

    stream = io.StringIO()
    _emit_json(
        [["R5s260001", "RAN5#111", "38.331"]],
        stream,
        ["tdoc_id", "meeting_name", "spec"],
        compact=True,
    )
    text = stream.getvalue()
    assert "\n" not in text
    assert ", " not in text
    assert ": " not in text
    # Round-trips back to the original payload.
    assert json.loads(text) == [
        {"tdoc_id": "R5s260001", "meeting_name": "RAN5#111", "spec": "38.331"}
    ]


def test_emit_json_default_still_pretty_prints() -> None:
    """Default (non-compact) output is byte-identical to today."""
    import io

    from doc3gpp.cli import _emit_json

    stream = io.StringIO()
    _emit_json(
        [["R5s260001", "RAN5#111"]],
        stream,
        ["tdoc_id", "meeting_name"],
    )
    text = stream.getvalue()
    assert text.endswith("\n")
    assert ",\n" in text  # pretty-print indent survives


def test_emit_markdown_compact_per_row_blocks() -> None:
    """Compact markdown drops the GFM table and emits ``key: value``
    blocks per row, separated by blank lines."""
    import io

    from doc3gpp.cli import _emit_markdown

    stream = io.StringIO()
    _emit_markdown(
        [
            ["R5s260001", "RAN5#111"],
            ["R5s260002", "RAN5#111"],
        ],
        stream,
        ["tdoc_id", "meeting_name"],
        compact=True,
    )
    text = stream.getvalue()
    # No GFM decorators survive.
    assert "|" not in text
    assert "---" not in text
    assert "```" not in text
    # Two rows, each with two ``key: value`` lines, blank-line separated.
    assert text.strip().split("\n\n") == [
        "tdoc_id: R5s260001\nmeeting_name: RAN5#111",
        "tdoc_id: R5s260002\nmeeting_name: RAN5#111",
    ]


def test_emit_markdown_default_still_gfm_table() -> None:
    """Default (non-compact) output is the legacy GFM table."""
    import io

    from doc3gpp.cli import _emit_markdown

    stream = io.StringIO()
    _emit_markdown(
        [["R5s260001", "RAN5#111"]],
        stream,
        ["tdoc_id", "meeting_name"],
    )
    text = stream.getvalue()
    assert text == (
        "| tdoc_id | meeting_name |\n"
        "|---|---|\n"
        "| R5s260001 | RAN5#111 |\n"
    )


def _make_record():
    from datetime import date

    from doc3gpp.models.tdoc_cr import TDocCRDetails

    return TDocCRDetails(
        tdoc_id="R5s260001",
        spec="38.300",
        cr_num="0001",
        rev="-",
        version="1.0.0",
        title="CR on 5G NR",
        source="RAN1",
        tsg="RAN1",
        related_wis="-",
        date=date(2026, 1, 15),
        cr_cat="F",
        release="Rel-18",
        reason_for_change="-",
        consequences_if_not_approved="-",
        clauses_affected="5.4.2",
    )


def test_emit_record_json_compact_single_line(tmp_path) -> None:
    """``_emit_record_json`` honours ``compact`` the same way as
    ``_emit_json`` (single line, no spaces, no trailing newline)."""
    import json

    from doc3gpp.cli import _emit_record_json

    record = _make_record()
    out = tmp_path / "out.json"
    _emit_record_json(record, str(out), compact=True)
    text = out.read_text(encoding="utf-8")
    assert "\n" not in text
    assert ", " not in text
    assert ": " not in text
    payload = json.loads(text)
    assert payload["tdoc_id"] == "R5s260001"
    assert payload["date"] == "2026-01-15"


def test_emit_record_markdown_compact_strips_decorators(tmp_path) -> None:
    """``_emit_record_markdown`` compact form drops the GFM table and
    emits ``field: value`` lines."""
    from doc3gpp.cli import _DIRECT_PARSE_FIELDS, _emit_record_markdown

    record = _make_record()
    out = tmp_path / "out.md"
    _emit_record_markdown(record, str(out), compact=True)
    text = out.read_text(encoding="utf-8")
    assert "|" not in text
    assert "---" not in text
    for label in _DIRECT_PARSE_FIELDS:
        assert f"{label}:" in text


def test_emit_record_table_compact_is_noop(tmp_path) -> None:
    """``_emit_record_table`` ignores ``compact`` (table is already
    line-oriented and maximally compact by construction)."""
    from doc3gpp.cli import _emit_record_table

    record = _make_record()
    plain = tmp_path / "plain.tsv"
    compact = tmp_path / "compact.tsv"
    _emit_record_table(record, str(plain))
    _emit_record_table(record, str(compact), compact=True)
    assert plain.read_text(encoding="utf-8") == compact.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Task 6 — tdoc show renderers: compact kwarg
# ---------------------------------------------------------------------------


def test_render_tdoc_show_json_compact_round_trips() -> None:
    """``_render_tdoc_show_json(record, None, compact=True)`` returns
    a single line, no operator-space, no trailing newline, and parses
    back to the same payload."""
    import io
    import json
    from datetime import date

    from doc3gpp.cli import _render_tdoc_show_json, TDocShowRecord
    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.models.tdoc_cr import TDocCRDetails

    tdoc = TDoc(
        tdoc_id="R5s260001",
        title="CR on 5G NR",
        ftp_url="x/1",
        source="RAN1",
        type="CR",
        status="approved",
        spec="38.300",
        cr_num="0001",
        version="1.0.0",
        release="Rel-18",
    )
    cover = TDocCRDetails(
        tdoc_id="R5s260001",
        spec="38.300",
        cr_num="0001",
        rev="-",
        version="1.0.0",
        title="CR on 5G NR",
        source="RAN1",
        tsg="RAN1",
        related_wis="-",
        date=date(2026, 1, 15),
        cr_cat="F",
        release="Rel-18",
        reason_for_change="-",
        consequences_if_not_approved="-",
        clauses_affected="5.4.2",
    )
    record = TDocShowRecord(tdoc=tdoc, cover=cover, ttcn=None, extracted_at=None, files=())
    stream = io.StringIO()
    _render_tdoc_show_json(record, stream, compact=True)
    text = stream.getvalue()
    assert "\n" not in text
    assert ", " not in text
    assert ": " not in text
    payload = json.loads(text)
    assert payload["tdoc"]["tdoc_id"] == "R5s260001"
    assert payload["cover"]["date"] == "2026-01-15"


def test_render_tdoc_show_markdown_compact_strips_decorators() -> None:
    """``_render_tdoc_show_markdown(..., compact=True)`` drops every
    CommonMark decorator and uses blank-line section separators."""
    import io

    from doc3gpp.cli import _render_tdoc_show_markdown, TDocShowRecord
    from doc3gpp.models.tdoc import TDoc

    tdoc = TDoc(
        tdoc_id="R5s260001",
        title="CR on 5G NR",
        ftp_url="x/1",
        source="RAN1",
        type="CR",
        status="approved",
    )
    record = TDocShowRecord(
        tdoc=tdoc,
        cover=None,
        ttcn=None,
        extracted_at=None,
        files=(),
    )
    stream = io.StringIO()
    _render_tdoc_show_markdown(record, stream, compact=True)
    text = stream.getvalue()
    # No CommonMark decorators survive.
    assert "##" not in text
    assert "**" not in text
    assert "*" not in text
    assert "```" not in text
    # Field labels still appear as ``key: value`` lines.
    assert "tdoc_id: R5s260001" in text
    assert "title: CR on 5G NR" in text


def test_render_tdoc_show_table_compact_is_noop() -> None:
    """``_render_tdoc_show_table`` ignores ``compact`` (table is
    already line-oriented)."""
    import io

    from doc3gpp.cli import _render_tdoc_show_table, TDocShowRecord
    from doc3gpp.models.tdoc import TDoc

    tdoc = TDoc(tdoc_id="R5s260001", title="X", ftp_url="x/1")
    record = TDocShowRecord(tdoc=tdoc, cover=None, ttcn=None, extracted_at=None, files=())
    plain = io.StringIO()
    _render_tdoc_show_table(record, plain)
    compact = io.StringIO()
    _render_tdoc_show_table(record, compact, compact=True)
    assert plain.getvalue() == compact.getvalue()


def test_render_tdoc_show_raw_compact_is_noop(monkeypatch) -> None:
    """``_render_tdoc_show_raw`` ignores ``compact`` (raw is already
    maximally compact by construction)."""
    from doc3gpp.cli import _render_tdoc_show_raw

    def fake_read_cached_markdown_path(*args, **kwargs):
        return "# hello"

    monkeypatch.setattr(
        "doc3gpp.cli._read_cached_markdown_path", fake_read_cached_markdown_path
    )
    monkeypatch.setattr("doc3gpp.cli._build_cache", lambda: type("C", (), {"root": "."})())

    class _Stub:
        def extract(self, _tdoc_id):
            from types import SimpleNamespace

            return SimpleNamespace(
                extract_meta=SimpleNamespace(cache_file="x.zip"),
                tdoc_id=_tdoc_id,
            )

    monkeypatch.setattr("doc3gpp.cli.build_tdoc_cr_service", lambda: _Stub())

    import io

    plain_buf = io.StringIO()
    compact_buf = io.StringIO()
    _render_tdoc_show_raw("R5s260001", plain_buf)
    _render_tdoc_show_raw("R5s260001", compact_buf, compact=True)
    assert plain_buf.getvalue() == compact_buf.getvalue()


# ---------------------------------------------------------------------------
# Task 7 — tdoc show --ftp-url renderers: compact kwarg
# ---------------------------------------------------------------------------


def test_render_tdoc_show_by_url_json_compact_round_trips() -> None:
    """``_render_tdoc_show_by_url_json(..., compact=True)`` returns
    a single line, no spaces, no trailing newline, and parses back."""
    import io
    import json

    from doc3gpp.cli import _render_tdoc_show_by_url_json, TDocShowRecordByUrl
    from doc3gpp.models.tdoc import TDoc

    tdoc = TDoc(tdoc_id="R5s260001", title="CR on 5G NR", ftp_url="x/1")
    record = TDocShowRecordByUrl(
        ftp_url="x/1", tdoc=tdoc, cover=None, ttcn=None,
        extracted_at=None, files=(),
    )
    stream = io.StringIO()
    _render_tdoc_show_by_url_json(record, stream, compact=True)
    text = stream.getvalue()
    assert "\n" not in text
    assert ", " not in text
    assert ": " not in text
    assert json.loads(text)["ftp_url"] == "x/1"


def test_render_tdoc_show_by_url_markdown_compact_strips_decorators() -> None:
    """``_render_tdoc_show_by_url_markdown(..., compact=True)`` drops
    every CommonMark decorator."""
    import io

    from doc3gpp.cli import _render_tdoc_show_by_url_markdown, TDocShowRecordByUrl
    from doc3gpp.models.tdoc import TDoc

    tdoc = TDoc(tdoc_id="R5s260001", title="X", ftp_url="x/1")
    record = TDocShowRecordByUrl(
        ftp_url="x/1", tdoc=tdoc, cover=None, ttcn=None,
        extracted_at=None, files=(),
    )
    stream = io.StringIO()
    _render_tdoc_show_by_url_markdown(record, stream, compact=True)
    text = stream.getvalue()
    assert "##" not in text
    assert "**" not in text
    assert "```" not in text
    assert "ftp_url: x/1" in text


def test_render_tdoc_show_markdown_compact_emits_changes_when_only_changes_populated() -> None:
    """``_render_tdoc_show_markdown(..., compact=True)`` must emit the
    compact ``changes:`` block when ONLY ``record.changes`` is populated
    (no cover, no TTCN, no extracted_at).

    Regression: the compact branch used to gate the changes render
    behind ``cover/ttcn/extracted_at is None``, so a TDoc with only
    a body-change sidecar dropped the ``## Change Details`` block
    silently. The spec is "every renderer branch is independent,
    omit-when-null per field", so each field renders when populated.
    """
    import io

    from doc3gpp.cli import _render_tdoc_show_markdown, TDocShowRecord
    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.models.tdoc_cr_change_details import TDocCRChangeDetails

    tdoc = TDoc(
        tdoc_id="R5s260001",
        title="CR on 5G NR",
        ftp_url="x/1",
        source="RAN1",
        type="CR",
        status="approved",
    )
    changes = TDocCRChangeDetails(
        ftp_url="x/1",
        tdoc_id="R5s260001",
        clauses=("5.4.2",),
        changes=({"clauses": ["5.4.2"], "text": "[F] add clause"},),
    )
    record = TDocShowRecord(
        tdoc=tdoc,
        cover=None,
        ttcn=None,
        changes=changes,
        extracted_at=None,
        files=(),
    )
    stream = io.StringIO()
    _render_tdoc_show_markdown(record, stream, compact=True)
    text = stream.getvalue()
    # The compact branch must surface the changes block.
    assert "changes: 1 block(s), 1 clause(s)" in text
    # The compact renderer prints the per-block layout with clauses first
    # and the change text on its own line. The change text must appear
    # under the block[1]: / clauses[1]: / changes[1]: sub-tree.
    assert "- block[1]:" in text
    assert "- clauses[1]: 5.4.2" in text
    assert "- changes[1]:" in text
    assert "[F] add clause" in text
    # And it must NOT regress to the "no extracted details" placeholder,
    # because changes is populated.
    assert "note: No extracted details" not in text


def test_render_tdoc_show_by_url_markdown_compact_emits_changes_when_only_changes_populated() -> None:
    """``_render_tdoc_show_by_url_markdown(..., compact=True)`` must emit
    the compact ``changes:`` block when ONLY ``record.changes`` is
    populated (no cover, no TTCN, no extracted_at).

    Same regression as the by-tdoc twin — the compact branch used to
    gate the changes render behind ``cover/ttcn/extracted_at is None``.
    """
    import io

    from doc3gpp.cli import _render_tdoc_show_by_url_markdown, TDocShowRecordByUrl
    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.models.tdoc_cr_change_details import TDocCRChangeDetails

    tdoc = TDoc(
        tdoc_id="R5s260001",
        title="CR on 5G NR",
        ftp_url="x/1",
        source="RAN1",
        type="CR",
        status="approved",
    )
    changes = TDocCRChangeDetails(
        ftp_url="x/1",
        tdoc_id="R5s260001",
        clauses=("5.4.2",),
        changes=({"clauses": ["5.4.2"], "text": "[F] add clause"},),
    )
    record = TDocShowRecordByUrl(
        ftp_url="x/1",
        tdoc=tdoc,
        cover=None,
        ttcn=None,
        changes=changes,
        extracted_at=None,
        files=(),
    )
    stream = io.StringIO()
    _render_tdoc_show_by_url_markdown(record, stream, compact=True)
    text = stream.getvalue()
    # The compact branch must surface the changes block.
    assert "changes: 1 block(s), 1 clause(s)" in text
    # The compact renderer prints the per-block layout with clauses first
    # and the change text on its own line. The change text must appear
    # under the block[1]: / clauses[1]: / changes[1]: sub-tree.
    assert "- block[1]:" in text
    assert "- clauses[1]: 5.4.2" in text
    assert "- changes[1]:" in text
    assert "[F] add clause" in text
    # And it must NOT regress to the "no extracted details" placeholder,
    # because changes is populated.
    assert "note: No extracted details" not in text


def test_render_tdoc_show_by_url_table_compact_is_noop() -> None:
    """``_render_tdoc_show_by_url_table`` ignores ``compact``."""
    import io

    from doc3gpp.cli import _render_tdoc_show_by_url_table, TDocShowRecordByUrl
    from doc3gpp.models.tdoc import TDoc

    tdoc = TDoc(tdoc_id="R5s260001", title="X", ftp_url="x/1")
    record = TDocShowRecordByUrl(
        ftp_url="x/1", tdoc=tdoc, cover=None, ttcn=None,
        extracted_at=None, files=(),
    )
    plain = io.StringIO()
    _render_tdoc_show_by_url_table(record, plain)
    compact = io.StringIO()
    _render_tdoc_show_by_url_table(record, compact, compact=True)
    assert plain.getvalue() == compact.getvalue()


def test_render_tdoc_show_raw_by_url_compact_is_noop(monkeypatch) -> None:
    """``_render_tdoc_show_raw_by_url`` ignores ``compact``."""
    from doc3gpp.cli import _render_tdoc_show_raw_by_url

    def fake_read_cached_markdown_path(*args, **kwargs):
        return "# hello"

    monkeypatch.setattr("doc3gpp.cli._read_cached_markdown_path", fake_read_cached_markdown_path)
    monkeypatch.setattr("doc3gpp.cli._build_cache", lambda: type("C", (), {"root": "."})())

    import io
    plain = io.StringIO()
    compact = io.StringIO()
    _render_tdoc_show_raw_by_url("https://x/1", plain)
    _render_tdoc_show_raw_by_url("https://x/1", compact, compact=True)
    assert plain.getvalue() == compact.getvalue()


# ---------------------------------------------------------------------------
# Task 10 — tdoc show --compact end-to-end via the Typer CLI runner
# ---------------------------------------------------------------------------


def test_tdoc_show_json_compact_via_cli(monkeypatch) -> None:
    """``tdoc show --tdoc <id> --format json --compact`` end-to-end via
    the CLI runner emits a single line of JSON.

    The brief's first-draft snippet monkeypatches ``_resolve_tdoc_show_record``
    which doesn't exist as a separate symbol — the dispatcher builds the
    ``TDocShowRecord`` inline. The cleanest seam that doesn't change the
    production code is to stub the four ``build_*_repository`` factories
    (and ``trigger_auto_sync``) so the by-tdoc path returns a pre-baked
    record. This mirrors how ``test_cli_auto_sync.py`` mocks the same
    factories for end-to-end CLI runs.
    """
    import json
    from datetime import date

    from typer.testing import CliRunner

    from doc3gpp.cli import app
    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.models.tdoc_cr import TDocCRDetails

    tdoc = TDoc(
        tdoc_id="R5s260001",
        title="CR on 5G NR",
        ftp_url="x/1",
        source="RAN1",
        type="CR",
        status="approved",
    )
    cover = TDocCRDetails(
        tdoc_id="R5s260001",
        spec="38.300",
        cr_num="0001",
        rev="-",
        version="1.0.0",
        title="CR on 5G NR",
        source="RAN1",
        tsg="RAN1",
        related_wis="-",
        date=date(2026, 1, 15),
        cr_cat="F",
        release="Rel-18",
        reason_for_change="-",
        consequences_if_not_approved="-",
        clauses_affected="5.4.2",
    )

    class _TDocRepoStub:
        def get_by_id(self, _tdoc_id: str) -> TDoc:
            return tdoc

    class _CrRepoStub:
        def get_by_url(self, _url: str) -> TDocCRDetails | None:
            return cover

        def get_extract_meta_by_url(self, _url: str):
            return None

    class _TtcnRepoStub:
        def get_by_url(self, _url: str):
            return None

    class _FileRepoStub:
        def get_for_tdoc_id(self, _tdoc_id: str) -> tuple:
            return ()

    monkeypatch.setattr("doc3gpp.cli.build_tdoc_repository", lambda: _TDocRepoStub())
    monkeypatch.setattr("doc3gpp.cli.build_tdoc_cr_repository", lambda: _CrRepoStub())
    monkeypatch.setattr("doc3gpp.cli.build_tdoc_cr_ttcn_repository", lambda: _TtcnRepoStub())
    monkeypatch.setattr("doc3gpp.cli.build_tdoc_file_repository", lambda: _FileRepoStub())
    monkeypatch.setattr("doc3gpp.cli.trigger_auto_sync", lambda **kwargs: None)

    result = CliRunner().invoke(
        app,
        ["tdoc", "show", "--tdoc", "R5s260001", "--format", "json", "--compact"],
    )
    assert result.exit_code == 0, result.output
    output = result.output
    assert "\n" not in output
    assert ", " not in output
    assert ": " not in output
    payload = json.loads(output)
    assert payload["tdoc"]["tdoc_id"] == "R5s260001"


def test_tdoc_show_by_ftp_url_markdown_compact_via_cli(monkeypatch) -> None:
    """``tdoc show --ftp-url <url> --format markdown --compact`` end-to-end
    via the CLI runner drops the CommonMark decorators and uses
    ``key: value`` lines.

    Mirrors :func:`test_tdoc_show_json_compact_via_cli` but exercises
    the ``_tdoc_show_by_ftp_url`` dispatcher and the by-url renderer.
    """
    from typer.testing import CliRunner

    from doc3gpp.cli import app
    from doc3gpp.models.tdoc import TDoc

    tdoc = TDoc(
        tdoc_id="R5s260001",
        title="CR on 5G NR",
        ftp_url="x/1",
        source="RAN1",
        type="CR",
        status="approved",
    )

    class _TDocRepoStub:
        def get_by_ftp_url(self, _url: str) -> TDoc:
            return tdoc

    class _CrRepoStub:
        def get_by_url(self, _url: str):
            return None

        def get_extract_meta_by_url(self, _url: str):
            return None

    class _TtcnRepoStub:
        def get_by_url(self, _url: str):
            return None

    class _FileRepoStub:
        def get_by_ftp_url(self, _url: str) -> tuple:
            return ()

    monkeypatch.setattr("doc3gpp.cli.build_tdoc_repository", lambda: _TDocRepoStub())
    monkeypatch.setattr("doc3gpp.cli.build_tdoc_cr_repository", lambda: _CrRepoStub())
    monkeypatch.setattr("doc3gpp.cli.build_tdoc_cr_ttcn_repository", lambda: _TtcnRepoStub())
    monkeypatch.setattr("doc3gpp.cli.build_tdoc_file_repository", lambda: _FileRepoStub())

    result = CliRunner().invoke(
        app,
        [
            "tdoc", "show", "--ftp-url", "x/1",
            "--format", "markdown", "--compact",
        ],
    )
    assert result.exit_code == 0, result.output
    output = result.output
    # No CommonMark decorators survive in compact markdown.
    assert "##" not in output
    assert "**" not in output
    assert "```" not in output
    assert "ftp_url: x/1" in output
    assert "tdoc_id: R5s260001" in output


def test_tdoc_show_json_default_still_pretty_prints(monkeypatch) -> None:
    """Default (no ``--compact``) keeps the legacy pretty-printed JSON.

    Guards the "default byte-identical" contract: a user who doesn't
    pass ``--compact`` (and has ``Settings.output.compact = False``)
    must see the same output as before this task landed.
    """
    import json

    from typer.testing import CliRunner

    from doc3gpp.cli import app
    from doc3gpp.models.tdoc import TDoc

    tdoc = TDoc(
        tdoc_id="R5s260001",
        title="CR on 5G NR",
        ftp_url="x/1",
        source="RAN1",
        type="CR",
        status="approved",
    )

    class _TDocRepoStub:
        def get_by_id(self, _tdoc_id: str) -> TDoc:
            return tdoc

    class _CrRepoStub:
        def get_by_url(self, _url: str):
            return None

        def get_extract_meta_by_url(self, _url: str):
            return None

    class _TtcnRepoStub:
        def get_by_url(self, _url: str):
            return None

    class _FileRepoStub:
        def get_for_tdoc_id(self, _tdoc_id: str) -> tuple:
            return ()

    monkeypatch.setattr("doc3gpp.cli.build_tdoc_repository", lambda: _TDocRepoStub())
    monkeypatch.setattr("doc3gpp.cli.build_tdoc_cr_repository", lambda: _CrRepoStub())
    monkeypatch.setattr("doc3gpp.cli.build_tdoc_cr_ttcn_repository", lambda: _TtcnRepoStub())
    monkeypatch.setattr("doc3gpp.cli.build_tdoc_file_repository", lambda: _FileRepoStub())
    monkeypatch.setattr("doc3gpp.cli.trigger_auto_sync", lambda **kwargs: None)

    result = CliRunner().invoke(
        app,
        ["tdoc", "show", "--tdoc", "R5s260001", "--format", "json"],
    )
    assert result.exit_code == 0, result.output
    output = result.output
    # Pretty-printed JSON: newline at end, key-colon-space preserved.
    assert output.endswith("\n")
    assert ": " in output
    payload = json.loads(output)
    assert payload["tdoc"]["tdoc_id"] == "R5s260001"

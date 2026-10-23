"""Direct-mode TDoc type detection: content sniffing, decisions, and auto-sync wiring."""

from __future__ import annotations

import io
import zipfile
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.services import tdoc_type_detection
from doc3gpp.services.tdoc_errors import TDocTypeSkippedError
from doc3gpp.services.tdoc_type_detection import (
    decide_direct_type,
    sniff_content_family,
)
from doc3gpp.settings.loader import get_settings

WID_MARKER = "3GPP™ Work Item Description"


def _make_zip(files: dict[str, bytes]) -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return payload.getvalue()


def _no_sniff() -> str:
    raise AssertionError("main_markdown must not be called")


def _filler(count: int) -> list[str]:
    return [f"filler line {index}" for index in range(count)]


def test_sniff_wid_within_first_20_non_empty_lines() -> None:
    on_line_20 = "\n".join([*_filler(19), WID_MARKER])
    on_line_21 = "\n".join([*_filler(20), WID_MARKER])

    found = sniff_content_family(on_line_20)

    assert found is not None
    assert found.key == "wid"
    assert sniff_content_family(on_line_21) is None


def test_sniff_wid_ignores_blank_lines_when_counting() -> None:
    blanks_between = "\n\n".join([*_filler(19), WID_MARKER])
    assert sniff_content_family(blanks_between) is not None


@pytest.mark.parametrize(
    "title",
    ["Title: LS on Security", "Title: Reply LS on Security", "   Title:   LS on  Security"],
)
def test_sniff_ls_title_patterns(title: str) -> None:
    found = sniff_content_family(f"{title}\n")

    assert found is not None
    assert found.key == "ls"


def test_sniff_ls_title_after_line_10_is_not_matched() -> None:
    markdown = "\n".join([*_filler(10), "Title: LS on Security"])

    assert sniff_content_family(markdown) is None


def test_sniff_discussion_title() -> None:
    found = sniff_content_family("Title: Discussion on RAN5 testing\n")

    assert found is not None
    assert found.key == "discussion"


def test_sniff_discussion_title_after_line_10_is_not_matched() -> None:
    markdown = "\n".join([*_filler(10), "Title: Discussion on RAN5 testing"])

    assert sniff_content_family(markdown) is None


def test_sniff_plain_text_matches_nothing() -> None:
    assert sniff_content_family("Just some text.\nNothing to see.\n") is None


def test_decide_uses_tdocs_type_without_converting() -> None:
    decision = decide_direct_type("R5-123456", tdoc_type="WID revised", main_markdown=_no_sniff)

    assert decision.kind == "family"
    assert decision.store is not None
    assert decision.store.key == "wid"
    assert decision.reason is None


def test_decide_known_cr_type_is_cr_without_converting() -> None:
    decision = decide_direct_type("R5-123456", tdoc_type="CR", main_markdown=_no_sniff)

    assert decision.kind == "cr"
    assert decision.store is not None
    assert decision.store.key == "cr"


def test_decide_known_unparsed_type_is_skipped_with_reason() -> None:
    decision = decide_direct_type("R5-123456", tdoc_type="other", main_markdown=_no_sniff)

    assert decision.kind == "skipped"
    assert decision.store is None
    assert decision.reason == "type 'other' is not parsed"


def test_decide_sniffs_when_no_row() -> None:
    decision = decide_direct_type(
        "R5-123456",
        tdoc_type=None,
        main_markdown=lambda: f"{WID_MARKER}\n",
    )

    assert decision.kind == "family"
    assert decision.store is not None
    assert decision.store.key == "wid"


def test_decide_cr_header_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tdoc_type_detection,
        "is_cr_header_present",
        lambda markdown: (True, "cover"),
    )

    decision = decide_direct_type(
        "R5-123456",
        tdoc_type=None,
        main_markdown=lambda: "no family marker here\n",
    )

    assert decision.kind == "cr"
    assert decision.store is not None
    assert decision.store.key == "cr"


def test_decide_unknown_is_skipped_with_reason() -> None:
    decision = decide_direct_type(
        "R5-123456",
        tdoc_type=None,
        main_markdown=lambda: "no family marker and no cover page\n",
    )

    assert decision.kind == "skipped"
    assert decision.store is None
    assert decision.reason == "type could not be determined"


def test_nbsp_only_type_is_not_treated_as_unknown() -> None:
    decision = decide_direct_type("R5-123456", tdoc_type="\u00a0", main_markdown=_no_sniff)

    assert decision.kind == "skipped"
    assert decision.store is None
    assert decision.reason is not None
    assert "is not parsed" in decision.reason


@pytest.mark.parametrize("tdoc_type", ["", "   "])
def test_ordinary_blank_type_still_sniffs(tdoc_type: str) -> None:
    calls: list[str] = []

    def sniffed_main() -> str:
        calls.append("main")
        return f"{WID_MARKER}\n"

    decision = decide_direct_type("R5-123456", tdoc_type=tdoc_type, main_markdown=sniffed_main)

    assert calls == ["main"]
    assert decision.kind == "family"
    assert decision.store is not None
    assert decision.store.key == "wid"


def test_skipped_error_carries_reason() -> None:
    error = TDocTypeSkippedError("R5-123456", "type 'other' is not parsed")

    assert error.tdoc_id == "R5-123456"
    assert error.reason == "type 'other' is not parsed"
    assert str(error) == "type 'other' is not parsed"


def _set_auto_sync(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    monkeypatch.setenv("DOC3GPP_SYNC__AUTO_SYNC", "true" if enabled else "false")
    get_settings.cache_clear()


def _stub_sync_services(monkeypatch: pytest.MonkeyPatch, sync: MagicMock) -> None:
    monkeypatch.setattr("doc3gpp.cli.trigger_auto_sync", sync)
    monkeypatch.setattr("doc3gpp.cli.build_meeting_service", lambda: MagicMock())
    monkeypatch.setattr("doc3gpp.cli.build_tdoc_sync_coordinator", lambda: MagicMock())


def test_auto_sync_on_triggers_sync_for_from_path(
    sqlite_env, monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    _set_auto_sync(monkeypatch, True)
    source = tmp_path / "R5-123456.zip"
    source.write_bytes(b"payload is not parsed in this test")
    parse = MagicMock()
    monkeypatch.setattr("doc3gpp.cli._tdoc_parse_direct", parse)
    sync = MagicMock(return_value=(0, 0))
    _stub_sync_services(monkeypatch, sync)

    result = CliRunner().invoke(app, ["tdoc", "parse", "--from-path", str(source)])

    assert result.exit_code == 0, result.output
    sync.assert_called_once()
    assert sync.call_args.kwargs["auto_sync_enabled"] is True
    assert sync.call_args.kwargs["tdoc_ids"] == {"R5-123456"}
    parse.assert_called_once()


def test_auto_sync_off_skips_sync(
    sqlite_env, monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    _set_auto_sync(monkeypatch, False)
    source = tmp_path / "R5-123456.zip"
    source.write_bytes(b"payload is not parsed in this test")
    parse = MagicMock()
    monkeypatch.setattr("doc3gpp.cli._tdoc_parse_direct", parse)
    sync = MagicMock(return_value=(0, 0))
    _stub_sync_services(monkeypatch, sync)

    result = CliRunner().invoke(app, ["tdoc", "parse", "--from-path", str(source)])

    assert result.exit_code == 0, result.output
    sync.assert_not_called()
    parse.assert_called_once()


def test_auto_sync_on_collects_ids_for_local_batch(
    sqlite_env, monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    _set_auto_sync(monkeypatch, True)
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    (input_dir / "R5-123456.zip").write_bytes(b"x")
    (input_dir / "R5-123457.docx").write_bytes(b"y")
    output_dir = tmp_path / "out"

    class _SkippingService:
        def prepare_from_bytes(self, payload, filename, *, full, max_tdoc_size_bytes):
            raise TDocTypeSkippedError(None, "type could not be determined")

    monkeypatch.setattr(
        "doc3gpp.cli.build_tdoc_cr_service",
        lambda *args, **kwargs: _SkippingService(),
    )
    sync = MagicMock(return_value=(0, 0))
    _stub_sync_services(monkeypatch, sync)

    result = CliRunner().invoke(
        app,
        ["tdoc", "parse", "--from-path", str(input_dir), "--output", str(output_dir)],
    )

    assert result.exit_code == 0, result.output
    sync.assert_called_once()
    assert sync.call_args.kwargs["tdoc_ids"] == {"R5-123456", "R5-123457"}


def test_auto_sync_on_triggers_sync_for_non_3gpp_from_url(
    sqlite_env, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-3GPP URL whose file name carries a TDoc id runs the sync steps (R57)."""
    _set_auto_sync(monkeypatch, True)
    parse = MagicMock()
    monkeypatch.setattr("doc3gpp.cli._tdoc_parse_direct", parse)
    sync = MagicMock(return_value=(0, 0))
    _stub_sync_services(monkeypatch, sync)

    result = CliRunner().invoke(
        app, ["tdoc", "parse", "--from-url", "https://example.com/R5-123456.zip"],
    )

    assert result.exit_code == 0, result.output
    sync.assert_called_once()
    assert sync.call_args.kwargs["auto_sync_enabled"] is True
    assert sync.call_args.kwargs["tdoc_ids"] == {"R5-123456"}
    parse.assert_called_once()


def test_sniff_converts_only_the_main_member() -> None:
    from doc3gpp.parsers.tdoc_zip_convert import convert_direct_main_markdown

    converted: list[str] = []

    def recording_convert(doc_bytes: bytes, filename: str) -> str:
        converted.append(filename)
        if filename != "R5-123456.docx":
            raise AssertionError(f"annex converted while sniffing: {filename}")
        return f"{WID_MARKER}\n"

    archive = _make_zip({
        "R5-123456.docx": b"main document bytes",
        "annex/bad.docx": b"not a word document",
    })

    markdown = convert_direct_main_markdown(
        archive,
        filename="R5-123456.zip",
        tdoc_id="R5-123456",
        max_documents=100,
        max_uncompressed_bytes=0,
        convert=recording_convert,
    )

    assert converted == ["R5-123456.docx"]
    assert WID_MARKER in markdown


def test_sniff_standalone_docx_converts_the_file() -> None:
    from doc3gpp.parsers.tdoc_zip_convert import convert_direct_main_markdown

    converted: list[str] = []

    def recording_convert(doc_bytes: bytes, filename: str) -> str:
        converted.append(filename)
        return f"{WID_MARKER}\n"

    markdown = convert_direct_main_markdown(
        b"docx bytes",
        filename="https://www.3gpp.org/ftp/x/R5-123456.docx",
        tdoc_id="R5-123456",
        max_documents=100,
        max_uncompressed_bytes=0,
        convert=recording_convert,
    )

    assert converted == ["R5-123456.docx"]
    assert WID_MARKER in markdown


def test_sniff_zip_without_named_main_raises_main_error() -> None:
    from doc3gpp.parsers.tdoc_family_main import TDocMainDocumentError
    from doc3gpp.parsers.tdoc_zip_convert import convert_direct_main_markdown

    archive = _make_zip({"annex.docx": b"no main here"})

    with pytest.raises(TDocMainDocumentError):
        convert_direct_main_markdown(
            archive,
            filename="R5-123456.zip",
            tdoc_id="R5-123456",
            max_documents=100,
            max_uncompressed_bytes=0,
            convert=lambda doc_bytes, filename: "",
        )

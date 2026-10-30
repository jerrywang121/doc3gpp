"""Legacy Word conversion and format discovery use the existing structured parsers."""

from __future__ import annotations

import io
import os
import shutil
import signal
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest

from doc3gpp.parsers.direct_extractor import is_3gpp_file_url, validate_tdoc_ftp_url
from doc3gpp.parsers.docx_converter import (
    HeadingBlock,
    TableBlock,
    convert_document_to_blocks,
    convert_document_to_markdown,
)
from doc3gpp.parsers.spec_doc import list_spec_docx
from doc3gpp.parsers.tdoc_family_main import TDocMainDocumentError, select_main_member
from doc3gpp.parsers.tdoc_zip import TDocZipLimitError, enumerate_docx_members
from doc3gpp.parsers.tdoc_zip_convert import convert_direct_payload, convert_tdoc_zip


@pytest.fixture(autouse=True)
def isolated_word_conversion_settings(monkeypatch, tmp_path):
    from doc3gpp.settings.loader import get_settings

    config = tmp_path / "doc3gpp.toml"
    config.write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DOC3GPP_CONFIG", str(config))
    monkeypatch.delenv("DOC3GPP_WORD_CONVERSION__TIMEOUT_SECONDS", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _zip(entries: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return output.getvalue()


@pytest.fixture
def word_bytes():
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_heading("1 Scope", level=1)
    document.add_paragraph("Legacy document body")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Name"
    table.cell(0, 1).text = "Value"
    table.cell(1, 0).text = "Release"
    table.cell(1, 1).text = "18"
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


@pytest.fixture
def office(monkeypatch, word_bytes):
    """Replace only the external executable; render its output with real python-docx."""
    workdirs = []
    profiles = []
    timeouts = []

    def popen(args, **kwargs):
        assert args[0] == "/usr/bin/libreoffice"
        assert "--headless" in args
        assert args[args.index("--convert-to") + 1] == "docx:Office Open XML Text"
        assert not kwargs.get("shell", False)
        assert kwargs["start_new_session"] == (os.name == "posix")
        source = Path(args[-1])
        assert source.read_bytes() == b"legacy Word bytes"
        outdir = Path(args[args.index("--outdir") + 1])
        profile_arg = next(arg for arg in args if arg.startswith("-env:UserInstallation="))
        profile = Path(unquote(urlparse(profile_arg.split("=", 1)[1]).path))
        assert profile.is_relative_to(source.parent)
        workdirs.append(source.parent)
        profiles.append(profile)
        (outdir / f"{source.stem}.docx").write_bytes(word_bytes)
        class Process:
            returncode = 0

            def communicate(self, *, timeout):
                assert timeout > 0
                timeouts.append(timeout)
                return "converted", ""

        return Process()

    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/libreoffice")
    monkeypatch.setattr(subprocess, "Popen", popen)
    return workdirs, profiles, timeouts


@pytest.mark.parametrize("filename", ["sample.doc", "SAMPLE.DOC", "folder/a b.doc"])
def test_doc_markdown_preserves_headings_and_tables(filename, office):
    markdown = convert_document_to_markdown(b"legacy Word bytes", filename)
    assert "# 1 Scope" in markdown
    assert "Legacy document body" in markdown
    assert "| Release | 18 |" in markdown
    assert all(not directory.exists() for directory in office[0])


def test_doc_blocks_preserve_spec_heading_and_table(office):
    blocks = convert_document_to_blocks(b"legacy Word bytes", "sample.doc")
    assert isinstance(blocks[0], HeadingBlock)
    assert blocks[0].section_no == "1"
    assert blocks[0].title == "Scope"
    assert any(isinstance(block, TableBlock) and "Release" in block.gfm for block in blocks)


def test_each_doc_conversion_has_an_isolated_profile_and_cleans_up(office):
    for _ in range(2):
        convert_document_to_markdown(b"legacy Word bytes", "same.doc")
    workdirs, profiles, _timeouts = office
    assert len(set(workdirs)) == 2
    assert len(set(profiles)) == 2
    assert all(not directory.exists() for directory in workdirs)


def test_doc_conversion_uses_configured_toml_timeout(office, tmp_path, monkeypatch):
    config = tmp_path / "doc3gpp.toml"
    config.write_text("[word_conversion]\ntimeout_seconds = 42\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DOC3GPP_CONFIG", str(config))
    monkeypatch.delenv("DOC3GPP_WORD_CONVERSION__TIMEOUT_SECONDS", raising=False)

    from doc3gpp.settings.loader import get_settings

    get_settings.cache_clear()
    try:
        convert_document_to_markdown(b"legacy Word bytes", "sample.doc")
        assert office[2] == [42]
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("convert", [convert_document_to_markdown, convert_document_to_blocks])
def test_missing_libreoffice_has_actionable_error(convert, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(RuntimeError, match="LibreOffice") as caught:
        convert(b"legacy Word bytes", "sample.doc")
    assert "install" in str(caught.value).lower()
    assert "PATH" in str(caught.value)


@pytest.mark.parametrize("failure", ["exit", "missing", "empty", "oserror"])
def test_failed_doc_conversion_reports_source_and_cleans_up(failure, monkeypatch):
    directories = []

    def popen(args, **kwargs):
        source = Path(args[-1])
        directories.append(source.parent)
        if failure == "oserror":
            raise OSError("cannot execute")
        if failure == "empty":
            outdir = Path(args[args.index("--outdir") + 1])
            (outdir / f"{source.stem}.docx").write_bytes(b"")
        class Process:
            returncode = 1 if failure == "exit" else 0

            def communicate(self, *, timeout):
                return "", "bad document"

        return Process()

    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/libreoffice")
    monkeypatch.setattr(subprocess, "Popen", popen)
    with pytest.raises(RuntimeError, match="sample.doc"):
        convert_document_to_markdown(b"legacy Word bytes", "sample.doc")
    assert directories
    assert all(not directory.exists() for directory in directories)


def test_docx_does_not_require_libreoffice(monkeypatch, word_bytes):
    def unexpected_lookup(_name):
        pytest.fail("DOCX parsing must not look for LibreOffice")

    monkeypatch.setattr(shutil, "which", unexpected_lookup)
    assert "Legacy document body" in convert_document_to_markdown(word_bytes, "sample.docx")
    assert convert_document_to_blocks(word_bytes, "sample.docx")[0].section_no == "1"


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-tree cleanup")
@pytest.mark.parametrize("interrupted", [False, True], ids=["timeout", "interruption"])
def test_conversion_abort_stops_launcher_children(monkeypatch, tmp_path, interrupted):
    marker = tmp_path / "child-survived"
    pid_file = tmp_path / "child.pid"
    child_code = (
        "import time; from pathlib import Path; time.sleep(2); "
        f"Path({str(marker)!r}).write_text('still running'); time.sleep(60)"
    )
    launcher = tmp_path / "libreoffice"
    launcher.write_text(
        f"#!{sys.executable}\n"
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
        f"Path({str(pid_file)!r}).write_text(str(child.pid))\n"
        "time.sleep(60)\n"
    )
    launcher.chmod(0o755)
    monkeypatch.setattr(shutil, "which", lambda _name: str(launcher))
    config = tmp_path / "doc3gpp.toml"
    config.write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DOC3GPP_CONFIG", str(config))
    monkeypatch.setenv("DOC3GPP_WORD_CONVERSION__TIMEOUT_SECONDS", "1")
    from doc3gpp.settings.loader import get_settings

    get_settings.cache_clear()
    if interrupted:
        communicate = subprocess.Popen.communicate
        first_call = True

        def interrupt(process, **kwargs):
            nonlocal first_call
            if first_call:
                first_call = False
                try:
                    communicate(process, **kwargs)
                except subprocess.TimeoutExpired:
                    raise KeyboardInterrupt from None
            return communicate(process, **kwargs)

        monkeypatch.setattr(subprocess.Popen, "communicate", interrupt)

    try:
        expected = KeyboardInterrupt if interrupted else RuntimeError
        with pytest.raises(expected) as caught:
            convert_document_to_markdown(b"legacy Word bytes", "sample.doc")
        if not interrupted:
            assert "timed out" in str(caught.value) and "sample.doc" in str(caught.value)
        assert pid_file.exists(), "launcher must start its child before the deadline"
        time.sleep(1.2)
        assert not marker.exists(), "conversion child survived an aborted conversion"
    finally:
        get_settings.cache_clear()
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_mixed_tdoc_zip_includes_doc_members_and_preserves_original_paths(office, word_bytes):
    raw = _zip({"R5-123456.doc": b"legacy Word bytes", "annex/new.docx": word_bytes})
    result = convert_tdoc_zip(
        raw, tdoc_id="R5-123456", identity="tsg_ran/R5-123456.zip",
        max_documents=100, max_uncompressed_bytes=0,
    )
    assert result.main_source_file == "R5-123456.doc"
    assert [doc.source_file for doc in result.documents] == ["R5-123456.doc", "annex/new.docx"]
    assert all("Legacy document body" in doc.markdown_text for doc in result.documents)


def test_direct_doc_is_its_own_main(office):
    result = convert_direct_payload(
        b"legacy Word bytes", filename="R5-123456.doc", tdoc_id="R5-123456",
        identity="tsg_ran/R5-123456.doc", max_documents=100, max_uncompressed_bytes=0,
    )
    assert result.main_source_file == "R5-123456.doc"
    assert "Legacy document body" in result.documents[0].markdown_text


@pytest.mark.parametrize("limit", ["count", "bytes"])
def test_doc_members_are_subject_to_zip_limits(limit):
    raw = _zip({"main.docx": b"1234", "annex/old.DOC": b"5678"})
    with pytest.raises(TDocZipLimitError):
        enumerate_docx_members(
            raw, max_documents=1 if limit == "count" else 0,
            max_uncompressed_bytes=7 if limit == "bytes" else 0,
        )


def test_doc_exact_main_name_can_disambiguate_annexes():
    assert select_main_member(
        "R5-123456", ["annex/R5-123456_note.docx", "R5-123456.DOC"]
    ) == "R5-123456.DOC"


def test_two_exact_main_formats_are_ambiguous():
    with pytest.raises(TDocMainDocumentError, match="ambiguous"):
        select_main_member("R5-123456", ["R5-123456.doc", "R5-123456.docx"])


def test_spec_listing_includes_legacy_doc_in_source_order():
    assert list_spec_docx(_zip({"old.DOC": b"old", "new.docx": b"new", "note.txt": b"x"})) == [
        ("old.DOC", b"old"), ("new.docx", b"new"),
    ]


def test_legacy_single_document_helper_still_prefers_docx():
    from doc3gpp.parsers.cr.helpers import extract_docx_from_zip

    assert extract_docx_from_zip(_zip({"a.doc": b"legacy", "z.docx": b"modern"})) == (
        "z.docx", b"modern",
    )


def test_3gpp_doc_url_is_a_file_not_a_folder():
    url = "https://www.3gpp.org/ftp/tsg_ran/R5-123456.DOC"
    assert is_3gpp_file_url(url)
    assert validate_tdoc_ftp_url(url) == url
    with pytest.raises(ValueError, match="TDoc"):
        validate_tdoc_ftp_url("https://www.3gpp.org/ftp/not-a-tdoc.doc")


def test_local_directory_discovery_includes_doc(tmp_path):
    from doc3gpp.cli import _collect_local_parse_targets

    for name in ("R5-123456.DOC", "R5-123457.docx", "R5-123458.zip", "unrelated.doc"):
        (tmp_path / name).write_bytes(b"data")
    assert [path.name for path in _collect_local_parse_targets(tmp_path, recursive=False)] == [
        "R5-123456.DOC", "R5-123457.docx", "R5-123458.zip",
    ]

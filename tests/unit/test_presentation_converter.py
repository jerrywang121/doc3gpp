"""Behavioral tests for shared PowerPoint-to-Markdown conversion."""

from __future__ import annotations

import importlib
import io
import os
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlparse

import pytest
from pptx.slide import Slide

from tests.fixtures.presentation_builders import (
    build_empty_presentation,
    build_presentation_with_content,
    build_presentation_with_graphics,
    build_presentation_without_notes,
)


def _presentation_converter():
    try:
        return importlib.import_module("doc3gpp.parsers.presentation_converter")
    except ModuleNotFoundError as exc:
        if exc.name == "doc3gpp.parsers.presentation_converter":
            pytest.fail("the shared presentation converter is missing")
        if exc.name == "pptx":
            pytest.fail("python-pptx must be imported lazily")
        raise


@pytest.fixture
def empty_deck() -> bytes:
    return build_empty_presentation()


@pytest.fixture
def deck_with_content() -> bytes:
    return build_presentation_with_content()


def test_slide_shapes_tables_and_notes_are_ordered(deck_with_content) -> None:
    converter = _presentation_converter()

    markdown = converter.convert_presentation_to_markdown(
        deck_with_content, "folder/slides.pptx"
    )

    assert markdown.index("## Slide 1") < markdown.index("日本語")
    assert markdown.index("日本語") < markdown.index("group text")
    assert markdown.index("group text") < markdown.index(r"a\|b")
    assert markdown.index(r"a\|b") < markdown.index("## Slide 2")
    assert "line1<br>line2" in markdown
    assert "**Speaker notes:**" in markdown and "speaker secret" in markdown


def test_empty_deck_is_known_content(empty_deck) -> None:
    converter = _presentation_converter()

    assert converter.convert_presentation_to_markdown(empty_deck, "empty.pptx") == ""


def test_malformed_presentation_names_its_source() -> None:
    converter = _presentation_converter()

    with pytest.raises(RuntimeError, match=r"slides/bad\.pptx"):
        converter.convert_presentation_to_markdown(b"not a zip", "slides/bad.pptx")


def test_absent_notes_slide_is_not_created(monkeypatch) -> None:
    converter = _presentation_converter()

    def forbidden_notes_slide(_slide):
        pytest.fail("converter accessed notes_slide when no notes slide exists")

    monkeypatch.setattr(Slide, "notes_slide", property(forbidden_notes_slide))
    markdown = converter.convert_presentation_to_markdown(
        build_presentation_without_notes(), "no-notes.pptx"
    )

    assert "slide without notes" in markdown


def test_python_pptx_is_imported_lazily_and_has_an_install_hint(
    monkeypatch, deck_with_content
) -> None:
    converter = _presentation_converter()

    monkeypatch.setitem(sys.modules, "pptx", None)
    try:
        converter = importlib.reload(converter)
    except ModuleNotFoundError as exc:
        if exc.name == "pptx":
            pytest.fail("importing the converter must not require python-pptx")
        raise

    with pytest.raises(converter.PythonPptxNotInstalledError) as caught:
        converter.convert_presentation_to_markdown(deck_with_content, "slides.pptx")
    assert "doc3gpp[extract]" in str(caught.value)


def test_invalid_extension_is_rejected() -> None:
    converter = _presentation_converter()

    with pytest.raises(ValueError, match=r"expected \.pptx or \.ppt"):
        converter.convert_presentation_to_markdown(b"unused", "slides.pdf")


def test_uppercase_pptx_extension_is_supported(deck_with_content, monkeypatch) -> None:
    converter = _presentation_converter()

    monkeypatch.setattr(
        converter.shutil,
        "which",
        lambda _name: pytest.fail("PPTX conversion must not invoke LibreOffice"),
    )
    markdown = converter.convert_presentation_to_markdown(
        deck_with_content, "folder/SLIDES.PPTX"
    )

    assert "日本語" in markdown


def test_image_and_chart_content_is_not_inferred() -> None:
    converter = _presentation_converter()

    markdown = converter.convert_presentation_to_markdown(
        build_presentation_with_graphics(), "graphics.pptx"
    )

    assert "visible text" in markdown
    assert "image secret" not in markdown
    assert "chart secret" not in markdown
    assert "category secret" not in markdown
    assert "series secret" not in markdown


def _mock_impress(monkeypatch, *, output: bytes | None) -> tuple[list, list[Path], list[float]]:
    converter = _presentation_converter()
    commands: list[list[str]] = []
    workdirs: list[Path] = []
    timeouts: list[float] = []

    class Process:
        returncode = 0
        pid = 12345
        stdout = io.BytesIO()
        stderr = io.BytesIO()

        def communicate(self, *, timeout):
            timeouts.append(timeout)
            return "", ""

        def kill(self) -> None:
            pytest.fail("a successful conversion must not be killed")

    def popen(command, **kwargs):
        commands.append(command)
        source = Path(command[-1])
        workdirs.append(source.parent)
        assert source.name == "input.ppt"
        assert source.read_bytes() == b"legacy PPT bytes"
        profile_arg = next(arg for arg in command if arg.startswith("-env:UserInstallation="))
        profile_uri = profile_arg.split("=", 1)[1]
        profile = Path(unquote(urlparse(profile_uri).path))
        assert profile.parent == source.parent
        assert profile.name == "profile"
        assert command[command.index("--convert-to") + 1] == (
            "pptx:Impress MS PowerPoint 2007 XML"
        )
        assert Path(command[command.index("--outdir") + 1]) == source.parent
        assert kwargs["stdout"] == subprocess.PIPE
        assert kwargs["stderr"] == subprocess.PIPE
        if output is not None:
            destination = source.parent / "input.pptx"
            destination.write_bytes(output)
        return Process()

    monkeypatch.setattr(converter.shutil, "which", lambda name: "/usr/bin/libreoffice")
    monkeypatch.setattr(converter.subprocess, "Popen", popen)
    monkeypatch.setattr(
        converter,
        "get_settings",
        lambda: SimpleNamespace(
            word_conversion=SimpleNamespace(timeout_seconds=42)
        ),
    )
    return commands, workdirs, timeouts


def test_legacy_ppt_uses_private_fixed_input_and_impress_profile(
    monkeypatch, deck_with_content
) -> None:
    converter = _presentation_converter()
    commands, workdirs, timeouts = _mock_impress(
        monkeypatch, output=deck_with_content
    )

    markdown = converter.convert_presentation_to_markdown(
        b"legacy PPT bytes", "folder/slides.ppt"
    )

    assert "日本語" in markdown
    assert commands and Path(commands[0][-1]).name == "input.ppt"
    assert "folder/slides.ppt" not in commands[0][-1]
    assert timeouts == [42]
    assert workdirs and all(not workdir.exists() for workdir in workdirs)


def test_legacy_ppt_reports_missing_libreoffice(monkeypatch) -> None:
    converter = _presentation_converter()
    monkeypatch.setattr(converter.shutil, "which", lambda _name: None)

    with pytest.raises(RuntimeError, match=r"LibreOffice.*slides\.ppt") as caught:
        converter.convert_presentation_to_markdown(b"legacy PPT bytes", "slides.ppt")
    assert "PATH" in str(caught.value)


@pytest.mark.parametrize("output", [None, b""])
def test_legacy_ppt_rejects_missing_or_empty_output(monkeypatch, output) -> None:
    converter = _presentation_converter()
    _commands, workdirs, _timeouts = _mock_impress(monkeypatch, output=output)

    with pytest.raises(RuntimeError, match=r"slides\.ppt"):
        converter.convert_presentation_to_markdown(b"legacy PPT bytes", "slides.ppt")
    assert workdirs and all(not workdir.exists() for workdir in workdirs)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group cleanup")
def test_legacy_ppt_timeout_kills_and_reaps_process_group(monkeypatch) -> None:
    converter = _presentation_converter()
    processes = []
    killed_groups = []
    workdirs = []
    timeouts = []

    class Process:
        pid = 24680
        returncode = None
        stdout = io.BytesIO()
        stderr = io.BytesIO()

        def __init__(self) -> None:
            self.communicate_calls = 0
            self.killed = False

        def communicate(self, *, timeout):
            timeouts.append(timeout)
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                raise subprocess.TimeoutExpired("libreoffice", timeout)
            return "", ""

        def kill(self) -> None:
            self.killed = True

    def popen(command, **_kwargs):
        workdirs.append(Path(command[-1]).parent)
        process = Process()
        processes.append(process)
        return process

    monkeypatch.setattr(converter.shutil, "which", lambda _name: "/usr/bin/libreoffice")
    monkeypatch.setattr(converter.subprocess, "Popen", popen)
    monkeypatch.setattr(
        converter.os,
        "killpg",
        lambda pid, sig: killed_groups.append((pid, sig)),
    )
    monkeypatch.setattr(
        converter,
        "get_settings",
        lambda: SimpleNamespace(
            word_conversion=SimpleNamespace(timeout_seconds=17)
        ),
    )

    with pytest.raises(RuntimeError, match=r"timed out after 17s.*slides\.ppt"):
        converter.convert_presentation_to_markdown(b"legacy PPT bytes", "slides.ppt")

    assert timeouts == [17, 5]
    assert processes and processes[0].communicate_calls == 2
    assert processes[0].killed
    assert killed_groups == [(24680, signal.SIGKILL)]
    assert workdirs and all(not workdir.exists() for workdir in workdirs)

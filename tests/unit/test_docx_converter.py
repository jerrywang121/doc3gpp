"""Unit tests for the python-docx wrapper in
``doc3gpp.parsers.docx_converter``.

These tests cover the loud-failure contract (ValueError for non-docx/doc
filenames, PythonDocxNotInstalledError when python-docx is missing,
RuntimeError on empty converter output) and the happy path against the
real CR fixtures shipped under ``tests/fixtures/tdoc_cr_doc/``.

The fixture-driven conversion tests are guarded with
``@pytest.mark.skipif`` so the suite stays green in environments
without ``python-docx`` installed. Install with
``pip install doc3gpp[extract]`` to exercise them locally.
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import pytest

from doc3gpp.parsers.docx_converter import (
    PythonDocxNotInstalledError,
    convert_document_to_markdown,
)


FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "tdoc_cr_doc"


def _docx_available() -> bool:
    """Return True iff ``python-docx`` imports cleanly.

    Used by ``@pytest.mark.skipif`` to keep the install-required tests in
    the default pool (pytest.ini excludes ``online`` but
    not these) while still auto-skipping in environments that haven't
    installed the ``[extract]`` extra.
    """
    try:
        from docx import Document  # noqa: F401
    except ImportError:
        return False
    return True


def _extract_docx_from_zip(zip_path: Path) -> tuple[str, bytes]:
    """Return ``(filename, bytes)`` for the first .docx/.doc inside a CR zip.

    Mirrors the reference's ``extract_docx_from_cr_zip`` behaviour:
    skip ``__MACOSX/`` entries and prefer ``.docx`` over ``.doc`` when
    both are present.
    """
    with zipfile.ZipFile(io.BytesIO(zip_path.read_bytes())) as zf:
        word_docs = [
            name
            for name in zf.namelist()
            if name.lower().endswith((".docx", ".doc"))
            and not name.lower().startswith("__macosx/")
        ]
        if not word_docs:
            raise AssertionError(f"No Word documents found in {zip_path}")
        word_docs.sort(
            key=lambda name: (0 if name.lower().endswith(".docx") else 1, name.lower())
        )
        target = word_docs[0]
        return target, zf.read(target)


# --- 1. Loud failure: reject non-.docx filenames -------------------


@pytest.mark.parametrize(
    "filename",
    ["foo.txt", "foo.pdf", "foo", "foo.xlsx", "spec.docxx", "notes.MD"],
)
def test_rejects_non_docx_filenames_with_value_error(filename: str) -> None:
    """Any extension other than .docx must raise ValueError, not silently
    return empty."""
    with pytest.raises(ValueError, match="Unsupported extension"):
        convert_document_to_markdown(b"hello", filename)


# --- 1b. Loud failure: reject .doc (legacy binary) filenames -----------


@pytest.mark.parametrize("filename", ["foo.doc", "FOO.DOC", "foo.bar.doc"])
def test_rejects_legacy_doc_format_with_value_error(filename: str) -> None:
    """The legacy ``.doc`` binary format is rejected at the wrapper
    boundary — python-docx can only parse the modern OOXML ``.docx``
    container, so we fail fast with a clear error message rather than
    letting python-docx raise an opaque parse error mid-flight.
    """
    with pytest.raises(ValueError, match=r"only \.docx is supported"):
        convert_document_to_markdown(b"hello", filename)


# --- 2. Loud failure: missing python-docx -------------------------------


@pytest.mark.skipif(
    _docx_available(),
    reason="requires python-docx NOT to be installed (env-specific; the "
    "monkeypatch can only force a failed import when python-docx is genuinely absent)",
)
def test_missing_python_docx_raises_PythonDocxNotInstalledError(monkeypatch) -> None:
    """Forcing ``import docx`` to fail must surface as
    PythonDocxNotInstalledError with an actionable install hint — NOT a
    bare ImportError and NOT a silent empty return.

    The converter wraps ``Document(io.BytesIO(doc_bytes))``; the module
    catches the missing-docx ``ImportError`` at import time and installs
    stub symbols, so importing the module succeeds without python-docx.
    Calling ``convert_document_to_markdown`` afterwards is what surfaces
    the install hint, because the stub ``Document.__init__`` raises the
    actionable error. The module-level import succeeds in both states so
    that ``from doc3gpp.cli import main`` works with only the ``[cli]``
    extra installed.
    """
    # Remove the cached ``docx`` modules so the next import attempt
    # re-imports from scratch and surfaces the missing package.
    monkeypatch.delitem(sys.modules, "docx", raising=False)
    monkeypatch.delitem(sys.modules, "docx_converter", raising=False)
    monkeypatch.delitem(sys.modules, "doc3gpp.parsers.docx_converter", raising=False)
    monkeypatch.setitem(sys.modules, "docx", None)

    # Re-import the converter module so its top-level ``from docx import
    # Document`` runs and surfaces the simulated ImportError. The module
    # must remain importable so ``doc3gpp.cli`` works without [extract].
    import importlib

    importlib.import_module("doc3gpp.parsers.docx_converter")

    with pytest.raises(PythonDocxNotInstalledError):
        convert_document_to_markdown(b"hello", "missing.docx")


def test_PythonDocxNotInstalledError_default_message_suggests_install_extra() -> None:
    """The bare-exception default message must point users at the install extra."""
    err = PythonDocxNotInstalledError()
    assert "doc3gpp[extract]" in str(err)
    assert "pip install" in str(err)


# --- 3. REMOVED --------------------------------------------------------



# --- 4 & 5. End-to-end conversion against real fixtures ----------------


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_converts_R5s260009_fixture_to_markdown() -> None:
    """Convert the .docx inside ``R5s260009.zip`` and assert the markdown
    is non-empty and contains a recognisable 3GPP marker."""
    zip_path = FIXTURES_DIR / "R5s260009.zip"
    assert zip_path.exists(), f"fixture missing: {zip_path}"

    filename, doc_bytes = _extract_docx_from_zip(zip_path)
    markdown = convert_document_to_markdown(doc_bytes, filename)

    assert isinstance(markdown, str)
    assert markdown.strip(), "converted markdown was empty"
    assert "3GPP" in markdown, (
        f"expected 3GPP marker in markdown; got first 200 chars: {markdown[:200]!r}"
    )


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_converts_C6_250028_fixture_to_markdown() -> None:
    """Convert the .docx inside ``C6-250028.zip`` (note the filename has
    spaces and a longer descriptive suffix) and assert the markdown is
    non-empty and contains a recognisable 3GPP marker."""
    zip_path = FIXTURES_DIR / "C6-250028.zip"
    assert zip_path.exists(), f"fixture missing: {zip_path}"

    filename, doc_bytes = _extract_docx_from_zip(zip_path)
    # The filename inside the zip has spaces; the converter doesn't care,
    # but we want to be sure our wrapper also accepts it.
    assert " " in filename, "sanity: the C6 fixture has spaces in the docx name"

    markdown = convert_document_to_markdown(doc_bytes, filename)

    assert isinstance(markdown, str)
    assert markdown.strip(), "converted markdown was empty"
    assert "3GPP" in markdown, (
        f"expected 3GPP marker in markdown; got first 200 chars: {markdown[:200]!r}"
    )
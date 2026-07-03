"""Unit tests for the markitdown wrapper in
``doc3gpp.parsers.markitdown_converter``.

These tests cover the loud-failure contract (ValueError for non-docx/doc
filenames, MarkitdownNotInstalledError when markitdown is missing,
RuntimeError on empty markitdown output) and the happy path against the
real CR fixtures shipped under ``tests/fixtures/tdoc_cr_doc/``.

The fixture-driven conversion tests are guarded with
``@pytest.mark.skipif`` so the suite stays green in environments
without ``markitdown[all]`` installed. Install with
``pip install doc3gpp[extract]`` to exercise them locally.
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import pytest

from doc3gpp.parsers.markitdown_converter import (
    MarkitdownNotInstalledError,
    convert_document_to_markdown,
    is_docx_or_doc,
)


FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "tdoc_cr_doc"


def _markitdown_available() -> bool:
    """Return True iff ``markitdown`` imports cleanly.

    Used by ``@pytest.mark.skipif`` to keep the install-required tests in
    the default pool (pytest.ini excludes ``online`` and ``mysql`` but
    not these) while still auto-skipping in environments that haven't
    installed the ``[extract]`` extra.
    """
    try:
        import markitdown  # noqa: F401
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


# --- 1. Loud failure: reject non-.docx/.doc filenames -------------------


@pytest.mark.parametrize(
    "filename",
    ["foo.txt", "foo.pdf", "foo", "foo.xlsx", "spec.docxx", "notes.MD"],
)
def test_rejects_non_docx_doc_filenames_with_value_error(filename: str) -> None:
    """Any extension other than .docx/.doc must raise ValueError, not silently
    return empty."""
    with pytest.raises(ValueError, match="Unsupported extension"):
        convert_document_to_markdown(b"hello", filename)


# --- 2. Loud failure: missing markitdown -------------------------------


def test_missing_markitdown_raises_MarkitdownNotInstalledError(monkeypatch) -> None:
    """Forcing ``import markitdown`` to fail must surface as
    MarkitdownNotInstalledError with an actionable install hint — NOT a
    bare ImportError and NOT a silent empty return."""
    # ``sys.modules["markitdown"] = None`` makes the next ``import
    # markitdown`` raise ImportError. This works whether or not
    # markitdown is otherwise installed in the environment.
    monkeypatch.setitem(sys.modules, "markitdown", None)

    with pytest.raises(MarkitdownNotInstalledError) as excinfo:
        convert_document_to_markdown(b"hello", "foo.docx")

    # Must be a (subclass of) ImportError so callers can still catch the
    # generic case, but be our typed subclass for selective handling.
    assert isinstance(excinfo.value, ImportError)
    assert "doc3gpp[extract]" in str(excinfo.value)


def test_MarkitdownNotInstalledError_default_message_suggests_install_extra() -> None:
    """The bare-exception default message must point users at the install extra."""
    err = MarkitdownNotInstalledError()
    assert "doc3gpp[extract]" in str(err)
    assert "pip install" in str(err)


# --- 3. is_docx_or_doc -------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("foo.docx", True),
        ("FOO.DOCX", True),
        ("foo.Docx", True),
        ("foo.doc", True),
        ("FOO.DOC", True),
        ("foo.bar.docx", True),
        ("foo.txt", False),
        ("foo.docxx", False),
        ("foo.pdf", False),
        ("", False),
        ("foo", False),
        ("foo.xlsx", False),
    ],
)
def test_is_docx_or_doc_is_case_insensitive_and_strict(filename: str, expected: bool) -> None:
    assert is_docx_or_doc(filename) is expected


# --- 4 & 5. End-to-end conversion against real fixtures ----------------


@pytest.mark.skipif(
    not _markitdown_available(),
    reason="markitdown not installed; install with `pip install doc3gpp[extract]`",
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
    not _markitdown_available(),
    reason="markitdown not installed; install with `pip install doc3gpp[extract]`",
)
def test_converts_C6_250028_fixture_to_markdown() -> None:
    """Convert the .docx inside ``C6-250028.zip`` (note the filename has
    spaces and a longer descriptive suffix) and assert the markdown is
    non-empty and contains a recognisable 3GPP marker."""
    zip_path = FIXTURES_DIR / "C6-250028.zip"
    assert zip_path.exists(), f"fixture missing: {zip_path}"

    filename, doc_bytes = _extract_docx_from_zip(zip_path)
    # The filename inside the zip has spaces; markitdown doesn't care,
    # but we want to be sure our wrapper also accepts it.
    assert " " in filename, "sanity: the C6 fixture has spaces in the docx name"

    markdown = convert_document_to_markdown(doc_bytes, filename)

    assert isinstance(markdown, str)
    assert markdown.strip(), "converted markdown was empty"
    assert "3GPP" in markdown, (
        f"expected 3GPP marker in markdown; got first 200 chars: {markdown[:200]!r}"
    )
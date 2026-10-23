"""Unit tests for shared TDoc ZIP conversion with tolerant annexes."""

from __future__ import annotations

import hashlib
import io
import logging
import zipfile
from collections.abc import Callable

import pytest

from doc3gpp.models.tdoc_document import markdown_member_name
from doc3gpp.models.tdoc_identity import document_id_for
from doc3gpp.parsers.docx_converter import convert_document_to_markdown
from doc3gpp.parsers.tdoc_family_main import TDocMainDocumentError
from doc3gpp.parsers.tdoc_zip import TDocZipLimitError, TDocZipValidationError
from doc3gpp.parsers.tdoc_zip_convert import TDocZipConversion, convert_tdoc_zip

IDENTITY = "tsg_ran/wg5_test_ex-t1/tsgr5_100/docs/r5-123456.zip"


def _docx_available() -> bool:
    try:
        from docx import Document  # noqa: F401
    except ImportError:
        return False
    return True


def _make_docx(text: str) -> bytes:
    from docx import Document

    document = Document()
    document.add_paragraph(text)
    payload = io.BytesIO()
    document.save(payload)
    return payload.getvalue()


def _make_zip(files: list[tuple[str, bytes]]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, payload in files:
            archive.writestr(name, payload)
    return output.getvalue()


def _decode(payload: bytes, source_file: str) -> str:
    """Stub converter: the member payload is already the Markdown text."""
    return payload.decode("utf-8")


def _failing(*failing: str) -> Callable[[bytes, str], str]:
    """Stub converter that raises for the named sources and decodes the rest."""

    def convert(payload: bytes, source_file: str) -> str:
        if source_file in failing:
            raise RuntimeError(f"cannot convert {source_file}")
        return _decode(payload, source_file)

    return convert


def _convert(
    zip_bytes: bytes,
    *,
    convert: Callable[[bytes, str], str] = _decode,
    max_documents: int = 100,
) -> TDocZipConversion:
    return convert_tdoc_zip(
        zip_bytes,
        tdoc_id="R5-123456",
        identity=IDENTITY,
        max_documents=max_documents,
        max_uncompressed_bytes=0,
        convert=convert,
    )


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_stores_main_then_annexes_in_order() -> None:
    zip_bytes = _make_zip(
        [
            ("annex/b.docx", _make_docx("annex b body")),
            ("R5-123456.docx", _make_docx("main body")),
            ("annex/a.docx", _make_docx("annex a body")),
        ]
    )

    result = _convert(zip_bytes, convert=convert_document_to_markdown)

    assert result.main_source_file == "R5-123456.docx"
    assert [document.source_file for document in result.documents] == [
        "R5-123456.docx",
        "annex/a.docx",
        "annex/b.docx",
    ]
    assert [document.file_order for document in result.documents] == [0, 1, 2]
    assert result.skipped_sources == ()
    for document in result.documents:
        assert document.document_id == document_id_for(IDENTITY, document.source_file)
        assert document.markdown_file == markdown_member_name(
            document.document_id, document.source_file
        )
        assert (
            document.content_hash
            == hashlib.sha256(document.markdown_text.encode("utf-8")).hexdigest()
        )
    assert "main body" in result.documents[0].markdown_text


def test_failing_annex_is_skipped_and_not_fatal(caplog: pytest.LogCaptureFixture) -> None:
    zip_bytes = _make_zip(
        [
            ("R5-123456.docx", b"main body"),
            ("annex/a.docx", b"annex a body"),
            ("annex/bad.docx", b"corrupt"),
            ("annex/c.docx", b"annex c body"),
        ]
    )

    with caplog.at_level(logging.WARNING):
        result = _convert(zip_bytes, convert=_failing("annex/bad.docx"))

    assert result.skipped_sources == ("annex/bad.docx",)
    assert [document.source_file for document in result.documents] == [
        "R5-123456.docx",
        "annex/a.docx",
        "annex/c.docx",
    ]
    assert [document.file_order for document in result.documents] == [0, 1, 2]
    assert any(
        record.levelno == logging.WARNING and "annex/bad.docx" in record.getMessage()
        for record in caplog.records
    )


def test_failing_main_raises() -> None:
    zip_bytes = _make_zip([("R5-123456.docx", b"main body"), ("annex/a.docx", b"annex")])

    with pytest.raises(RuntimeError, match="cannot convert R5-123456.docx"):
        _convert(zip_bytes, convert=_failing("R5-123456.docx"))


def test_doc_only_main_fails_archive_with_docx_reason() -> None:
    zip_bytes = _make_zip([("R5-123456.doc", b"legacy binary")])

    with pytest.raises(TDocMainDocumentError, match="is not .docx"):
        _convert(zip_bytes)


def test_zero_text_documents_are_kept() -> None:
    zip_bytes = _make_zip([("R5-123456.docx", b""), ("annex/a.docx", b"")])

    result = _convert(zip_bytes, convert=lambda _payload, _source_file: "")

    assert [document.source_file for document in result.documents] == [
        "R5-123456.docx",
        "annex/a.docx",
    ]
    assert [document.markdown_text for document in result.documents] == ["", ""]
    empty_hash = hashlib.sha256(b"").hexdigest()
    assert [document.content_hash for document in result.documents] == [empty_hash, empty_hash]
    assert result.skipped_sources == ()


def test_zip_document_limit_raises() -> None:
    zip_bytes = _make_zip([("R5-123456.docx", b"main"), ("annex/a.docx", b"annex")])

    with pytest.raises(TDocZipLimitError):
        _convert(zip_bytes, max_documents=1)


def test_traversing_member_rejected() -> None:
    zip_bytes = _make_zip([("R5-123456.docx", b"main"), ("../evil.docx", b"evil")])

    with pytest.raises(TDocZipValidationError):
        _convert(zip_bytes)


def test_main_inside_folder_keeps_full_path() -> None:
    zip_bytes = _make_zip([("R5-123456/R5-123456.docx", b"main"), ("annex/a.docx", b"annex")])

    result = _convert(zip_bytes)

    assert result.main_source_file == "R5-123456/R5-123456.docx"
    assert result.documents[0].source_file == "R5-123456/R5-123456.docx"

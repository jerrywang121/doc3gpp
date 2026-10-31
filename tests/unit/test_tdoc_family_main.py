"""Unit tests for TDoc family main-document member selection."""

from __future__ import annotations

import io
import zipfile

import pytest

from doc3gpp.parsers.tdoc_family_main import (
    TDocMainDocumentError,
    member_names,
    select_main_member,
)


def _make_zip(files: list[tuple[str, bytes]]) -> bytes:
    """Build an in-memory ZIP, retaining insertion order for determinism checks."""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, payload in files:
            archive.writestr(name, payload)
    return output.getvalue()


def test_single_prefixed_docx_is_main() -> None:
    assert select_main_member("R5-123456", ["R5-123456.docx", "annex/x.docx"]) == "R5-123456.docx"


def test_prefix_match_is_case_insensitive() -> None:
    assert select_main_member("R5-123456", ["r5-123456.DOCX"]) == "r5-123456.DOCX"


def test_exact_name_breaks_tie() -> None:
    assert (
        select_main_member("R5-123456", ["R5-123456_rev2.docx", "R5-123456.docx"])
        == "R5-123456.docx"
    )


def test_longer_id_sibling_does_not_win() -> None:
    assert (
        select_main_member("R5-123456", ["R5-1234567.docx", "R5-123456.docx"]) == "R5-123456.docx"
    )


def test_ambiguous_without_exact_name_fails() -> None:
    with pytest.raises(TDocMainDocumentError, match="^ambiguous main document for R5-123456"):
        select_main_member("R5-123456", ["R5-123456a.docx", "R5-123456b.docx"])


def test_main_inside_folder_uses_basename() -> None:
    assert (
        select_main_member("R5-123456", ["R5-123456/R5-123456.docx", "annex/x.docx"])
        == "R5-123456/R5-123456.docx"
    )


def test_unsupported_main_reports_word_format_requirement() -> None:
    with pytest.raises(TDocMainDocumentError) as excinfo:
        select_main_member("R5-123456", docx_members=(), other_members=("R5-123456.pdf",))

    message = str(excinfo.value)
    assert "is not .docx" in message
    assert "R5-123456.pdf" in message


def test_no_main_at_all_reports_no_main() -> None:
    with pytest.raises(TDocMainDocumentError) as excinfo:
        select_main_member(
            "R5-123456",
            docx_members=["annex/x.docx"],
            other_members=["annex/readme.txt"],
        )

    assert "no main .docx, .doc, .pptx or .ppt" in str(excinfo.value)


def test_lone_longer_id_docx_is_not_a_candidate() -> None:
    with pytest.raises(
        TDocMainDocumentError,
        match="^no main [.]docx, [.]doc, [.]pptx or [.]ppt for R5-123456",
    ):
        select_main_member("R5-123456", ["R5-1234567.docx"])


def test_lone_non_digit_suffix_docx_is_main() -> None:
    assert select_main_member("R5-123456", ["R5-123456a.docx"]) == "R5-123456a.docx"


def test_longer_id_doc_is_not_a_candidate() -> None:
    with pytest.raises(
        TDocMainDocumentError,
        match="^no main [.]docx, [.]doc, [.]pptx or [.]ppt for R5-123456",
    ):
        select_main_member("R5-123456", docx_members=("R5-1234567.doc",))


@pytest.mark.parametrize("tdoc_id", ["", "   "])
def test_blank_tdoc_id_is_rejected(tdoc_id: str) -> None:
    with pytest.raises(ValueError, match="tdoc_id"):
        select_main_member(tdoc_id, ["R5-123456.docx"])


def test_member_names_skips_directories_and_macosx(tmp_path) -> None:
    archive_path = tmp_path / "tdoc.zip"
    archive_path.write_bytes(
        _make_zip(
            [
                ("R5-123456.docx", b"main"),
                ("annex/", b""),
                ("annex/x.docx", b"annex"),
                ("docs/manual.pdf", b"pdf"),
                ("__MACOSX/annex/._x.docx", b"resource fork"),
                ("Folder\\Win.docx", b"windows"),
            ]
        )
    )

    assert member_names(archive_path.read_bytes()) == (
        "R5-123456.docx",
        "annex/x.docx",
        "docs/manual.pdf",
        "Folder/Win.docx",
    )


def test_presentation_main_and_exact_ambiguity() -> None:
    assert select_main_member("R5-260001", ["R5-260001.pptx"]) == "R5-260001.pptx"

    with pytest.raises(TDocMainDocumentError, match="ambiguous"):
        select_main_member(
            "R5-260001",
            ["R5-260001.pptx", "R5-260001.docx"],
        )

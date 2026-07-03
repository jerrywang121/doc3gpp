"""Unit tests for ``doc3gpp.parsers.tdoc_file_parser``."""

from __future__ import annotations

from doc3gpp.models.tdoc_file import (
    TDocFileTypeRevision,
    TDocFileTypeReview,
    TDocFileTypeSupport,
)
from doc3gpp.parsers.tdoc_file_parser import (
    classify_tdoc_filename,
    parse_tdoc_files_from_listing,
)


_TDOC_IDS = ["R5s260001", "R5s260002", "R5w260100", "R5-260300"]


# ---------------------------------------------------------------------------
# classify_tdoc_filename
# ---------------------------------------------------------------------------


def test_classify_revision_file() -> None:
    assert classify_tdoc_filename("R5s260001r1.zip", _TDOC_IDS) == (
        "R5s260001",
        TDocFileTypeRevision,
    )


def test_classify_revision_file_high_index() -> None:
    assert classify_tdoc_filename("R5w260100r12.zip", _TDOC_IDS) == (
        "R5w260100",
        TDocFileTypeRevision,
    )


def test_classify_review_file() -> None:
    assert classify_tdoc_filename(
        "R5s260001_MCC160Comments.zip", _TDOC_IDS
    ) == ("R5s260001", TDocFileTypeReview)


def test_classify_review_revision_file() -> None:
    assert classify_tdoc_filename(
        "R5s260001_MCC160Comments_r1.zip", _TDOC_IDS
    ) == ("R5s260001", TDocFileTypeReview)


def test_classify_support_file() -> None:
    assert classify_tdoc_filename(
        "R5s260001_draft_prose.zip", _TDOC_IDS
    ) == ("R5s260001", TDocFileTypeSupport)


def test_classify_case_insensitive_zip_extension() -> None:
    assert classify_tdoc_filename(
        "R5s260001r1.ZIP", _TDOC_IDS
    ) == ("R5s260001", TDocFileTypeRevision)


def test_classify_longest_id_match_wins() -> None:
    ids = ["R5s260", "R5s260001"]
    assert classify_tdoc_filename("R5s260001r1.zip", ids) == (
        "R5s260001",
        TDocFileTypeRevision,
    )


def test_classify_base_tdoc_file_is_skipped() -> None:
    assert classify_tdoc_filename("R5s260001.zip", _TDOC_IDS) is None


def test_classify_unknown_tdoc_id_is_skipped() -> None:
    assert classify_tdoc_filename("R5s999999r1.zip", _TDOC_IDS) is None


def test_classify_non_zip_file_is_skipped() -> None:
    assert classify_tdoc_filename("readme.txt", _TDOC_IDS) is None
    assert classify_tdoc_filename("agenda.docx", _TDOC_IDS) is None


def test_classify_unrecognized_suffix_is_skipped() -> None:
    # Suffix has no underscore and no revision marker.
    assert classify_tdoc_filename("R5s260001abc.zip", _TDOC_IDS) is None


def test_classify_empty_filename_is_skipped() -> None:
    assert classify_tdoc_filename("", _TDOC_IDS) is None


def test_classify_empty_id_set_is_skipped() -> None:
    assert classify_tdoc_filename("R5s260001r1.zip", []) is None


def test_classify_id_with_underscore_suffix_is_not_misread_as_support() -> None:
    # "R5-260300_extra.zip" -> the ID "R5-260300" is in _TDOC_IDS and the
    # remainder "_extra" should be classified as a support file, not
    # collide with the "r\d+" revision pattern.
    assert classify_tdoc_filename(
        "R5-260300_extra.zip", _TDOC_IDS
    ) == ("R5-260300", TDocFileTypeSupport)


# ---------------------------------------------------------------------------
# parse_tdoc_files_from_listing
# ---------------------------------------------------------------------------


def _listing_html(*hrefs: str) -> str:
    body = "\n".join(
        f'<a class="file" href="{href}">{href.rsplit("/", 1)[-1]}</a>'
        for href in hrefs
    )
    return f"<html><body>{body}</body></html>"


def test_parse_returns_matched_files_with_absolute_urls() -> None:
    base = "https://www.3gpp.org/ftp/tsg_ran/WG5/Test_2026/Docs/"
    html = _listing_html(
        f"{base}R5s260001r1.zip",
        f"{base}R5s260001_MCC160Comments.zip",
        f"{base}R5s260001_draft.zip",
    )

    files = parse_tdoc_files_from_listing(html, base, ["R5s260001"])
    by_name = {f.file: f for f in files}

    assert len(files) == 3
    assert by_name["R5s260001r1.zip"].type == TDocFileTypeRevision
    assert by_name["R5s260001_MCC160Comments.zip"].type == TDocFileTypeReview
    assert by_name["R5s260001_draft.zip"].type == TDocFileTypeSupport
    assert all(f.tdoc_id == "R5s260001" for f in files)
    assert all(f.url.startswith(base) for f in files)


def test_parse_skips_files_for_unknown_tdoc_ids() -> None:
    base = "https://www.3gpp.org/ftp/Docs/"
    html = _listing_html(
        f"{base}R5s260001r1.zip",
        f"{base}R5s999999r1.zip",  # not in tdoc_ids
    )

    files = parse_tdoc_files_from_listing(html, base, ["R5s260001"])
    assert len(files) == 1
    assert files[0].tdoc_id == "R5s260001"


def test_parse_skips_subfolder_anchors_without_file_class() -> None:
    base = "https://www.3gpp.org/ftp/Docs/"
    html = (
        f'<html><body>'
        f'<a href="{base}Inbox/">Inbox/</a>'  # no class="file"
        f'<a class="file" href="{base}R5s260001r1.zip">R5s260001r1.zip</a>'
        f'</body></html>'
    )

    files = parse_tdoc_files_from_listing(html, base, ["R5s260001"])
    assert len(files) == 1
    assert files[0].file == "R5s260001r1.zip"


def test_parse_returns_empty_list_for_empty_tdoc_ids() -> None:
    base = "https://www.3gpp.org/ftp/Docs/"
    html = _listing_html(f"{base}R5s260001r1.zip")
    assert parse_tdoc_files_from_listing(html, base, []) == []


def test_parse_skips_base_tdoc_file() -> None:
    base = "https://www.3gpp.org/ftp/Docs/"
    html = _listing_html(
        f"{base}R5s260001.zip",  # base TDoc - already in tdocs table
        f"{base}R5s260001r1.zip",
    )

    files = parse_tdoc_files_from_listing(html, base, ["R5s260001"])
    assert [f.file for f in files] == ["R5s260001r1.zip"]


def test_parse_preserves_filename_text_over_href_basename() -> None:
    base = "https://www.3gpp.org/ftp/Docs/"
    # The href and the anchor text are intentionally different to
    # verify the parser uses the human-readable text in the stored
    # ``file`` field.
    html = (
        f'<html><body>'
        f'<a class="file" href="{base}encoded%20name.zip">R5s260001r1.zip</a>'
        f'</body></html>'
    )

    files = parse_tdoc_files_from_listing(html, base, ["R5s260001"])
    assert files[0].file == "R5s260001r1.zip"
    assert files[0].url == f"{base}encoded%20name.zip"

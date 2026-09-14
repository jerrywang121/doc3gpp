from __future__ import annotations

from doc3gpp.parsers.html_parsers import parse_title
from doc3gpp.parsers.normalizers import clean_whitespace, normalize_ftp_path


def test_parse_title_extracts_title_text() -> None:
    html = "<html><head><title>  Hello 3GPP  </title></head><body></body></html>"
    assert parse_title(html) == "Hello 3GPP"


def test_parse_title_returns_empty_without_title() -> None:
    html = "<html><head></head><body>No title</body></html>"
    assert parse_title(html) == ""


def test_clean_whitespace_collapses_runs() -> None:
    assert clean_whitespace("a   b\n\t c") == "a b c"


def test_normalize_ftp_path_lowercases_whole_relative_path() -> None:
    # The DB joins sidecars to ``tdocs`` on exact ``ftp_url`` equality,
    # so the normaliser canonicalises the whole relative path (server
    # is case-insensitive end to end — probed live).
    assert (
        normalize_ftp_path(
            "https://www.3gpp.org/ftp/TSG_RAN/WG5_Test_ex-T1/TTCN/TTCN_CRs/2026/Docs/R5s260121.zip"
        )
        == "tsg_ran/wg5_test_ex-t1/ttcn/ttcn_crs/2026/docs/r5s260121.zip"
    )
    assert normalize_ftp_path("Joint_Meetings/foo.ZIP") == "joint_meetings/foo.zip"

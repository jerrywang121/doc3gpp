from __future__ import annotations

from doc3gpp.parsers.html_parsers import parse_title
from doc3gpp.parsers.normalizers import clean_whitespace


def test_parse_title_extracts_title_text() -> None:
    html = "<html><head><title>  Hello 3GPP  </title></head><body></body></html>"
    assert parse_title(html) == "Hello 3GPP"


def test_parse_title_returns_empty_without_title() -> None:
    html = "<html><head></head><body>No title</body></html>"
    assert parse_title(html) == ""


def test_clean_whitespace_collapses_runs() -> None:
    assert clean_whitespace("a   b\n\t c") == "a b c"

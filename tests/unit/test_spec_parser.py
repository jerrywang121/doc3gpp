from pathlib import Path

from doc3gpp.parsers.spec_parser import parse_spec_list

FIXTURE = Path(__file__).parent.parent / "fixtures" / "spec_pages" / "R5_list.html"


def test_parse_spec_list_extracts_specs() -> None:
    html = FIXTURE.read_text(encoding="utf-8")
    specs = parse_spec_list(html, "R5")
    assert len(specs) == 2
    assert specs[0].spec_id == "36.579-5"
    assert specs[0].type == "TS"
    assert specs[0].title == "NR UE conformance test"
    assert specs[0].tsg == "R5"
    assert specs[1].spec_id == "38.760-1"
    assert specs[1].type == "TR"


def test_parse_spec_list_skips_bad_rows() -> None:
    assert parse_spec_list("<html><body></body></html>", "R5") == []

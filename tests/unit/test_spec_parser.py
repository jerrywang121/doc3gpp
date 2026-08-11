from pathlib import Path

from doc3gpp.parsers.spec_parser import (
    extract_cr_tdocs,
    extract_etsi_pdf_url,
    parse_spec_detail,
    parse_spec_list,
)

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


DETAIL_FIXTURE = Path(__file__).parent.parent / "fixtures" / "spec_pages" / "R5_detail.html"


def test_parse_spec_detail_header_fields() -> None:
    html = DETAIL_FIXTURE.read_text(encoding="utf-8")
    header, versions = parse_spec_detail(html, "36.579-5", "R5")
    assert header.status == "Under change control"
    assert header.initial_release == "Rel-20"
    assert header.radio_tech == "2G,3G,LTE,5G"
    assert header.wis == "NR_CONFORMANCE,RF_TESTING"
    assert header.tsg == "R5"


PORTAL_FIXTURE = (
    Path(__file__).parent.parent / "fixtures" / "spec_pages" / "R5_detail_portal.html"
)


def test_parse_spec_detail_header_wis_telerik_grid() -> None:
    html = PORTAL_FIXTURE.read_text(encoding="utf-8")
    header, versions = parse_spec_detail(html, "38.508-1", "R5")
    assert header.wis == "5GS_NR_LTE-UEConTest,NR_SA-UEConTest"
    assert header.status == "Under change control"
    assert header.initial_release == "Rel-15"
    assert header.radio_tech == "LTE,5G"
    assert len(versions) == 1


def test_parse_spec_detail_versions() -> None:
    html = DETAIL_FIXTURE.read_text(encoding="utf-8")
    header, versions = parse_spec_detail(html, "36.579-5", "R5")
    assert len(versions) == 2
    v0 = versions[0]
    assert v0.version == "18.3.0"
    assert v0.release == "Rel-18"
    assert v0.meeting_id == 108
    assert v0.meeting_name == "RAN#108"
    assert v0.version_id == 92276
    assert v0.wki_id == 12345
    assert v0.upload_date.isoformat() == "2025-06-01"
    assert v0.comment == "Some comment here"
    v1 = versions[1]
    assert v1.release == "Rel-17"
    assert v1.meeting_id == 100
    assert v1.version_id == 90000
    assert v1.wki_id is None
    assert v1.pdf_url is None


ETSI_FIXTURE = Path(__file__).parent.parent / "fixtures" / "spec_pages" / "R5_etsi.html"
CRS_FIXTURE = Path(__file__).parent.parent / "fixtures" / "spec_pages" / "R5_crs.html"


def test_extract_etsi_pdf_url() -> None:
    html = ETSI_FIXTURE.read_text(encoding="utf-8")
    url = extract_etsi_pdf_url(html)
    assert url is not None
    assert url.endswith(".pdf")


def test_extract_etsi_pdf_url_miss() -> None:
    assert extract_etsi_pdf_url("<html></html>") is None


def test_extract_cr_tdocs() -> None:
    html = CRS_FIXTURE.read_text(encoding="utf-8")
    assert extract_cr_tdocs(html) == ["R5-253030", "R5-253031"]

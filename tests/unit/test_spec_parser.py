from pathlib import Path

from doc3gpp.parsers.spec_parser import (
    extract_cr_tdocs,
    extract_etsi_pdf_url,
    normalise_tsg_long_name,
    parse_dynareport_header,
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
    assert header.rapporteurs == "Ericsson LM"
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


HEADER_HTML = """
<html><body>
<table>
  <tr>
    <td class="TabLineLeft">
      <span id="titleLbl">Title:</span>
    </td>
    <td class="TabLineRight">
      <span id="titleVal">Presence service using the IP Multimedia (IM) Core Network (CN) subsystem; Stage 3</span>
    </td>
  </tr>
  <tr>
    <td class="TabLineLeft">
      <span id="typeLbl">Type:</span>
    </td>
    <td class="TabLineRight">
      <span id="typeVal">Technical specification (TS)</span>
    </td>
  </tr>
  <tr>
    <td class="TabLineLeft">
      <span id="PrimaryResponsibleGroupLbl">Primary responsible group:</span>
    </td>
    <td class="TabLineRight">
      <span>
        <span>CT 1</span>
      </span>
    </td>
  </tr>
</table>
</body></html>
"""


def test_parse_dynareport_header_extracts_all_three_fields() -> None:
    fields = parse_dynareport_header(HEADER_HTML)
    assert fields.title == (
        "Presence service using the IP Multimedia (IM) Core Network "
        "(CN) subsystem; Stage 3"
    )
    assert fields.type == "TS"
    assert fields.tsg_long_name == "CT 1"


def test_parse_dynareport_header_type_tr_token() -> None:
    html = HEADER_HTML.replace(
        '<span id="typeVal">Technical specification (TS)</span>',
        '<span id="typeVal">Technical report (TR)</span>',
    )
    assert parse_dynareport_header(html).type == "TR"


def test_parse_dynareport_header_missing_fields_are_none() -> None:
    html = "<html><body><table></table></body></html>"
    fields = parse_dynareport_header(html)
    assert fields == (None, None, None)


def test_normalise_tsg_long_name_ran_with_number() -> None:
    assert normalise_tsg_long_name("RAN 1") == "R1"
    assert normalise_tsg_long_name("RAN WG1") == "R1"
    assert normalise_tsg_long_name("RAN5") == "R5"
    assert normalise_tsg_long_name("ran 5") == "R5"


def test_normalise_tsg_long_name_ct_and_sa() -> None:
    assert normalise_tsg_long_name("CT 1") == "C1"
    assert normalise_tsg_long_name("CT 3") == "C3"
    assert normalise_tsg_long_name("SA 2") == "S2"
    assert normalise_tsg_long_name("SA WG6") == "S6"


def test_normalise_tsg_long_name_plenary() -> None:
    assert normalise_tsg_long_name("RT") == "RT"
    assert normalise_tsg_long_name("RP") == "RP"
    assert normalise_tsg_long_name("CP") == "CP"
    assert normalise_tsg_long_name("SP") == "SP"


def test_normalise_tsg_long_name_unknown_returns_none() -> None:
    assert normalise_tsg_long_name("RAN AH1") is None
    assert normalise_tsg_long_name("RAN") is None
    assert normalise_tsg_long_name("") is None
    assert normalise_tsg_long_name("bogus") is None

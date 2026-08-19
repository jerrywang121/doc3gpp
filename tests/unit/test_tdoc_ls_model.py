import pytest

from doc3gpp.models.tdoc_ls import TDocLSDetails, TDocLSParserResult


def test_default_construction_is_valid():
    d = TDocLSDetails()
    assert d.tdoc_id is None
    assert d.ftp_url is None
    assert d.variant == "3gpp"
    assert d.title is None
    assert d.response_to is None
    assert d.release is None
    assert d.work_item_name is None
    assert d.work_item_code is None
    assert d.source is None
    assert d.to_groups == ""
    assert d.cc_groups == ""
    assert d.attachments == ()
    assert d.parser_version == "1.0.0"
    assert d.extracted_at is None


def test_empty_string_ftp_url_is_rejected():
    with pytest.raises(ValueError, match="non-empty ftp_url"):
        TDocLSDetails(ftp_url="   ")


def test_empty_string_tdoc_id_is_rejected():
    with pytest.raises(ValueError, match="non-empty tdoc_id"):
        TDocLSDetails(tdoc_id="   ")


def test_none_string_fields_pass_through():
    d = TDocLSDetails(
        tdoc_id="R5-240001",
        ftp_url="tsg/ls/R5-240001.doc",
        title="LS on foo",
        response_to="LS R5-234567 on foo from RAN WG2",
        release="Rel-17",
        work_item_name="5G_eHealth",
        work_item_code="WI-123456",
        source="3GPP TSG",
        attachments=({"doc_number": "TR 38.901 v0.1.0", "description": ""},),
    )
    assert d.title == "LS on foo"
    assert d.attachments[0]["doc_number"] == "TR 38.901 v0.1.0"


def test_parse_result_default_is_none_cover():
    r = TDocLSParserResult()
    assert r.cover is None

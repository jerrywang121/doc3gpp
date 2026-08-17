import pytest

from doc3gpp.parsers.ls.header import LSHeaderMissingError
from doc3gpp.parsers.ls.ls_parsers import LSParserBase
from doc3gpp.parsers.ls.variants.three_gpp import ThreeGPPLSParser


_LS_MD = """3GPP TSG RAN WG2 Meeting #104\tTDoc R5-240001

Title:\tLS on 5G_eHealth WI status update
Response to:\tLS R5-234567 on 5G_eHealth WI status from RAN WG3
Release:\tRelease 17
Work Item:\t5G_eHealth (WI-123456)

Source:\t3GPP TSG RAN WG2
To:\tRAN WG3
Cc:\tSA WG2

Attachments:\tTR 38.901 v0.1.0 [draft].
"""


def test_three_gpp_parser_stamps_variant_and_happy_path():
    parser = ThreeGPPLSParser()
    result = parser.parse_ls(_LS_MD, tdoc_id="R5-240001")
    assert result.cover is not None
    assert result.cover.tdoc_id == "R5-240001"
    assert result.cover.variant == "3gpp"
    assert result.cover.title == "LS on 5G_eHealth WI status update"
    assert result.cover.work_item_code == "WI-123456"


def test_supports_requires_ls_tdoc_type():
    parser = ThreeGPPLSParser()
    assert parser.supports("R5-240001", tdoc_type="LS", source="3GPP TSG") is True
    assert parser.supports("R5-240001", tdoc_type="CR") is False
    assert parser.supports("R5-240001", tdoc_type=None) is False


def test_parse_method_raises_for_ls_variant():
    parser = ThreeGPPLSParser()
    with pytest.raises(NotImplementedError, match="does not parse CR"):
        parser.parse(_LS_MD, tdoc_id="R5-240001")


def test_missing_header_raises_ls_header_missing_error():
    parser = ThreeGPPLSParser()
    bad_md = "| CHANGE REQUEST |\n| 38.300 | CR | 1234 | rev | 1 |\n"
    with pytest.raises(LSHeaderMissingError) as excinfo:
        parser.parse_ls(bad_md, tdoc_id="R5-240001")
    assert excinfo.value.snippet == "\n".join(bad_md.splitlines()[:100])


def test_ls_parser_base_is_abstract():
    """LSParserBase can be subclassed with a custom cover extractor."""

    class _Stub:
        VARIANT = "stub"
        name = "stub_cover"

        @staticmethod
        def supports_source(source):
            return True

        def parse(self, lines, *, max_text_length=0, full=False):
            return True, {"title": "stub-title"}, len(lines)

    base = LSParserBase(cover=_Stub())
    assert base.parse_ls(_LS_MD, tdoc_id="X-1").cover.title == "stub-title"

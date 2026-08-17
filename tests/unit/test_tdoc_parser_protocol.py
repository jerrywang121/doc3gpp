import pytest

from doc3gpp.parsers.cr.cr_parsers import CRParser
from doc3gpp.parsers.ls.variants.three_gpp import ThreeGPPLSParser
from doc3gpp.parsers.tdoc_parsers import build_default_registry


def test_default_registry_resolves_ls_to_three_gpp():
    reg = build_default_registry()
    parser = reg.resolve("R5-240001", tdoc_type="LS", source="3GPP TSG")
    assert isinstance(parser, ThreeGPPLSParser)


def test_default_registry_ls_with_unknown_source_resolves_to_three_gpp():
    reg = build_default_registry()
    parser = reg.resolve("R5-240001", tdoc_type="LS", source="IEEE 802.11")
    assert isinstance(parser, ThreeGPPLSParser)


def test_default_registry_ls_with_none_source_resolves_to_three_gpp():
    reg = build_default_registry()
    parser = reg.resolve("R5-240001", tdoc_type="LS", source=None)
    assert isinstance(parser, ThreeGPPLSParser)


def test_default_registry_still_resolves_cr_to_cr_parser():
    reg = build_default_registry()
    parser = reg.resolve("R5-240001", tdoc_type="CR")
    assert isinstance(parser, CRParser)


def test_cr_parser_parse_ls_raises_not_implemented():
    parser = CRParser()
    with pytest.raises(NotImplementedError):
        parser.parse_ls("dummy", tdoc_id="X-1")

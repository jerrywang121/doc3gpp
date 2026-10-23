from types import SimpleNamespace

import pytest

from doc3gpp.models.schema_info import TDOC_TYPES
from doc3gpp.models.tdoc_content_families import (
    FAMILY_STORES,
    TDOC_CONTENT_STORES,
    eligible_type_values,
    is_complete_cr_extraction,
    is_complete_family_extraction,
    is_eligible_type,
    store_for_type,
)


def _meta(**kw):
    base = {
        "ftp_url": "x",
        "tdoc_id": "R5-000001",
        "doc_filename": "main.docx",
        "extraction_version": 2,
        "document_count": 0,
        "skipped_sources": (),
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _doc(source):
    return SimpleNamespace(source_file=source)


def test_store_lookup_is_case_insensitive_and_strips():
    assert store_for_type("WID new").table_name == "tdoc_wid_contents"
    assert store_for_type(" wid revised ").key == "wid"
    assert store_for_type("LS out").table_name == "tdoc_ls_contents"
    assert store_for_type("Discussion").table_name == "tdoc_discussion_contents"
    assert store_for_type("cr").table_name == "tdoc_cr_annexes"
    assert store_for_type("cr").includes_main is False


def test_stores_and_family_stores_are_ordered():
    assert [store.key for store in TDOC_CONTENT_STORES] == ["cr", "wid", "ls", "discussion"]
    assert [store.key for store in FAMILY_STORES] == ["wid", "ls", "discussion"]
    assert all(store.includes_main for store in FAMILY_STORES)


def test_unmatched_types_have_no_store():
    for value in ("other", "pCR", "", "   ", None):
        assert store_for_type(value) is None


def test_eligibility_and_exact_value_tuple():
    assert is_eligible_type("cr") and is_eligible_type("LS in")
    assert not is_eligible_type("other")
    assert eligible_type_values() == ("CR", "WID new", "WID revised", "LS in", "LS out", "discussion")


def test_store_values_are_known_tdoc_types():
    known = {t.casefold() for t in TDOC_TYPES}
    for store in TDOC_CONTENT_STORES:
        assert {v.casefold() for v in store.type_values} <= known


@pytest.mark.parametrize("kwargs, expected", [
    ({}, True),
    ({"skipped_sources": ("a.docx",)}, False),
    ({"extraction_version": 1}, False),
    ({"document_count": 2}, False),
])
def test_family_rule_parametrized(kwargs, expected):
    docs = [_doc("main.docx")]
    meta = _meta(**{"document_count": 1, **kwargs})
    assert is_complete_family_extraction(meta, docs) is expected


def test_family_rule_rejects_missing_or_unordered_main():
    assert not is_complete_family_extraction(_meta(document_count=2), [_doc("a.docx"), _doc("main.docx")])
    assert not is_complete_family_extraction(_meta(document_count=0), [])


def test_family_rule_rejects_duplicate_sources():
    docs = [_doc("main.docx"), _doc("a.docx"), _doc("a.docx")]
    assert not is_complete_family_extraction(_meta(document_count=3), docs)


def test_cr_rule_zero_annexes_complete_with_cover():
    assert is_complete_cr_extraction(_meta(document_count=0), [], has_cover=True)
    assert not is_complete_cr_extraction(_meta(document_count=0), [], has_cover=False)


def test_cr_rule_requires_extraction_version_two():
    assert not is_complete_cr_extraction(
        _meta(extraction_version=1, document_count=0), [], has_cover=True
    )


def test_cr_rule_rejects_main_among_annexes_and_count_mismatch():
    assert not is_complete_cr_extraction(_meta(document_count=1), [_doc("main.docx")], has_cover=True)
    assert not is_complete_cr_extraction(_meta(document_count=2), [_doc("a.docx")], has_cover=True)
    assert not is_complete_cr_extraction(_meta(document_count=1, skipped_sources=("b.docx",)), [_doc("a.docx")], has_cover=True)

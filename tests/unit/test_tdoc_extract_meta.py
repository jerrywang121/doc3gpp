"""Unit coverage for TDocExtractMeta skipped-source and completeness rules."""

from __future__ import annotations

import pytest

from doc3gpp.models.tdoc_content_families import is_complete_cr_extraction
from doc3gpp.models.tdoc_cr import TDocExtractMeta


def _meta(**overrides) -> TDocExtractMeta:
    values = {
        "ftp_url": "r5/docs/r5s260009.zip",
        "tdoc_id": "R5s260009",
        "cache_file": "r5s260009.zip",
        "doc_filename": "R5s260009.docx",
    }
    values.update(overrides)
    return TDocExtractMeta(**values)


def test_extract_meta_skipped_sources_validation() -> None:
    assert _meta().skipped_sources == ()

    accepted = _meta(skipped_sources=("annex/bad.docx",))
    assert accepted.skipped_sources == ("annex/bad.docx",)

    with pytest.raises(ValueError, match="skipped_sources"):
        _meta(skipped_sources=("",))

    with pytest.raises(ValueError, match="skipped_sources"):
        _meta(skipped_sources=("   ",))


def test_extract_meta_skipped_sources_rejects_bare_string_bytes_and_none() -> None:
    with pytest.raises(ValueError, match="skipped_sources"):
        _meta(skipped_sources="annex/bad.docx")

    with pytest.raises(ValueError, match="skipped_sources"):
        _meta(skipped_sources=b"annex/bad.docx")

    with pytest.raises(ValueError, match="skipped_sources"):
        _meta(skipped_sources=None)


def test_extract_meta_skipped_sources_list_is_stored_as_tuple() -> None:
    meta = _meta(skipped_sources=["annex/bad.docx"])
    assert meta.skipped_sources == ("annex/bad.docx",)


def test_extract_meta_complete_rule() -> None:
    assert is_complete_cr_extraction(_meta(extraction_version=2), (), has_cover=True)

    skipped = _meta(extraction_version=2, skipped_sources=("annex/bad.docx",))
    assert not is_complete_cr_extraction(skipped, (), has_cover=True)

    assert not is_complete_cr_extraction(_meta(extraction_version=1), (), has_cover=True)

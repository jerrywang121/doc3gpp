from __future__ import annotations

import hashlib

from doc3gpp.models.tdoc_document import (
    TDocDocument,
    markdown_member_name,
    ordered_sources,
    sanitized_source_stem,
    source_sort_key,
)
from doc3gpp.parsers.tdoc_zip import _markdown_member_for  # regression anchor


def test_stem_strips_docx_and_sanitizes():
    assert sanitized_source_stem("R5-123456.docx") == "R5-123456"
    assert sanitized_source_stem("R5-123456.DOCX") == "R5-123456"
    assert sanitized_source_stem("annex/with space.docx") == "with_space"
    assert sanitized_source_stem("annex/Ünïcode > x.docx") == "n_code_x"


def test_empty_stem_falls_back_to_document():
    assert sanitized_source_stem("..docx") == "document"


def test_ordering_is_main_first_then_casefold_path():
    sources = ["annex/b.docx", "Annex/A.docx", "main.docx"]
    assert ordered_sources(sources, "main.docx") == ("main.docx", "Annex/A.docx", "annex/b.docx")


def test_source_sort_key_is_casefold_then_original_path():
    sources = ["b.docx", "a.docx", "A.docx"]
    assert sorted(sources, key=source_sort_key) == ["A.docx", "a.docx", "b.docx"]


def test_member_name_uses_document_id_and_stem():
    assert markdown_member_name("ab" * 32, "CRs/R5-1.docx") == f"documents/{'ab' * 32}/R5-1.md"


def test_parser_member_name_matches_shared_helper():
    document_id = "ab" * 32
    source_file = "annex/with space.docx"
    document = TDocDocument(
        document_id=document_id,
        source_file=source_file,
        markdown_file=f"documents/{document_id}/with_space.md",
        file_order=0,
        markdown_text="body",
        content_hash=hashlib.sha256(b"body").hexdigest(),
    )
    member = _markdown_member_for(document)
    assert member == markdown_member_name(document_id, source_file)
    assert member.endswith("/with_space.md")

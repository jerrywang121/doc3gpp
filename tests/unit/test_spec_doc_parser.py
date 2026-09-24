"""Unit tests for spec-doc multi-docx listing, ordering, and TOC extraction."""
from __future__ import annotations

import io
import logging
import zipfile

from doc3gpp.parsers.docx_converter import HeadingBlock, ParagraphBlock
from doc3gpp.parsers.spec_doc import (
    SpecDocFileBlocks,
    extract_spec_toc,
    list_spec_docx,
    order_spec_files,
)


def _f(name, first):
    sec, title = first
    if sec:
        return SpecDocFileBlocks(
            file_order=0,
            source_file=name,
            blocks=[HeadingBlock(1, sec, title, f"# {sec} {title}")],
        )
    return SpecDocFileBlocks(
        file_order=0, source_file=name, blocks=[ParagraphBlock("Foreword text")]
    )


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_list_spec_docx_returns_docx_pairs():
    payload = _zip_bytes(
        {"a.docx": b"AAA", "sub/b.docx": b"BBB", "note.txt": b"ignore"}
    )
    out = list_spec_docx(payload)
    assert sorted(n for n, _ in out) == ["a.docx", "sub/b.docx"]
    assert dict(out)["a.docx"] == b"AAA"


def test_list_spec_docx_skips_macosx_and_warns_on_legacy_doc(caplog):
    payload = _zip_bytes(
        {"__MACOSX/._a.docx": b"junk", "legacy.doc": b"old", "ok.docx": b"OK"}
    )
    with caplog.at_level(logging.WARNING):
        out = list_spec_docx(payload)
    assert [n for n, _ in out] == ["ok.docx"]
    assert any("legacy.doc" in r.message for r in caplog.records)


def test_order_numbered_first():
    files = [_f("b.docx", ("5", "Later")), _f("a.docx", ("4", "Earlier"))]
    ordered = order_spec_files(files)
    assert [f.source_file for f in ordered] == ["a.docx", "b.docx"]
    assert [f.file_order for f in ordered] == [0, 1]


def test_order_numbered_files_handles_alphanumeric_sections():
    files = [
        _f("late.docx", ("7.2A.3A", "Later")),
        _f("early.docx", ("7.2A.3", "Earlier")),
        _f("numeric.docx", ("7.10", "Numeric")),
    ]
    ordered = order_spec_files(files)
    assert [f.source_file for f in ordered] == [
        "early.docx", "late.docx", "numeric.docx",
    ]


def test_toc_preserves_alphanumeric_section_number():
    entries, _ = extract_spec_toc(
        [_f("part.docx", ("7.2A.3A", "Extended details"))]
    )
    assert entries[0].section_no == "7.2A.3A"


def test_toc_tiebreak_first():
    toc = _f("toc.docx", (None, "x"))
    toc.blocks = [HeadingBlock(1, None, "Contents", "# Contents")]
    numbered = _f("n.docx", ("1", "Scope"))
    ordered = order_spec_files([numbered, toc])
    assert ordered[0].source_file == "toc.docx"


def test_order_numbered_with_contents_heading_sorts_by_section():
    numbered = _f("n.docx", ("2", "Refs"))
    numbered.blocks.append(HeadingBlock(1, None, "Contents", "# Contents"))
    toc = SpecDocFileBlocks(
        file_order=0,
        source_file="toc.docx",
        blocks=[HeadingBlock(1, None, "Contents", "# Contents")],
    )
    first = _f("s.docx", ("1", "Scope"))
    ordered = order_spec_files([numbered, toc, first])
    assert [f.source_file for f in ordered] == ["toc.docx", "s.docx", "n.docx"]


def test_order_unnumbered_last_lexicographic():
    files = [
        _f("z.docx", (None, "x")),
        _f("m.docx", ("1", "Scope")),
        _f("a.docx", (None, "y")),
    ]
    ordered = order_spec_files(files)
    assert [f.source_file for f in ordered] == ["m.docx", "a.docx", "z.docx"]
    assert [f.file_order for f in ordered] == [0, 1, 2]


def test_extract_toc_shape():
    files = order_spec_files(
        [_f("a.docx", ("1", "Scope")), _f("b.docx", ("2", "Refs"))]
    )
    entries, filemap = extract_spec_toc(files)
    assert [(e.section_no, e.title) for e in entries] == [
        ("1", "Scope"),
        ("2", "Refs"),
    ]
    assert [f.source_file for f in filemap] == ["a.docx", "b.docx"]

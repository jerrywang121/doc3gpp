"""Multi-docx listing, content-derived ordering, and TOC extraction. Pure parsing, no network."""
from __future__ import annotations

import io
import logging
import zipfile
from dataclasses import dataclass, field

from doc3gpp.models.spec_doc import SpecDocTocEntry, SpecDocTocFile
from doc3gpp.parsers.docx_converter import (
    Block,
    HeadingBlock,
    ParagraphBlock,
    TableBlock,
    convert_document_to_blocks,
)

__all__ = [
    "Block",
    "HeadingBlock",
    "ParagraphBlock",
    "TableBlock",
    "SpecDocFileBlocks",
    "convert_document_to_blocks",
    "extract_spec_toc",
    "list_spec_docx",
    "order_spec_files",
]

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SpecDocFileBlocks:
    file_order: int
    source_file: str
    blocks: list[Block] = field(default_factory=list)


def list_spec_docx(zip_bytes: bytes) -> list[tuple[str, bytes]]:
    out = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        for info in z.infolist():
            name = info.filename
            if info.is_dir() or name.startswith("__MACOSX/"):
                continue
            low = name.lower()
            if low.endswith(".docx"):
                out.append((name, z.read(info.filename)))
            elif low.endswith(".doc"):
                logger.warning(
                    "skipping legacy .doc entry %s (only .docx is parsed)", name
                )
    return out


_TOC_TITLES = {"contents", "table of contents", "content"}


def _first_section(blocks: list[Block]) -> tuple[int, ...] | None:
    for b in blocks:
        if isinstance(b, HeadingBlock) and b.section_no:
            try:
                return tuple(int(p) for p in b.section_no.split("."))
            except ValueError:
                return None
    return None


def _is_toc_file(blocks: list[Block]) -> bool:
    return any(
        isinstance(b, HeadingBlock) and b.title.strip().lower() in _TOC_TITLES
        for b in blocks[:5]
    )


def order_spec_files(
    files: list[SpecDocFileBlocks],
) -> list[SpecDocFileBlocks]:
    # A front-matter file (TOC-like headings, no numeric section) sorts before
    # everything — it is the cover/contents. Numbered files sort by section
    # tuple even when they contain a contents heading further down.
    def norm(f: SpecDocFileBlocks) -> tuple[int, tuple[int, ...], int, str]:
        sec = _first_section(f.blocks)
        if sec is None and _is_toc_file(f.blocks):
            return (-1, (), 0, f.source_file)
        if sec is not None:
            return (0, sec, 0, f.source_file)
        logger.debug("unnumbered spec file sorts last: %s", f.source_file)
        return (1, (), 0, f.source_file)

    ordered = sorted(files, key=norm)
    for i, f in enumerate(ordered):
        f.file_order = i
    return ordered


def extract_spec_toc(
    ordered: list[SpecDocFileBlocks],
) -> tuple[list[SpecDocTocEntry], list[SpecDocTocFile]]:
    entries: list[SpecDocTocEntry] = []
    filemap: list[SpecDocTocFile] = []
    for f in ordered:
        first = None
        for b in f.blocks:
            if isinstance(b, HeadingBlock):
                if first is None and b.section_no:
                    first = b.section_no
                entries.append(
                    SpecDocTocEntry(
                        b.level, b.section_no, b.title, f.source_file, f.file_order
                    )
                )
        filemap.append(SpecDocTocFile(f.source_file, f.file_order, first))
    return entries, filemap

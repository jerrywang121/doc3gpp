"""python-docx → markdown converter for TDoc .docx extraction.

The converter is a thin wrapper around python-docx that renders the OOXML
document tree into a markdown string. The converter is not intended to be
a general-purpose ``.docx`` → markdown library; it is tuned to the specific
needs of TDoc CR extraction. In particular: 
- it ignores any bold/italic formatting to make it easier to parse
- it has limited support for tables (complex table structure may be lost)
- it ignores images and other non-text content
- it tries to ensure all field references are rendered (e.g. property fields in 3GPP TDocs templates that used in the cover page).
- it removes certain fields tags from the output (e.g. TOC, PAGEREF, DOCPROPERTY)
- it preserves tabs in the output, but normalises them to 4xspaces respectively
- it preserves line breaks in tables, but normalises them to <br>, and preserves spaces in table cells - this is important for parsing tables with multiple lines in a single cell (e.g. cover-page, TTCN changes tables, etc.)
- it tries to render inserted/deleted revision marks in a way that is parseable (e.g. <ins>...</ins> and <del>...</del>)


* Reject anything that is not a ``.docx`` filename with ``ValueError``
  — python-docx only supports the OOXML/``.docx`` format, so we fail
  fast on the legacy ``.doc`` binary format rather than letting
  python-docx raise an opaque error mid-parse. The caller is the only
  one who could feed us a different format, and silently doing nothing
  is worse than refusing.
* Surface a missing :mod:`docx` (python-docx) package as
  :class:`PythonDocxNotInstalledError` (an :class:`ImportError` subclass)
  with an actionable install hint. ``docx`` is in the ``[extract]``
  extra (``pip install doc3gpp[extract]``); the lazy import keeps the
  rest of the package importable without the extra installed.
* Raise :class:`RuntimeError` when the rendered markdown is empty so a
  regression in the converter surfaces immediately rather than
  degrading the downstream CR parser silently.
"""

from __future__ import annotations

import io
import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

logger = logging.getLogger(__name__)


class PythonDocxNotInstalledError(ImportError):
    """Raised when python-docx is required but not installed.

    Surfaces an actionable install hint instead of letting a bare
    ``ImportError`` propagate up through the service layer and crash the
    ``tdoc parse`` CLI with an unfriendly traceback.
    """

    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            message
            or (
                "python-docx is required for TDoc extraction. "
                "Install with `pip install doc3gpp[extract]`."
            )
        )


# python-docx is optional — see ``[extract]`` extra in pyproject.toml.
# Module-level import is wrapped in try/except so ``import doc3gpp.cli``
# succeeds without python-docx; when the extra is missing, the stub
# definitions below replace the real symbols and raise the install hint
# only if a caller actually invokes the docx conversion path.
try:
    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph
except ImportError:

    class Document:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs) -> None:
            raise PythonDocxNotInstalledError()

    def qn(tag: str) -> str:  # type: ignore[no-redef]
        # Tag constants are built at import time, so the stub must not raise.
        return tag

    class Table:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs) -> None:
            raise PythonDocxNotInstalledError()

    class Paragraph:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs) -> None:
            raise PythonDocxNotInstalledError()


# ---------------------------------------------------------------------------
# Regex constants (compiled once at import — the reference compiles them
# inside the parsing functions, which is noisier without being faster).
# ---------------------------------------------------------------------------

# matching DOCPROPERTY field tag, e.g. ` DOCPROPERTY  TSG/WGRef  \* MERGEFORMAT `
_DOCPROPERTY_PATTERN = re.compile(r"(?:^|\s{0,1})DOCPROPERTY\s+.+?\s+\\\* MERGEFORMAT(?:\s|$)")
# matching TOC field tag, e.g. `TOC \o "1-3" `
_TOC_PATTERN = re.compile(r'^TOC\s+\\o\s+".+?"\s+')
# matching PAGEREF field tag, e.g. ` PAGEREF _Toc217032158 \h `
_PAGEREF_PATTERN = re.compile(r"\sPAGEREF\s+_Toc\d+\s+\\h\s")

# ---------------------------------------------------------------------------
# Block helpers (originally prototyped in docs/docx2md.py — promoted into
# the package and refined for the TDoc CR extraction pipeline; see the
# module docstring for the full list of behaviours).
# ---------------------------------------------------------------------------


_W_P = qn("w:p")
_W_TBL = qn("w:tbl")
_W_R = qn("w:r")
_W_RPR = qn("w:rPr")
_W_B = qn("w:b")
_W_I = qn("w:i")
_W_T = qn("w:t")
_W_TAB = qn("w:tab")
_W_DEL = qn("w:del")
_W_INS = qn("w:ins")
_W_DEL_TEXT = qn("w:delText")
_W_FLD_CHAR = qn("w:fldChar")
_W_INSTR_TEXT = qn("w:instrText")
_BREAK_TAGS = frozenset({qn("w:br"), qn("w:cr")})
_PASSTHROUGH_TAGS = frozenset({qn("w:hyperlink"), qn("w:fldSimple")})


def _is_paragraph(child) -> bool:
    return child.tag == _W_P


def _is_table(child) -> bool:
    return child.tag == _W_TBL


def _style_name(paragraph) -> str:
    return paragraph.style.name if paragraph.style else ""


def _cached_style_name(paragraph, style_cache: dict[str | None, str]) -> str:
    key = paragraph._p.style
    style_name = style_cache.get(key)
    if style_name is None:
        style_name = _style_name(paragraph).lower()
        style_cache[key] = style_name
    return style_name


def _run_formatting(run_element) -> tuple[bool, bool]:
    rpr = run_element.find(_W_RPR)
    if rpr is None:
        return False, False
    bold = rpr.find(_W_B) is not None
    italic = rpr.find(_W_I) is not None
    return bold, italic


def _wrap_text(text: str, bold: bool, italic: bool) -> str:
    if not text:
        return ""
    if bold and italic:
        return f"***{text}***"
    if bold:
        return f"**{text}**"
    if italic:
        return f"*{text}*"
    return text


def _render_revision(element, inserted: bool, bold: bool, italic: bool) -> str:
    content = "".join(_render_children(child, bold=bold, italic=italic) for child in element.iterchildren())
    content = content.strip()
    if not content:
        return ""
    return f"<ins>{content}</ins>" if inserted else f"<del>{content}</del>"


def _render_children(element, bold: bool = False, italic: bool = False) -> str:
    parts = []
    for child in element.iterchildren():
        tag = child.tag
        if tag == _W_T:
            if child.text:
                parts.append(child.text)
        elif tag == _W_TAB:
            parts.append("\t")
        elif tag in _BREAK_TAGS:
            parts.append("\n")
        elif tag == _W_R:
            run_bold, run_italic = _run_formatting(child)
            parts.append(_render_children(child, bold=bold or run_bold, italic=italic or run_italic))
        elif tag == _W_DEL:
            parts.append(_render_revision(child, inserted=False, bold=bold, italic=italic))
        elif tag == _W_INS:
            parts.append(_render_revision(child, inserted=True, bold=bold, italic=italic))
        elif tag == _W_DEL_TEXT:
            if child.text:
                parts.append(child.text)
        elif tag in _PASSTHROUGH_TAGS:
            parts.append(_render_children(child, bold=bold, italic=italic))
        elif tag == _W_FLD_CHAR:
            continue
        elif tag == _W_INSTR_TEXT:
            if child.text:
                parts.append(child.text)
        else:
            parts.append(_render_children(child, bold=bold, italic=italic))
    return "".join(parts)  # _wrap_text("".join(parts), bold, italic)


def _paragraph_to_markdown(paragraph, style_name: str | None = None) -> str:
    text = _render_children(paragraph._p)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\t", "    ")
    text = text.strip()

    if style_name is None:
        style_name = _style_name(paragraph).lower()
    if style_name.startswith("heading"):
        level = int(style_name.split()[-1]) if style_name.split()[-1].isdigit() else 1
        return f"{'#' * level} {text}"
    if style_name.startswith("title"):
        return f"# {text}"
    if style_name.startswith("subtitle"):
        return f"## {text}"
    if "list" in style_name:
        return f"- {text}"
    return text


def _grid_span(cell) -> int:
    grid_span = 1
    gs = cell._tc.xpath('.//w:gridSpan')
    if gs:
        val = gs[0].get(qn("w:val"))
        if val and val.isdigit():
            grid_span = int(val)
    return grid_span

def _in_table_paragraph_text(paragraph, preserve_leading_space: bool = False) -> str:
    # For in-table paragraphs, render the paragraph text (with field codes), preserving line breaks (as <br>) and tabs, 
    # and optionally preserving leading spaces (for preserving the paragraph structure).
    text = _render_children(paragraph._p)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\t", "    ")
    text = text.replace("\n", "<br>")  # keep table cells on one line
    if preserve_leading_space:
        return text.rstrip()  # Only remove trailing whitespace
    else:
        return text.strip()   # Remove both leading and trailing

def _table_to_markdown(table) -> str:
    rows = []
    for row in table.rows:
        cells = []
        ci = 0
        row_cells = row.cells
        total_cells = len(row_cells)
        while ci < total_cells:
            cell = row_cells[ci]
            cell_text = "<br>".join(_in_table_paragraph_text(p, preserve_leading_space=True) for p in cell.paragraphs)
            cells.append(cell_text.replace("|", "\\|"))
            span = _grid_span(cell)
            ci += max(span, 1)
        row_text = "| " + " | ".join(cells) + " |"
        if any(cell.strip() for cell in cells):
            rows.append(row_text)
    if not rows:
        return ""
    header_line = rows[0]
    header_width = len(header_line.split("|")) - 2
    separator = "| " + " | ".join(["---"] * header_width) + " |"
    return "\n".join([header_line, separator, *rows[1:]])


def _clean_text(text: str) -> str:
    # remove matching ` DOCPROPERTY  TSG/WGRef  \* MERGEFORMAT ` with regex
    text = re.sub(_DOCPROPERTY_PATTERN, "", text)
    # remove matching `TOC \o "1-3" ` with regex
    text = re.sub(_TOC_PATTERN, "", text)
    # remove matching ` PAGEREF _Toc217032158 \h ` with regex
    text = re.sub(_PAGEREF_PATTERN, " ...... ", text)
    # remove all occurrences of `**`
    # text = text.replace("**", "")
    return text.strip()


def _document_to_markdown(document: Document) -> str:
    blocks = []
    style_cache: dict[str | None, str] = {}
    body = document.element.body
    for child in body.iterchildren():
        if _is_paragraph(child):
            paragraph = Paragraph(child, document)
            paragraph_text = _paragraph_to_markdown(paragraph, _cached_style_name(paragraph, style_cache))
            if paragraph_text:
                blocks.append(_clean_text(paragraph_text))
        elif _is_table(child):
            table = Table(child, document)
            table_text = _table_to_markdown(table)
            if table_text:
                blocks.append(_clean_text(table_text))
    return "\n\n".join(blocks).strip() + "\n"


# ---------------------------------------------------------------------------
# Service-facing entry point.
# ---------------------------------------------------------------------------


def convert_document_to_markdown(doc_bytes: bytes, filename: str) -> str:
    """Convert ``.docx`` bytes to markdown via python-docx.

    Args:
        doc_bytes: Raw bytes of the Word document.
        filename: Source filename; only the extension is consulted (used
            for the extension guard, not the actual parse path).

    Returns:
        The extracted markdown text.

    Raises:
        ValueError: if ``filename``'s extension is not ``.docx``. The
            legacy ``.doc`` binary format is rejected here because
            python-docx only supports the modern OOXML container.
        PythonDocxNotInstalledError: if python-docx (``docx``) cannot be
            imported. The exception message includes the install hint.
        RuntimeError: if the converter returns no extractable text.
    """
    suffix = Path(filename).suffix.lower()
    if suffix != ".docx":
        raise ValueError(
            f"Unsupported extension {suffix!r}; only .docx is supported "
            "(python-docx cannot parse the legacy .doc binary format)"
        )

    try:
        document = Document(io.BytesIO(doc_bytes))
    except ImportError as exc:  # pragma: no cover - only triggered when [extract] missing
        logger.error(
            "python-docx is not installed; it is required for document conversion. "
            "Install with `pip install doc3gpp[extract]`."
        )
        raise PythonDocxNotInstalledError() from exc

    markdown = _document_to_markdown(document)
    if not markdown.strip():
        raise RuntimeError(
            f"python-docx returned empty result for {filename}"
        )
    return markdown


__all__ = [
    "DOCX_WORKERS",
    "Block",
    "DocxConvertPoolError",
    "DocxMemberPool",
    "HeadingBlock",
    "InlineDocxPool",
    "MemberKind",
    "ParagraphBlock",
    "PythonDocxNotInstalledError",
    "TableBlock",
    "convert_document_to_blocks",
    "convert_document_to_markdown",
]


# ---------------------------------------------------------------------------
# Block converter (spec-doc corpus).
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class HeadingBlock:
    level: int
    section_no: str | None
    title: str
    raw: str


@dataclass(slots=True, frozen=True)
class ParagraphBlock:
    text: str


@dataclass(slots=True, frozen=True)
class TableBlock:
    gfm: str
    table_no: str | None = None
    table_title: str | None = None


Block = HeadingBlock | ParagraphBlock | TableBlock


_SECTION_RE = re.compile(r"^(\d[A-Za-z0-9]*(?:\.[A-Za-z0-9]+)*)\s+(.*)$", re.DOTALL)
_TABLE_IDENTIFIER = (
    r"\d[A-Za-z0-9]*(?:\.[A-Za-z0-9]+)*"
    r"(?:-[A-Za-z0-9]+(?:\.[A-Za-z0-9]+)*)?"
)
_CAPTION_RE = re.compile(
    rf"^Table\s+(?P<table_no>{_TABLE_IDENTIFIER})"
    r"(?:\s*:\s*|\s*-\s*|\s+)(?P<title>.+)$",
    re.IGNORECASE,
)


def _split_section(text: str) -> tuple[str | None, str]:
    m = _SECTION_RE.match(text.strip())
    if m:
        return m.group(1), m.group(2).strip()
    return None, text.strip()


def _split_table_caption(text: str) -> tuple[str | None, str | None]:
    normalized = " ".join(text.split())
    match = _CAPTION_RE.fullmatch(normalized)
    if match is None:
        return None, None
    return match.group("table_no"), match.group("title").strip()


def convert_document_to_blocks(doc_bytes: bytes, filename: str) -> list[HeadingBlock | ParagraphBlock | TableBlock]:
    """Convert ``.docx`` bytes to a flat list of heading/paragraph/table blocks.

    Same extension guard, missing-dependency path, and empty-render error
    as :func:`convert_document_to_markdown`; only the wrapper is new — GFM
    rendering (:func:`_table_to_markdown`) and heading-style logic are
    unchanged.
    """
    suffix = Path(filename).suffix.lower()
    if suffix != ".docx":
        raise ValueError(f"Unsupported extension {suffix!r}; only .docx is supported")
    try:
        document = Document(io.BytesIO(doc_bytes))
    except ImportError as exc:  # pragma: no cover - only triggered when [extract] missing
        logger.error(
            "python-docx is not installed; it is required for document conversion. "
            "Install with `pip install doc3gpp[extract]`."
        )
        raise PythonDocxNotInstalledError() from exc
    raw_blocks: list[tuple[str, object]] = []  # ("h"|"p"|"t", payload)
    style_cache: dict[str | None, str] = {}
    body = document.element.body
    for child in body.iterchildren():
        if _is_paragraph(child):
            p = Paragraph(child, document)
            style = _cached_style_name(p, style_cache)
            md = _clean_text(_paragraph_to_markdown(p, style))
            if not md:
                continue
            if style.startswith(("heading", "title", "subtitle")):
                level = 1
                if style.startswith("heading"):
                    try:
                        level = int(style.split()[-1])
                    except ValueError:
                        level = 1
                sec, title = _split_section(md.lstrip("#").strip())
                raw_blocks.append(("h", HeadingBlock(level, sec, title, md)))
            else:
                raw_blocks.append(("p", ParagraphBlock(md)))
        elif _is_table(child):
            t = Table(child, document)
            gfm = _clean_text(_table_to_markdown(t))
            if gfm:
                raw_blocks.append(("t", TableBlock(gfm)))
    # second pass: attach "Table N: title" captions from adjacent paragraphs
    out: list = []
    for i, (kind, payload) in enumerate(raw_blocks):
        if kind == "t":
            no, title = None, None
            if i > 0 and raw_blocks[i - 1][0] == "p":
                no, title = _split_table_caption(
                    raw_blocks[i - 1][1].text  # type: ignore[union-attr]
                )
            if no is None and i + 1 < len(raw_blocks) and raw_blocks[i + 1][0] == "p":
                no, title = _split_table_caption(
                    raw_blocks[i + 1][1].text  # type: ignore[union-attr]
                )
            out.append(TableBlock(payload.gfm, no, title))  # type: ignore[union-attr]
        else:
            out.append(payload)
    if not out:
        raise RuntimeError(f"python-docx returned empty result for {filename}")
    return out


# ---------------------------------------------------------------------------
# DOCX member pool (fan-out contract for the spec-doc archive pipeline).
# ---------------------------------------------------------------------------

MemberKind = Literal["blocks", "markdown"]


class DocxConvertPoolError(RuntimeError):
    """A DOCX conversion worker died, so the member could not be converted."""


class DocxMemberPool(Protocol):
    """Converts ``(name, bytes)`` members; returns one outcome per member, in input order.

    An outcome is the converted value, or the exception instance raised for
    that member. Implementations never raise for a per-member failure.
    """

    def convert_members(
        self, kind: MemberKind, members: Sequence[tuple[str, bytes]]
    ) -> list[object]: ...


DOCX_WORKERS: dict[MemberKind, Callable[[bytes, str], object]] = {
    "blocks": convert_document_to_blocks,
    "markdown": convert_document_to_markdown,
}


class InlineDocxPool:
    """Runs the matching worker in-process for each member."""

    def convert_members(
        self, kind: MemberKind, members: Sequence[tuple[str, bytes]]
    ) -> list[object]:
        worker = DOCX_WORKERS[kind]
        outcomes: list[object] = []
        for name, payload in members:
            try:
                outcomes.append(worker(payload, name))
            except Exception as exc:  # noqa: BLE001 - per-member failures are outcomes, not batch aborts
                outcomes.append(exc)
        return outcomes

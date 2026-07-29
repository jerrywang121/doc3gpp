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
from pathlib import Path
import re

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

    def qn(*args, **kwargs):  # type: ignore[no-redef]
        raise PythonDocxNotInstalledError()

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


def _is_paragraph(child) -> bool:
    return child.tag == qn("w:p")


def _is_table(child) -> bool:
    return child.tag == qn("w:tbl")


def _style_name(paragraph) -> str:
    return paragraph.style.name if paragraph.style else ""


def _run_formatting(run_element) -> tuple[bool, bool]:
    rpr = run_element.find(qn("w:rPr"))
    if rpr is None:
        return False, False
    bold = rpr.find(qn("w:b")) is not None
    italic = rpr.find(qn("w:i")) is not None
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
        if tag == qn("w:t"):
            if child.text:
                parts.append(child.text)
        elif tag == qn("w:tab"):
            parts.append("\t")
        elif tag in {qn("w:br"), qn("w:cr")}:
            parts.append("\n")
        elif tag == qn("w:r"):
            run_bold, run_italic = _run_formatting(child)
            parts.append(_render_children(child, bold=bold or run_bold, italic=italic or run_italic))
        elif tag == qn("w:del"):
            parts.append(_render_revision(child, inserted=False, bold=bold, italic=italic))
        elif tag == qn("w:ins"):
            parts.append(_render_revision(child, inserted=True, bold=bold, italic=italic))
        elif tag == qn("w:delText"):
            if child.text:
                parts.append(child.text)
        elif tag in {qn("w:hyperlink"), qn("w:fldSimple")}:
            parts.append(_render_children(child, bold=bold, italic=italic))
        elif tag == qn("w:fldChar"):
            continue
        elif tag == qn("w:instrText"):
            if child.text:
                parts.append(child.text)
        else:
            parts.append(_render_children(child, bold=bold, italic=italic))
    return "".join(parts)  # _wrap_text("".join(parts), bold, italic)


def _paragraph_to_markdown(paragraph) -> str:
    text = _render_children(paragraph._p)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\t", "    ")
    text = text.strip()

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
        total_cells = len(row.cells)
        while ci < total_cells:
            cell = row.cells[ci]
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
    body = document.element.body
    for child in body.iterchildren():
        if _is_paragraph(child):
            paragraph = Paragraph(child, document)
            paragraph_text = _paragraph_to_markdown(paragraph)
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
    "PythonDocxNotInstalledError",
    "convert_document_to_markdown",
]
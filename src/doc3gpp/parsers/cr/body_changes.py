"""Line-by-line extractor for body-derived change blocks.

The body of a 3GPP CR is the markdown produced by
:mod:`doc3gpp.parsers.docx_converter`. Each ``<w:ins>`` / ``<w:del>``
revision mark is rendered as a self-contained
``<ins>[Inserted: <content>]</ins>`` or
``<del>[Deleted: <content>]</del>`` span on a single line. A CR
author's change usually lives in a *run* of marker-bearing lines,
optionally bridged by a few lines of plain prose.

This module finds those runs, groups nearby markers into the same
change block (gap-bridging), captures each block plus a
configurable amount of plain context on each side, and records the
heading / table-number clauses that contextualise each block.

The output is a :class:`TDocCRChangeDetails` with
``ftp_url=None`` and ``tdoc_id=None`` — the "unknown yet" sentinels
the service layer fills in via :func:`dataclasses.replace` once the
immutable download URL and the parent TDoc id are known.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from doc3gpp.models.tdoc_cr_change_details import TDocCRChangeDetails

# Heading number extractor. Accepts ``#`` / ``##`` / ``###`` /
# ``####`` / ``#####`` / ``######` lines and captures the leading
# dotted number (``5``, ``5.2``, ``5.2.3``) with an optional trailing
# sub-number (``-1``) used for sub-bullets of a numbered section.
_HEADING_RE = re.compile(
    r"^\s{0,3}#{1,6}\s+(\d+(?:\.\d+){0,4})(?:-(\d+))?\b"
)

# Table number extractor. ``Table 5.2.3-1:`` / ``Table 5.2.3.`` etc.
_TABLE_RE = re.compile(
    r"^\s*Table\s+(\d+(?:\.\d+){0,4}(?:-\d+)?)\b[:.\s]",
    re.IGNORECASE,
)

# A line is a "marker line" when it contains either of the docx
# converter's bracketed revision forms.
_MARKER_RE = re.compile(r"<(ins|del)\[|\[(Inserted|Deleted):", re.IGNORECASE)


def _is_marker_line(line: str) -> bool:
    return bool(_MARKER_RE.search(line))


def _match_heading(line: str) -> str | None:
    m = _HEADING_RE.match(line)
    if m is None:
        return None
    number = m.group(1)
    if m.group(2) is not None:
        return f"{number}-{m.group(2)}"
    return number


def _match_table_number(line: str) -> str | None:
    m = _TABLE_RE.match(line)
    if m is None:
        return None
    return m.group(1)


def extract_body_changes(
    lines: Sequence[str],
    *,
    gap_window: int = 2,
    context_padding: int = 2,
) -> TDocCRChangeDetails:
    """Walk ``lines`` and capture every revision-marked change block.

    Args:
        lines: Converted markdown line list (one element per line).
        gap_window: Max number of plain (non-marker) lines tolerated
            between two marker lines that still count as the same
            change block. ``0`` = only consecutive marker lines.
        context_padding: Plain context lines captured before and
            after each change block. ``0`` = no context, only marker
            lines + gap-bridge.

    Returns:
        A :class:`TDocCRChangeDetails` with ``ftp_url=None`` and
        ``tdoc_id=None`` (the parser-side "unknown yet" sentinels the
        service layer fills in via :func:`dataclasses.replace`).
        Empty ``clauses`` / ``changes`` when no revision marks are
        present.
    """
    if gap_window < 0:
        raise ValueError("gap_window must be >= 0")
    if context_padding < 0:
        raise ValueError("context_padding must be >= 0")

    all_clauses: set[str] = set()
    blocks: list[tuple[str, ...]] = []
    last_heading: str | None = None
    last_table: str | None = None

    # Run state. A "run" is a maximal sequence of marker lines +
    # bridging plain lines bounded by either a heading line, a gap
    # exceeding ``gap_window``, or the end of the document.
    run_start: int | None = None
    run_end: int | None = None
    run_gap_remaining = 0
    block_pre_clauses: list[str] = []
    block_clauses: list[str] = []

    def flush() -> None:
        nonlocal run_start, run_end, run_gap_remaining
        nonlocal block_clauses, block_pre_clauses
        if run_start is None:
            return
        start_ctx = max(0, run_start - context_padding)
        end_ctx = min(len(lines), run_end + 1 + context_padding)
        captured = tuple(lines[start_ctx:end_ctx])
        blocks.append(captured)
        for c in block_pre_clauses + block_clauses:
            all_clauses.add(c)
        run_start = None
        run_end = None
        run_gap_remaining = 0
        block_clauses = []
        block_pre_clauses = [c for c in (last_heading, last_table) if c]

    for i, line in enumerate(lines):
        heading = _match_heading(line)
        if heading is not None:
            last_heading = heading
            if run_start is not None:
                # Headings terminate the current run. The new run
                # (if any) starts under the fresh heading.
                flush()
            continue

        table_no = _match_table_number(line)
        if table_no is not None:
            last_table = table_no
            if run_start is not None:
                block_clauses.append(table_no)
            continue

        if _is_marker_line(line):
            if run_start is None:
                run_start = i
                block_pre_clauses = [
                    c for c in (last_heading, last_table) if c
                ]
            run_end = i
            if last_heading is not None:
                block_clauses.append(last_heading)
            if last_table is not None:
                block_clauses.append(last_table)
            run_gap_remaining = gap_window + 1
            continue

        # Plain line.
        if run_start is not None and run_gap_remaining > 0:
            run_gap_remaining -= 1
        elif run_start is not None:
            flush()

    flush()

    return TDocCRChangeDetails(
        ftp_url=None,
        tdoc_id=None,
        clauses=tuple(sorted(all_clauses)),
        changes=tuple(blocks),
    )

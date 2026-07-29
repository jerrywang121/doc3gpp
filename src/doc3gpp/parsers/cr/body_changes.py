"""Line-by-line extractor for body-derived change blocks.

The body of a 3GPP CR is the markdown produced by
:mod:`doc3gpp.parsers.docx_converter`. Each ``<w:ins>`` / ``<w:del>``
revision mark is rendered as a self-contained ``<ins>...</ins>`` or
``<del>...</del>`` span on a single line. A CR author's change
usually lives in a *run* of marker-bearing lines, optionally bridged
by a few lines of plain prose.

This module finds those runs, groups nearby markers into the same
change block (gap-bridging), captures each block plus a
configurable amount of plain context on each side, and records the
heading / table-number clauses that contextualise each block.

Heading and table-caption lines that fall *inside* a marker line
(``<ins>...</ins>`` or ``<del>...</del>``) are also detected so
that newly added / deleted clauses / tables, and renumbered clauses
/ tables, are attributed to the right block. Inside an ``<ins>``
they get a ``(new)`` suffix; inside a ``<del>`` they get a
``(del)`` suffix; renumbered cases (``<ins>X</ins><del>Y</del>``
in either order) emit *both* ``X (new)`` and ``Y (del)``.

The output is a :class:`TDocCRChangeDetails` with
``ftp_url=None`` and ``tdoc_id=None`` — the "unknown yet" sentinels
the service layer fills in via :func:`dataclasses.replace` once the
immutable download URL and the parent TDoc id are known.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from doc3gpp.models.tdoc_cr_change_details import (
    ChangeBlock,
    TDocCRChangeDetails,
)

# Heading number extractor. Accepts ``#`` / ``##`` / ``###`` /
# ``####`` / ``#####`` / ``######`` lines and captures the leading
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

# A line is a "marker line" when it contains any ``<ins>`` / ``<del>``
# span. The earlier ``[Inserted: ...]`` / ``[Deleted: ...]`` form has
# been replaced with bare brackets so headings / table captions inside
# revision marks become detectable for the renumbering cases.
_INS_RE = re.compile(r"<ins>.*?</ins>", re.IGNORECASE | re.DOTALL)
_DEL_RE = re.compile(r"<del>.*?</del>", re.IGNORECASE | re.DOTALL)


def _is_marker_line(line: str) -> bool:
    return bool(_INS_RE.search(line) or _DEL_RE.search(line))


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


def _strip_marker_spans(line: str) -> str:
    """Return ``line`` with every ``<ins>``/``<del>`` span removed.

    Useful for running heading / table regexes against the
    surrounding context, ignoring revision-marked content.
    """
    cleaned = _INS_RE.sub("", line)
    cleaned = _DEL_RE.sub("", cleaned)
    return cleaned


def _extract_clause_spans(
    line: str,
) -> list[tuple[str, str]]:
    """Return a list of ``(clause_label, suffix)`` pairs attributed to ``line``.

    Walks each ``<ins>...</ins>`` and ``<del>...</del>`` span,
    extracts any heading / table-caption clause label found inside
    the span, and attaches the suffix ``"(new)"`` for ``<ins>`` and
    ``"(del)"`` for ``<del>``. Spans are scanned in document order
    so a renumbering like ``<ins>X</ins><del>Y</del>`` returns
    both labels.

    Table-caption labels are returned with a ``Table `` prefix so
    the result matches the above-block ``Table <n>`` convention.
    Heading labels are returned bare (no prefix).

    The span contents are stripped of the ``<ins>``/``<del>``
    wrappers before the heading / table regexes run so a heading
    like ``<ins># 5.2.4 New heading</ins>`` yields ``5.2.4``.
    """
    out: list[tuple[str, str]] = []
    for m in _INS_RE.finditer(line):
        body = m.group(0)
        inner = re.sub(r"</?ins>", "", body, flags=re.IGNORECASE)
        heading = _match_heading(inner)
        table = _match_table_number(inner)
        if heading is not None:
            out.append((heading, " (new)"))
        elif table is not None:
            out.append((f"Table {table}", " (new)"))
    for m in _DEL_RE.finditer(line):
        body = m.group(0)
        inner = re.sub(r"</?del>", "", body, flags=re.IGNORECASE)
        heading = _match_heading(inner)
        table = _match_table_number(inner)
        if heading is not None:
            out.append((heading, " (del)"))
        elif table is not None:
            out.append((f"Table {table}", " (del)"))
    return out


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
    blocks: list[ChangeBlock] = []
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
    block_clauses_seen: set[str] = set()

    def push_pre(c: str) -> None:
        if c not in block_clauses_seen:
            block_clauses_seen.add(c)
            block_clauses.append(c)

    def flush() -> None:
        nonlocal run_start, run_end, run_gap_remaining
        nonlocal block_clauses, block_pre_clauses, block_clauses_seen
        if run_start is None:
            return
        start_ctx = max(0, run_start - context_padding)
        end_ctx = min(len(lines), run_end + 1 + context_padding)
        captured = lines[start_ctx:end_ctx]
        text = "\n".join(captured)
        clauses = list(block_pre_clauses) + block_clauses
        blocks.append(ChangeBlock(clauses=clauses, text=text))
        for c in clauses:
            all_clauses.add(c)
        run_start = None
        run_end = None
        run_gap_remaining = 0
        block_clauses = []
        block_pre_clauses = [c for c in (last_heading, last_table) if c]
        block_clauses_seen = set(block_pre_clauses)

    def push_inside(c: str) -> None:
        if c not in block_clauses_seen:
            block_clauses_seen.add(c)
            block_clauses.append(c)

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
            last_table = f"Table {table_no}"
            if run_start is not None:
                push_inside(last_table)
            continue

        if _is_marker_line(line):
            if run_start is None:
                run_start = i
                block_pre_clauses = [c for c in (last_heading, last_table) if c]
                block_clauses_seen = set(block_pre_clauses)
            run_end = i
            # Heading above the run stays on the pre-clauses side; the
            # marker line itself can also carry inside-run clauses
            # (e.g. a newly added heading or table caption).
            for label, suffix in _extract_clause_spans(line):
                push_inside(f"{label}{suffix}")
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


__all__ = ["extract_body_changes"]
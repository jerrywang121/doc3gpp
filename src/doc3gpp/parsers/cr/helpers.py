"""Shared text helpers, regex constants, and zip extraction for CR parsers."""

from __future__ import annotations

import io
import logging
import re
import zipfile
from collections.abc import Sequence


logger = logging.getLogger(__name__)


_MD_RM_KEEP_HEADERS = 0x01
_MD_RM_KEEP_BLOCKQUOTES = 0x02
_MD_RM_KEEP_CODE = 0x04
_MD_RM_KEEP_LINKS = 0x08
_MD_RM_KEEP_HORIZONTAL = 0x10


def _collapse_whitespace(text: str) -> str:
    """Collapse every whitespace run to a single space and ``strip()``.

    Kept here for callers that want the helper without depending on
    :mod:`header`. The header module also defines a private
    :func:`header._collapse_whitespace`; both share the same regex.
    """
    return re.sub(r"\s+", " ", text).strip()


def _remove_markdown_formatting(text: str, flags: int = 0) -> str:
    """Strip common markdown formatting from ``text``.

    Default behaviour removes **bold**, *italic*, ``code``,
    ``[text](url)`` link markup, ``# heading`` prefixes, and ``>``
    blockquote markers. Bit flags can keep selected constructs in
    place; that is reserved for TTCN overview parsing where we want
    to retain heading lines (``MD_RM_KEEP_HEADERS``).

    This helper is re-exported with a leading underscore because the
    service layer (Phase 6) and other parsers occasionally need to
    test the formatting rules independently.
    """
    text = re.sub(r"(?<!\\)\*\*([^\s*].*?)(?<![\s\\])\*\*", r"\1", text)
    text = re.sub(r"(?<!\\)__([^\s*].*?)(?<![\s\\])__", r"\1", text)
    text = re.sub(r"(?<!\\)\*([^\s*].*?)(?<![\s\\])\*", r"\1", text)
    text = re.sub(r"(?<!\\)_([^\s*].*?)(?<![\s\\])_", r"\1", text)

    text = text.replace("\\_", "_")
    text = text.replace("\\*", "*")

    if not (flags & _MD_RM_KEEP_HEADERS):
        text = re.sub(r"^#+\s*(.*)", r"\1", text, flags=re.MULTILINE)
        text = re.sub(r"^\s*={3,}\s*$", "", text, flags=re.MULTILINE)
        text = re.sub(r"^\s*-{3,}\s*$", "", text, flags=re.MULTILINE)
    if not (flags & _MD_RM_KEEP_LINKS):
        text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
        text = re.sub(r"!\[([^\]]*)\]\([^\)]+\)", "", text)
    if not (flags & _MD_RM_KEEP_CODE):
        text = re.sub(r"`([^`]+)`", r"\1", text)
        text = re.sub(r"```[\s\S]*?```", "", text)
    if not (flags & _MD_RM_KEEP_BLOCKQUOTES):
        text = re.sub(r"^>\s*(.*)", r"\1", text, flags=re.MULTILINE)
    return text.strip()


def derive_tech_from_spec(spec: str) -> str:
    """Derive a technology label from a 3GPP spec number.

    Examples:
        ``"38.523"`` → ``"5G"``
        ``"36.523"`` → ``"LTE"``
        ``"34.229"`` → ``"IMS"``
        ``""`` / ``None``-like → ``""``
    """
    spec = (spec or "").strip()
    if not spec:
        return ""
    m = re.match(r"(\d+\.\d+)", spec)
    if not m:
        return ""
    base = m.group(1)
    exact = {
        "34.229": "IMS",
        "37.579": "MCX",
        "36.579": "MCX",
        "37.571": "POS",
        "38.523": "5G",
        "36.523": "LTE",
    }
    return exact.get(base, "")


def _year_from_tdoc_id(tdoc_id: str) -> int | None:
    """Derive meeting year from a TDoc id (``positions 3–4`` → ``20YY``)."""
    if len(tdoc_id) < 5:
        return None
    digits = tdoc_id[3:5]
    if not digits.isdigit():
        return None
    return 2000 + int(digits)


def extract_docx_from_zip(zip_bytes: bytes) -> tuple[str, bytes]:
    """Pull a single ``(filename, bytes)`` pair out of a CR zip.

    Mirrors the reference's behaviour:

    * Skips ``__MACOSX/`` resource-fork entries (Apple drops them on
      many macs; they are not real Word files).
    * Prefers ``.docx`` over ``.doc`` when both are present
      (only ``docx`` parses are supported).
    * Falls back to the lexicographically smaller filename when
      ``.docx`` is not present, matching the reference's stable
      ordering.

    Args:
        zip_bytes: Raw bytes of the CR zip.

    Returns:
        ``(filename_in_zip, bytes_of_word_doc)``.

    Raises:
        zipfile.BadZipFile: If the input is not a valid zip archive.
        ValueError: If the zip contains no ``.docx`` / ``.doc`` entry
            (only ``__MACOSX/``-style entries were present, or the
            archive was empty).
    """
    if not zip_bytes:
        raise ValueError("Cannot extract from empty zip bytes")
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        word_docs = [
            name
            for name in zf.namelist()
            if name.lower().endswith((".docx", ".doc"))
            and not name.lower().startswith("__macosx/")
        ]
        if not word_docs:
            raise ValueError(
                "No .docx or .doc entries found in CR zip "
                "(after skipping __MACOSX/ resource fork)"
            )
        word_docs.sort(
            key=lambda n: (0 if n.lower().endswith(".docx") else 1, n.lower())
        )
        target = word_docs[0]
        return target, zf.read(target)


def _search_pattern_in_lines(
    lines: Sequence[str],
    data: dict[str, str],
    field_names: Sequence[str],
    group_numbers: Sequence[int],
    pattern: re.Pattern[str],
) -> int:
    """Find ``pattern`` in the next ``len(lines)`` lines, populating ``data``.

    Mirrors the reference function — when matched, assigns
    ``match.group(n).strip()`` (with a trailing ``|`` cleanup) to each
    ``field_names[i]`` from group ``group_numbers[i]``.

    Raises:
        ValueError: When ``len(field_names) != len(group_numbers)``;
            the reference raises this on a programming error.
    """
    if len(field_names) != len(group_numbers):
        raise ValueError(
            f"field_names ({len(field_names)}) and group_numbers "
            f"({len(group_numbers)}) must have the same length"
        )
    advancing = 0
    for line in lines:
        advancing += 1
        match = pattern.search(line)
        if match:
            for field_name, group_number in zip(field_names, group_numbers):
                raw_value = match.group(group_number).strip()
                raw_value = re.sub(r"\s*\|+\s*$", "", raw_value)
                data[field_name] = raw_value
            break
    return advancing


_COVER_FIELDS = (
    "spec",
    "cr_num",
    "rev",
    "version",
    "title",
    "source",
    "tsg",
    "related_wis",
    "cr_cat",
    "release",
    "reason_for_change",
    "consequences_if_not_approved",
    "summary_of_change",
    "clauses_affected",
    "other_comments",
    "revision_history",
    "date",
)
"""Domain models and date-set algebra for ETSI LISTSERV archives."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

DatePrecision = Literal["datetime", "date"]

_DATE_EXPR_RE = re.compile(
    r"""^\s*
        (?P<op>>=|<=|!=|>|<|=)\s*
        (?:'(?P<quoted>\d{4}-\d{2}-\d{2})'|(?P<bare>\d{4}-\d{2}-\d{2}))
        \s*$""",
    re.VERBOSE,
)


def normalize_list_id(list_id: str) -> str:
    """Normalize a LISTSERV list ID to stripped uppercase."""
    normalized = list_id.strip().upper()
    if not normalized:
        raise ValueError("LISTSERV list_id must not be empty.")
    return normalized


def normalize_tdoc_ids(tdocs: Sequence[str]) -> tuple[str, ...]:
    """Deduplicate TDoc IDs case-insensitively while preserving order in uppercase."""
    seen: set[str] = set()
    result: list[str] = []
    for raw in tdocs:
        item = raw.strip().upper()
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return tuple(result)


@dataclass(slots=True, frozen=True)
class DateInterval:
    """Inclusive date interval; ``None`` represents an unbounded endpoint."""

    start: date | None
    end: date | None

    def __post_init__(self) -> None:
        if self.start is not None and self.end is not None and self.start > self.end:
            raise ValueError(
                f"DateInterval start {self.start.isoformat()} must not be after end {self.end.isoformat()}."
            )


def _normalize_intervals(intervals: Sequence[DateInterval]) -> tuple[DateInterval, ...]:
    """Sort and merge overlapping or adjacent inclusive intervals."""
    valid = [
        iv
        for iv in intervals
        if iv.start is None or iv.end is None or iv.start <= iv.end
    ]
    if not valid:
        return ()

    def _sort_key(iv: DateInterval) -> tuple[int, date, int, date]:
        start_unbounded = 0 if iv.start is None else 1
        start_val = date.min if iv.start is None else iv.start
        end_unbounded = 1 if iv.end is None else 0
        end_val = date.max if iv.end is None else iv.end
        return (start_unbounded, start_val, end_unbounded, end_val)

    sorted_ivs = sorted(valid, key=_sort_key)
    merged: list[DateInterval] = [sorted_ivs[0]]
    for current in sorted_ivs[1:]:
        prev = merged[-1]
        if prev.end is None:
            continue
        if current.start is None or (
            prev.end == date.max or current.start <= prev.end + timedelta(days=1)
        ):
            new_end: date | None
            if current.end is None:
                new_end = None
            else:
                new_end = max(prev.end, current.end)
            merged[-1] = DateInterval(start=prev.start, end=new_end)
        else:
            merged.append(current)
    return tuple(merged)


@dataclass(slots=True, frozen=True)
class DateSet:
    """Normalized set of sorted, disjoint inclusive :class:`DateInterval` ranges."""

    intervals: tuple[DateInterval, ...] = ()

    def __init__(self, intervals: Sequence[DateInterval] = ()) -> None:
        object.__setattr__(self, "intervals", _normalize_intervals(intervals))


def _intersect_two_intervals(
    a: DateInterval, b: DateInterval
) -> DateInterval | None:
    if a.start is None:
        start = b.start
    elif b.start is None:
        start = a.start
    else:
        start = max(a.start, b.start)

    if a.end is None:
        end = b.end
    elif b.end is None:
        end = a.end
    else:
        end = min(a.end, b.end)

    if start is not None and end is not None and start > end:
        return None
    return DateInterval(start=start, end=end)


def intersect_date_sets(left: DateSet, right: DateSet) -> DateSet:
    """Return the intersection of two :class:`DateSet` instances."""
    if not left.intervals or not right.intervals:
        return DateSet()
    pieces: list[DateInterval] = []
    for a in left.intervals:
        for b in right.intervals:
            overlap = _intersect_two_intervals(a, b)
            if overlap is not None:
                pieces.append(overlap)
    return DateSet(tuple(pieces))


def _parse_single_date_expression(expr: str) -> DateSet:
    match = _DATE_EXPR_RE.match(expr)
    if match is None:
        raise ValueError(
            f"Invalid date filter {expr!r}. Expected '<op> YYYY-MM-DD' or "
            f"\"<op> 'YYYY-MM-DD'\" with one of =, !=, <, <=, >, >=."
        )
    op = match.group("op")
    raw_date = match.group("quoted") or match.group("bare")
    try:
        target = date.fromisoformat(raw_date)
    except ValueError as exc:
        raise ValueError(f"Invalid calendar date in filter {expr!r}: {exc}") from exc

    if op == "=":
        return DateSet((DateInterval(target, target),))
    if op == ">=":
        return DateSet((DateInterval(target, None),))
    if op == "<=":
        return DateSet((DateInterval(None, target),))
    if op == ">":
        if target == date.max:
            return DateSet()
        return DateSet((DateInterval(target + timedelta(days=1), None),))
    if op == "<":
        if target == date.min:
            return DateSet()
        return DateSet((DateInterval(None, target - timedelta(days=1)),))
    if op == "!=":
        parts: list[DateInterval] = []
        if target > date.min:
            parts.append(DateInterval(None, target - timedelta(days=1)))
        if target < date.max:
            parts.append(DateInterval(target + timedelta(days=1), None))
        return DateSet(tuple(parts))
    raise ValueError(f"Unsupported date operator {op!r} in {expr!r}.")


def parse_date_set(expressions: Sequence[str]) -> DateSet:
    """Parse and intersect repeatable date filter expressions into a :class:`DateSet`.

    An empty sequence represents all dates ``(DateInterval(None, None),)``.
    """
    current = DateSet((DateInterval(None, None),))
    for expr in expressions:
        parsed = _parse_single_date_expression(expr)
        current = intersect_date_sets(current, parsed)
    return current


def default_sync_dates(today: date) -> DateSet:
    """Return the rolling preceding calendar year through ``today`` inclusive.

    A February 29 anniversary clamps to February 28 in a non-leap preceding year.
    """
    prev_year = today.year - 1
    try:
        start = today.replace(year=prev_year)
    except ValueError:
        start = date(prev_year, 2, 28)
    return DateSet((DateInterval(start=start, end=today),))


def date_matches(dates: DateSet, value: date) -> bool:
    """Return ``True`` when ``value`` falls within any interval of ``dates``."""
    for iv in dates.intervals:
        if iv.start is not None and value < iv.start:
            continue
        if iv.end is not None and value > iv.end:
            continue
        return True
    return False


def _interval_contains(outer: DateInterval, inner: DateInterval) -> bool:
    if outer.start is not None and (
        inner.start is None or inner.start < outer.start
    ):
        return False
    return not (
        outer.end is not None and (inner.end is None or inner.end > outer.end)
    )


def coverage_contains(coverage: DateSet, requested: DateSet) -> bool:
    """Return ``True`` when every interval in ``requested`` is covered by ``coverage``."""
    if not requested.intervals:
        return True
    if not coverage.intervals:
        return False
    for req in requested.intervals:
        if not any(_interval_contains(cov, req) for cov in coverage.intervals):
            return False
    return True


def union_coverage(left: DateSet, right: DateSet) -> DateSet:
    """Return the normalized union of two :class:`DateSet` instances."""
    return DateSet((*left.intervals, *right.intervals))


@dataclass(slots=True, frozen=True)
class ArchivePage:
    """One LISTSERV archive page discovered from an index."""

    list_id: str
    url: str
    period: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "list_id", normalize_list_id(self.list_id))


@dataclass(slots=True, frozen=True)
class ListservMessage:
    """Authoritative message listing metadata keyed by canonical detail URL."""

    url: str
    list_id: str
    subject: str
    sender: str
    date: datetime
    date_precision: DatePrecision
    week: str
    subject_tdocs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "list_id", normalize_list_id(self.list_id))
        if self.date_precision not in ("datetime", "date"):
            raise ValueError(
                f"Invalid date_precision {self.date_precision!r}; expected 'datetime' or 'date'."
            )
        object.__setattr__(
            self, "subject_tdocs", normalize_tdoc_ids(self.subject_tdocs)
        )


@dataclass(slots=True, frozen=True)
class ListservAttachment:
    """Named message attachment with resolved URL."""

    name: str
    url: str


@dataclass(slots=True, frozen=True)
class ListservDetail:
    """Parsed message detail linked by canonical detail URL."""

    url: str
    attachments: tuple[ListservAttachment, ...]
    tdocs: tuple[str, ...]
    content: str
    content_truncated: bool
    parser_version: str
    parsed_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "attachments", tuple(self.attachments))
        object.__setattr__(self, "tdocs", normalize_tdoc_ids(self.tdocs))


@dataclass(slots=True, frozen=True)
class ListservFilters:
    """Filter criteria for LISTSERV queries and searches."""

    list_id: str | None = None
    sender: str | None = None
    subject: str | None = None
    tdocs: str | None = None
    dates: DateSet | None = None
    limit: int = 20
    offset: int = 0

    def __post_init__(self) -> None:
        if self.limit < 0 or self.limit > 150:
            raise ValueError(f"limit must be between 0 and 150, got {self.limit}")
        if self.offset < 0:
            raise ValueError(f"offset must be nonnegative, got {self.offset}")


@dataclass(slots=True, frozen=True)
class ListservBatchResult:
    """Batch processing summary for sync and parse operations."""

    processed: int
    skipped: int
    failures: dict[str, str]


@dataclass(slots=True, frozen=True)
class ListservList:
    """Immutable catalog metadata for one LISTSERV list."""

    list_id: str
    tsg: str | None
    description: str | None


@dataclass(slots=True, frozen=True)
class ListservRecord:
    """Stored message listing joined with optional parsed detail."""

    message: ListservMessage
    detail: ListservDetail | None
    tdocs: tuple[str, ...]
    source_revision: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "tdocs", normalize_tdoc_ids(self.tdocs))


@dataclass(slots=True, frozen=True)
class ListservDetailMetadata:
    """Detail metadata exposed on search records without full body content."""

    attachments: tuple[ListservAttachment, ...]
    content_truncated: bool
    parser_version: str
    parsed_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "attachments", tuple(self.attachments))


@dataclass(slots=True, frozen=True)
class ListservSearchRecord:
    """Search-surface message projection excluding full stored body content."""

    message: ListservMessage
    detail: ListservDetailMetadata | None
    tdocs: tuple[str, ...]
    source_revision: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "tdocs", normalize_tdoc_ids(self.tdocs))


@dataclass(slots=True, frozen=True)
class ListservSearchResult:
    """One ranked or filter-only LISTSERV search hit."""

    record: ListservSearchRecord
    score: float | None
    snippet: str | None
    matched_source: str | None
    content_preview: str | None
    preview_truncated: bool | None


__all__ = [
    "ArchivePage",
    "DateInterval",
    "DatePrecision",
    "DateSet",
    "ListservAttachment",
    "ListservBatchResult",
    "ListservDetail",
    "ListservDetailMetadata",
    "ListservFilters",
    "ListservList",
    "ListservMessage",
    "ListservRecord",
    "ListservSearchRecord",
    "ListservSearchResult",
    "coverage_contains",
    "date_matches",
    "default_sync_dates",
    "intersect_date_sets",
    "normalize_list_id",
    "normalize_tdoc_ids",
    "parse_date_set",
    "union_coverage",
]

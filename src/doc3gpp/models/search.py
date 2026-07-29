"""DTOs and error hierarchy for the FTS5 search subsystem.

The :class:`SearchError` tree lets the CLI distinguish infrastructure
problems (FTS5 missing, wrong dialect, extra not installed) from query
problems (malformed ``MATCH`` expression) from index corruption
(virtual table broken) — each gets its own exit code in ``cli.py``.

DTOs are :class:`slots=True, frozen=True` dataclasses so they can be
handed back and forth between the CLI, the service, and the repo
without defensive copies. The service layer is responsible for
translating them into either a repo call (write path) or a
Typer-compatible render (read path).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


class SearchError(Exception):
    """Base class for every error raised by the search subsystem."""


class SearchUnavailableError(SearchError):
    """FTS5 is not available in this build or dialect.

    Raised by :class:`~doc3gpp.storage.repositories.search_sql.SQLAlchemySearchIndexRepository`
    when the runtime probe detects a missing FTS5 compile option or a
    non-sqlite backend. The CLI surfaces the install hint
    (``pip install doc3gpp[search]``) on this error.
    """


class SearchQueryError(SearchError):
    """The user-supplied FTS5 ``MATCH`` expression is malformed.

    Raised when the normalized query cannot be parsed by FTS5
    (unbalanced quotes, invalid operator). Exit code 2.
    """


class SearchIndexCorruptError(SearchError):
    """The FTS5 virtual table is broken (schema/tokenizer mismatch).

    Raised when the index exists but reads/writes fail with a sqlite
    ``OperationalError`` that the repo classifies as corruption rather
    than a query problem. Exit code 3; the CLI suggests running
    ``doc3gpp search index --rebuild``.
    """


@dataclass(slots=True, frozen=True)
class SearchFilters:
    """Filter arguments for a search query.

    All fields are optional; the repo AND-combines them into the
    generated SQL. ``limit`` caps the result count (default 20).
    ``None`` filters are dropped from the WHERE clause entirely.
    """

    tsg: str | None = None
    meeting: str | None = None
    meeting_id: int | None = None
    tdoc_id: str | None = None
    release: str | None = None
    spec: str | None = None
    since: str | None = None
    until: str | None = None
    limit: int = 20


@dataclass(slots=True, frozen=True)
class SearchHit:
    """A single FTS5 hit joined back to ``tdocs`` + ``meetings``.

    ``score`` is the raw ``bm25(tdoc_search)`` value (lower = better
    match in FTS5). ``preview`` is the FTS5 ``snippet(...)`` output
    with ``<<...>>`` markers around matches; the CLI renders it as a
    separate column in ``table`` and as an inline ``>`` blockquote in
    ``markdown``.
    """

    tdoc_id: str
    score: float
    preview: str
    title: str
    meeting: str | None
    tsg: str | None
    uploaded_date: str | None


@dataclass(slots=True, frozen=True)
class RebuildProgress:
    """One batch of the ``search index --rebuild`` generator.

    The CLI consumes the generator and (unless ``--quiet``) prints a
    progress line per batch. ``current_tdoc_id`` is the last id
    processed in this batch — also written to the ``tdoc_search_meta``
    resume cursor. The value is the user-facing ``tdocs.tdoc_id``
    string (e.g. ``"R5-1234567"``), NOT the sqlite-internal rowid
    int, so the resume cursor survives a full ``--rebuild`` cycle
    (FTS5 rowids get re-allocated on each ``DELETE+INSERT``).
    """

    processed: int
    total: int
    current_tdoc_id: str


@dataclass(slots=True, frozen=True)
class SearchIndexStatus:
    """Snapshot of the index state for ``search index`` (no flags).

    ``is_stale`` is ``True`` when
    ``latest_tdocs_uploaded_date > last_indexed_uploaded_date``
    (i.e. there are newer TDoc uploads that have not been re-indexed).
    The CLI prints a "run ``search index --rebuild``" hint when this
    is true, gated behind ``--quiet`` and a per-invocation latch.
    """

    enabled: bool
    row_count: int
    last_rebuild_at: datetime | None
    last_indexed_uploaded_date: datetime | None
    latest_tdocs_uploaded_date: datetime | None
    is_stale: bool


__all__ = [
    "RebuildProgress",
    "SearchError",
    "SearchFilters",
    "SearchHit",
    "SearchIndexCorruptError",
    "SearchIndexStatus",
    "SearchQueryError",
    "SearchUnavailableError",
]
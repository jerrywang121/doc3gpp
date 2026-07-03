from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from typing import Protocol

from doc3gpp.models.meeting import Meeting
from doc3gpp.models.tdoc import TDoc, TDocWithMeeting
from doc3gpp.models.tdoc_file import TDocFile
from doc3gpp.models.tsg import Tsg
from doc3gpp.models.wi import Wi


class TDocRepository(Protocol):
    """Storage operations used by service layer."""

    def upsert(self, tdoc: TDoc) -> None:
        """Insert or update a TDoc record in storage."""
        ...

    def upsert_many(self, tdocs: list[TDoc]) -> int:
        """Insert or update multiple TDoc records in a single transaction.

        Existing rows (matched by ``tdoc_id``) are updated in place so callers
        can re-sync without producing duplicates. Returns the number of input
        rows processed.
        """
        ...

    def list(
        self,
        limit: int = 20,
        tsg: str | None = None,
        meeting_like: str | None = None,
        year: int | None = None,
        source_like: str | None = None,
        spec_like: str | None = None,
        wi_like: str | None = None,
        title_like: str | None = None,
        cat_like: str | None = None,
        status_like: str | None = None,
        type_like: str | None = None,
    ) -> list[TDoc]:
        """Return a list of recent TDoc records with optional filters.

        Pure persistence shape — no joined meeting metadata. Callers that
        need a human-readable meeting name should use :meth:`list_with_meeting`.
        """
        ...

    def list_tdoc_ids_for_meeting(self, meeting_id: int) -> list[str]:
        """Return the TDoc IDs currently stored for ``meeting_id``.

        Used by the TDoc sync flow to compute the ID set passed to
        :class:`TDocFileService`, whose FTP scan needs a known prefix
        list to recognise attachments. Returns an empty list when the
        meeting has no TDocs (or does not exist).
        """
        ...

    def list_with_meeting(
        self,
        limit: int = 20,
        tsg: str | None = None,
        meeting_like: str | None = None,
        year: int | None = None,
        source_like: str | None = None,
        spec_like: str | None = None,
        wi_like: str | None = None,
        title_like: str | None = None,
        cat_like: str | None = None,
        status_like: str | None = None,
        type_like: str | None = None,
    ) -> list[TDocWithMeeting]:
        """Like :meth:`list` but wraps each row with its meeting's display name.

        Equivalent to ``list(...)`` plus a ``meetings`` JOIN to populate
        ``meeting_name``. Suitable for CLI / export code paths that surface
        the meeting name alongside TDoc fields.
        """
        ...


class MeetingRepository(Protocol):
    """Storage operations used by meetings sync service."""

    def upsert_many(self, meetings: list[Meeting]) -> int:
        """Save or update multiple meeting records.

        Implementations should stamp ``Meeting.updated_at`` on every write so
        callers can detect re-sync activity.
        """
        ...

    def list(
        self,
        limit: int = 50,
        offset: int = 0,
        tsg: str | None = None,
        name_like: str | None = None,
        location_like: str | None = None,
        year: int | None = None,
    ) -> list[Meeting]:
        """Return a list of meeting records, optionally filtered and paginated.

        Optional filters:
            tsg: matches meeting names that *start with* this value
                (case-insensitive).
            name_like: SQL ``LIKE`` pattern applied to the meeting name column.
            location_like: SQL ``LIKE`` pattern applied to the meeting
                location column.
            year: integer year to match against ``end_date``.
        Pagination:
            offset: rows to skip before applying ``limit``. Combined with
                ``limit`` this enables CLI pagination without re-running the
                filters.
        """
        ...

    def get_by_id(self, meeting_id: int) -> Meeting | None:
        """Return a meeting record by its numeric ID."""
        ...

    def get_by_name(self, meeting_name: str) -> Meeting | None:
        """Return a meeting record by its exact meeting name."""
        ...

    def delete_with_end_before(self, cutoff: date) -> int:
        """Delete persisted meetings whose ``end_date`` is strictly before ``cutoff``.

        Used by the sync pipeline to trim out-of-window rows after re-syncing
        with a narrower year window. Returns the number of rows deleted.
        """
        ...


class TsgRepository(Protocol):
    """Storage operations for 3GPP TSG reference records."""

    def upsert_many(self, tsgs: list[Tsg]) -> int:
        """Insert or update multiple TSG records keyed by ``tsg_name``."""
        ...

    def list_all(self) -> list[Tsg]:
        """Return all TSG records, ordered by ``tsg_name``."""
        ...

    def get_by_short_name(self, short_name: str) -> Tsg | None:
        """Return a TSG record by its short name (case-insensitive)."""
        ...

    def get_by_tsg_name(self, tsg_name: str) -> Tsg | None:
        """Return a TSG record by its full ``tsg_name`` (case-insensitive)."""
        ...

    def count(self) -> int:
        """Return the number of stored TSG records."""
        ...


class WiRepository(Protocol):
    """Storage operations used by the WI service layer."""

    def upsert_many(self, wis: list[Wi]) -> int:
        """Insert or update multiple WI records keyed by ``(wi_id, tsg_short)``.

        Existing rows (matched by the composite key) are refreshed in place so
        callers can use this method to re-sync a TSG without producing
        duplicates. Returns the number of input rows that were written.
        """
        ...

    def list(
        self,
        limit: int = 20,
        tsg: str | None = None,
        name_like: str | None = None,
        acronym_like: str | None = None,
        release_like: str | None = None,
    ) -> list[Wi]:
        """Return a list of stored WI records matching the filters.

        - ``tsg``: restrict to a single TSG short name (case-insensitive).
        - ``name_like``, ``acronym_like``, ``release_like``: SQL ``LIKE``
          patterns applied to the corresponding text columns.
        """
        ...


class TDocFileRepository(Protocol):
    """Storage operations for auxiliary TDoc files (revisions, reviews, support)."""

    def upsert_many(self, files: list[TDocFile]) -> int:
        """Insert or update multiple TDocFile records keyed by ``url``.

        The fully-qualified download URL is the natural identity of a file
        on the 3GPP FTP — the same attachment lives at exactly one
        upstream location — so the unique index on ``url`` is the upsert
        key. Existing rows are refreshed in place (the ``file`` label and
        ``updated_at`` are rewritten) so re-syncing a meeting does not
        produce duplicates. Returns the number of input rows that were
        written.
        """
        ...

    def list(
        self,
        limit: int = 20,
        tdoc_id: str | None = None,
        file_type: str | None = None,
        file_type_in: Iterable[str] | None = None,
    ) -> list[TDocFile]:
        """Return stored TDocFile records, ordered by most recently updated.

        Optional filters:
        - ``tdoc_id``: exact match against the owning TDoc identifier.
        - ``file_type``: exact match against the ``type`` column
          (``"revision"`` / ``"review"`` / ``"support"``).
        - ``file_type_in``: iterable of allowed ``type`` values; useful for
          ``type IN ('revision', 'review')`` queries.
        """
        ...

    def delete_for_tdoc_ids(self, tdoc_ids: Iterable[str]) -> int:
        """Delete every TDocFile whose ``tdoc_id`` is in ``tdoc_ids``.

        Used by the sync flow to clear stale rows for a meeting before
        re-inserting the freshly scraped set, so files removed upstream
        do not linger in the table. Returns the number of rows deleted.
        Passing an empty iterable is a no-op (returns 0).
        """
        ...

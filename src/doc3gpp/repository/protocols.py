from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Protocol

from doc3gpp.models.meeting import Meeting
from doc3gpp.models.tdoc import TDoc, TDocWithMeeting
from doc3gpp.models.tdoc_cr import (
    TDocCRDetails,
    TDocCRTTCNDetails,
    TDocExtractMeta,
)
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

    def get_by_id(self, tdoc_id: str) -> TDoc | None:
        """Return a TDoc record by its canonical ``tdoc_id`` (PK lookup).

        Used by :class:`doc3gpp.services.tdoc_cr_service.TDocCrService` to
        validate that the requested id exists and to check ``type == "CR"``
        before triggering a download. Returns ``None`` when the row is
        absent — callers translate that into :class:`TDocNotFoundError`.
        """
        ...

    def get_by_ftp_url(self, ftp_url: str) -> TDoc | None:
        """Return the TDoc whose ``ftp_url`` matches exactly.

        The 3GPP upload pipeline maintains a 1:1 invariant between
        ``ftp_url`` and ``tdoc_id`` (one upload batch produces one
        ``tdoc_id`` at one URL); the schema does NOT have a DB-level
        UNIQUE constraint on ``tdocs.ftp_url``. If the invariant is
        ever violated at runtime, this method deterministically
        returns the lexically-first ``tdoc_id`` (``ORDER BY tdoc_id
        ASC LIMIT 1``) so CLI output is reproducible.

        Used by ``tdoc show --ftp-url <url>`` to anchor a URL-keyed
        read when the parent ``TDoc`` is unknown to the caller.
        """
        ...

    def list(
        self,
        limit: int = 20,
        offset: int = 0,
        tdoc_id: str | None = None,
        meeting_like: str | None = None,
        meeting_id: int | None = None,
        status: str | None = None,
        cr_cat: str | None = None,
        spec: str | None = None,
        wi: str | None = None,
        revision_of: str | None = None,
        revised_to: str | None = None,
        title: str | None = None,
        ftp_url: str | None = None,
        source: str | None = None,
        tdoc_type: str | None = None,
        uploaded_date: str | None = None,
        release: str | None = None,
        version: str | None = None,
        cr_num: str | None = None,
        cr_pack: str | None = None,
        exclude_parsed: bool = False,
    ) -> list[TDoc]:
        """Return a list of recent TDoc records with optional filters.

        Optional filters:
        - ``tdoc_id``: SQL ``LIKE`` pattern matched against the
          ``tdocs.tdoc_id`` column. Accepts the rich filter syntax
          described in :mod:`doc3gpp.cli_filters` (``null`` /
          ``not-null`` / ``!pattern`` / plain LIKE). Powers the
          ``--tdoc`` flag on both ``tdoc parse`` and ``tdoc list`` —
          combined with ``--meeting-id`` or any text filter it narrows
          the candidate set.
        - ``meeting_like``: SQL ``LIKE`` pattern applied to the parent
          meeting's name (joins the ``meetings`` table).
        - ``meeting_id``: exact match against ``tdocs.meeting_id``. Can
          be combined with ``meeting_like``; rows must satisfy both.
        - The remaining parameters accept the rich filter syntax
          described in :mod:`doc3gpp.cli_filters` — ``null`` /
          ``not-null`` match the column's nullability, a leading ``!``
          flips the comparison to ``NOT LIKE`` (with the ``!``
          consumed), and anything else is treated as a ``LIKE`` pattern.
          ``uploaded_date`` additionally accepts a
          ``"<op> 'YYYY-MM-DD'"`` comparison with ``<op>`` in
          ``=`` / ``!=`` / ``<`` / ``<=`` / ``>`` / ``>=``.
          ``release``, ``version``, ``cr_num``, ``cr_pack`` are
          text-column filters newly wired through for both
          ``tdoc list`` and ``tdoc parse`` — they accept the same
          ``null`` / ``not-null`` / ``!pattern`` / plain LIKE grammar
          as the other text columns.
        - ``exclude_parsed``: when ``True``, drop every TDoc whose
          ``tdoc_id`` already has a row in ``tdoc_cr_cover_page`` —
          applied in SQL *before* ``ORDER BY ... OFFSET ... LIMIT`` so
          the limit reflects the un-parsed candidate set. Default
          ``False`` keeps the raw match set.

        Pagination:
        - ``offset``: rows to skip before applying ``limit``. Combined
          with ``limit`` this enables CLI pagination without re-running
          the filters.

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

    def list_distinct_meeting_ids(self) -> list[int]:
        """Return the distinct, non-null meeting IDs stored in ``tdocs``.

        Values are returned in ascending order so iteration is deterministic.
        Used by ``tdoc sync`` when no explicit ``--meeting-id`` or
        ``--meeting`` is supplied. Orphaned TDocs (``meeting_id IS NULL``)
        are excluded.
        """
        ...

    def list_with_meeting(
        self,
        limit: int = 20,
        offset: int = 0,
        tdoc_id: str | None = None,
        meeting_like: str | None = None,
        meeting_id: int | None = None,
        status: str | None = None,
        cr_cat: str | None = None,
        spec: str | None = None,
        wi: str | None = None,
        revision_of: str | None = None,
        revised_to: str | None = None,
        title: str | None = None,
        ftp_url: str | None = None,
        source: str | None = None,
        tdoc_type: str | None = None,
        uploaded_date: str | None = None,
        release: str | None = None,
        version: str | None = None,
        cr_num: str | None = None,
        cr_pack: str | None = None,
        exclude_parsed: bool = False,
    ) -> list[TDocWithMeeting]:
        """Like :meth:`list` but wraps each row with its meeting's display name.

        Equivalent to ``list(...)`` plus a ``meetings`` lookup to populate
        ``meeting_name``. Suitable for CLI / export code paths that surface
        the meeting name alongside TDoc fields. Accepts the same filters
        and pagination as :meth:`list` (including ``exclude_parsed``).
        """
        ...


class MeetingRepository(Protocol):
    """Storage operations used by meetings sync service."""

    def upsert_many(self, meetings: list[Meeting]) -> int:
        """Save or update multiple meeting records."""
        ...

    def list(
        self,
        limit: int = 50,
        offset: int = 0,
        tsg: str | None = None,
        name_like: str | None = None,
        location_like: str | None = None,
        year: int | None = None,
        tdoc_id: tuple[str, int] | None = None,
    ) -> list[Meeting]:
        """Return a list of meeting records, optionally filtered and paginated.

        Optional filters:
            tsg: SQL ``LIKE`` pattern applied to the ``meetings.tsg`` FK
                (case-insensitive on input, stored canonicalised in upper-case
                by sync). Rows whose ``tsg`` is ``NULL`` (e.g. imported before
                the column was added) are excluded.
            name_like: SQL ``LIKE`` pattern applied to the meeting name column.
            location_like: SQL ``LIKE`` pattern applied to the meeting
                location column.
            year: integer year to match against ``end_date``.
            tdoc_id: ``(prefix, number)`` tuple (e.g. ``("R5-", 260013)``)
                identifying the TDoc to find a containing meeting for.
                A meeting matches when its ``start_doc`` prefix equals
                ``prefix`` and its 6-digit ``start_doc`` number is ``<=
                number``; if ``end_doc`` is non-null its prefix must also
                equal ``prefix`` and its 6-digit number must be ``>=
                number``. Prefix match is case-insensitive (``r5s``,
                ``R5S`` and ``r5S`` all match a stored ``R5s…`` row).
                Meetings without a ``start_doc`` never match. The tuple
                is produced by :func:`cli_filters.parse_tdoc_id` so
                callers don't need to re-validate the input shape.
        Pagination:
            offset: rows to skip before applying ``limit``. Combined with
                ``limit`` this enables CLI pagination without re-running
                the filters.
        """
        ...

    def get_by_id(self, meeting_id: int) -> Meeting | None:
        """Return a meeting record by its numeric ID."""
        ...

    def get_by_name(self, meeting_name: str) -> Meeting | None:
        """Return a meeting record by its exact meeting name."""
        ...

    def update_tdoc_list_last_sync(self, meeting_id: int, synced_at: datetime) -> bool:
        """Record when the TDoc list was last synced for a meeting.

        Returns ``True`` when a matching row existed and was updated,
        ``False`` otherwise.
        """
        ...

    def list_distinct_tsgs(self) -> list[str]:
        """Return the distinct, non-null TSG short names stored in meetings.

        Values are returned in ascending lexical order so callers can
        iterate deterministically. Used by ``meeting sync`` when no
        explicit ``--tsg`` is supplied.
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

    def update_meeting_last_sync(self, short_name: str, synced_at: datetime) -> bool:
        """Record when the meeting calendar was last synced for a TSG.

        Returns ``True`` when a matching row existed and was updated,
        ``False`` otherwise.
        """
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
        key. Existing rows are refreshed in place (the ``file`` label is
        rewritten) so re-syncing a meeting does not produce duplicates.
        Returns the number of input rows that were written.
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

    def get_for_tdoc_id(self, tdoc_id: str) -> list[TDocFile]:
        """Return every TDocFile whose ``tdoc_id`` matches, no limit.

        Ordered by ``(type, ftp_url) ASC`` so the output groups by
        category (``revision`` / ``review`` / ``support``) and is
        deterministic across calls. Used by ``tdoc show`` to surface
        every auxiliary file for the requested TDoc.
        """
        ...

    def get_by_ftp_url(self, ftp_url: str) -> list[TDocFile]:
        """Return every TDocFile whose ``ftp_url`` matches exactly.

        The unique index on ``tdoc_files.ftp_url`` makes this a
        constant-time PK-like lookup; in practice it returns at most
        one row. The list return type keeps the caller-side shape
        uniform with the multi-row :meth:`get_for_tdoc_id`. Used by
        ``tdoc show --ftp-url <url>`` to surface the auxiliary file
        (revision / review / support) attached to the requested URL
        rather than to a parent ``tdoc_id``.
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


class TDocCrDetailRepository(Protocol):
    """Storage operations for the slim ``tdoc_cr_cover_page`` table.

    Owns the cover-page fields extracted from a CR document. The
    companion ``tdoc_extracts`` table (cache paths + provenance) is still
    queried through this repository for read convenience, but writes are
    split into a separate :meth:`upsert_extract_meta` method so the
    service layer can persist cover-page rows, TTCN sidecars, and
    extract metadata independently.

    Identity is the immutable download ``url`` — 3GPP zip assets are
    byte-for-byte identical for the lifetime of a URL, while the logical
    ``tdoc_id`` may map to multiple URLs across revisions. ``tdoc_id``
    remains a non-PK FK into ``tdocs.tdoc_id`` with ``ondelete="CASCADE"``
    so deleting a parent TDoc still cleans up every revision's detail rows.
    """

    def get(self, tdoc_id: str) -> list[TDocCRDetails]:
        """Return every detail row for ``tdoc_id``.

        The same ``tdoc_id`` may map to multiple URLs across revisions;
        callers (the ``tdoc show`` CLI, debugging scripts) want every
        revision. Ordered by ``ftp_url`` ascending for a deterministic
        result that no longer depends on the removed ``extracted_at``
        column.
        """
        ...

    def get_by_url(self, url: str) -> TDocCRDetails | None:
        """Return the detail row for an immutable ``url``, or ``None``.

        Used by the extraction service to perform an O(1) cache-hit
        check after the download has resolved the actual URL.
        """
        ...

    def upsert(self, details: TDocCRDetails) -> None:
        """Insert/update the cover-page row in ``tdoc_cr_cover_page``.

        Rows are keyed by ``url`` (the immutable download URL), so
        re-extracting the same URL is idempotent.
        """
        ...

    def upsert_extract_meta(self, meta: TDocExtractMeta) -> None:
        """Insert/update the cache-extract metadata row in ``tdoc_extracts``."""
        ...

    def get_extract_meta(self, tdoc_id: str) -> list[TDocExtractMeta]:
        """Return every cached-extract metadata row for ``tdoc_id``.

        Mirror of :meth:`get` for the ``tdoc_extracts`` sidecar.
        Indexed lookup on the FK; ordered ``extracted_at`` desc so
        callers see the most recent extract first.
        """
        ...

    def get_extract_meta_by_url(self, url: str) -> TDocExtractMeta | None:
        """Return the extract-metadata row for an immutable ``url``."""
        ...

    def list_all(self) -> list[TDocCRDetails]:
        """Return every persisted detail row (CLI / debugging)."""
        ...


class TDocCrTTCNDetailRepository(Protocol):
    """Storage operations for the TTCN sidecar (one row per immutable ftp_url)."""

    def upsert(self, details: TDocCRTTCNDetails) -> None:
        """Insert/update the TTCN detail row in ``tdoc_cr_ttcn_details``."""
        ...

    def get_by_url(self, url: str) -> TDocCRTTCNDetails | None:
        """Return the TTCN detail row for an immutable ``url``, or ``None``."""
        ...

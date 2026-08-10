from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

from doc3gpp.models.jobs import Job, JobKind, JobStatus, JSONValue
from doc3gpp.models.meeting import Meeting
from doc3gpp.models.tdoc import TDoc, TDocWithMeeting
from doc3gpp.models.tdoc_cr import (
    TDocCRDetails,
    TDocCRTTCNDetails,
    TDocExtractMeta,
)
from doc3gpp.models.search import (
    RebuildProgress,  # noqa: F401
    SearchFilters,
    SearchHit,
    SearchIndexStatus,
    TDocMeta,
)
from doc3gpp.models.tdoc_cr_change_details import TDocCRChangeDetails
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
        - ``meeting_like``: rich-filter grammar applied to the parent
          meeting's name (joins the ``meetings`` table) — ``null`` /
          ``not-null`` match the column's nullability, a leading ``!``
          flips the comparison to ``NOT LIKE``, and anything else is
          treated as a ``LIKE`` pattern.
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
            tsg: rich-filter grammar applied to the ``meetings.tsg`` FK
                (``null`` / ``not-null`` / ``!pattern`` / plain LIKE;
                stored canonicalised in upper-case by sync, so plain
                LIKE patterns are upper-cased to match any case). Rows
                whose ``tsg`` is ``NULL`` (e.g. imported before the
                column was added) are excluded unless ``tsg='null'``.
            name_like: rich-filter grammar applied to the meeting name column.
            location_like: rich-filter grammar applied to the meeting
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

    def update_spec_last_sync(self, short_name: str, synced_at: datetime) -> bool:
        """Record when the spec list was last synced for a TSG.

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
        - ``name_like``, ``acronym_like``, ``release_like``: rich-filter
          grammar applied to the corresponding text columns (``null`` /
          ``not-null`` / ``!pattern`` / plain LIKE).
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


class TDocCrChangeDetailsRepository(Protocol):
    """Storage operations for the body-change sidecar (one row per immutable ftp_url)."""

    def upsert(self, details: TDocCRChangeDetails) -> None:
        """Insert/update the body-change row in ``tdoc_cr_change_details``."""
        ...

    def get_by_url(self, url: str) -> TDocCRChangeDetails | None:
        """Return the body-change row for an immutable ``url``, or ``None``."""
        ...

    def get_for_tdoc_id(self, tdoc_id: str) -> list[TDocCRChangeDetails]:
        """Return every body-change row for ``tdoc_id``."""
        ...


class SearchIndexRepository(Protocol):
    """FTS5-backed search index for ``tdocs``.

    All write paths are idempotent (``INSERT OR REPLACE`` /
    ``DELETE``). The repository owns the JOIN between the FTS5
    virtual table and the ``tdocs`` / ``meetings`` rows so callers
    never need to know that index rows are projections rather than
    copies of the source data.

    Implementations are dialect-aware: on sqlite with FTS5 enabled
    everything works; on non-sqlite or FTS5-less builds every
    method raises :class:`SearchUnavailableError`. The factory layer
    (``build_search_service``) catches that error once at startup
    and returns ``None`` so callers can degrade gracefully.
    """

    def upsert(self, tdoc_id: str) -> None:
        """Rebuild the FTS5 row for ``tdoc_id`` from the joined tables.

        Idempotent: a re-upsert of the same ``tdoc_id`` replaces the
        existing row in place. Decompresses the gzip JSON blobs in
        Python (sqlite has no ``gzip()`` SQL builtin) and inserts the
        concatenated text into every FTS5 column.

        ``tdoc_id`` is the user-facing string (e.g. ``"R5-1234567"``)
        — NOT the sqlite-internal rowid int — so the FTS5 row
        identity stays stable across full ``--rebuild`` cycles
        (FTS5 rowids get re-allocated on each ``DELETE+INSERT``).
        """
        ...

    def remove(self, tdoc_id: str) -> None:
        """Delete the FTS5 row for ``tdoc_id``. No-op if absent."""
        ...

    def search(
        self, query: str, filters: SearchFilters,
        snippet_tokens: int | None = None,
    ) -> list[SearchHit]:
        """Run an FTS5 ``MATCH`` + filters + ``bm25()`` scoring.

        Returns at most ``filters.limit`` hits, ordered by score
        ascending (lower = better in FTS5). Filters apply as
        additional ``AND`` clauses on the joined ``tdocs`` /
        ``meetings`` tables.

        ``snippet_tokens`` is an optional per-call override for the
        configured ``Settings.search.snippet_tokens`` knob. When
        ``None`` the cached setting value is used; the CLI's
        ``--snippet-tokens`` flag forwards the user-supplied value
        so a single invocation can retune the preview length
        without mutating the config.
        """
        ...

    def rebuild_batch(
        self,
        batch_size: int,
        after_id: str | None,
        stale_only: bool,
    ) -> Iterable[list[str]]:
        """Yield batches of ``tdoc_id`` strings for the rebuild loop.

        ``stale_only=True`` returns only rows whose
        ``tdocs.uploaded_date > last_indexed_uploaded_date``; the
        default (``False``) returns every ``tdoc_id``. ``after_id``
        sets the cursor — rows with ``tdoc_id > after_id`` are
        returned so resume picks up where the previous crash left
        off. The comparison is the natural TEXT order on
        ``tdocs.tdoc_id`` (e.g. ``"R5-1234567"`` < ``"R5-1234568"``).
        The caller (``SearchService``) iterates the batches and
        invokes :meth:`upsert` per id.
        """
        ...

    def count_tdocs_to_index(self, stale_only: bool) -> int:
        """Return how many rows the next rebuild will process.

        The CLI prints this total up-front so operators can size the
        rebuild. ``stale_only=True`` matches :meth:`rebuild_batch`'s
        filtering.
        """
        ...

    def get_resume_cursor(self) -> str | None:
        """Return the last ``tdoc_id`` written to ``tdoc_search_meta``.

        ``None`` means no cursor has been recorded; ``search index
        --rebuild --resume`` starts at the first id > cursor.
        """
        ...

    def set_resume_cursor(self, tdoc_id: str) -> None:
        """Update the resume cursor after a successful batch upsert.

        ``tdoc_id`` is the user-facing string (e.g. ``"R5-1234567"``).
        """
        ...

    def clear_resume_cursor(self) -> None:
        """Remove the resume cursor so the next rebuild starts at
        the first TDoc.

        ``search index --rebuild`` (no ``--resume``) calls this at
        the start of a rebuild to force a truly fresh start; the
        first successful batch upsert then writes a new cursor via
        :meth:`set_resume_cursor`.
        """
        ...

    def status(self) -> SearchIndexStatus:
        """Return a :class:`SearchIndexStatus` snapshot for ``search index``.

        Reads ``tdoc_search_meta`` + ``COUNT(*)`` from the FTS5 table
        + ``MAX(uploaded_date)`` from ``tdocs`` to compute
        ``is_stale``. ``enabled`` reflects whether the repo was
        constructed without raising :class:`SearchUnavailableError`
        — the service layer translates a missing service into
        ``enabled=False``.
        """
        ...


class EmbeddingReranker(Protocol):
    """Re-score an FTS5 hit list using a semantic model.

    The v1 default :class:`~doc3gpp.services.search_service.PassthroughReranker`
    returns ``hits`` unchanged so the service contract is testable
    before the embedding spec lands. When the embedding spec lands,
    a new impl plugs in here without any change to
    :class:`SearchService` or the CLI.
    """

    def rerank(
        self,
        semantic_query: str,
        hits: list[SearchHit],
        final_limit: int | None = None,
        quiet: bool = False,
    ) -> list[SearchHit]:
        """Return ``hits`` re-ordered (and possibly truncated) by relevance.

        ``semantic_query`` is the *embedding* input — distinct from any
        FTS5 expression. The default ``PassthroughReranker`` returns
        ``hits`` verbatim (a copy, sliced to ``final_limit`` if given).
        ``SemanticReranker`` encodes ``semantic_query`` once, looks up
        each candidate's closest chunk in ``vec_tdoc_embeddings`` via
        :meth:`VectorIndexRepository.get_min_distance_for_tdocs`,
        sorts by ``-min_distance`` desc, and truncates to
        ``final_limit``.

        ``final_limit`` is the user-visible output count (e.g. the
        ``--limit`` value from ``search query --sem-query``).
        The caller (CLI) is responsible for asking the upstream
        FTS5 repo for a *wider* candidate bag, then letting the
        reranker trim back to ``final_limit``.

        ``quiet`` gates the one-shot ``logger.warning`` that
        ``SemanticReranker`` emits when every candidate maps to
        ``MISSING_FLOOR`` (empty ``vec_tdoc_embeddings``). The flag
        is forwarded from the CLI's ``--quiet`` so scripted users
        can opt out of the side-channel warning without changing
        the visible output order. The default ``PassthroughReranker``
        never warns, so it accepts and ignores the flag.
        """
        ...


class Embedder(Protocol):
    """Embedding backend for the semantic search subsystem.

    The v1 default :class:`~doc3gpp.services.embedding.embedder.SentenceTransformerEmbedder`
    loads a HuggingFace sentence-transformers model lazily on first
    ``.encode()`` call. A future hosted-API impl plugs in here
    without any change to :class:`SemanticSearchService` or the CLI.
    """

    def encode(self, texts: list[str]) -> "np.ndarray":
        """Return shape ``(len(texts), dim)``, dtype float32."""
        ...

    @property
    def dim(self) -> int:
        """The model's embedding dimension (e.g. 384 for all-MiniLM-L6-v2)."""
        ...


class VectorIndexRepository(Protocol):
    """sqlite-vec backed vector index for ``tdocs``.

    All write paths are idempotent (``DELETE`` + ``INSERT``). One
    ``tdoc_id`` maps to N chunk rows (``chunk_id = "{tdoc_id}#{i}"``).
    Implementations are dialect-aware: on sqlite with sqlite-vec
    enabled everything works; on non-sqlite or sqlite-vec-less builds
    every method raises :class:`VectorIndexUnavailableError`. The
    factory layer catches that error once at startup and returns
    ``None`` so callers can degrade gracefully.
    """

    def upsert_chunks(self, tdoc_id: str, embeddings: list[np.ndarray]) -> None:
        """Replace all chunk rows for ``tdoc_id`` with the new embeddings.

        Deletes existing chunks for ``tdoc_id`` then inserts the new
        chunk rows in a single transaction. ``chunk_id`` is
        ``f"{tdoc_id}#{i}"`` for ``i`` in ``range(len(embeddings))``.
        """
        ...

    def remove_for_tdoc(self, tdoc_id: str) -> None:
        """Delete all chunk rows for ``tdoc_id``. No-op if absent."""
        ...

    def knn(
        self, query_vec: "np.ndarray", limit: int,
        filters: "SearchFilters | None" = None,
    ) -> list[tuple[str, str, int, float]]:
        """KNN by cosine distance; returns ``(tdoc_id, chunk_id, chunk_index, distance)``.

        Joins to ``tdocs`` / ``meetings`` for filters. ``limit`` caps
        the chunk count (NOT the tdoc count — the service reduces
        chunks to tdocs via ``min(distance)``).
        """
        ...

    def rebuild_batch(
        self, batch_size: int, after_id: "str | None", stale_only: bool,
    ) -> Iterable[list[str]]:
        """Yield batches of ``tdoc_id`` strings in ``ORDER BY tdoc_id ASC``."""
        ...

    def count_tdocs_to_index(self, stale_only: bool) -> int: ...

    def get_resume_cursor(self) -> "str | None": ...

    def set_resume_cursor(self, tdoc_id: str) -> None: ...

    def clear_resume_cursor(self) -> None: ...

    def status(self) -> "SearchIndexStatus": ...

    def get_tdocs_metadata(
        self, tdoc_ids: list[str],
    ) -> dict[str, TDocMeta]:
        """Batch-fetch ``tdocs`` + ``meetings`` metadata for the given ids.

        Used by the search service to enrich vector-only hits with
        ``title``, ``ftp_url``, ``wis``, ``meeting``, ``tsg``, and
        ``uploaded_date`` so the CLI can render real data instead
        of a blank stub when FTS5 missed the hit. Returns an empty
        dict for tdoc_ids that no longer exist (deleted between
        index and query).
        TDoc ids are bound individually as named parameters (no
        string interpolation) so the call is SQL-injection safe.
        """
        ...

    def get_min_distance_for_tdocs(
        self,
        tdoc_ids: Sequence[str],
        query_vec: Sequence[float],
    ) -> dict[str, tuple[float, str] | None]:
        """For each tdoc_id, return the closest-chunk distance to ``query_vec``.

        Returns a dict keyed by tdoc_id; each value is either
        ``(min_distance, best_chunk_id)`` for the row with the
        smallest cosine distance to ``query_vec``, or ``None`` if the
        tdoc has no rows in ``vec_tdoc_embeddings``.

        The implementation must issue a single batched SQL trip.
        TDoc ids with no rows are not an error — they map to ``None``
        so the caller can apply its missing-candidate policy
        (e.g. ``SemanticReranker`` uses ``MISSING_FLOOR``).

        Empty ``tdoc_ids`` returns an empty dict without touching
        the database.
        """
        ...


class JobRepository(Protocol):
    """Storage operations for background-job records.

    Pure persistence layer over the ``jobs`` SQL table. The HTTP and
    MCP routes plus the asyncio worker (T7) share this contract so
    any backend (sqlite, postgres) can be swapped without rewriting
    the route / handler code.

    Lifecycle methods (``mark_running``, ``mark_succeeded``,
    ``mark_failed``, ``mark_cancelled``) are the only way to
    transition a job's ``status``; the repository stamps the matching
    ``started_at`` / ``finished_at`` timestamp automatically so the
    caller never has to remember to set it.
    """

    def create(self, kind: JobKind, params: Mapping[str, JSONValue]) -> Job:
        """Persist a fresh ``QUEUED`` row and return the resulting ``Job``.

        Mints a new UUID4 hex ``id`` and stamps ``created_at`` with
        the current UTC time. ``status`` is always ``QUEUED`` on
        return; ``log_lines`` is an empty tuple; ``result_summary``
        / ``error`` / ``started_at`` / ``finished_at`` are ``None``.
        """
        ...

    def get(self, job_id: str) -> Job | None:
        """Return the job row for ``job_id``, or ``None`` when absent."""
        ...

    def list(
        self,
        *,
        limit: int = 50,
        status: JobStatus | None = None,
    ) -> list[Job]:
        """Return recent job rows ordered by descending ``created_at``.

        ``limit`` caps the result count (default 50). When ``status``
        is supplied the rows are filtered to that lifecycle state.
        The composite ``(status, created_at DESC)`` index covers
        the filtered case; the unfiltered case falls back to a full
        scan ordered by ``created_at`` because no single-column
        index on ``created_at`` is needed for the v1 dashboard.
        """
        ...

    def mark_running(self, job_id: str, *, message: str = "starting") -> tuple[bool, Job]:
        """Transition ``job_id`` from ``QUEUED`` to ``RUNNING``.

        Stamps ``started_at`` with the current UTC time and
        appends ``message`` to ``log_lines``. Returns a
        ``(claimed, job)`` pair: ``claimed`` is ``True`` when this
        call performed the transition (the row was ``QUEUED``), and
        ``False`` when the row was already ``RUNNING`` / terminal —
        the claim lost a race against another worker. ``job`` is the
        refreshed row in either case, so callers that only need the
        row can ignore the flag.
        """
        ...

    def append_log(self, job_id: str, *, line: str) -> None:
        """Append ``line`` to ``log_lines``, capping the buffer at 50 entries.

        FIFO eviction — the oldest entry is dropped when the buffer
        is full so the column never grows unboundedly. The cap is
        enforced in the repository, not the dataclass, so the
        :class:`doc3gpp.models.jobs.Job` value object remains a
        pure projection of the persisted state.
        """
        ...

    def mark_succeeded(
        self,
        job_id: str,
        *,
        summary: Mapping[str, JSONValue],
    ) -> Job:
        """Transition ``job_id`` to ``SUCCEEDED`` with ``summary``.

        Stamps ``finished_at`` with the current UTC time and writes
        ``summary`` into ``result_summary``. Returns the refreshed
        job.
        """
        ...

    def mark_failed(self, job_id: str, *, error: str) -> Job:
        """Transition ``job_id`` to ``FAILED`` with ``error``.

        Stamps ``finished_at`` with the current UTC time and writes
        ``error`` into the ``error`` column. Returns the refreshed
        job.
        """
        ...

    def mark_cancelled(self, job_id: str) -> Job:
        """Transition ``job_id`` to ``CANCELLED``.

        Stamps ``finished_at`` with the current UTC time. Returns
        the refreshed job. ``error`` is left ``None`` — cancellation
        is an operator decision, not a failure.
        """
        ...

    def delete_older_than(self, cutoff: datetime) -> int:
        """Delete terminal jobs older than ``cutoff``; return the row count.

        Only ``SUCCEEDED``, ``FAILED``, and ``CANCELLED`` rows whose
        ``finished_at`` is strictly older than ``cutoff`` are
        removed. ``QUEUED`` and ``RUNNING`` rows are left alone
        because the worker may still be reading them. The method
        returns the number of rows actually deleted.
        """
        ...


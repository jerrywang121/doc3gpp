"""Domain model for 3GPP CR (Change Request) TDocs.

This is the value object returned by :mod:`doc3gpp.parsers.cr_parser`
when it parses the markdown produced from a CR's ``.docx`` body. It is
the domain shape used by the service layer (Phase 6) to persist CR
details.

Design notes:

* ``@dataclass(slots=True, frozen=True)`` keeps the object immutable
  and hashable — service / repo code can use it as a dict key or in a
  set without surprises.
* ``extracted_tdoc_id`` records what the header parser actually found
  in the document. It may diverge from the caller's input ``tdoc_id``
  when the document uses docx field codes that python-docx does not
  render — that's a diagnostic signal, not a hard error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from datetime import datetime
from typing import Any, Literal


_PARSER_VERSION = "1.0.0"


@dataclass(slots=True, frozen=True)
class TDocCRDetails:
    """Structured fields extracted from a 3GPP CR TDoc.

    A frozen dataclass so callers (parsers, service layers, repo
    implementations) can rely on value semantics. Use
    :meth:`to_persisted` to convert into the JSON-friendly shape the
    SQL repo writes to the ``tdoc_cr_cover_page`` table.

    Attributes:
        tdoc_id: Canonical TDoc identifier, always set from the
            caller's input. The parser never overrides this field;
            what it found in the document body is recorded in
            :attr:`extracted_tdoc_id` instead.
        spec: 3GPP spec number (e.g. ``"38.523-3"``). May be ``None``
            when the document stores it in a docx field that
            python-docx does not render.
        cr_num: Numeric CR identifier as a string (e.g. ``"3790"``).
        rev: CR revision, normalised to a digit string; the cover
            page's ``-`` placeholder becomes ``"0"``.
        version: Current spec version (e.g. ``"18.4.0"``).
        title: CR title.
        source: Contents of ``Source to WG:``.
        tsg: Contents of ``Source to TSG:``. ``None`` when not
            rendered by python-docx (e.g. docx field codes).
        related_wis: Contents of ``Work item code:``.
        date: Cover-page date (``YYYY-MM-DD``) parsed from the ``Date:``
            cell on the docx cover page into a :class:`datetime.date`.
            ``None`` when the cell is missing or the value is not a
            valid ISO 8601 date.
        cr_cat: Single-letter category code (F / B / A / C / D).
        release: Release label (e.g. ``"Rel-18"``).
        reason_for_change: Reason-for-change cell text.
        consequences_if_not_approved: Consequences cell text.
        clauses_affected: Clauses-affected cell text.
        other_comments: Other-comments cell text.
        revision_history: Revision-history cell text.
        extracted_tdoc_id: What the header parser actually found in
            the document (may differ from ``tdoc_id`` when the docx
            uses field codes that python-docx does not render).
        ftp_url: Exact URL the TDoc zip was downloaded from during
            this extract, stored as a path relative to
            ``https://www.3gpp.org/ftp/``. ``None`` when the zip came
            from a prior cache hit (the originating URL is not tracked
            there) or when no provenance was captured.
    """

    tdoc_id: str
    spec: str | None = None
    cr_num: str | None = None
    rev: str | None = None
    version: str | None = None
    title: str | None = None
    source: str | None = None
    tsg: str | None = None
    related_wis: str | None = None
    date: date | None = None
    cr_cat: str | None = None
    release: str | None = None
    reason_for_change: str | None = None
    consequences_if_not_approved: str | None = None
    clauses_affected: str | None = None
    other_comments: str | None = None
    revision_history: str | None = None
    extracted_tdoc_id: str | None = None
    # Download provenance (None on cache hits; otherwise the relative URL
    # path, relative to https://www.3gpp.org/ftp/, that supplied the cached
    # zip bytes during this extract).
    ftp_url: str | None = None

    def __post_init__(self) -> None:
        # Validate tdoc_id is non-empty; ``frozen=True`` means we use
        # ``object.__setattr__`` for any post-init mutation. None of
        # the other fields have a hard validation rule today.
        stripped = self.tdoc_id.strip()
        if not stripped:
            raise ValueError("TDocCRDetails requires a non-empty tdoc_id")
        # Normalise leading/trailing whitespace without breaking
        # frozen semantics.
        if stripped != self.tdoc_id:
            object.__setattr__(self, "tdoc_id", stripped)

    def to_persisted(self) -> dict[str, Any]:
        """Return a copy shaped for SQL persistence.

        Other fields pass through unchanged.

        Returns:
            Dict keyed by SQL column name.
        """
        payload: dict[str, Any] = {
            "tdoc_id": self.tdoc_id,
            "spec": self.spec,
            "cr_num": self.cr_num,
            "rev": self.rev,
            "version": self.version,
            "title": self.title,
            "source": self.source,
            "tsg": self.tsg,
            "related_wis": self.related_wis,
            "date": self.date,
            "cr_cat": self.cr_cat,
            "release": self.release,
            "reason_for_change": self.reason_for_change,
            "consequences_if_not_approved": self.consequences_if_not_approved,
            "clauses_affected": self.clauses_affected,
            "other_comments": self.other_comments,
            "revision_history": self.revision_history,
            "extracted_tdoc_id": self.extracted_tdoc_id,
            "ftp_url": self.ftp_url,
        }
        return payload


@dataclass(slots=True, frozen=True)
class TDocCRTTCNDetails:
    """TTCN-specific details extracted from a TTCN CR cover page.

    A frozen sidecar value object that mirrors the
    ``tdoc_cr_ttcn_details`` SQL table. It holds the six overview fields
    exposed by the TTCN parser plus the list of required corrections.

    Attributes:
        tdoc_id: Canonical TDoc identifier (FK into ``tdocs.tdoc_id``).
        ftp_url: Immutable download URL this row is keyed on, stored as
            a path relative to ``https://www.3gpp.org/ftp/``. ``None`` in
            the parser before the service layer supplies the provenance
            URL.
        testcase: Testcase overview field.
        ue: UE overview field.
        ss: SS overview field.
        ats_version: ATS version overview field.
        ttcn_release: TTCN release derived from the ATS version.
        test_suite: Test suite overview field.
        required_changes: List of correction dicts produced by the TTCN
            parser.
        changed_functions:
            Sorted, deduplicated `<module>.<function>` entries derived from
            `required_changes`. Empty list when no entries could be extracted.
    """

    tdoc_id: str
    ftp_url: str | None = None
    testcase: str | None = None
    ue: str | None = None
    ss: str | None = None
    ats_version: str | None = None
    ttcn_release: str | None = None
    test_suite: str | None = None
    required_changes: list[dict[str, Any]] = field(default_factory=list)
    changed_functions: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Mirror TDocExtractMeta' invariant; the URL is the row identity.
        # ``None`` is allowed here because the parser does not know the
        # provenance URL; the service layer sets it before persistence.
        if self.ftp_url is not None:
            stripped = self.ftp_url.strip()
            if not stripped:
                raise ValueError("TDocCRTTCNDetails requires a non-empty ftp_url")
            if stripped != self.ftp_url:
                object.__setattr__(self, "ftp_url", stripped)
        stripped_id = self.tdoc_id.strip()
        if not stripped_id:
            raise ValueError("TDocCRTTCNDetails requires a non-empty tdoc_id")
        if stripped_id != self.tdoc_id:
            object.__setattr__(self, "tdoc_id", stripped_id)


@dataclass(slots=True, frozen=True)
class TDocCRParseResult:
    """Bundle produced by a CR parser.

    Wraps the cover-page details and the optional TTCN sidecar so the
    service layer can route each slice to its own repository in one
    pass.

    Attributes:
        cover: Cover-page fields extracted from the CR document.
        ttcn: TTCN-specific sidecar when the parser recognised a TTCN CR;
            ``None`` for non-TTCN CRs.
    """

    cover: TDocCRDetails
    ttcn: TDocCRTTCNDetails | None = None


@dataclass(slots=True, frozen=True)
class TDocExtractMeta:
    """Cache-extraction metadata for one **immutable download URL**.

    Mirrors :class:`doc3gpp.storage.db.models.TDocExtractOrm` but stays
    a pure value object so the service layer can move the data around
    without leaking SQLAlchemy attributes. Only the *paths* of the
    cached artefacts are persisted here — the bytes live under
    :mod:`doc3gpp.scraping.cache`.

    Identity is the immutable URL — the same URL serves byte-for-byte
    identical 3GPP artefacts, while a TDoc id may map to multiple URLs
    across revisions. ``tdoc_id`` stays as a logical reference and a
    foreign key into ``tdocs``.

    Attributes:
        ftp_url: Immutable download URL this cache row is keyed on,
            stored as a path relative to ``https://www.3gpp.org/ftp/``;
            matches the corresponding :class:`TDocCRDetails` row's
            ``ftp_url``.
        tdoc_id: Canonical TDoc identifier (logical reference, FK into
            ``tdocs.tdoc_id``).
        cache_file: Basename of the cached artefacts on disk — the
            absolute paths are reconstructed as
            ``cache.root / 'zips' / cache_file`` and
            ``cache.root / 'markdown' / cache_file`` at lookup time
            (see :mod:`doc3gpp.scraping.cache`). Only the basename
            is persisted so the row stays portable when ``cache.dir``
            moves.
        doc_filename: Filename of the word document inside the zip
            (e.g. ``"R5s260009.docx"``).
        extracted_at: When the extract was performed. ``None`` on
            newly-built values (the SQL repo stamps a fresh value on
            first insert via the column's ``server_default``).
        parser_version: Version of the parser that produced this
            metadata. ``"1.0.0"`` matches the value baked into the
            ORM column's default and :class:`TDocCRDetails`.
    """

    ftp_url: str
    tdoc_id: str
    cache_file: str
    doc_filename: str
    extracted_at: datetime | None = None
    parser_version: str = _PARSER_VERSION

    def __post_init__(self) -> None:
        # Mirror TDocCRDetails' invariant; the URL is the row identity.
        stripped = self.ftp_url.strip()
        if not stripped:
            raise ValueError("TDocExtractMeta requires a non-empty ftp_url")
        if stripped != self.ftp_url:
            object.__setattr__(self, "ftp_url", stripped)
        stripped_id = self.tdoc_id.strip()
        if not stripped_id:
            raise ValueError("TDocExtractMeta requires a non-empty tdoc_id")
        if stripped_id != self.tdoc_id:
            object.__setattr__(self, "tdoc_id", stripped_id)


# Allowed values for ``DirectParseResult.source_kind``. Stored as a
# module-level literal so callers (CLI, tests) can exhaustively match
# without hard-coding strings. ``"local"`` and the two URL variants
# cover every behaviour matrix cell in the direct-parse plan.
DirectSourceKind = Literal["local", "url-3gpp", "url-other"]


@dataclass(slots=True, frozen=True)
class DirectParseResult:
    """Outcome of a single ``tdoc parse --from-path/--from-url`` call.

    The dataclass bundles every value the CLI dispatcher needs to
    decide what to write to disk, the database, and stdout — keeping
    the service-layer return type a single value object avoids a
    multi-return tuple at the CLI boundary.

    Attributes:
        source_kind: Where the bytes came from. One of ``"local"``
            (file on disk), ``"url-3gpp"`` (HTTP(S) URL on the
            canonical 3GPP FTP root), or ``"url-other"`` (any other
            HTTP(S) URL). Mutually exclusive; the dispatcher picks
            exactly one per call.
        markdown: The converted markdown, always populated. For
            ``--format raw`` this is what the CLI emits verbatim; for
            the structured formats it feeds ``parse_cr_details``.
        details: The parsed CR fields. ``None`` when ``--format raw``
            was selected (the raw converter never calls the parser).
        extract_meta: Cache-extract metadata sidecar. ``None`` when
            the call did not write to the on-disk cache (local files,
            non-3GPP URLs, and 3GPP URLs whose FK target is missing
            from ``tdocs``).
        from_cache: ``True`` when the call hit a previously persisted
            ``tdoc_cr_cover_page`` row and short-circuited the network
            and parser paths. Only ever set for 3GPP-URL happy-path
            cells.
        persisted: ``True`` when this call wrote both a
            ``tdoc_extracts`` row and, unless ``--format raw``, a
            ``tdoc_cr_cover_page`` row. False for local files,
            non-3GPP URLs, the FK-miss cells, and cache hits (which
            do not re-write the rows).
        tdoc_id: The 3GPP TDoc id extracted from the source
            filename when the pattern matches; ``None`` when the
            filename has no 3GPP id (a synthetic ``LOCAL-<stem>``
            is used internally by the parser but is *not* surfaced
            here so the caller can branch on "did the filename
            match?" without recomputing the synthetic id).
        tdoc_id_in_tdocs: ``True`` when the extracted ``tdoc_id``
            has a matching row in the ``tdocs`` table — i.e. when
            the FK target for the DB writes exists. Always
            ``False`` for local files and non-3GPP URLs; only the
            3GPP-URL branch consults the ``tdocs`` table.
        source_url: The original file or folder URL supplied to the
            direct-parse call. Populated for URL sources so batch
            emitters can mirror the upstream folder structure.
    """

    source_kind: DirectSourceKind
    markdown: str
    details: TDocCRDetails | None
    extract_meta: TDocExtractMeta | None
    from_cache: bool
    persisted: bool
    tdoc_id: str | None
    tdoc_id_in_tdocs: bool
    source_url: str | None = None


@dataclass(slots=True, frozen=True)
class DirectParseBatchResult:
    """Outcome of a batch ``tdoc parse --from-url <folder>`` run.

    Bundles every per-file :class:`DirectParseResult` with a failure map
    and a skip map so the CLI can emit a summary without recomputing
    visit order. ``skipped`` carries URLs that were too large for the
    ``tdoc_parse.max_tdoc_size_kb`` cap (size-limit skip) — kept
    separate from ``failures`` because the operator-facing meaning
    differs (size-skip is a budget decision, failure is a bug).
    """

    results: list[DirectParseResult]
    failures: dict[str, str]
    skipped: dict[str, str] = field(default_factory=dict)

"""Domain model for 3GPP CR (Change Request) TDocs.

This is the value object returned by :mod:`doc3gpp.parsers.cr_parser`
when it parses the markdown produced from a CR's ``.docx`` body. It is
the domain shape used by the service layer (Phase 6) to persist CR
details.

Design notes:

* ``@dataclass(slots=True, frozen=True)`` keeps the object immutable
  and hashable — service / repo code can use it as a dict key or in a
  set without surprises.
* ``corrections`` is a ``list[dict]`` (one entry per metadata table)
  rather than a single blob; the service layer JSON-serialises it on
  demand via :meth:`TDocCRDetails.to_persisted`.
* ``extracted_tdoc_id`` records what the header parser actually found
  in the document. It may diverge from the caller's input ``tdoc_id``
  when the document uses docx field codes that python-docx does not
  render — that's a diagnostic signal, not a hard error.
* ``tech`` and ``year`` are derived fields rather than parsed; the
  caller can verify them independently or override them downstream.
"""

from __future__ import annotations

import json
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
    SQL repo writes to the ``tdoc_cr_details`` table.

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
        ats_version: TTCN ATS version identifier (e.g.
            ``"iwd-TTCN3-B2512-..."``); ``None`` for non-TTCN CRs.
        ttcn_release: Last six chars of ``ats_version``.
        test_case: TTCN test-case name (e.g. ``"7.1.3.5.3"``).
        test_suite: TTCN test-suite label (e.g. ``"NR5GC"``).
        ue: TTCN UE-used entry.
        ss: TTCN SS-used entry.
        corrections: List of per-correction metadata dicts. Each
            entry holds ``function_name``, ``reason_for_change``,
            ``summary_of_change``, ``ttcn_module``, ``mcc160_comment``,
            plus ``before_change`` / ``after_change`` / ``new_change``
            when ``full=True``.
        year: Four-digit year derived from ``tdoc_id`` (positions
            3–4 → ``"20YY"``); matches 3GPP meeting numbering.
        tech: Technology label derived from ``spec`` (e.g.
            ``"5G"`` / ``"LTE"``).
        extracted_tdoc_id: What the header parser actually found in
            the document (may differ from ``tdoc_id`` when the docx
            uses field codes that python-docx does not render).
        ftp_url: Exact URL the TDoc zip was downloaded from during
            this extract, stored as a path relative to
            ``https://www.3gpp.org/ftp/``. ``None`` when the zip came
            from a prior cache hit (the originating URL is not tracked
            there) or when no provenance was captured.
        parser_version: Version of the parser that produced this
            object, persisted alongside the row for debugging.
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
    # TTCN-only fields
    ats_version: str | None = None
    ttcn_release: str | None = None
    test_case: str | None = None
    test_suite: str | None = None
    ue: str | None = None
    ss: str | None = None
    # Corrections (TTCN-only; list of per-correction metadata)
    corrections: list[dict[str, str]] = field(default_factory=list)
    # Derived
    year: int | None = None
    tech: str | None = None
    extracted_tdoc_id: str | None = None
    # Download provenance (None on cache hits; otherwise the relative URL
    # path, relative to https://www.3gpp.org/ftp/, that supplied the cached
    # zip bytes during this extract).
    ftp_url: str | None = None
    parser_version: str = _PARSER_VERSION

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

        The SQL schema stores ``corrections`` as a JSON ``TEXT`` blob
        rather than a relation, so the service layer converts the
        list-of-dicts in this dataclass into a single string. Other
        fields pass through unchanged.

        Returns:
            Dict keyed by SQL column name with ``corrections_json``
            replacing the in-memory ``corrections`` list.
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
            "ats_version": self.ats_version,
            "ttcn_release": self.ttcn_release,
            "test_case": self.test_case,
            "test_suite": self.test_suite,
            "ue": self.ue,
            "ss": self.ss,
            "year": self.year,
            "tech": self.tech,
            "extracted_tdoc_id": self.extracted_tdoc_id,
            "ftp_url": self.ftp_url,
            "parser_version": self.parser_version,
            "corrections_json": json.dumps(
                self.corrections, ensure_ascii=False
            ),
        }
        return payload


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
        zip_path: Absolute path to the cached 3GPP zip download.
        markdown_path: Absolute path to the cached markdown rendering
            of the CR's ``.docx`` body.
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
    zip_path: str
    markdown_path: str
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
    """Outcome of a single ``tdoc parse --from-file/--from-url`` call.

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
            ``tdoc_cr_details`` row and short-circuited the network
            and parser paths. Only ever set for 3GPP-URL happy-path
            cells.
        persisted: ``True`` when this call wrote both a
            ``tdoc_extracts`` row and, unless ``--format raw``, a
            ``tdoc_cr_details`` row. False for local files,
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
    """

    source_kind: DirectSourceKind
    markdown: str
    details: TDocCRDetails | None
    extract_meta: TDocExtractMeta | None
    from_cache: bool
    persisted: bool
    tdoc_id: str | None
    tdoc_id_in_tdocs: bool

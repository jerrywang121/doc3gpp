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
from typing import Any


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
        url: Exact URL the TDoc zip was downloaded from during this
            extract. ``None`` when the zip came from a prior cache
            hit (the originating URL is not tracked there) or when
            no provenance was captured.
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
    # Download provenance (None on cache hits; otherwise the URL that
    # supplied the cached zip bytes during this extract).
    url: str | None = None
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
            "url": self.url,
            "parser_version": self.parser_version,
            "corrections_json": json.dumps(
                self.corrections, ensure_ascii=False
            ),
        }
        return payload


@dataclass(slots=True, frozen=True)
class TDocExtractMeta:
    """Cache-extraction metadata for one TDoc.

    Mirrors :class:`doc3gpp.storage.db.models.TDocExtractOrm` but stays
    a pure value object so the service layer can move the data around
    without leaking SQLAlchemy attributes. Only the *paths* of the
    cached artefacts are persisted here — the bytes live under
    :mod:`doc3gpp.scraping.cache`.

    Attributes:
        tdoc_id: Canonical TDoc identifier; primary key in both the
            metadata table and the detail table.
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

    tdoc_id: str
    zip_path: str
    markdown_path: str
    doc_filename: str
    extracted_at: datetime | None = None
    parser_version: str = _PARSER_VERSION

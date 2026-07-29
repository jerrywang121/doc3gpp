"""Sidecar domain object for the body-change extractor.

A frozen dataclass mirroring the ``tdoc_cr_change_details`` SQL
table. Carries the immutable download URL the row is keyed on, the
``tdoc_id`` FK, the sorted/unique clause numbers observed across
the body, and the captured change blocks (each a tuple of the
literal markdown lines that surround the revision marks).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class TDocCRChangeDetails:
    """Body-derived change details for a non-TTCN TDoc CR.

    Mirrors :class:`doc3gpp.models.tdoc_cr.TDocCRTTCNDetails`'s
    "parser leaves URL / tdoc_id blank, service fills them in"
    contract: both ``ftp_url`` and ``tdoc_id`` are typed
    ``str | None`` and accepted as ``None`` here. The parser emits
    the ``None`` sentinel because the body-extraction step has no
    download URL or TDoc id of its own; the service layer fills
    both in via :func:`dataclasses.replace` before persistence.
    An empty string supplied to either field is still rejected —
    the validation only relaxes for ``None``.

    Attributes:
        ftp_url: Immutable download URL this row is keyed on, stored
            as a path relative to ``https://www.3gpp.org/ftp/``. ``None``
            in the parser (the service layer fills it in once the
            download URL is known); non-empty on the persisted row.
        tdoc_id: Canonical TDoc identifier (FK into ``tdocs.tdoc_id``).
            ``None`` in the parser (the service layer fills it in);
            non-empty on the persisted row.
        clauses: Sorted, unique clause numbers observed in the body
            that belong to a captured change block. Stored as
            newline-delimited text on the table; reconstructed from
            ``splitlines()`` on read.
        changes: One tuple per captured change block. Each block is
            itself a tuple of the original markdown lines (marker
            lines + gap-window bridge + context-padding plain lines).
            The outer structure round-trips as gzip-JSON.
    """

    ftp_url: str | None = None
    tdoc_id: str | None = None
    clauses: tuple[str, ...] = ()
    changes: tuple[tuple[str, ...], ...] = ()

    def __post_init__(self) -> None:
        # Mirror TDocCRTTCNDetails' invariant: the URL is the row
        # identity, ``None`` is the parser-side "unknown yet" sentinel,
        # and an empty string is still a programmer error.
        if self.ftp_url is not None:
            stripped = self.ftp_url.strip()
            if not stripped:
                raise ValueError(
                    "TDocCRChangeDetails requires a non-empty ftp_url"
                )
            if stripped != self.ftp_url:
                object.__setattr__(self, "ftp_url", stripped)
        if self.tdoc_id is not None:
            stripped_id = self.tdoc_id.strip()
            if not stripped_id:
                raise ValueError(
                    "TDocCRChangeDetails requires a non-empty tdoc_id"
                )
            if stripped_id != self.tdoc_id:
                object.__setattr__(self, "tdoc_id", stripped_id)

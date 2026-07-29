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

    Attributes:
        ftp_url: Immutable download URL this row is keyed on, stored
            as a path relative to ``https://www.3gpp.org/ftp/``.
            ``None`` is not allowed — the row identity is the URL.
        tdoc_id: Canonical TDoc identifier (FK into ``tdocs.tdoc_id``).
        clauses: Sorted, unique clause numbers observed in the body
            that belong to a captured change block. Stored as
            newline-delimited text on the table; reconstructed from
            ``splitlines()`` on read.
        changes: One tuple per captured change block. Each block is
            itself a tuple of the original markdown lines (marker
            lines + gap-window bridge + context-padding plain lines).
            The outer structure round-trips as gzip-JSON.
    """

    ftp_url: str
    tdoc_id: str
    clauses: tuple[str, ...] = ()
    changes: tuple[tuple[str, ...], ...] = ()

    def __post_init__(self) -> None:
        stripped_url = self.ftp_url.strip()
        if not stripped_url:
            raise ValueError(
                "TDocCRChangeDetails requires a non-empty ftp_url"
            )
        if stripped_url != self.ftp_url:
            object.__setattr__(self, "ftp_url", stripped_url)

        stripped_id = self.tdoc_id.strip()
        if not stripped_id:
            raise ValueError(
                "TDocCRChangeDetails requires a non-empty tdoc_id"
            )
        if stripped_id != self.tdoc_id:
            object.__setattr__(self, "tdoc_id", stripped_id)

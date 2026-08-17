"""Domain objects for the LS TDoc sidecar.

The :class:`TDocLSDetails` dataclass mirrors the ``tdoc_cr_ls_details``
SQL table — one row per immutable ``ftp_url`` — and carries the
eleven header fields extracted from a 3GPP LS markdown body plus
bookkeeping columns. The :class:`TDocLSParserResult` is the parser's
output envelope; ``cover`` holds the parsed details, ``None`` when
the parser declined (header missing).

Like the CR sidecar models, the parser emits ``None`` for ``ftp_url``
and ``tdoc_id`` because the body extractor has no download URL or
TDoc id of its own; the service layer fills them in via
:func:`dataclasses.replace` before persistence. An empty string is
still a programmer error — the validation only relaxes for ``None``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TypedDict


class LSAttachment(TypedDict):
    """One attachment row from the LS P17 line."""

    doc_number: str
    description: str


@dataclass(slots=True, frozen=True)
class TDocLSDetails:
    """Structured header fields for an LS TDoc, plus bookkeeping.

    Attributes:
        ftp_url: Immutable download URL the row is keyed on. ``None``
            in the parser; non-empty on the persisted row.
        tdoc_id: Canonical TDoc identifier (FK into ``tdocs.tdoc_id``).
            ``None`` in the parser; non-empty on the persisted row.
        variant: Format variant tag (e.g. ``"3gpp"``). Defaults to
            ``"3gpp"``. Future variants (e.g. ``"ieee"``, ``"etsi"``)
            register distinct parsers and stamp their own value here.
        title: P3 ``Title:`` cell, minus the ``LS on`` prefix.
        response_to_doc: P4 regex group 1 — original LS-out doc number.
        response_to_title: P4 regex group 2 — original LS title.
        response_to_group: P4 regex group 3 — original LS group.
        release: P5 ``Release:`` cell.
        work_item_name: P6 regex group 1 — work item name.
        work_item_code: P6 regex group 2 — work item code.
        source: P8 ``Source:`` cell — submitting organisation name(s).
        to_groups: P9 ``To:`` cell, newline-delimited.
        cc_groups: P10 ``Cc:`` cell, newline-delimited.
        attachments: P17 ``Attachments:`` cells, parsed as
            ``LSAttachment`` records. Stored as gzip-JSON on the table.
        parser_version: Parser version string.
        extracted_at: Server-side UTC timestamp, populated by the
            repository's ``upsert`` method.
    """

    ftp_url: str | None = None
    tdoc_id: str | None = None
    variant: str = "3gpp"
    title: str | None = None
    response_to_doc: str | None = None
    response_to_title: str | None = None
    response_to_group: str | None = None
    release: str | None = None
    work_item_name: str | None = None
    work_item_code: str | None = None
    source: str | None = None
    to_groups: str = ""
    cc_groups: str = ""
    attachments: tuple[dict[str, str], ...] = ()
    parser_version: str = "1.0.0"
    extracted_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.ftp_url is not None:
            stripped = self.ftp_url.strip()
            if not stripped:
                raise ValueError("TDocLSDetails requires a non-empty ftp_url")
            if stripped != self.ftp_url:
                object.__setattr__(self, "ftp_url", stripped)
        if self.tdoc_id is not None:
            stripped_id = self.tdoc_id.strip()
            if not stripped_id:
                raise ValueError("TDocLSDetails requires a non-empty tdoc_id")
            if stripped_id != self.tdoc_id:
                object.__setattr__(self, "tdoc_id", stripped_id)


@dataclass(slots=True, frozen=True)
class TDocLSParserResult:
    """Output envelope for an LS parser invocation.

    ``cover`` is ``None`` when the parser declined (header missing or
    the variant extractor chose not to fire). All other LS sidecar
    slots (TTCN, body changes) are absent for LS rows.
    """

    cover: TDocLSDetails | None = None


__all__ = ["LSAttachment", "TDocLSDetails", "TDocLSParserResult"]

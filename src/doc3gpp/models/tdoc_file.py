"""Domain model for auxiliary files attached to a 3GPP TDoc.

A ``TDocFile`` is an attachment that lives next to a base TDoc in a meeting
FTP directory. Three kinds of attachments are recognised:

- ``revision``: a renamed revision of the base TDoc
  (e.g. ``R5s260001r1.zip``). Stored under ``Inbox/`` for most meetings and
  under ``Docs/`` for R5 TTCN Workshop meetings.
- ``review``: a TTCN CR review document produced for a base TDoc
  (e.g. ``R5s260001_MCC160Comments.zip``). Stored under ``Review/`` for
  R5 TTCN email meetings.
- ``support``: a supporting draft prose CR document for the same TTCN CR
  (e.g. ``R5s260001_draft_prose.zip``). Stored alongside reviews in
  ``Review/``.

The base TDoc itself (``{tdoc_id}.zip``) is **not** modelled here — it lives
on :class:`doc3gpp.models.tdoc.TDoc` as the ``url`` field.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


# Allowed values for :attr:`TDocFile.type`. Centralised as module constants so
# callers and the SQL column constraint can share one source of truth.
TDocFileTypeRevision = "revision"
TDocFileTypeReview = "review"
TDocFileTypeSupport = "support"
TDocFileTypes: frozenset[str] = frozenset(
    {TDocFileTypeRevision, TDocFileTypeReview, TDocFileTypeSupport}
)


@dataclass(slots=True)
class TDocFile:
    """An auxiliary file attached to a :class:`doc3gpp.models.tdoc.TDoc`.

    Attributes:
        id: Database-assigned primary key. ``None`` for instances that have
            not been persisted yet (mirrors the ``WiORM`` pattern of
            allowing in-memory construction).
        tdoc_id: Foreign key into ``tdocs.tdoc_id``. The owning TDoc must
            already be persisted before a :class:`TDocFile` is upserted so
            the FK constraint is satisfied.
        type: One of ``"revision"``, ``"review"``, or ``"support"`` (see
            module-level constants). Stored as a short text column with a
            length-32 budget; the full set of valid values is fixed so the
            column does not need a CHECK constraint.
        file: Bare filename of the attachment, e.g.
            ``R5s260001_MCC160Comments.zip``. Stored without any directory
            prefix so the same file can be discovered across different
            meeting layouts.
        url: Fully-qualified download URL on ``https://www.3gpp.org/ftp/``.
            Unique across the table; the repository uses it as the
            idempotency key for upserts.
        uploaded_date: Date the attachment was uploaded to the 3GPP FTP,
            parsed from the directory listing's ``Last Modified`` column
            (``YYYY/MM/DD HH:MM``). ``None`` when the upstream listing
            omits a date or the parser cannot decode it. Mirrors the
            ``uploaded_date`` field on :class:`doc3gpp.models.tdoc.TDoc`
            so cross-table joins do not require type coercion.
        updated_at: Timestamp of the most recent upsert. ``None`` until the
            row is persisted.
    """

    tdoc_id: str
    type: str
    file: str
    url: str
    id: int | None = None
    uploaded_date: date | None = None
    updated_at: datetime | None = None

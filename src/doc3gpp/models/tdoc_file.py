"""Domain model for auxiliary files attached to a 3GPP TDoc.

A ``TDocFile`` is an auxiliary file in a meeting FTP directory. Files under
Inbox, Docs and Tdoc are classified as ``draft`` when their names start with
``draft``, otherwise as ``revision``. Files under Review are classified as
``review`` when their names contain ``MCC160Comment``, otherwise as ``support``.

The base TDoc itself (``{tdoc_id}.zip``) is **not** modelled here — it lives
on :class:`doc3gpp.models.tdoc.TDoc` as the ``ftp_url`` field.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# Allowed values for :attr:`TDocFile.type`, shared by application callers.
TDocFileTypeRevision = "revision"
TDocFileTypeDraft = "draft"
TDocFileTypeReview = "review"
TDocFileTypeSupport = "support"
TDocFileTypes: frozenset[str] = frozenset(
    {TDocFileTypeRevision, TDocFileTypeDraft, TDocFileTypeReview, TDocFileTypeSupport}
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
        type: One of ``"revision"``, ``"draft"``, ``"review"``, or ``"support"`` (see
            module-level constants). Stored as a short text column with a
            length-32 budget; the full set of valid values is fixed so the
            column does not need a CHECK constraint.
        file: Bare filename of the attachment, e.g.
            ``R5s260001_MCC160Comments.zip``. Stored without any directory
            prefix so the same file can be discovered across different
            meeting layouts.
        ftp_url: Relative download URL on ``https://www.3gpp.org/ftp/``,
            stored as a path relative to the FTP root. Unique across the
            table; the repository uses it as the idempotency key for
            upserts.
        uploaded_date: Date the attachment was uploaded to the 3GPP FTP,
            parsed from the directory listing's ``Last Modified`` column
            (``YYYY/MM/DD HH:MM``). ``None`` when the upstream listing
            omits a date or the parser cannot decode it. Mirrors the
            ``uploaded_date`` field on :class:`doc3gpp.models.tdoc.TDoc`
            so cross-table joins do not require type coercion.
    """

    tdoc_id: str
    type: str
    file: str
    ftp_url: str
    id: int | None = None
    uploaded_date: date | None = None

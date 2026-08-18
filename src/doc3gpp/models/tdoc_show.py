"""Bundled TDoc-show DTOs shared by the CLI and the HTTP web surface.

The CLI's ``tdoc show`` and the web ``GET /tdocs/{id}`` route compose
the same record from the same six repository reads:

* ``tdocs`` — the parent TDoc row (by ``tdoc_id``).
* ``tdoc_cr_cover_page`` — the slim cover-page sidecar, keyed by the
  parent TDoc's ``ftp_url``.
* ``tdoc_cr_ttcn_details`` — the optional TTCN sidecar, keyed by the
  parent TDoc's ``ftp_url`` (only populated for TTCN-shape ids).
* ``tdoc_cr_ls_details`` — the optional LS header sidecar, keyed by
  the parent TDoc's ``ftp_url``.
* ``tdoc_extracts`` — the cache-extract metadata sidecar, keyed by the
  parent TDoc's ``ftp_url``. The web / CLI renderers lift the
  ``extracted_at`` timestamp out of this row.
* ``tdoc_files`` — auxiliary revisions / reviews / support files,
  keyed by ``tdoc_id``.

:class:`TDocShowRecord` mirrors that composition by ``tdoc_id``;
:class:`TDocShowRecordByUrl` mirrors the same six reads but anchors
on ``ftp_url`` instead (used by ``tdoc show --ftp-url``). Both move
out of :mod:`doc3gpp.cli` into the models layer so the web route can
share the composition and the JSON envelope stays byte-identical
between ``doc3gpp tdoc show --format json`` and
``GET /tdocs/{id}?format=json``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from doc3gpp.models.tdoc import TDoc
from doc3gpp.models.tdoc_cr import (
    TDocCRDetails,
    TDocCRTTCNDetails,
)
from doc3gpp.models.tdoc_cr_change_details import TDocCRChangeDetails
from doc3gpp.models.tdoc_file import TDocFile
from doc3gpp.models.tdoc_ls import TDocLSDetails
from doc3gpp.parsers.cr.header import is_ttcn_tdoc


@dataclass(slots=True, frozen=True)
class TDocShowRecord:
    """Bundled output of ``tdoc show`` for the JSON / markdown / table renderers.

    Carries the parent :class:`TDoc`, the optional cover-page row keyed
    by the stored ``tdoc.ftp_url`` (the slim ``TDocCRDetails`` shape),
    the optional TTCN sidecar (only populated for TTCN CRs), the
    optional body-derived change-details sidecar, the extract
    ``extracted_at`` timestamp derived from ``tdoc_extracts``, and
    every auxiliary ``tdoc_files`` row whose ``tdoc_id`` matches.
    Keys are omitted (not null) in renderers when the corresponding
    value is absent.

    Attributes:
        tdoc: The resolved parent TDoc row from the ``tdocs`` table.
        cover: Slim cover-page fields keyed by ``tdoc.ftp_url``;
            ``None`` when no extract row exists for that URL.
        ttcn: TTCN sidecar keyed by ``tdoc.ftp_url``; ``None`` when no
            sidecar row exists for that URL or when the TDoc is not
            a TTCN CR.
        changes: Body-derived change-details sidecar for non-TTCN CRs;
            ``None`` for TTCN CRs or when no row exists for the parent.
        ls: LS header sidecar keyed by ``tdoc.ftp_url``; ``None`` when
            no LS row exists for that URL.
        extracted_at: Cache-extract timestamp for ``tdoc.ftp_url``;
            ``None`` when no ``tdoc_extracts`` row exists for that URL.
        files: Auxiliary ``tdoc_files`` rows matching ``tdoc_id``;
            empty tuple when the parent TDoc has no auxiliary files.
    """

    tdoc: TDoc
    cover: TDocCRDetails | None = None
    ttcn: TDocCRTTCNDetails | None = None
    changes: TDocCRChangeDetails | None = None
    ls: TDocLSDetails | None = None
    extracted_at: datetime | None = None
    files: tuple[TDocFile, ...] = ()

    @classmethod
    def from_tdoc_id(
        cls,
        tdoc_id: str,
        repos: "TDocShowRepos",
    ) -> "TDocShowRecord":
        """Compose a :class:`TDocShowRecord` by resolving ``tdoc_id`` across all 6 repos.

        Mirrors the CLI's :func:`doc3gpp.cli._tdoc_show_command`
        composition: ``tdocs`` PK lookup → cover sidecar by ``ftp_url``
        → extract-metadata ``extracted_at`` by ``ftp_url`` → optional
        TTCN sidecar by ``ftp_url`` (structural gate via
        :func:`is_ttcn_tdoc`) → optional body-change sidecar by
        ``tdoc_id`` (first row only) → optional LS sidecar by
        ``ftp_url`` → auxiliary ``tdoc_files`` rows by ``tdoc_id``.
        Raises :class:`TDocNotFoundError` when no matching ``tdocs``
        row exists.
        """
        from doc3gpp.services.tdoc_cr_service import TDocNotFoundError

        record = repos.tdoc.get_by_id(tdoc_id)
        if record is None:
            raise TDocNotFoundError(
                f"TDoc '{tdoc_id}' is not stored. Run 'doc3gpp tdoc list' to "
                "see stored TDocs, or 'doc3gpp tdoc sync' to ingest a "
                "meeting's TDocs first."
            )

        cover: TDocCRDetails | None = None
        extracted_at: datetime | None = None
        ttcn: TDocCRTTCNDetails | None = None
        changes: TDocCRChangeDetails | None = None
        ls: TDocLSDetails | None = None
        if record.ftp_url:
            cover = repos.cr.get_by_url(record.ftp_url)
            meta = repos.cr.get_extract_meta_by_url(record.ftp_url)
            if meta is not None:
                extracted_at = meta.extracted_at
            if is_ttcn_tdoc(record.tdoc_id):
                ttcn = repos.cr_ttcn.get_by_url(record.ftp_url)
            change_details = repos.cr_change_details.get_for_tdoc_id(record.tdoc_id)
            changes = change_details[0] if change_details else None
            ls = repos.ls.get_by_url(record.ftp_url)

        files = tuple(repos.file.get_for_tdoc_id(record.tdoc_id))
        return cls(
            tdoc=record,
            cover=cover,
            ttcn=ttcn,
            changes=changes,
            ls=ls,
            extracted_at=extracted_at,
            files=files,
        )


@dataclass(slots=True, frozen=True)
class TDocShowRecordByUrl:
    """Bundled output of ``tdoc show --ftp-url`` for the JSON / markdown / table renderers.

    Mirrors :class:`TDocShowRecord` but anchors on the URL rather than
    on a parent ``TDoc``. The 1:1 invariant between ``ftp_url`` and
    ``tdoc_id`` (enforced by the upload pipeline) means the parent
    ``TDoc`` is optional from the caller's perspective — a URL may
    surface a cover row, TTCN sidecar, extract meta, or auxiliary
    files without a matching ``tdocs`` row.

    Attributes:
        ftp_url: The normalised URL the user supplied.
        tdoc: The unique TDoc whose ``ftp_url`` matches (1:1 invariant);
            ``None`` when no ``tdocs`` row exists for the URL.
        cover: Slim cover-page fields keyed by ``ftp_url``;
            ``None`` when no extract row exists.
        ttcn: TTCN sidecar keyed by ``ftp_url``; ``None`` when no
            sidecar row exists.
        changes: Body-change sidecar keyed by ``ftp_url``;
            ``None`` when no row exists.
        ls: LS header sidecar keyed by ``ftp_url``; ``None`` when no
            LS row exists for that URL.
        extracted_at: Cache-extract timestamp for ``ftp_url``;
            ``None`` when no ``tdoc_extracts`` row exists.
        files: Auxiliary ``tdoc_files`` rows matching ``ftp_url``;
            empty tuple when no auxiliary file is attached.
    """

    ftp_url: str
    tdoc: TDoc | None = None
    cover: TDocCRDetails | None = None
    ttcn: TDocCRTTCNDetails | None = None
    changes: TDocCRChangeDetails | None = None
    ls: TDocLSDetails | None = None
    extracted_at: datetime | None = None
    files: tuple[TDocFile, ...] = ()

    @classmethod
    def from_ftp_url(
        cls,
        ftp_url: str,
        repos: "TDocShowRepos",
    ) -> "TDocShowRecordByUrl":
        """Compose a :class:`TDocShowRecordByUrl` by URL across all 6 repos.

        Mirrors the CLI's :func:`doc3gpp.cli._tdoc_show_by_ftp_url`
        composition: ``tdocs`` URL lookup → cover sidecar by URL →
        extract-metadata ``extracted_at`` by URL → TTCN sidecar by URL
        (only when a cover row exists, since the cover parser is what
        produces it) → body-change sidecar by URL → LS sidecar by URL
        → auxiliary ``tdoc_files`` rows by URL. Returns ``None``-free
        fields when no rows match anywhere.
        """
        tdoc = repos.tdoc.get_by_ftp_url(ftp_url)
        cover = repos.cr.get_by_url(ftp_url)
        meta = repos.cr.get_extract_meta_by_url(ftp_url)
        extracted_at = meta.extracted_at if meta is not None else None
        # TTCN sidecar can only exist when the URL has a cover row
        # (the cover parser is what produces it), so gate the lookup.
        ttcn = repos.cr_ttcn.get_by_url(ftp_url) if cover is not None else None
        changes = repos.cr_change_details.get_by_url(ftp_url)
        ls = repos.ls.get_by_url(ftp_url)
        files = tuple(repos.file.get_by_ftp_url(ftp_url))
        return cls(
            ftp_url=ftp_url,
            tdoc=tdoc,
            cover=cover,
            ttcn=ttcn,
            changes=changes,
            ls=ls,
            extracted_at=extracted_at,
            files=files,
        )


@dataclass(slots=True, frozen=True)
class TDocShowRepos:
    """Bag of repository handles consumed by the :class:`TDocShowRecord` classmethods.

    Captures the six repositories the show-composition reads from so
    the CLI and the web route can hand a single object to the
    classmethod instead of repeating six ``build_*`` calls at every
    call site. Tests can construct an instance with fake repositories
    directly to assert the composition round-trip.
    """

    tdoc: "object"  # TDocRepository
    cr: "object"  # TDocCrDetailRepository
    cr_ttcn: "object"  # TDocCrTTCNDetailRepository
    cr_change_details: "object"  # TDocCrChangeDetailsRepository
    file: "object"  # TDocFileRepository
    ls: "object"  # LSParserRepository


__all__ = [
    "TDocShowRecord",
    "TDocShowRecordByUrl",
    "TDocShowRepos",
]
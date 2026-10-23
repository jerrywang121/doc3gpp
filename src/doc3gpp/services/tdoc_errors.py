"""TDoc errors shared by the CR and family extraction services."""

from __future__ import annotations


class TDocNotYetOnFTPError(ValueError):
    """The row is stored but its ``ftp_url`` is still NULL.

    Surfaced by :meth:`TDocCrService.extract` when
    :attr:`doc3gpp.models.tdoc.TDoc.ftp_url` is ``None`` — meaning
    the 3GPP FTP pipeline has not propagated the upload yet, so there
    is nothing to download. Distinct from :class:`TDocNotFoundError`
    (the row exists) and :class:`TDocZipDownloadError` (we tried but
    failed); the upstream pipeline state is the actionable cause.
    """

    def __init__(self, tdoc_id: str) -> None:
        self.tdoc_id = tdoc_id
        super().__init__(
            f"TDoc {tdoc_id!r} has no ftp_url yet — the 3GPP upload "
            "pipeline has not propagated a final URL; try again later "
            "or run `doc3gpp tdoc sync` to refresh the tdocs table"
        )


class TDocTypeUnsupportedError(ValueError):
    """The given tdoc_id's ``type`` has no content store.

    Raised before any download. Inherits from :class:`ValueError` so callers
    that catch the broad class still handle it. The CLI translates it into a
    friendly ``typer.BadParameter`` with the observed ``type`` value.
    """

    def __init__(self, tdoc_id: str, observed_type: str | None) -> None:
        self.tdoc_id = tdoc_id
        self.observed_type = observed_type
        super().__init__(
            f"TDoc {tdoc_id!r} has type {observed_type!r}; "
            "only CR, WID, LS, and discussion TDocs are parsed"
        )

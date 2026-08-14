from __future__ import annotations

import logging
from collections.abc import Callable

from doc3gpp.models.tdoc import TDocWithMeeting
from doc3gpp.repository.protocols import TDocRepository
from doc3gpp.scraping.portal_source import fetch_tdocs_from_portal

logger = logging.getLogger(__name__)


class TDocService:
    """Service methods for persisting and retrieving TDoc records."""

    def __init__(self, repository: TDocRepository) -> None:
        """Initialize the TDoc service with a repository backing the TDoc storage."""
        self._repository = repository

    def list_recent_with_meeting(
        self,
        limit: int = 20,
        offset: int = 0,
        tdoc_id: str | None = None,
        meeting_like: str | None = None,
        meeting_id: int | None = None,
        status: str | None = None,
        cr_cat: str | None = None,
        spec: str | None = None,
        wi: str | None = None,
        revision_of: str | None = None,
        revised_to: str | None = None,
        title: str | None = None,
        ftp_url: str | None = None,
        source: str | None = None,
        tdoc_type: str | None = None,
        uploaded_date: str | None = None,
        release: str | None = None,
        version: str | None = None,
        cr_num: str | None = None,
        cr_pack: str | None = None,
    ) -> list[TDocWithMeeting]:
        """Return recent TDoc records joined with their parent meeting name.

        Convenience for CLI / export paths that want to show ``meeting_name``
        next to TDoc fields without exposing the DTO composition to callers.

        The filter parameters accept the rich grammar described in
        :mod:`doc3gpp.cli_filters` — ``null`` / ``not-null`` match the
        column's nullability, a leading ``!`` flips the comparison to
        ``NOT LIKE`` (with the ``!`` consumed), and anything else is
        treated as a ``LIKE`` pattern. ``uploaded_date`` additionally
        accepts a ``"<op> 'YYYY-MM-DD'"`` comparison. ``release``,
        ``version``, ``cr_num``, ``cr_pack`` are the text-column
        filters newly wired through for ``tdoc list`` and ``tdoc parse``
        and accept the same grammar.

        ``tdoc_id`` is the LIKE pattern against ``tdocs.tdoc_id`` and
        mirrors the ``--tdoc`` flag on ``tdoc parse`` / ``tdoc list``.

        ``offset`` is applied before ``limit`` to support CLI pagination.
        """
        return self._repository.list_with_meeting(
            limit=limit,
            offset=offset,
            tdoc_id=tdoc_id,
            meeting_like=meeting_like,
            meeting_id=meeting_id,
            status=status,
            cr_cat=cr_cat,
            spec=spec,
            wi=wi,
            revision_of=revision_of,
            revised_to=revised_to,
            title=title,
            ftp_url=ftp_url,
            source=source,
            tdoc_type=tdoc_type,
            uploaded_date=uploaded_date,
            release=release,
            version=version,
            cr_num=cr_num,
            cr_pack=cr_pack,
        )

    def sync_tdoc_list(
        self,
        meeting_id: int,
        url_template: str,
        on_progress: Callable[[str], None] | None = None,
    ) -> int:
        logger.info(
            "Syncing TDoc list for meeting_id %s from portal template %s",
            meeting_id,
            url_template,
        )
        tdocs = fetch_tdocs_from_portal(
            meeting_id=meeting_id, url_template=url_template
        )
        stored = self._repository.upsert_many(tdocs)
        if on_progress is not None:
            on_progress(f"tdoc list for meeting {meeting_id}: {stored} rows stored")
        logger.info("Stored %s TDoc records", stored)
        return stored
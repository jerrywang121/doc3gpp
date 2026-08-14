"""Service methods for syncing auxiliary TDoc files."""

from __future__ import annotations

import logging
from collections.abc import Callable
from collections.abc import Iterable

from doc3gpp.models.tdoc_file import TDocFile
from doc3gpp.repository.protocols import TDocFileRepository
from doc3gpp.scraping.ftp_source import fetch_tdoc_files_from_meeting_ftp

logger = logging.getLogger(__name__)


class TDocFileService:
    """Orchestrates scraping and persistence of auxiliary TDoc files.

    Mirrors the additive sync model of :class:`TDocService`: each call
    upserts every file found in the meeting FTP subfolders. Files that
    disappear upstream are left in place; the unique ``url`` constraint
    keeps re-syncs idempotent and the operator can prune stale rows out
    of band.
    """

    def __init__(self, repository: TDocFileRepository) -> None:
        self._repository = repository

    def sync_from_meeting_ftp(
        self,
        ftp_url: str,
        tdoc_ids: Iterable[str] | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> int:
        """Fetch auxiliary TDoc files from the meeting FTP and persist them.

        Args:
            ftp_url: The meeting's stored FTP URL.
            tdoc_ids: Iterable of TDoc IDs to scope the scan. Files that
                do not start with any of these IDs are dropped by the
                parser. ``None`` or an empty iterable is a no-op (the
                parser cannot recognise files without a known prefix to
                match against).

        Returns:
            The number of TDocFile rows written (insert or update).
        """
        ids = [tid for tid in (tdoc_ids or []) if tid]
        if not ids:
            logger.info(
                "No TDoc IDs supplied for %s; skipping TDoc file sync", ftp_url
            )
            return 0

        logger.info(
            "Syncing TDoc files from FTP %s for %s TDoc ID(s)",
            ftp_url,
            len(ids),
        )
        files = fetch_tdoc_files_from_meeting_ftp(ftp_url, ids)
        if not files:
            logger.info("No auxiliary TDoc files found under %s", ftp_url)
            return 0
        written = self._repository.upsert_many(files)
        if on_progress is not None:
            on_progress(f"aux files for meeting: {written} stored")
        logger.info("Stored %s TDocFile records", written)
        return written

    def list_recent(
        self,
        limit: int = 20,
        tdoc_id: str | None = None,
        file_type: str | None = None,
        file_type_in: Iterable[str] | None = None,
    ) -> list[TDocFile]:
        """Return recently updated TDocFile records."""
        return self._repository.list(
            limit=limit,
            tdoc_id=tdoc_id,
            file_type=file_type,
            file_type_in=file_type_in,
        )

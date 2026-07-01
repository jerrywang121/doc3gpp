"""Service layer for 3GPP Work Item (WI) records."""

from __future__ import annotations

import logging

from doc3gpp.models.wi import Wi
from doc3gpp.parsers.wi_parser import parse_3gpp_wis
from doc3gpp.repository.protocols import WiRepository
from doc3gpp.scraping.wi_source import fetch_wis

logger = logging.getLogger(__name__)


class WiService:
    """Service methods for syncing and querying 3GPP WI records."""

    def __init__(self, repository: WiRepository) -> None:
        """Initialize the service with a repository backing WI storage."""
        self._repository = repository

    def sync(self, tsg_short: str) -> int:
        """Fetch the WI list for a TSG and persist it.

        Args:
            tsg_short: Canonical TSG short name (e.g. ``R5``). Case-insensitive.

        Returns:
            The number of WI rows written (insert or update) for the TSG.
        """
        canonical = tsg_short.upper()
        logger.info("Syncing WIs for TSG %s", canonical)
        html = fetch_wis(canonical)
        wis = parse_3gpp_wis(html, tsg_short=canonical)
        stored = self._repository.upsert_many(wis)
        logger.info("Stored %s WI records for TSG %s", stored, canonical)
        return stored

    def list_recent(
        self,
        limit: int = 20,
        tsg: str | None = None,
        name_like: str | None = None,
        acronym_like: str | None = None,
        release_like: str | None = None,
    ) -> list[Wi]:
        """Return the most recently updated WI records matching the filters.

        Filters are passed through to the repository layer. ``tsg`` is
        uppercased so callers may pass ``r5`` or ``R5`` interchangeably.
        The remaining arguments are SQL ``LIKE`` patterns (``%`` and ``_``
        wildcards).
        """
        logger.debug(
            "Listing %s recent WIs with filters tsg=%s name_like=%s acronym_like=%s release_like=%s",
            limit,
            tsg,
            name_like,
            acronym_like,
            release_like,
        )
        normalized_tsg = tsg.upper() if tsg else None
        return self._repository.list(
            limit=limit,
            tsg=normalized_tsg,
            name_like=name_like,
            acronym_like=acronym_like,
            release_like=release_like,
        )

"""Service layer for 3GPP TSG (Technical Specification Group) reference data.

Holds the canonical TSG list and provides seeding, validation, and query
operations against the configured :class:`TsgRepository`.
"""

from __future__ import annotations

import logging

from doc3gpp.models.tsg import Tsg
from doc3gpp.models.tsg_defaults import build_tsg_url, default_tsgs
from doc3gpp.repository.protocols import TsgRepository

logger = logging.getLogger(__name__)


class TsgService:
    """Service for seeding and querying 3GPP TSG reference records."""

    def __init__(self, repository: TsgRepository) -> None:
        """Initialize the service with a repository backing the TSG storage."""
        self._repository = repository

    def list_all(self) -> list[Tsg]:
        """Return all stored TSG records ordered by ``tsg_name``."""
        return self._repository.list_all()

    def count(self) -> int:
        """Return the number of stored TSG records.

        Used by the CLI to decide whether to auto-seed on a fresh install
        without paying for a full ``list_all()`` round-trip.
        """
        return self._repository.count()

    def get_by_short_name(self, short_name: str) -> Tsg | None:
        """Return a stored TSG by its short name (case-insensitive)."""
        return self._repository.get_by_short_name(short_name)

    def get_by_tsg_name(self, tsg_name: str) -> Tsg | None:
        """Return a stored TSG by its full ``tsg_name`` (case-insensitive)."""
        return self._repository.get_by_tsg_name(tsg_name)

    def is_known_short_name(self, short_name: str) -> bool:
        """Return ``True`` iff ``short_name`` matches a stored TSG.

        The check is case-insensitive and treats an empty/blank input as
        unknown so callers can short-circuit validation.
        """
        if not short_name or not short_name.strip():
            return False
        return self._repository.get_by_short_name(short_name) is not None

    def known_short_names(self) -> list[str]:
        """Return the canonical uppercase short names of all stored TSGs."""
        return [tsg.short_name for tsg in self._repository.list_all()]

    def seed_defaults(self) -> int:
        """Insert or refresh the canonical 3GPP TSG list.

        Explicit seeding refreshes existing reference metadata. URL defaults
        come from the canonical model definitions; the closed RAN WG6 record
        deliberately remains without a URL.
        """
        seed_rows = list(default_tsgs())
        logger.info("Seeding %s default TSG records", len(seed_rows))
        return self._repository.upsert_many(seed_rows)


__all__ = ["TsgService", "build_tsg_url"]

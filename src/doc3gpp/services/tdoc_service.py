from __future__ import annotations

from doc3gpp.models.tdoc import TDoc
from doc3gpp.repository.protocols import TDocRepository


class TDocService:
    """Service methods for persisting and retrieving TDoc records."""

    def __init__(self, repository: TDocRepository) -> None:
        self._repository = repository

    def save(self, tdoc: TDoc) -> None:
        self._repository.upsert(tdoc)

    def list_recent(self, limit: int = 20) -> list[TDoc]:
        return self._repository.list(limit=limit)

"""SQL-only read service for stored ETSI LISTSERV lists and message records."""

from __future__ import annotations

from dataclasses import replace

from doc3gpp.models.listserv import ListservList, ListservRecord, normalize_tdoc_ids
from doc3gpp.repository.listserv import ListservRepository


class ListservMessageNotFoundError(LookupError):
    """Raised when no stored LISTSERV message exists for the requested detail URL."""

    def __init__(self, url: str) -> None:
        self.url = url
        super().__init__(f"LISTSERV message not found: {url}")


class ListservService:
    """SQL-only read service for stored LISTSERV catalog entries and messages."""

    def __init__(self, repository: ListservRepository) -> None:
        self._repository = repository

    def list_lists(
        self, *, list_id: str | None = None, tsg: str | None = None
    ) -> list[ListservList]:
        """Return filtered catalog entries ordered by list ID without network access."""
        return self._repository.list_lists(list_id=list_id, tsg=tsg)

    def list_ids(self) -> list[str]:
        """Return catalog list IDs in ascending order without network access."""
        return self._repository.list_ids()

    def show(self, url: str) -> ListservRecord:
        """Return the stored :class:`ListservRecord` for ``url`` without network access.

        Raises :class:`ListservMessageNotFoundError` when no stored message exists.
        """
        record = self._repository.get(url)
        if record is None:
            raise ListservMessageNotFoundError(url)
        if record.detail is None:
            unioned = normalize_tdoc_ids(record.message.subject_tdocs)
        else:
            unioned = normalize_tdoc_ids(
                (*record.message.subject_tdocs, *record.detail.tdocs)
            )
        if record.tdocs != unioned:
            return replace(record, tdocs=unioned)
        return record


__all__ = [
    "ListservMessageNotFoundError",
    "ListservService",
]

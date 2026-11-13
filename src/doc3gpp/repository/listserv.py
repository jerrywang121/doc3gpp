"""Repository protocol for ETSI LISTSERV archives."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Protocol

from doc3gpp.models.listserv import (
    ArchivePage,
    DateSet,
    ListservDetail,
    ListservFilters,
    ListservList,
    ListservMessage,
    ListservRecord,
)


class ListservRepository(Protocol):
    """Storage operations for the LISTSERV catalog, messages, details, and pages."""

    def commit_page(
        self,
        page: ArchivePage,
        messages: Sequence[ListservMessage],
        coverage: DateSet,
        next_urls: Sequence[str],
    ) -> None:
        """Atomically upsert page messages, union coverage, and persist pagination URLs."""
        ...

    def get_coverage(self, page: ArchivePage) -> DateSet:
        """Return the stored date coverage for ``page`` (empty set when unknown)."""
        ...

    def get_next_urls(self, page: ArchivePage) -> tuple[str, ...]:
        """Return discovered pagination URLs stored for ``page``."""
        ...

    def save_detail(self, detail: ListservDetail) -> None:
        """Atomically persist parsed message detail and bump ``source_revision`` on change."""
        ...

    def get(self, url: str) -> ListservRecord | None:
        """Return the stored message record for canonical detail ``url``, or ``None``."""
        ...

    def get_many(self, urls: Sequence[str]) -> dict[str, ListservRecord]:
        """Return stored message records keyed by canonical detail URL for ``urls``."""
        ...

    def has_list(self, list_id: str) -> bool:
        """Return whether ``list_id`` exists in the LISTSERV catalog."""
        ...

    def list_lists(self) -> list[ListservList]:
        """Return catalog entries ordered by list ID."""
        ...

    def list_ids(self) -> list[str]:
        """Return catalog list IDs in ascending order."""
        ...

    def select(self, filters: ListservFilters) -> list[ListservRecord]:
        """Return matching stored records ordered newest first with URL tie-break."""
        ...

    def iter_for_parse(
        self,
        list_id: str,
        dates: DateSet | None,
        force: bool,
    ) -> Iterator[ListservRecord]:
        """Iterate all matching records for ``list_id`` (independent of search pagination)."""
        ...


__all__ = ["ListservRepository"]

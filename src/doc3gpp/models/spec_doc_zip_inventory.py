"""Domain values for spec-document outer-ZIP inventories."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class SpecDocZipInventory:
    """Ordered outer-ZIP member names for one spec and version."""

    spec_id: str
    version: str
    release: str | None
    zip_files: tuple[str, ...]


class SpecDocInventorySchemaError(RuntimeError):
    """The specdata database needs the inventory table upgrade."""

"""Prepared ZIP inventory results for database-anchored TDoc parsing."""

from __future__ import annotations

from dataclasses import dataclass

from doc3gpp.models.tdoc_zip_inventory import TDocZipInventory


@dataclass(frozen=True, slots=True)
class PreparedInventoryFailure:
    """An enumerated source inventory paired with the original parse error."""

    inventory: TDocZipInventory
    error: Exception


__all__ = ["PreparedInventoryFailure"]

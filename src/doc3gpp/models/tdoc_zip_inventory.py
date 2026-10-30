"""Domain value object for the outer-ZIP file inventory of a TDoc."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class TDocZipInventory:
    """Ordered names from a known TDoc ZIP, keyed by its download URL."""

    ftp_url: str
    tdoc_id: str
    zip_files: tuple[str, ...]

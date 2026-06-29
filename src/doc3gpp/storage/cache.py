from __future__ import annotations

from pathlib import Path


class FileCache:
    """Simple file-based cache for fetched documents."""

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir.expanduser()
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, key: str) -> Path:
        safe_key = key.replace("/", "_")
        return self.cache_dir / f"{safe_key}.html"

"""Shared services-package helpers.

Private to :mod:`doc3gpp.services` — the underscore prefix signals
"implementation detail, not part of the public API surface". Importing
modules use ``from doc3gpp.services._duration import format_duration``.
"""
from __future__ import annotations

from datetime import timedelta


def format_duration(delta: timedelta) -> str:
    """Return a concise human-readable representation of a ``timedelta``.

    Examples: ``"42s"``, ``"5m"``, ``"2h 30m"``, ``"1d 6h"``, ``"3d"``.
    """
    total_seconds = int(delta.total_seconds())
    if total_seconds < 60:
        return f"{total_seconds}s"
    if total_seconds < 3600:
        return f"{total_seconds // 60}m"
    if total_seconds < 86400:
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        if minutes:
            return f"{hours}h {minutes}m"
        return f"{hours}h"
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    if hours:
        return f"{days}d {hours}h"
    return f"{days}d"

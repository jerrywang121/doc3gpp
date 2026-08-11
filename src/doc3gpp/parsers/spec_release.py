"""Release-marker normalisation for spec headers and version rows.

Upstream uses two shapes (``Release 20`` and ``R99``); this module
provides a single canonical form so the CLI / web / MCP surfaces do
not special-case the upstream shape.
"""

from __future__ import annotations

import re

_PRE_RELEASE_MAJORS = {"1", "2", "3"}


def normalise_release(text: str) -> str:
    """Return the canonical release marker.

    - ``"Release 20"`` → ``"Rel-20"``
    - ``"Release 9"``  → ``"Rel-9"``
    - ``"Release 1999"`` → ``"R99"`` (special pre-Rel-4 marker)
    - ``"R99"``        → ``"R99"`` (passed through; pre-Rel-4 marker)
    - ``"draft"`` / ``"pre-release"`` / already-canonical values pass through.
    - Empty / whitespace → empty string.
    """
    stripped = text.strip()
    if not stripped:
        return ""
    if stripped == "R99" or stripped in ("draft", "pre-release"):
        return stripped
    match = re.fullmatch(r"Release\s+(\d+)", stripped, flags=re.IGNORECASE)
    if match:
        return "R99" if match.group(1) == "1999" else f"Rel-{match.group(1)}"
    return stripped


def release_from_version(version: str) -> str:
    """Derive the canonical release marker from a version string.

    - ``0.x.y`` → ``draft``
    - ``1.x.y`` / ``2.x.y`` / ``3.x.y`` → ``pre-release``
    - else → ``Rel-{major}``
    """
    major = version.split(".")[0] if version else ""
    if major == "0":
        return "draft"
    if major in _PRE_RELEASE_MAJORS:
        return "pre-release"
    if major.isdigit():
        return f"Rel-{major}"
    return ""

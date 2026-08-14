"""Jinja2 templates + static-files mount for the web surface.

A single shared :class:`fastapi.templating.Jinja2Templates` instance is
configured against the ``web/templates/`` directory and reused by every
read router. The instance carries ``url_for`` as a global so templates
can resolve static asset paths (``/static/htmx.min.js``,
``/static/style.css``) without a custom request context.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from doc3gpp import __version__ as _APP_VERSION


_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"


templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _url_for(request: Request, name: str) -> str:
    """Forward ``request.url_for(name)`` for templates that need it.

    Falls back to an empty string when the route is not registered so
    partials (e.g. ``job_status``) can still render during tests where
    some routes are overridden.
    """
    try:
        return str(request.url_for(name))
    except Exception:
        return ""


templates.env.globals["url_for"] = _url_for
templates.env.globals["app_version"] = _APP_VERSION


def dt_short(value: datetime | None) -> str | None:
    """Format a (UTC) datetime as ``YYYY-MM-DD HH:MM``; ``None`` stays ``None``."""
    if value is None:
        return None
    return value.strftime("%Y-%m-%d %H:%M")


def sync_state(value: datetime | None) -> str:
    """Classify sync freshness: ``never`` / ``fresh`` (<=24h) / ``stale`` (>24h)."""
    if value is None:
        return "never"
    if value >= datetime.now(timezone.utc) - timedelta(hours=24):
        return "fresh"
    return "stale"


# Status colour rules for the tdoc list page: ordered, case-insensitive
# substring matches; the first matching entry wins (spec 2026-08-04).
_STATUS_COLOR_RULES: list[tuple[str, str]] = [
    ("conditionally", "status-lgreen"),
    ("partially", "status-lgreen"),
    ("agreed", "status-green"),
    ("approved", "status-green"),
    ("revised", "status-vanilla"),
    ("reissued", "status-vanilla"),
    ("merged", "status-vanilla"),
    ("rejected", "status-red"),
    ("withdrawn", "status-grey"),
    ("postponed", "status-pink"),
    ("noted", "status-lblue"),
    ("treated", "status-lblue"),
    ("endorsed", "status-lblue"),
]


def status_color_class(value: str | None) -> str:
    """Map a tdoc status string to a pastel row-background CSS class.

    Matching is case-insensitive substring; the first rule whose needle
    appears wins. ``None`` / empty / unmatched values yield ``""`` so
    the row renders with no background.
    """
    if not value:
        return ""
    lowered = value.lower()
    for needle, cls in _STATUS_COLOR_RULES:
        if needle in lowered:
            return cls
    return ""


def truncate_csv(value: str | None, max_chars: int = 32) -> str:
    """Truncate a comma-joined string to ``max_chars``, breaking at the
    nearest comma delimiter and appending ``...`` when truncated.

    ``None`` / empty values pass through unchanged. The break happens at
    the last comma whose position is ``<= max_chars`` so a truncated
    cell never ends mid-token.
    """
    if not value:
        return value or ""
    if len(value) <= max_chars:
        return value
    cut = value.rfind(",", 0, max_chars)
    if cut == -1:
        cut = max_chars
    return value[:cut] + "..."


def wrap_csv(value: str | None, max_chars: int = 150) -> str:
    """Wrap a comma-joined string into newline-separated lines, each at
    most ``max_chars`` long, breaking at the nearest comma delimiter.

    ``None`` / empty values pass through unchanged. A single token
    longer than ``max_chars`` is hard-broken at ``max_chars``.
    """
    if not value:
        return value or ""
    lines: list[str] = []
    remaining = value
    while len(remaining) > max_chars:
        cut = remaining.rfind(",", 0, max_chars)
        if cut == -1:
            cut = max_chars
        lines.append(remaining[:cut])
        remaining = remaining[cut:].lstrip(",")
    lines.append(remaining)
    return "\n".join(lines)


templates.env.filters["dt_short"] = dt_short
templates.env.filters["sync_state"] = sync_state
templates.env.filters["truncate_csv"] = truncate_csv
templates.env.filters["wrap_csv"] = wrap_csv
templates.env.globals["status_color_class"] = status_color_class


static_files = StaticFiles(directory=str(_STATIC_DIR))


def mount_static(app) -> None:
    """Mount the vendored static assets (HTMX + CSS) at ``/static``.

    Kept as a small helper so :func:`doc3gpp.web.app.build_app` can
    call it without knowing the on-disk layout. Future tasks may add
    cache-control headers here; T6 keeps the mount minimal.
    """
    app.mount("/static", static_files, name="static")


__all__ = ["templates", "static_files", "mount_static"]
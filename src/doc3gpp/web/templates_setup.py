"""Jinja2 templates + static-files mount for the web surface.

A single shared :class:`fastapi.templating.Jinja2Templates` instance is
configured against the ``web/templates/`` directory and reused by every
read router. The instance carries ``url_for`` as a global so templates
can resolve static asset paths (``/static/htmx.min.js``,
``/static/style.css``) without a custom request context.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates


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


static_files = StaticFiles(directory=str(_STATIC_DIR))


def mount_static(app) -> None:
    """Mount the vendored static assets (HTMX + CSS) at ``/static``.

    Kept as a small helper so :func:`doc3gpp.web.app.build_app` can
    call it without knowing the on-disk layout. Future tasks may add
    cache-control headers here; T6 keeps the mount minimal.
    """
    app.mount("/static", static_files, name="static")


__all__ = ["templates", "static_files", "mount_static"]
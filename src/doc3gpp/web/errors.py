"""Domain exception catalogue and FastAPI error handlers.

The exception classes declared here are the canonical names imported by
the rest of the HTTP / MCP server surface (route handlers, dependency
helpers, and downstream services). T5 (``web/filters.py``) and T8
(``routes/jobs.py``) import from this module rather than redefining
local copies; the dataclass bodies live here permanently so the rest of
the web package sees a single source of truth.

Mapping table (``map_domain_error``):

* :class:`TDocNotFoundError` / :class:`MeetingNotFoundError` /
  :class:`TSGNotFoundError` / :class:`WINotFoundError` -> 404
* :class:`InvalidFilterError` -> 400
* :class:`JobNotFoundError` -> 404
* :class:`JobAlreadyTerminalError` -> 409
* :class:`SettingsDisabledError` -> 503
* :class:`httpx.HTTPError` (and subclasses) -> 502
* generic :class:`Exception` -> 500 (with ``request_id`` correlation id)

The mapping function is exposed standalone so the unit tests can call
it directly without going through a real FastAPI request lifecycle.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from doc3gpp.services.tdoc_cr_service import TDocNotFoundError
from doc3gpp.services.tdoc_sync_coordinator import MeetingNotFoundError

logger = logging.getLogger(__name__)


class TSGNotFoundError(LookupError):
    """Raised when a TSG short-name cannot be resolved."""


class WINotFoundError(LookupError):
    """Raised when a WI id cannot be resolved."""


class InvalidFilterError(ValueError):
    """Raised when a filter expression is malformed."""


class JobNotFoundError(LookupError):
    """Raised when a Job id cannot be resolved."""


class JobAlreadyTerminalError(RuntimeError):
    """Raised when an action is attempted on a job that has already reached a terminal state."""


class SettingsDisabledError(RuntimeError):
    """Raised when an HTTP route requires an opt-in feature that is disabled in settings."""


_ERROR_SLUGS: dict[type[Exception], str] = {
    TDocNotFoundError: "tdoc_not_found",
    MeetingNotFoundError: "meeting_not_found",
    TSGNotFoundError: "tsg_not_found",
    WINotFoundError: "wi_not_found",
    InvalidFilterError: "invalid_filter",
    JobNotFoundError: "job_not_found",
    JobAlreadyTerminalError: "job_already_terminal",
    SettingsDisabledError: "settings_disabled",
}


_STATUS_BY_EXC: dict[type[Exception], int] = {
    TDocNotFoundError: 404,
    MeetingNotFoundError: 404,
    TSGNotFoundError: 404,
    WINotFoundError: 404,
    InvalidFilterError: 400,
    JobNotFoundError: 404,
    JobAlreadyTerminalError: 409,
    SettingsDisabledError: 503,
    httpx.HTTPError: 502,
}


def _internal_error_response() -> JSONResponse:
    """Build the canonical 500 envelope with a fresh correlation id."""
    request_id = uuid.uuid4().hex
    logger.exception("unhandled exception request_id=%s", request_id)
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_error",
            "detail": "internal server error",
            "request_id": request_id,
        },
    )


def map_domain_error(exc: Exception) -> JSONResponse:
    """Map a domain exception to a JSONResponse with the canonical envelope.

    Body shape: ``{"error": "<slug>", "detail": "<message>"}`` for the
    named exceptions and the generic 500 path. The generic 500 path
    also includes a ``"request_id"`` correlation id so operators can
    grep server logs for the matching request.
    """
    if isinstance(exc, httpx.HTTPError):
        return JSONResponse(
            status_code=502,
            content={"error": "upstream_unavailable", "detail": str(exc)},
        )

    for exc_type, status in _STATUS_BY_EXC.items():
        if exc_type is Exception:
            continue
        if isinstance(exc, exc_type):
            slug = _ERROR_SLUGS[exc_type]
            return JSONResponse(
                status_code=status,
                content={"error": slug, "detail": str(exc)},
            )

    return _internal_error_response()


def _make_handler(exc_type: type[Exception]) -> Any:
    """Build a FastAPI exception handler bound to ``exc_type``."""

    async def _handler(request: Request, exc: Exception) -> JSONResponse:
        return map_domain_error(exc)

    return _handler


def register_error_handlers(app: FastAPI) -> None:
    """Register every mapped exception class + the generic ``Exception`` handler."""
    for exc_type in _STATUS_BY_EXC:
        app.add_exception_handler(exc_type, _make_handler(exc_type))
    app.add_exception_handler(Exception, _make_handler(Exception))


__all__ = [
    "TSGNotFoundError",
    "WINotFoundError",
    "InvalidFilterError",
    "JobNotFoundError",
    "JobAlreadyTerminalError",
    "SettingsDisabledError",
    "map_domain_error",
    "register_error_handlers",
]

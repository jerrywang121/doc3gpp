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
* :class:`InvalidFilterError` / :class:`SearchQueryError` -> 400
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

from doc3gpp.models.search import SearchQueryError
from doc3gpp.services.spec_service import (
    SpecUnknownOnUpstreamError,
    UnknownTsgError,
)
from doc3gpp.services.tdoc_cr_service import TDocNotFoundError
from doc3gpp.services.tdoc_sync_coordinator import MeetingNotFoundError

logger = logging.getLogger(__name__)


class TSGNotFoundError(LookupError):
    """Raised when a TSG short-name cannot be resolved."""


class WINotFoundError(LookupError):
    """Raised when a WI id cannot be resolved."""


class SpecNotFoundError(LookupError):
    """Raised when a spec id cannot be resolved."""


class InvalidFilterError(ValueError):
    """Raised when a filter expression is malformed."""


class JobNotFoundError(LookupError):
    """Raised when a Job id cannot be resolved."""


class JobAlreadyTerminalError(RuntimeError):
    """Raised when an action is attempted on a job that has already reached a terminal state."""


class SettingsDisabledError(RuntimeError):
    """Raised when an HTTP route requires an opt-in feature that is disabled in settings."""


class CacheMissError(LookupError):
    """Raised when a cached artifact (e.g. markdown) is missing for a stored TDoc.

    Carries an optional ``hint`` telling the operator how to populate
    the cache; the hint is surfaced as the envelope's ``hint`` field
    (spec: ``{"error": "cache_miss", "hint": "run: ..."}``).
    """

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


# JSON-RPC error codes used on the MCP surface (see the design spec's
# "Error mapping" section). -32000..-32099 are MCP-application-defined;
# -32600..-32603 are the standard JSON-RPC codes.
MCP_CODE_NOT_FOUND = -32004
MCP_CODE_CACHE_MISS = -32005
MCP_CODE_TOO_LARGE = -32006
MCP_CODE_INVALID_PARAMS = -32602
MCP_CODE_INTERNAL_ERROR = -32603

# domain exception -> (resource slug, MCP code). The HTTP status is
# derived separately via ``_STATUS_BY_EXC``; keeping the two tables in
# lock-step is a deliberate invariant (the same exception class must map
# to one canonical resource on both transports).
_MCP_RESOURCE_BY_EXC: dict[type[Exception], tuple[str, int]] = {
    TDocNotFoundError: ("tdoc", MCP_CODE_NOT_FOUND),
    MeetingNotFoundError: ("meeting", MCP_CODE_NOT_FOUND),
    TSGNotFoundError: ("tsg", MCP_CODE_NOT_FOUND),
    WINotFoundError: ("wi", MCP_CODE_NOT_FOUND),
    SpecNotFoundError: ("spec", MCP_CODE_NOT_FOUND),
    SpecUnknownOnUpstreamError: ("spec", MCP_CODE_NOT_FOUND),
    UnknownTsgError: ("spec", MCP_CODE_INVALID_PARAMS),
    JobNotFoundError: ("job", MCP_CODE_NOT_FOUND),
    CacheMissError: ("tdoc_content", MCP_CODE_CACHE_MISS),
    InvalidFilterError: ("filter", MCP_CODE_INVALID_PARAMS),
    SearchQueryError: ("query", MCP_CODE_INVALID_PARAMS),
}


def map_mcp_error(exc: Exception) -> tuple[int, str, dict[str, Any]] | None:
    """Map a domain exception to an MCP JSON-RPC error ``(code, message, data)``.

    Returns ``None`` for exceptions with no MCP-specific mapping (they
    bubble up as the generic ``-32603`` internal error from the caller).
    The ``data`` payload mirrors the HTTP envelope: the canonical
    ``error`` slug plus the ``detail`` message, with a ``resource`` /
    ``hint`` where applicable so an AI client can correlate with a URL.
    """
    for exc_type, (resource, code) in _MCP_RESOURCE_BY_EXC.items():
        if isinstance(exc, exc_type):
            data: dict[str, Any] = {"error": _ERROR_SLUGS[exc_type], "detail": str(exc), "resource": resource}
            if isinstance(exc, CacheMissError):
                if exc.hint:
                    data["hint"] = exc.hint
            elif exc.args:
                data["id"] = exc.args[0]
            return (code, str(exc), data)
    if isinstance(exc, (JobAlreadyTerminalError, SettingsDisabledError, httpx.HTTPError)):
        return (MCP_CODE_INTERNAL_ERROR, str(exc), {"error": _ERROR_SLUGS.get(type(exc), "internal_error"), "detail": str(exc)})
    return None


_ERROR_SLUGS: dict[type[Exception], str] = {
    TDocNotFoundError: "tdoc_not_found",
    MeetingNotFoundError: "meeting_not_found",
    TSGNotFoundError: "tsg_not_found",
    WINotFoundError: "wi_not_found",
    SpecNotFoundError: "spec_not_found",
    SpecUnknownOnUpstreamError: "spec_unknown_on_upstream",
    UnknownTsgError: "unknown_tsg",
    InvalidFilterError: "invalid_filter",
    SearchQueryError: "invalid_query",
    JobNotFoundError: "job_not_found",
    JobAlreadyTerminalError: "job_already_terminal",
    SettingsDisabledError: "settings_disabled",
    CacheMissError: "cache_miss",
}


_STATUS_BY_EXC: dict[type[Exception], int] = {
    TDocNotFoundError: 404,
    MeetingNotFoundError: 404,
    TSGNotFoundError: 404,
    WINotFoundError: 404,
    SpecNotFoundError: 404,
    SpecUnknownOnUpstreamError: 404,
    UnknownTsgError: 400,
    InvalidFilterError: 400,
    SearchQueryError: 400,
    JobNotFoundError: 404,
    JobAlreadyTerminalError: 409,
    SettingsDisabledError: 503,
    CacheMissError: 404,
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
            body: dict[str, Any] = {"error": slug, "detail": str(exc)}
            if isinstance(exc, CacheMissError) and exc.hint:
                body["hint"] = exc.hint
            return JSONResponse(status_code=status, content=body)

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
    "CacheMissError",
    "TSGNotFoundError",
    "WINotFoundError",
    "SpecNotFoundError",
    "InvalidFilterError",
    "SearchQueryError",
    "JobNotFoundError",
    "JobAlreadyTerminalError",
    "SettingsDisabledError",
    "MCP_CODE_NOT_FOUND",
    "MCP_CODE_CACHE_MISS",
    "MCP_CODE_TOO_LARGE",
    "MCP_CODE_INVALID_PARAMS",
    "MCP_CODE_INTERNAL_ERROR",
    "map_mcp_error",
    "map_domain_error",
    "register_error_handlers",
]

"""FastAPI dependency helpers exposing :class:`WebState` collaborators.

Every helper is a zero-argument callable taking the inbound
``Request`` so FastAPI can wire it via ``Depends(...)``. The helpers
return the live object stored on ``request.app.state.web``; route
handlers should never reach into ``app.state`` directly.
"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.engine import Engine

from doc3gpp.models.jobs import JobStatus
from doc3gpp.repository.protocols import JobRepository
from doc3gpp.services.meetings_service import MeetingService
from doc3gpp.services.search_service import SearchService
from doc3gpp.services.semantic_search_service import SemanticSearchService
from doc3gpp.services.tdoc_cr_service import TDocCrService
from doc3gpp.services.tdoc_service import TDocService
from doc3gpp.services.tsg_service import TsgService
from doc3gpp.services.wi_service import WiService
from doc3gpp.settings.schema import Settings
from doc3gpp.storage.repositories.tdoc_file_sql import SQLAlchemyTDocFileRepository
from doc3gpp.web.state import JobWorkerHandle, ServiceContainer, WebState


def get_state(request: Request) -> WebState:
    """Return the per-app :class:`WebState`."""
    return request.app.state.web


def get_settings(request: Request) -> Settings:
    """Return the resolved :class:`Settings`."""
    return get_state(request).settings


def get_engine(request: Request) -> Engine:
    """Return the singleton SQLAlchemy :class:`Engine`."""
    return get_state(request).engine


def get_services(request: Request) -> ServiceContainer:
    """Return the wired :class:`ServiceContainer`."""
    return get_state(request).services


def get_meeting_service(request: Request) -> MeetingService:
    return get_services(request).meeting


def get_tdoc_service(request: Request) -> TDocService:
    return get_services(request).tdoc


def get_tdoc_cr_service(request: Request) -> TDocCrService:
    return get_services(request).tdoc_cr


def get_wi_service(request: Request) -> WiService:
    return get_services(request).wi


def get_tsg_service(request: Request) -> TsgService:
    """Return the wired :class:`TsgService` from the per-app container."""
    return get_services(request).tsg


def get_search_service(request: Request) -> SearchService | None:
    return get_services(request).search


def get_semantic_search_service(request: Request) -> SemanticSearchService | None:
    return get_services(request).semantic_search


def get_tdoc_file_repo(request: Request) -> SQLAlchemyTDocFileRepository:
    return get_services(request).tdoc_file_repo


def get_job_repo(request: Request) -> JobRepository:
    return get_services(request).job_repo


def get_pending_jobs(request: Request) -> int:
    """Return the number of queued background jobs (for the nav badge).

    Counts ``QUEUED`` rows via the :class:`JobRepository` protocol's
    ``list(status=...)``; a missing ``jobs`` table (fresh database
    before schema bootstrap) degrades to ``0`` instead of 500-ing the
    page chrome.
    """
    try:
        return len(get_job_repo(request).list(status=JobStatus.QUEUED))
    except Exception:
        return 0


def get_job_worker(request: Request) -> JobWorkerHandle:
    """Return the (placeholder) :class:`JobWorkerHandle` for the running app."""
    return get_state(request).jobs


__all__ = [
    "get_engine",
    "get_job_repo",
    "get_job_worker",
    "get_meeting_service",
    "get_pending_jobs",
    "get_search_service",
    "get_semantic_search_service",
    "get_services",
    "get_settings",
    "get_state",
    "get_tdoc_cr_service",
    "get_tdoc_file_repo",
    "get_tdoc_service",
    "get_tsg_service",
    "get_wi_service",
]

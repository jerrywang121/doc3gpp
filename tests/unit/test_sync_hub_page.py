"""Tests for the ``GET /sync`` hub page + ``?format=fragment`` table refresh."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from doc3gpp.models.jobs import Job, JobKind, JobStatus
from doc3gpp.web.deps import get_job_repo
from tests.unit.test_web_routes import app_with_fakes  # noqa: F401  (reused fixture)


class _FakeJobRepo:
    """JobRepository stub returning one sample job so the table renders."""

    def list(
        self,
        *,
        limit: int = 50,
        status: JobStatus | None = None,
    ) -> list[Job]:
        return [
            Job(
                id="0123456789abcdef",
                kind=JobKind.SYNC_MEETINGS,
                status=JobStatus.SUCCEEDED,
                params={},
                log_lines=(),
                result_summary=None,
                error=None,
                created_at=datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc),
                started_at=None,
                finished_at=None,
            )
        ]


@pytest.fixture()
def client(app_with_fakes: Any) -> TestClient:  # noqa: F811  (fixture param shadows imported fixture)
    app_with_fakes.dependency_overrides[get_job_repo] = lambda: _FakeJobRepo()
    return TestClient(app_with_fakes)


def test_sync_page_returns_200_and_contains_all_panels(client: Any) -> None:
    """``GET /sync`` returns 200 and lists every panel's ``<h2>`` in document order."""
    assert client.get("/sync").status_code == 200
    text = client.get("/sync").text
    for heading in (
        "Meeting sync",
        "TDoc sync",
        "Spec sync",
        "Parse TDocs (filter-driven)",
        "Parse from URL",
        "Rebuild search index",
        "Purge cache",
        "Recent sync jobs",
    ):
        assert heading in text, f"missing heading: {heading}"


def test_sync_page_renders_all_nine_forms(client: Any) -> None:
    """Each of the nine enqueue panels has a unique form id."""
    text = client.get("/sync").text
    for form_id in (
        "sync-meetings-form",
        "sync-tdocs-form",
        "sync-tdocs-all-form",
        "sync-specs-tsg-form",
        "sync-specs-id-form",
        "parse-tdocs-form",
        "parse-tdoc-url-form",
        "rebuild-search-form",
        "purge-cache-form",
    ):
        assert f'id="{form_id}"' in text, f"missing form id: {form_id}"


def test_sync_fragment_returns_partial_only(client: Any) -> None:
    """``GET /sync?format=fragment`` returns just the recent-jobs table fragment."""
    response = client.get("/sync?format=fragment")
    assert response.status_code == 200
    text = response.text
    assert "<table" in text
    # Must NOT be a full HTML document
    assert "<html" not in text.lower()
    assert "Recent sync jobs" not in text  # heading lives on the parent page only


def test_sync_fragment_includes_recent_jobs_table(client: Any) -> None:
    """The fragment wraps its table in ``<div id="recent-jobs">`` for HTMX swap target identity."""
    text = client.get("/sync?format=fragment").text
    assert 'id="recent-jobs"' in text
    assert "<table" in text


def test_landing_lists_sync_link(client: Any) -> None:
    """The landing page nav links to /sync."""
    html = client.get("/").text
    assert "/sync" in html


def test_nav_includes_sync_link(client: Any) -> None:
    """The top nav contains a ``/sync`` link next to Jobs."""
    html = client.get("/").text
    nav = html.split('<nav class="topnav">')[1].split("</nav>")[0]
    assert 'href="/sync"' in nav

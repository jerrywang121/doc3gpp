"""Unit tests for the shared jobs-list row partial.

The partial is rendered for every job in ``/jobs`` and is also the
HTMX ``outerHTML`` swap target for the Cancel button on
``POST /jobs/{id}/cancel?format=html``. These tests lock in:

- RUNNING and QUEUED rows render the Cancel button with the expected
  HTMX wire shape (``hx-post`` targets the cancel URL, ``hx-target`` is
  the row's own id, ``hx-confirm`` is set).
- Terminal rows (SUCCEEDED, FAILED, CANCELLED) omit the Cancel button
  but still render the ``show`` link and the row id.
- The partial escapes untrusted text (e.g. a status string with HTML
  chars) so the row cannot be hijacked by a future status-source change.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from doc3gpp.models.jobs import Job, JobKind, JobStatus
from doc3gpp.web.templates_setup import templates


def _make_job(status: JobStatus) -> Job:
    return Job(
        id="abcdef1234567890abcdef1234567890",
        kind=JobKind.SYNC_MEETINGS,
        status=status,
        params={"tsg": "SA2"},
        result_summary=None,
        error=None,
        log_lines=(),
        created_at=datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc),
        started_at=None,
        finished_at=None,
    )


@pytest.fixture()
def client() -> TestClient:
    """Render the partial through a minimal FastAPI app + request.

    ``templates.TemplateResponse`` requires a ``Request`` instance; the
    empty app provides one without registering the routes under test.
    The fixture exposes ``render(job)`` so each test can supply its own
    row payload.
    """
    app = FastAPI()

    @app.get("/__render")
    def _render_route(request: Request) -> object:
        # Pull the job out of app.state; tests mutate it via ``render``.
        return templates.TemplateResponse(
            request=request,
            name="partials/_job_row.html",
            context={"job": app.state.payload},
        )

    def render(payload: object) -> str:
        app.state.payload = payload
        return TestClient(app).get("/__render").text

    # Return a small handle so tests can call ``render(job)``.
    with TestClient(app) as c:
        c.render = render  # type: ignore[attr-defined]
        yield c


def test_job_row_renders_cancel_button_for_running(client: TestClient) -> None:
    job = _make_job(JobStatus.RUNNING)
    body = client.render(job)  # type: ignore[attr-defined]

    assert '<tr id="job-row-' in body
    assert ">running</td>" in body
    # Cancel button + HTMX wire shape.
    assert "Cancel" in body
    assert "</button>" in body
    assert 'hx-post="/jobs/' in body
    assert '/cancel?format=html"' in body
    assert 'hx-swap="outerHTML"' in body
    assert 'hx-confirm="Cancel this job?"' in body
    # The target must be the row's own id so HTMX swaps in place.
    assert f'hx-target="#job-row-{job.id}"' in body
    # show link is still present.
    assert ">show</a>" in body


def test_job_row_renders_cancel_button_for_queued(client: TestClient) -> None:
    job = _make_job(JobStatus.QUEUED)
    body = client.render(job)  # type: ignore[attr-defined]

    assert ">queued</td>" in body
    assert "Cancel" in body
    assert "</button>" in body
    assert "/cancel?format=html" in body


@pytest.mark.parametrize(
    "terminal_status",
    [JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED],
)
def test_job_row_omits_cancel_button_for_terminal_jobs(
    client: TestClient, terminal_status: JobStatus
) -> None:
    job = _make_job(terminal_status)
    body = client.render(job)  # type: ignore[attr-defined]

    assert "Cancel" not in body
    assert "</button>" not in body
    assert "/cancel?format=html" not in body
    # show link still renders + row id is stable for any future swap.
    assert ">show</a>" in body
    assert f'id="job-row-{job.id}"' in body
    assert f">{terminal_status.value}</td>" in body


def test_job_row_escapes_status_in_text_node(client: TestClient) -> None:
    """A status containing HTML chars must NOT render raw markup.

    JobStatus values are enum-controlled today so this is a forward-
    looking guard: if a future contributor ever wires a user-supplied
    string into ``job.status`` the partial must still escape via
    Jinja's default auto-escape (``True`` on Jinja2Templates). The
    template only ever emits ``job.status.value`` and ``job.kind.value``
    in text nodes, so we substitute a SimpleNamespace carrying a
    malicious string and assert the escaped form lands in the body.
    """
    fake = SimpleNamespace(
        id="abcdef1234567890abcdef1234567890",
        kind=SimpleNamespace(value="sync_meetings"),
        status=SimpleNamespace(value="<script>alert(1)</script>"),
        created_at=datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc),
    )
    body = client.render(fake)  # type: ignore[attr-defined]
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
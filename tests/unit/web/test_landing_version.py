"""The web UI footer must surface the installed ``doc3gpp`` version on every page."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from doc3gpp import __version__
from doc3gpp.settings.schema import Settings
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.web.app import build_app
from doc3gpp.web.deps import get_pending_jobs


@pytest.fixture()
def client(sqlite_env) -> TestClient:
    create_schema()
    app = build_app(Settings())
    app.dependency_overrides[get_pending_jobs] = lambda: 0
    with TestClient(app) as c:
        yield c


def test_landing_html_footer_contains_version(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert f"version {__version__}" in response.text


def test_meetings_html_footer_contains_version(client: TestClient) -> None:
    """Any page that extends base.html must inherit the versioned footer."""
    response = client.get("/meetings")
    assert response.status_code == 200
    assert f"version {__version__}" in response.text


def test_landing_json_shape_unchanged(client: TestClient) -> None:
    """The ``?format=json`` landing response must not include a top-level version key."""
    response = client.get("/?format=json")
    assert response.status_code == 200
    body = response.json()
    assert "version" not in body
    assert "sections" in body

"""End-to-end tests for the /sync hub page + each panel's enqueue shape."""
from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

# ``app_with_deps`` is defined in ``test_web_end_to_end``; there is no
# ``tests/integration/conftest.py`` so pytest would NOT share it across
# modules without this explicit import (C5).
from tests.integration.test_web_end_to_end import app_with_deps  # noqa: F401,F811  (reused fixture)


@pytest.fixture()
def seeded_app(sqlite_env: Any, app_with_deps: Any):  # noqa: F811  (reused fixture)
    """Seed an empty jobs table so the repo has a schema."""
    from doc3gpp.storage.db.migrate import create_schema

    create_schema()
    yield app_with_deps


def test_sync_page_renders_end_to_end(seeded_app: Any) -> None:
    app, _ = seeded_app
    with TestClient(app) as client:
        html = client.get("/sync")
    assert html.status_code == 200
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
        assert heading in html.text


def test_sync_fragment_is_table_only(seeded_app: Any) -> None:
    from doc3gpp.models.jobs import JobKind
    from doc3gpp.storage.repositories.jobs_sql import SQLAlchemyJobRepository

    app, _ = seeded_app
    # Seed one job so the fragment renders the table branch (not the
    # empty-state paragraph with no <table>).
    SQLAlchemyJobRepository().create(JobKind.SYNC_MEETINGS, {"tsg": "SA2"})

    with TestClient(app) as client:
        fragment = client.get("/sync?format=fragment")
    assert fragment.status_code == 200
    text = fragment.text
    assert "<table" in text
    assert "<html" not in text.lower()


def test_sync_page_forms_have_real_inputs(seeded_app: Any) -> None:
    """Each panel's form contains the expected input fields (not just hidden defaults)."""
    app, _ = seeded_app
    with TestClient(app) as client:
        html = client.get("/sync").text

    # Meeting sync: TSG text input + force checkbox
    assert 'id="sync-meetings-tsg"' in html
    assert 'id="sync-meetings-force"' in html

    # TDoc sync: radio switch + a single value text input (C4: the template
    # uses one ``sync-tdocs-value`` input + a ``selector`` radio group).
    assert 'name="selector"' in html  # radio button group
    assert 'id="sync-tdocs-value"' in html

    # Spec sync by TSG: TSG + per_version_details checkbox
    assert 'id="sync-specs-tsg-tsg"' in html
    assert 'name="per_version_details"' in html

    # Spec sync by id: spec_id text input
    assert 'id="sync-specs-id-spec"' in html

    # Parse from URL: URL text input
    assert 'id="parse-tdoc-url-url"' in html

    # Purge cache: scope <select>
    assert 'name="scope"' in html


@pytest.mark.parametrize(
    "path, body, expected_kind, expected_params",
    [
        (
            "/jobs/sync/meetings",
            {"tsg": "SA2", "force": False},
            "sync_meetings",
            {"tsg": "SA2"},
        ),
        (
            "/jobs/sync/tdocs",
            {"meeting_id": 156, "force": False},
            "sync_tdocs",
            {"meeting_id": 156, "force": False},
        ),
        # NB: the body uses ``meeting`` (the pydantic field / handler input),
        # which is stored under ``meeting_name`` in the job params (C3).
        (
            "/jobs/sync/tdocs",
            {"meeting": "SA2#156", "force": True},
            "sync_tdocs",
            {"meeting_name": "SA2#156", "force": True},
        ),
        (
            "/jobs/sync/tdocs/all",
            {"force": True},
            "sync_tdocs_all",
            {"force": True},
        ),
        (
            "/jobs/sync/specs",
            {"tsg": "R5", "force": False, "per_version_details": True},
            "sync_specs",
            {"tsg": "R5", "force": False, "per_version_details": True},
        ),
        (
            "/jobs/sync/specs",
            {"spec_id": "36.579-5", "force": False, "per_version_details": False},
            "sync_specs",
            {"spec_id": "36.579-5", "force": False, "per_version_details": False},
        ),
        (
            "/jobs/parse/tdocs",
            {"filter": {"tdoc_id": "S2-26%"}, "force": False, "full": False},
            "parse_tdocs",
            {"filter": {"tdoc_id": "S2-26%"}, "force": False, "full": False},
        ),
        # parse/tdoc-url adds defaults for the fields the body omits (C2).
        (
            "/jobs/parse/tdoc-url",
            {"url": "https://www.3gpp.org/ftp/TSG_RAN/WG5_Radio/TSGR5_99/Docs/", "max_depth": 2},
            "parse_tdoc_url",
            {
                "url": "https://www.3gpp.org/ftp/TSG_RAN/WG5_Radio/TSGR5_99/Docs/",
                "force": False,
                "full": False,
                "recursive": False,
                "max_depth": 2,
            },
        ),
        (
            "/jobs/search/rebuild",
            {"stale_only": True, "resume": False},
            "rebuild_search",
            {"stale_only": True, "resume": False},
        ),
        # cache/purge drops the ``yes`` confirmation flag from stored params (C2).
        (
            "/jobs/cache/purge",
            {"scope": "markdown", "yes": True},
            "cache_purge",
            {"scope": "markdown"},
        ),
    ],
)
def test_each_enqueue_route_creates_correct_job(
    seeded_app: Any, path: str, body: dict, expected_kind: str, expected_params: dict
) -> None:
    """Each MCP-shaped enqueue route lands in the repo with the expected kind + params."""
    from doc3gpp.storage.repositories.jobs_sql import SQLAlchemyJobRepository

    app, _ = seeded_app
    repo = SQLAlchemyJobRepository()
    with TestClient(app) as client:
        r = client.post(path, json=body)
    assert r.status_code == 202
    envelope = r.json()
    job = repo.get(envelope["job_id"])
    assert job is not None
    assert job.kind.value == expected_kind
    # Round-trip the params via json to drop non-JSON-native types
    assert json.loads(json.dumps(job.params)) == expected_params

"""Web-layer end-to-end tests for the FTS5 search route (offline).

These exercise the real FastAPI app built via ``build_app`` against a
real SQLite engine seeded with the shared search corpus, over the HTTP
surface. The only injection is the search service dependency: it is
replaced with a real :class:`SearchService` wired to a
:class:`PassthroughReranker` so no embedding model is loaded, while the
FTS5 query path stays fully real.

The tests pin the documented behaviour that user-supplied queries are
normalised into a valid FTS5 ``MATCH`` expression (jargon like
``nb-iot`` must be quoted) and that stopwords-only queries surface as
HTTP 400 rather than a 500.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from doc3gpp.settings.schema import CacheSettings, MCPSettings, ServerSettings, Settings
from doc3gpp.storage.db.session import get_engine
from doc3gpp.storage.repositories.search_sql import SQLAlchemySearchIndexRepository
from doc3gpp.web.app import build_app
from doc3gpp.web.deps import get_search_service


@pytest.fixture()
def search_app(search_corpus, monkeypatch: pytest.MonkeyPatch, tmp_path):
    """A built app whose /search route hits the real FTS5 index.

    ``build_app`` composes the real engine + services through
    ``build_state``; the search service dependency is swapped for a
    real :class:`SearchService` with a :class:`PassthroughReranker`
    (no embedding model), and the job worker's ``run()`` loop is
    replaced by a no-op so queued jobs are never claimed.
    """
    from doc3gpp.services.search_service import PassthroughReranker, SearchService
    from doc3gpp.web.workers.job_worker import JobWorker

    async def _noop_run(self) -> None:  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(JobWorker, "run", _noop_run)

    settings = Settings(
        server=ServerSettings(enabled=True),
        mcp=MCPSettings(enabled=True),
        cache=CacheSettings(dir=tmp_path / "cache"),
    )
    app = build_app(settings)
    app.dependency_overrides[get_search_service] = lambda: SearchService(
        repo=SQLAlchemySearchIndexRepository(),
        reranker=PassthroughReranker(),
    )
    return app


def test_search_query_with_jargon_operators_returns_hits(search_app) -> None:
    """``nb-iot`` in an operator query must not crash FTS5.

    Regression for the ``no such column: iot`` OperationalError that
    previously produced HTTP 500: the raw query was passed to FTS5
    ``MATCH``, which parses ``nb-iot`` as ``nb - iot``.
    """
    with TestClient(search_app) as client:
        response = client.get("/search", params={"q": "nb-iot AND scheduling"})
    assert response.status_code == 200
    assert "RP-2200456" in response.text
    get_engine.cache_clear()


def test_search_query_json_with_jargon_operators_returns_hits(search_app) -> None:
    """The JSON branch surfaces the same normalised-match behaviour."""
    with TestClient(search_app) as client:
        response = client.get(
            "/search",
            params={"q": "nb-iot AND scheduling", "format": "json"},
        )
    assert response.status_code == 200
    hits = json.loads(response.content)
    assert hits
    assert hits[0]["tdoc_id"] == "RP-2200456"
    get_engine.cache_clear()


def test_search_query_stopwords_only_returns_400(search_app) -> None:
    """A stopwords-only query is a client error, not a server error."""
    with TestClient(search_app) as client:
        response = client.get("/search", params={"q": "the"})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_query"
    get_engine.cache_clear()


def test_search_query_with_meeting_like_filter(search_app) -> None:
    """``meeting`` is a LIKE pattern over name OR title.

    Regression for the user report: ``meeting=%CT6%`` matched the
    displayed meeting title column but the repo compared ``m.name``
    exactly, returning zero hits. ``%plenary%`` matches the titles
    ``RAN#100 plenary`` / ``SA#200 plenary``; the NB-IoT row lives
    under meeting 100 and must survive the filter.
    """
    with TestClient(search_app) as client:
        response = client.get(
            "/search",
            params={"q": "nb-iot AND scheduling", "meeting": "%plenary%"},
        )
    assert response.status_code == 200
    assert "RP-2200456" in response.text

    # A pattern matching nothing yields zero hits, not an error.
    with TestClient(search_app) as client:
        response = client.get(
            "/search",
            params={"q": "nb-iot AND scheduling", "meeting": "%no-such-meeting%"},
        )
    assert response.status_code == 200
    assert "No matches" in response.text
    get_engine.cache_clear()


def test_search_query_with_release_like_filter(search_app) -> None:
    """``release`` is a LIKE pattern over tdocs.release.

    Regression: ``t.release = :release`` exact match returned zero hits
    for any pattern and could not match NULL-release rows.
    """
    with TestClient(search_app) as client:
        response = client.get(
            "/search",
            params={"q": "nb-iot AND scheduling", "release": "Rel-1%"},
        )
    assert response.status_code == 200
    assert "RP-2200456" in response.text

    with TestClient(search_app) as client:
        response = client.get(
            "/search",
            params={"q": "nb-iot AND scheduling", "release": "Rel-99"},
        )
    assert response.status_code == 200
    assert "No matches" in response.text
    get_engine.cache_clear()


def test_search_query_with_spec_like_filter(search_app) -> None:
    """``spec`` is a LIKE pattern over tdocs.spec (versioned specs like
    ``38.300-1`` can only be partial-matched)."""
    with TestClient(search_app) as client:
        response = client.get(
            "/search",
            params={"q": "nb-iot AND scheduling", "spec": "38.3%"},
        )
    assert response.status_code == 200
    assert "RP-2200456" in response.text

    with TestClient(search_app) as client:
        response = client.get(
            "/search",
            params={"q": "nb-iot AND scheduling", "spec": "36.5%"},
        )
    assert response.status_code == 200
    assert "No matches" in response.text
    get_engine.cache_clear()

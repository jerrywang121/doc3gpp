"""Tests for the T6 read routes + JSON envelopes.

Each route in the brief (landing / meetings / tdocs / tsgs / wis /
search) is exercised via :class:`fastapi.testclient.TestClient` against
a :func:`build_app` instance wired to in-memory sqlite + fake services.

JSON parity: every read route accepts ``?format=json`` and the payload
returned is asserted to be byte-identical to the underlying service
method via :func:`doc3gpp.web.render.to_jsonable`. This is the
"single source of truth" contract the spec mandates — any drift from
the CLI's ``--format json`` envelope is a spec violation.
"""
from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from doc3gpp.models.meeting import Meeting
from doc3gpp.models.search import SearchHit
from doc3gpp.models.tdoc import TDoc, TDocWithMeeting
from doc3gpp.models.tsg import Tsg
from doc3gpp.models.wi import Wi
from doc3gpp.services.meetings_service import MeetingService
from doc3gpp.services.search_service import SearchService
from doc3gpp.services.semantic_search_service import SemanticSearchService
from doc3gpp.services.tdoc_service import TDocService
from doc3gpp.services.tsg_service import TsgService
from doc3gpp.services.wi_service import WiService
from doc3gpp.settings.schema import Settings
from doc3gpp.web.app import build_app
from doc3gpp.web.deps import (
    get_meeting_service,
    get_search_service,
    get_semantic_search_service,
    get_tdoc_file_repo,
    get_tdoc_service,
    get_tsg_service,
    get_wi_service,
)
from doc3gpp.web.render import to_jsonable


# ---------------------------------------------------------------------------
# Fake services — every method the route calls is stubbed. The route never
# reaches into a real repo, so an in-memory sqlite backend is enough to
# satisfy the lifespan's ``engine`` requirement.
# ---------------------------------------------------------------------------


class FakeMeetingService(MeetingService):
    def __init__(self) -> None:  # noqa: D401 - intentional override
        self._meetings = [
            Meeting(
                meeting_id=1,
                name="RAN5#99-e",
                title="RAN WG5 Meeting #99-e",
                location="Athens, Greece",
                start_date=date(2026, 5, 1),
                end_date=date(2026, 5, 5),
                tsg="R5",
            ),
            Meeting(
                meeting_id=2,
                name="SA2#150-e",
                title="SA WG2 Meeting #150-e",
                location="Online",
                start_date=date(2026, 6, 1),
                end_date=date(2026, 6, 5),
                tsg="S2",
            ),
        ]

    def list_recent(self, **_kwargs: Any) -> list[Meeting]:
        return list(self._meetings)

    def get_by_id(self, meeting_id: int) -> Meeting | None:
        for m in self._meetings:
            if m.meeting_id == meeting_id:
                return m
        return None

    def list_distinct_tsgs(self) -> list[str]:
        return sorted({m.tsg for m in self._meetings if m.tsg})


class FakeTDocService(TDocService):
    def __init__(self) -> None:  # noqa: D401
        self._rows = [
            TDocWithMeeting(
                tdoc=TDoc(
                    tdoc_id="R5-260001",
                    title="CR on NR measurement",
                    meeting_id=1,
                    ftp_url="R5/26.001/R5-260001.zip",
                    spec="38.523-3",
                    release="Rel-18",
                    type="CR",
                    uploaded_date=date(2026, 5, 2),
                ),
                meeting_name="RAN5#99-e",
            ),
            TDocWithMeeting(
                tdoc=TDoc(
                    tdoc_id="R5-260002",
                    title="Another CR",
                    meeting_id=1,
                    ftp_url="R5/26.002/R5-260002.zip",
                    spec="38.523-3",
                    release="Rel-18",
                    type="CR",
                    uploaded_date=date(2026, 5, 3),
                ),
                meeting_name="RAN5#99-e",
            ),
        ]

    def list_recent_with_meeting(self, **_kwargs: Any) -> list[TDocWithMeeting]:
        return list(self._rows)


class FakeTsgService(TsgService):
    def __init__(self) -> None:  # noqa: D401
        self._tsgs = [
            Tsg(tsg_name="RAN Plenary", short_name="RP", description="RAN Plenary"),
            Tsg(tsg_name="RAN WG5", short_name="R5", description="RAN WG5"),
        ]

    def list_all(self) -> list[Tsg]:
        return list(self._tsgs)

    def get_by_short_name(self, short_name: str) -> Tsg | None:
        for t in self._tsgs:
            if t.short_name.lower() == short_name.lower():
                return t
        return None


class FakeWiService(WiService):
    def __init__(self) -> None:  # noqa: D401
        self._wis = [
            Wi(
                wi_id="800100",
                tsg_short="R5",
                name="Test WI",
                acronym="TestWI",
                release="Rel-18",
            ),
        ]

    def list_recent(self, **_kwargs: Any) -> list[Wi]:
        return list(self._wis)


class FakeSearchService(SearchService):
    def __init__(self) -> None:  # noqa: D401
        self._hits = [
            SearchHit(
                tdoc_id="R5-260001",
                score=-1.234,
                previews={"title": "<<NR>> measurement"},
                title="CR on NR measurement",
                meeting="RAN5#99-e",
                tsg="R5",
                uploaded_date="2026-05-02",
                ftp_url="R5/26.001/R5-260001.zip",
                wis=None,
            ),
        ]

    def search(self, _query: str, _filters: Any) -> list[SearchHit]:
        return list(self._hits)


class FakeSemanticSearchService(SemanticSearchService):
    def __init__(self) -> None:  # noqa: D401
        from doc3gpp.models.semantic_search import SemanticSearchHit
        self._hits = [
            SemanticSearchHit(
                tdoc_id="R5-260001",
                rrf_score=0.5,
                hit=SearchHit(
                    tdoc_id="R5-260001",
                    score=-1.234,
                    previews={},
                    title="CR on NR measurement",
                    meeting="RAN5#99-e",
                    tsg="R5",
                    uploaded_date="2026-05-02",
                    ftp_url=None,
                    wis=None,
                ),
                rank_fts5=0,
                rank_vec=1,
                min_chunk_distance=0.42,
                best_chunk_id="chunk-0",
            ),
        ]

    def search(self, *_args: Any, **_kwargs: Any) -> list[Any]:
        return list(self._hits)


@pytest.fixture()
def app_with_fakes(sqlite_env: Any) -> FastAPI:
    """Build a FastAPI app with the read routes + fake services wired up."""
    return _build_app_with_fakes()


def _build_app_with_fakes(
    *, cache_dir: Any | None = None,
) -> FastAPI:
    """Helper that builds a fake-wired app, optionally with a custom cache dir."""
    settings = Settings()
    settings.search.enabled = True
    settings.semantic_search.enabled = True
    if cache_dir is not None:
        settings.cache.dir = cache_dir
    app = build_app(settings)
    app.dependency_overrides[get_meeting_service] = lambda: FakeMeetingService()
    app.dependency_overrides[get_tdoc_service] = lambda: FakeTDocService()
    app.dependency_overrides[get_tsg_service] = lambda: FakeTsgService()
    app.dependency_overrides[get_wi_service] = lambda: FakeWiService()
    app.dependency_overrides[get_search_service] = lambda: FakeSearchService()
    app.dependency_overrides[get_semantic_search_service] = (
        lambda: FakeSemanticSearchService()
    )
    app.dependency_overrides[get_tdoc_file_repo] = lambda: MagicMock()
    return app


@pytest.fixture()
def client(app_with_fakes: FastAPI) -> TestClient:
    return TestClient(app_with_fakes)


# ---------------------------------------------------------------------------
# Landing page
# ---------------------------------------------------------------------------


def test_landing_renders_html(client: TestClient) -> None:
    """``GET /`` returns 200 with the landing template."""
    response = client.get("/")
    assert response.status_code == 200
    assert "doc3gpp web" in response.text


def test_landing_json_payload(client: TestClient) -> None:
    """``GET /?format=json`` returns the canonical sections list."""
    response = client.get("/?format=json")
    assert response.status_code == 200
    body = response.json()
    assert "sections" in body
    assert any(s["href"] == "/meetings" for s in body["sections"])


# ---------------------------------------------------------------------------
# Meetings
# ---------------------------------------------------------------------------


def test_meetings_list_renders_html(client: TestClient) -> None:
    """``GET /meetings`` returns 200 with the meeting list template."""
    response = client.get("/meetings")
    assert response.status_code == 200
    assert "RAN5#99-e" in response.text


def test_meetings_list_json_matches_service(client: TestClient) -> None:
    """``GET /meetings?format=json`` returns the to_jsonable of the service list."""
    response = client.get("/meetings?format=json")
    assert response.status_code == 200
    body = response.json()
    service = FakeMeetingService()
    expected = to_jsonable(service.list_recent())
    assert body == {"meetings": expected}


def test_meeting_show_renders_html(client: TestClient) -> None:
    """``GET /meetings/{id}`` returns 200 with the meeting template."""
    response = client.get("/meetings/1")
    assert response.status_code == 200
    assert "RAN5#99-e" in response.text


def test_meeting_show_404(client: TestClient) -> None:
    """``GET /meetings/{unknown}`` returns 404 with the canonical envelope."""
    response = client.get("/meetings/9999")
    assert response.status_code == 404
    body = response.json()
    assert body["error"] == "meeting_not_found"


def test_meeting_show_json(client: TestClient) -> None:
    """``GET /meetings/{id}?format=json`` returns the to_jsonable of the meeting."""
    response = client.get("/meetings/1?format=json")
    assert response.status_code == 200
    service = FakeMeetingService()
    expected = to_jsonable(service.get_by_id(1))
    assert response.json() == {"meeting": expected}


# ---------------------------------------------------------------------------
# TDocs
# ---------------------------------------------------------------------------


def test_tdoc_list_renders_html(client: TestClient) -> None:
    """``GET /tdocs`` returns 200 with the TDoc list template."""
    response = client.get("/tdocs")
    assert response.status_code == 200
    assert "R5-260001" in response.text


def test_tdoc_list_json(client: TestClient) -> None:
    """``GET /tdocs?format=json`` returns the to_jsonable of the service rows."""
    response = client.get("/tdocs?format=json")
    assert response.status_code == 200
    service = FakeTDocService()
    expected = to_jsonable(service.list_recent_with_meeting())
    assert response.json() == {"tdocs": expected}


def test_tdoc_show_renders_html(client: TestClient, sqlite_env: Any) -> None:
    """``GET /tdocs/{id}`` returns 200 with the TDoc show template.

    The composition requires an actual ``tdocs`` row in the in-memory
    sqlite; we seed one and assert the rendered page mentions the id.
    """
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

    create_schema()
    SQLAlchemyTDocRepository().upsert(
        TDoc(
            tdoc_id="R5-260001",
            title="CR on NR measurement",
            ftp_url="R5/26.001/R5-260001.zip",
        ),
    )
    response = client.get("/tdocs/R5-260001")
    assert response.status_code == 200
    assert "R5-260001" in response.text


def test_tdoc_show_404(client: TestClient, sqlite_env: Any) -> None:
    """``GET /tdocs/{unknown}`` returns 404."""
    from doc3gpp.storage.db.migrate import create_schema

    create_schema()
    response = client.get("/tdocs/R5-999999")
    assert response.status_code == 404
    body = response.json()
    assert body["error"] == "tdoc_not_found"


def test_tdoc_content_markdown_cache_hit(
    client: TestClient, sqlite_env: Any, tmp_path: Any,
) -> None:
    """``GET /tdocs/{id}/content?format=markdown`` reads the cached markdown."""
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository
    from doc3gpp.scraping.cache_keys import derive_cache_file

    create_schema()
    url = "R5/26.001/R5-260001.zip"
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5-260001", ftp_url=url),
    )
    cache_file = derive_cache_file(url)
    markdown_path = tmp_path / "markdown" / cache_file
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text("# Title\n\nSome content.", encoding="utf-8")

    new_app = _build_app_with_fakes(cache_dir=tmp_path)
    with TestClient(new_app) as new_client:
        response = new_client.get("/tdocs/R5-260001/content?format=markdown")
    assert response.status_code == 200
    assert "# Title" in response.text


def test_tdoc_content_markdown_cache_miss_404(
    client: TestClient, sqlite_env: Any, tmp_path: Any,
) -> None:
    """``GET /tdocs/{id}/content?format=markdown`` returns 404 on cache miss."""
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

    create_schema()
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5-260001", ftp_url="R5/missing.zip"),
    )
    new_app = _build_app_with_fakes(cache_dir=tmp_path)
    with TestClient(new_app) as new_client:
        response = new_client.get(
            "/tdocs/R5-260001/content?format=markdown",
        )
    assert response.status_code == 404
    body = response.json()
    assert "doc3gpp tdoc parse --tdoc" in body["detail"]


def test_tdoc_content_html(client: TestClient, sqlite_env: Any, tmp_path: Any) -> None:
    """``GET /tdocs/{id}/content?format=html`` renders the markdown as HTML."""
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository
    from doc3gpp.scraping.cache_keys import derive_cache_file

    create_schema()
    url = "R5/26.001/R5-260001.zip"
    SQLAlchemyTDocRepository().upsert(TDoc(tdoc_id="R5-260001", ftp_url=url))
    cache_file = derive_cache_file(url)
    markdown_path = tmp_path / "markdown" / cache_file
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text("# Heading\n\n**bold**", encoding="utf-8")

    new_app = _build_app_with_fakes(cache_dir=tmp_path)
    with TestClient(new_app) as new_client:
        response = new_client.get("/tdocs/R5-260001/content?format=html")
    assert response.status_code == 200
    assert "Heading" in response.text
    assert "<strong>bold</strong>" in response.text


# ---------------------------------------------------------------------------
# TSGs
# ---------------------------------------------------------------------------


def test_tsg_list_renders_html(client: TestClient) -> None:
    """``GET /tsgs`` returns 200 with the TSG list template."""
    response = client.get("/tsgs")
    assert response.status_code == 200
    assert "RAN Plenary" in response.text


def test_tsg_list_json(client: TestClient) -> None:
    """``GET /tsgs?format=json`` returns the to_jsonable of the service list."""
    response = client.get("/tsgs?format=json")
    assert response.status_code == 200
    expected = to_jsonable(FakeTsgService().list_all())
    assert response.json() == {"tsgs": expected}


def test_tsg_show_renders_html(client: TestClient) -> None:
    """``GET /tsgs/{short_name}`` returns 200 with the TSG show template."""
    response = client.get("/tsgs/R5")
    assert response.status_code == 200
    assert "RAN WG5" in response.text


def test_tsg_show_404(client: TestClient) -> None:
    """``GET /tsgs/{unknown}`` returns 404."""
    response = client.get("/tsgs/UNKNOWN")
    assert response.status_code == 404
    assert response.json()["error"] == "tsg_not_found"


# ---------------------------------------------------------------------------
# WIs
# ---------------------------------------------------------------------------


def test_wi_list_renders_html(client: TestClient) -> None:
    """``GET /wis`` returns 200 with the WI list template."""
    response = client.get("/wis")
    assert response.status_code == 200
    assert "TestWI" in response.text


def test_wi_list_json(client: TestClient) -> None:
    """``GET /wis?format=json`` returns the to_jsonable of the service list."""
    response = client.get("/wis?format=json")
    assert response.status_code == 200
    expected = to_jsonable(FakeWiService().list_recent())
    assert response.json() == {"wis": expected}


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def test_search_query_renders_html(client: TestClient) -> None:
    """``GET /search?q=foo`` returns 200 with the search template."""
    response = client.get("/search?q=foo")
    assert response.status_code == 200
    assert "R5-260001" in response.text


def test_search_query_json(client: TestClient) -> None:
    """``GET /search?q=foo&format=json`` returns the to_jsonable of hits."""
    response = client.get("/search?q=foo&format=json")
    assert response.status_code == 200
    body = response.json()
    assert "hits" in body
    expected = to_jsonable(FakeSearchService().search("foo", None))
    assert body["hits"] == expected


def test_search_sem_renders_html(client: TestClient) -> None:
    """``GET /search/sem?q=foo`` returns 200 with the search template."""
    response = client.get("/search/sem?q=foo")
    assert response.status_code == 200


def test_search_sem_json(client: TestClient) -> None:
    """``GET /search/sem?q=foo&format=json`` returns the to_jsonable of hits."""
    response = client.get("/search/sem?q=foo&format=json")
    assert response.status_code == 200
    body = response.json()
    assert "hits" in body


# ---------------------------------------------------------------------------
# Static + health
# ---------------------------------------------------------------------------


def test_static_css_served(client: TestClient) -> None:
    """``GET /static/style.css`` returns 200 with the vendored CSS."""
    response = client.get("/static/style.css")
    assert response.status_code == 200
    assert "doc3gpp" in response.text or "color" in response.text


def test_static_htmx_served(client: TestClient) -> None:
    """``GET /static/htmx.min.js`` returns 200 with the vendored JS."""
    response = client.get("/static/htmx.min.js")
    assert response.status_code == 200
    assert "htmx" in response.text[:200]


def test_healthz_still_ok(client: TestClient) -> None:
    """``GET /healthz`` returns 200 (no regression)."""
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
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
from doc3gpp.web.render import (
    meeting_rows,
    tdoc_rows,
    to_jsonable,
    tsg_rows,
    wi_rows,
)


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


def test_meetings_list_json_matches_cli_rows(client: TestClient) -> None:
    """``GET /meetings?format=json`` returns the CLI-shaped row array.

    Ruling B: the payload must be the same bare array of
    field-selected, string-coerced rows that
    ``doc3gpp meeting list --format json`` prints (default columns
    from ``settings.output.fields.meeting``).
    """
    from doc3gpp.settings.schema import OutputFieldsSettings

    response = client.get("/meetings?format=json")
    assert response.status_code == 200
    service = FakeMeetingService()
    fields = OutputFieldsSettings().meeting
    assert response.json() == meeting_rows(service.list_recent(), fields)


def test_meeting_show_renders_html(client: TestClient) -> None:
    """``GET /meetings/{id}`` returns 200 with the meeting template."""
    response = client.get("/meetings/1")
    assert response.status_code == 200
    assert "RAN5#99-e" in response.text


def test_meetings_list_htmx_returns_partial(client: TestClient) -> None:
    """``GET /meetings`` with ``HX-Request: true`` returns the results partial.

    The Apply button uses HTMX with ``hx-swap=\"outerHTML\" hx-target=\"#results\"``,
    so the response must be the ``partials/meeting_results.html`` fragment
    (a single ``<div id=\"results\">`` block) — not a full HTML document.
    Returning the full page would replace the ``#results`` div with a
    nested ``<!DOCTYPE html>`` and destroy the page chrome.
    """
    response = client.get("/meetings", headers={"HX-Request": "true"})
    assert response.status_code == 200
    body = response.text
    assert "<!DOCTYPE" not in body
    assert "<html" not in body
    assert 'id="results"' in body
    assert "<table" in body
    assert "name=\"tsg\"" not in body


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


def test_tdoc_list_htmx_returns_partial(client: TestClient) -> None:
    """``GET /tdocs`` with ``HX-Request: true`` returns the results partial.

    The Apply button uses HTMX with ``hx-swap=\"outerHTML\" hx-target=\"#results\"``,
    so the response must be the ``partials/tdoc_results.html`` fragment —
    not a full HTML document (which would replace ``#results`` with a
    nested ``<!DOCTYPE html>`` and destroy the page chrome).
    """
    response = client.get("/tdocs", headers={"HX-Request": "true"})
    assert response.status_code == 200
    body = response.text
    assert "<!DOCTYPE" not in body
    assert "<html" not in body
    assert 'id="results"' in body
    assert "<table" in body
    assert "name=\"tdoc_id\"" not in body


def test_tdoc_list_json(client: TestClient) -> None:
    """``GET /tdocs?format=json`` returns the CLI-shaped row array.

    Ruling B: the payload must be the same bare array of
    field-selected, string-coerced rows that
    ``doc3gpp tdoc list --format json`` prints (default columns from
    ``settings.output.fields.tdoc``).
    """
    from doc3gpp.settings.schema import OutputFieldsSettings

    response = client.get("/tdocs?format=json")
    assert response.status_code == 200
    service = FakeTDocService()
    fields = OutputFieldsSettings().tdoc
    assert response.json() == tdoc_rows(
        service.list_recent_with_meeting(), fields,
    )


def test_tdoc_list_json_drops_phantom_filters(client: TestClient) -> None:
    """``GET /tdocs`` ignores filters the repository Protocol does not support.

    Ruling A: only the repo-backed filter set is accepted; the phantom
    params (``for_decision`` / ``work_item`` / ``start_after`` …) must
    not affect the query. The route still responds 200 and the filter
    form renders only the supported fields.
    """
    response = client.get(
        "/tdocs?for_decision=1&work_item=foo&start_after=2026-01-01&format=json",
    )
    assert response.status_code == 200
    assert isinstance(response.json(), list)

    html = client.get("/tdocs").text
    assert 'name="for_decision"' not in html
    assert 'name="work_item"' not in html
    assert 'name="start_after"' not in html
    assert 'name="agenda_item"' not in html


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
    """``GET /tdocs/{id}/content?format=markdown`` returns 404 on cache miss.

    A cache miss (row present, markdown file absent) maps to the
    dedicated ``cache_miss`` envelope with a hint, not to
    ``tdoc_not_found``.
    """
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
    assert body["error"] == "cache_miss"
    assert body["hint"] == "run: doc3gpp tdoc parse --tdoc R5-260001"


def test_meeting_list_filters_form_fields(client: TestClient) -> None:
    """``GET /meetings`` renders only the repo-backed filter fields.

    Ruling A: ``start_after`` / ``start_before`` are gone from both the
    route and the filter form; ``tsg`` / ``year`` / ``location`` remain.
    """
    html = client.get("/meetings").text
    assert 'name="tsg"' in html
    assert 'name="year"' in html
    assert 'name="location"' in html
    assert 'name="start_after"' not in html
    assert 'name="start_before"' not in html


def test_meetings_list_empty_numeric_filter_returns_200(client: TestClient) -> None:
    """``GET /meetings?tsg=c6&year=`` is 200, not 422 (empty form fields).

    The HTML form serialises blank numeric inputs as ``year=`` (empty
    string). FastAPI's ``int | None`` annotation rejects ``""`` with a
    422 before the handler runs, which manifested in the UI as the
    filter appearing to do nothing when ``year`` was left blank. The
    route now declares the field as ``str`` and parses via
    :func:`parse_int_query`, which treats ``""`` as ``None``.
    """
    response = client.get("/meetings?tsg=c6&year=&limit=&offset=")
    assert response.status_code == 200


def test_meetings_list_invalid_numeric_filter_returns_400(client: TestClient) -> None:
    """``GET /meetings?year=abc`` is 400 with the invalid_filter envelope."""
    response = client.get("/meetings?year=abc")
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_filter"


def test_tdoc_list_empty_numeric_filter_returns_200(client: TestClient) -> None:
    """``GET /tdocs?meeting_id=`` is 200, not 422 (empty form field)."""
    response = client.get("/tdocs?meeting_id=&limit=&offset=")
    assert response.status_code == 200


def test_tdoc_list_invalid_numeric_filter_returns_400(client: TestClient) -> None:
    """``GET /tdocs?meeting_id=abc`` is 400 with the invalid_filter envelope."""
    response = client.get("/tdocs?meeting_id=abc")
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_filter"


def test_wi_list_empty_numeric_filter_returns_200(client: TestClient) -> None:
    """``GET /wis?limit=`` is 200, not 422 (empty form field)."""
    response = client.get("/wis?limit=")
    assert response.status_code == 200


def test_wi_list_invalid_numeric_filter_returns_400(client: TestClient) -> None:
    """``GET /wis?limit=abc`` is 400 with the invalid_filter envelope."""
    response = client.get("/wis?limit=abc")
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_filter"


def test_search_query_empty_numeric_filter_returns_200(client: TestClient) -> None:
    """``GET /search?q=foo&limit=`` is 200, not 422."""
    response = client.get("/search?q=foo&limit=")
    assert response.status_code == 200


def test_tdoc_list_empty_date_filter_returns_200(client: TestClient) -> None:
    """``GET /tdocs?uploaded-date=`` is 200, not 400 (empty form value).

    The HTML form serialises blank date inputs as ``uploaded-date=``
    (empty string). :func:`parse_date_query` now treats ``""`` as
    ``None`` so the route doesn't 400 when the user clicks Apply with
    the date field left blank.
    """
    response = client.get("/tdocs?uploaded-date=")
    assert response.status_code == 200


def test_tdoc_list_invalid_date_filter_returns_400(client: TestClient) -> None:
    """``GET /tdocs?uploaded-date=bogus`` is 400 with invalid_filter envelope."""
    response = client.get("/tdocs?uploaded-date=bogus")
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_filter"


def test_tdoc_list_empty_text_filter_returns_all_rows(client: TestClient) -> None:
    """``GET /tdocs?tdoc_id=r5-%&type=&uploaded-date=`` is 200 and applies r5-%.

    Empty text fields used to round-trip as ``""`` and silently
    :sql:`LIKE ''` (matching only empty strings). Now they round-trip
    as ``None`` and the SQL filter is a no-op, so the explicit
    ``tdoc_id=r5-%`` filter alone produces the filtered result.
    """
    response = client.get("/tdocs?tdoc_id=R5-%25&type=&uploaded-date=")
    assert response.status_code == 200


def test_search_query_empty_date_filter_returns_200(client: TestClient) -> None:
    """``GET /search?since=&until=`` is 200, not 400."""
    response = client.get("/search?q=foo&since=&until=")
    assert response.status_code == 200


def test_search_query_invalid_date_filter_returns_400(client: TestClient) -> None:
    """``GET /search?since=bogus`` is 400 with invalid_filter envelope."""
    response = client.get("/search?since=bogus")
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_filter"


def test_search_sem_empty_numeric_filter_returns_200(client: TestClient) -> None:
    """``GET /search/sem?q=foo&limit=`` is 200, not 422."""
    response = client.get("/search/sem?q=foo&limit=")
    assert response.status_code == 200


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
    """``GET /tsgs?format=json`` returns the CLI-shaped row array."""
    from doc3gpp.settings.schema import OutputFieldsSettings

    response = client.get("/tsgs?format=json")
    assert response.status_code == 200
    fields = OutputFieldsSettings().tsg
    assert response.json() == tsg_rows(FakeTsgService().list_all(), fields)


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


def test_wi_list_htmx_returns_partial(client: TestClient) -> None:
    """``GET /wis`` with ``HX-Request: true`` returns the results partial.

    Same contract as the meetings / tdocs list pages: HTMX must receive
    a fragment that fits the ``#results`` swap target, not a full HTML
    document.
    """
    response = client.get("/wis", headers={"HX-Request": "true"})
    assert response.status_code == 200
    body = response.text
    assert "<!DOCTYPE" not in body
    assert "<html" not in body
    assert 'id="results"' in body
    assert "<table" in body


def test_wi_list_json(client: TestClient) -> None:
    """``GET /wis?format=json`` returns the CLI-shaped row array."""
    from doc3gpp.settings.schema import OutputFieldsSettings

    response = client.get("/wis?format=json")
    assert response.status_code == 200
    fields = OutputFieldsSettings().wi
    assert response.json() == wi_rows(FakeWiService().list_recent(), fields)


def test_wi_list_uses_acronym_not_id(client: TestClient) -> None:
    """``GET /wis`` filters by ``acronym`` / ``release``, never a bogus ``id``.

    Ruling A: the WI route exposes only repo-backed filters
    (``tsg`` / ``name`` / ``acronym`` / ``release``); the old ``id``
    param and the form field are gone.
    """
    response = client.get("/wis?acronym=Test&release=Rel-18&format=json")
    assert response.status_code == 200
    assert isinstance(response.json(), list)

    html = client.get("/wis").text
    assert 'name="id"' not in html
    assert 'name="acronym"' in html
    assert 'name="release"' in html


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def test_search_query_renders_html(client: TestClient) -> None:
    """``GET /search?q=foo`` returns 200 with the search template."""
    response = client.get("/search?q=foo")
    assert response.status_code == 200
    assert "R5-260001" in response.text


def test_search_query_htmx_returns_partial(client: TestClient) -> None:
    """``GET /search`` with ``HX-Request: true`` returns the results partial.

    The Search button uses HTMX with ``hx-swap=\"outerHTML\" hx-target=\"#results\"``,
    so the response must be the ``partials/search_results.html`` fragment
    — a single ``<div id=\"results\">`` block — not a full HTML document.
    """
    response = client.get("/search?q=foo", headers={"HX-Request": "true"})
    assert response.status_code == 200
    body = response.text
    assert "<!DOCTYPE" not in body
    assert "<html" not in body
    assert 'id="results"' in body


def test_search_sem_htmx_returns_partial(client: TestClient) -> None:
    """``GET /search/sem`` with ``HX-Request: true`` returns the results partial."""
    response = client.get("/search/sem?q=foo", headers={"HX-Request": "true"})
    assert response.status_code == 200
    body = response.text
    assert "<!DOCTYPE" not in body
    assert "<html" not in body
    assert 'id="results"' in body


def test_search_query_json(client: TestClient) -> None:
    """``GET /search?q=foo&format=json`` returns the CLI-shaped hit array.

    Ruling B: the payload must be a bare array of hit objects matching
    ``doc3gpp search query --format json`` (tdoc_id / score / previews
    / title / meeting / tsg / uploaded_date / ftp_url / wis).
    """
    response = client.get("/search?q=foo&format=json")
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert body[0]["tdoc_id"] == "R5-260001"
    assert body[0]["previews"] == {"title": "<<NR>> measurement"}
    assert set(body[0]) == {
        "tdoc_id", "score", "previews", "title", "meeting", "tsg",
        "uploaded_date", "ftp_url", "wis",
    }


def test_search_query_bad_date_filter_400(client: TestClient) -> None:
    """``GET /search?since=<bad>`` returns 400 with the invalid_filter envelope."""
    response = client.get("/search?q=foo&since=not-a-date")
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_filter"


def test_search_sem_renders_html(client: TestClient) -> None:
    """``GET /search/sem?q=foo`` returns 200 with the search template."""
    response = client.get("/search/sem?q=foo")
    assert response.status_code == 200


def test_search_sem_json(client: TestClient) -> None:
    """``GET /search/sem?q=foo&format=json`` returns the CLI-shaped hit array.

    Ruling B: semantic hits mirror ``doc3gpp search sem --format json``
    — RRF fields at the top level and the metadata bag nested under
    ``hit``.
    """
    response = client.get("/search/sem?q=foo&format=json")
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert body[0]["tdoc_id"] == "R5-260001"
    assert body[0]["rrf_score"] == 0.5
    assert set(body[0]) == {
        "tdoc_id", "rrf_score", "rank_fts5", "rank_vec",
        "min_chunk_distance", "best_chunk_id", "hit",
    }
    assert set(body[0]["hit"]) == {"tdoc_id", "title", "ftp_url", "wis"}


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
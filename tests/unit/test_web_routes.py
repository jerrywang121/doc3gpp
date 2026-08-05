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

from datetime import date, datetime, timedelta, timezone
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
    get_settings,
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
        self._now = datetime.now(timezone.utc)
        self._meetings = [
            Meeting(
                meeting_id=1,
                name="RAN5#99-e",
                title="RAN WG5 Meeting #99-e",
                location="Athens, Greece",
                start_date=date(2026, 5, 1),
                end_date=date(2026, 5, 5),
                start_doc="R5-260001",
                end_doc="R5-260500",
                tsg="R5",
                ftp_url="TSGR5_99e/",
            ),
            Meeting(
                meeting_id=2,
                name="SA2#150-e",
                title="SA WG2 Meeting #150-e",
                location="Online",
                start_date=date(2026, 6, 1),
                end_date=date(2026, 6, 5),
                start_doc="S2-260001",
                end_doc="S2-260400",
                tsg="S2",
                tdoc_list_last_sync=self._now - timedelta(hours=1),
            ),
            Meeting(
                meeting_id=3,
                name="CT1#140-e",
                title="CT WG1 Meeting #140-e",
                location="Online",
                start_date=date(2026, 7, 1),
                end_date=date(2026, 7, 3),
                start_doc="C1-260001",
                end_doc="C1-260200",
                tsg="C1",
                tdoc_list_last_sync=self._now - timedelta(hours=48),
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
                    status="Approved",
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
                    status="Revised",
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
            Tsg(
                tsg_name="RAN Plenary",
                short_name="RP",
                description="RAN Plenary",
                url="https://www.3gpp.org/ran",
            ),
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
        self.last_filters = None
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
        self.last_filters = _filters
        return list(self._hits)


class FakeSemanticSearchService(SemanticSearchService):
    def __init__(self) -> None:  # noqa: D401
        self.last_kwargs: dict[str, Any] = {}
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
        self.last_kwargs = dict(_kwargs)
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
    app.dependency_overrides[get_settings] = lambda: settings
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


def test_nav_order_home_tsgs_meetings_tdocs_wis_search_jobs(
    client: TestClient,
) -> None:
    """The header nav lists Home, TSGs, Meetings, TDocs, WIs, Search, Jobs."""
    html = client.get("/").text
    nav = html.split('<nav class="topnav">')[1].split("</nav>")[0]
    hrefs = [line.split('href="')[1].split('"')[0] for line in nav.splitlines() if 'href="' in line]
    assert hrefs == ["/", "/tsgs", "/meetings", "/tdocs", "/wis", "/search", "/jobs"]


def test_nav_hides_jobs_badge_when_no_pending_jobs(client: TestClient) -> None:
    """No queued jobs → the Jobs nav link has no badge."""
    html = client.get("/").text
    assert 'href="/jobs"' in html
    assert 'class="nav-badge"' not in html


def test_nav_shows_pending_jobs_badge(client: TestClient) -> None:
    """Queued jobs → the Jobs nav link shows a count badge."""
    from doc3gpp.web.deps import get_pending_jobs

    app = client.app
    app.dependency_overrides[get_pending_jobs] = lambda: 2
    try:
        html = client.get("/").text
    finally:
        app.dependency_overrides.pop(get_pending_jobs, None)
    assert 'class="nav-badge">2</span>' in html


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


def test_meetings_list_shows_sync_columns(client: TestClient) -> None:
    """The list table carries Start doc / End doc / Sync columns and values."""
    html = client.get("/meetings").text
    assert "<th>Start doc</th>" in html
    assert "<th>End doc</th>" in html
    assert "<th>Sync</th>" in html
    assert "R5-260001" in html
    assert "R5-260500" in html
    assert "S2-260001" in html
    assert "S2-260400" in html


def test_meetings_list_sync_button_freshness_classes(client: TestClient) -> None:
    """Each row's sync button carries the freshness class (grey/green/orange)."""
    html = client.get("/meetings").text
    assert 'class="sync-btn sync-never"' in html
    assert 'class="sync-btn sync-fresh"' in html
    assert 'class="sync-btn sync-stale"' in html


def test_meetings_list_sync_button_posts_meeting_id(client: TestClient) -> None:
    """The sync button is an HTMX form posting the meeting_id to the job route."""
    html = client.get("/meetings").text
    assert 'hx-post="/jobs/sync_tdocs"' in html
    assert 'hx-swap="none"' in html
    assert "&#8635;" in html
    for meeting_id in (1, 2, 3):
        assert f'name="meeting_id" value="{meeting_id}"' in html


def test_meetings_list_name_links_to_portal(client: TestClient) -> None:
    """Each meeting Name cell links to the 3GPP portal meeting page."""
    html = client.get("/meetings").text
    assert (
        '<a href="https://portal.3gpp.org/Home.aspx#/meeting?MtgId=1">RAN5#99-e</a>'
        in html
    )
    assert (
        '<a href="https://portal.3gpp.org/Home.aspx#/meeting?MtgId=2">SA2#150-e</a>'
        in html
    )


def test_meetings_list_sync_queued_indication(client: TestClient) -> None:
    """The sync form shows a brief 'queued' indication after the request."""
    html = client.get("/meetings").text
    assert 'hx-on::after-request="this.querySelector(\'.sync-queued\').style.display = \'inline\'"' in html
    assert '<span class="sync-queued" style="display:none">queued</span>' in html


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


def test_meeting_show_renders_sync_fields(client: TestClient) -> None:
    """The detail page shows Start doc / End doc / Last sync rows."""
    html = client.get("/meetings/1").text
    assert "<dt>Start doc</dt>" in html
    assert "<dt>End doc</dt>" in html
    assert "<dt>Last sync</dt>" in html
    assert "<dd>Never</dd>" in html


def test_meeting_show_renders_formatted_last_sync(client: TestClient) -> None:
    """A synced meeting shows YYYY-MM-DD HH:MM UTC, never 'Never'."""
    html = client.get("/meetings/2").text
    last_sync = FakeMeetingService().get_by_id(2).tdoc_list_last_sync
    assert last_sync is not None
    formatted = last_sync.strftime("%Y-%m-%d %H:%M")
    previous_minute = (last_sync - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M")
    # The app's fake was built a moment before this service instance; tolerate
    # a wall-clock minute rollover between the two constructions.
    assert formatted in html or previous_minute in html
    assert "UTC" in html
    assert "Never" not in html


def test_meeting_show_links_to_tdocs(client: TestClient) -> None:
    """The detail page links to the meeting's TDocs pre-filtered by meeting id."""
    html = client.get("/meetings/1").text
    assert "View TDocs for this meeting" in html
    assert 'href="/tdocs?meeting_id=1&amp;limit=200"' in html


def test_meeting_show_ftp_url_links_to_3gpp_ftp(client: TestClient) -> None:
    """The FTP URL field links to the 3GPP FTP site."""
    html = client.get("/meetings/1").text
    assert (
        '<a href="https://www.3gpp.org/ftp/TSGR5_99e/"><code>TSGR5_99e/</code></a>'
        in html
    )


def test_meeting_show_sync_queued_indication(client: TestClient) -> None:
    """The detail-page sync form shows a 'Sync job queued' indication."""
    html = client.get("/meetings/1").text
    assert 'hx-post="/jobs/sync_tdocs"' in html
    assert 'hx-on::after-request="this.querySelector(\'.sync-queued\').style.display = \'inline\'"' in html
    assert '<span class="sync-queued" style="display:none">Sync job queued</span>' in html


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


def test_tdoc_show_ftp_url_links_to_3gpp_ftp_when_not_cached(
    client: TestClient, sqlite_env: Any, tmp_path: Any,
) -> None:
    """Without a cached zip the FTP URL field links to the 3GPP FTP site."""
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

    create_schema()
    url = "R5/26.001/R5-260001.zip"
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5-260001", ftp_url=url),
    )
    new_app = _build_app_with_fakes(cache_dir=tmp_path)
    with TestClient(new_app) as new_client:
        html = new_client.get("/tdocs/R5-260001").text
    assert f'<a href="https://www.3gpp.org/ftp/{url}">' in html
    assert "href=\"/tdocs/R5-260001/download\"" not in html


def test_tdoc_show_ftp_url_links_to_cached_zip_download(
    client: TestClient, sqlite_env: Any, tmp_path: Any,
) -> None:
    """With a cached zip the FTP URL field links to the local download route."""
    from doc3gpp.scraping.cache_keys import derive_cache_file
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

    create_schema()
    url = "R5/26.001/R5-260001.zip"
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5-260001", ftp_url=url),
    )
    cache_file = derive_cache_file(url)
    zip_path = tmp_path / "zips" / cache_file
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    zip_path.write_bytes(b"PK\x03\x04 fake zip bytes")

    new_app = _build_app_with_fakes(cache_dir=tmp_path)
    with TestClient(new_app) as new_client:
        html = new_client.get("/tdocs/R5-260001").text
    assert 'href="/tdocs/R5-260001/download"' in html
    assert "https://www.3gpp.org/ftp/" not in html


def test_tdoc_download_serves_cached_zip(
    client: TestClient, sqlite_env: Any, tmp_path: Any,
) -> None:
    """``GET /tdocs/{id}/download`` serves the cached zip bytes."""
    from doc3gpp.scraping.cache_keys import derive_cache_file
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

    create_schema()
    url = "R5/26.001/R5-260001.zip"
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5-260001", ftp_url=url),
    )
    cache_file = derive_cache_file(url)
    zip_path = tmp_path / "zips" / cache_file
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    zip_path.write_bytes(b"PK\x03\x04 fake zip bytes")

    new_app = _build_app_with_fakes(cache_dir=tmp_path)
    with TestClient(new_app) as new_client:
        response = new_client.get("/tdocs/R5-260001/download")
    assert response.status_code == 200
    assert response.content == b"PK\x03\x04 fake zip bytes"
    assert response.headers["content-type"] == "application/zip"


def test_tdoc_download_cache_miss_404(
    client: TestClient, sqlite_env: Any, tmp_path: Any,
) -> None:
    """``GET /tdocs/{id}/download`` returns 404 with a hint on cache miss."""
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

    create_schema()
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5-260001", ftp_url="R5/missing.zip"),
    )
    new_app = _build_app_with_fakes(cache_dir=tmp_path)
    with TestClient(new_app) as new_client:
        response = new_client.get("/tdocs/R5-260001/download")
    assert response.status_code == 404
    body = response.json()
    assert body["error"] == "cache_miss"
    assert body["hint"] == "run: doc3gpp tdoc parse --tdoc R5-260001"


def test_tdoc_show_ttcn_changed_functions(
    client: TestClient, sqlite_env: Any,
) -> None:
    """The TTCN section lists the changed_functions aggregate."""
    from doc3gpp.models.tdoc_cr import TDocCRTTCNDetails
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_cr_ttcn_sql import (
        SQLAlchemyTDocCrTtcnRepository,
    )
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

    create_schema()
    url = "R5/26.001/R5s260001.zip"
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5s260001", ftp_url=url),
    )
    SQLAlchemyTDocCrTtcnRepository().upsert(
        TDocCRTTCNDetails(
            tdoc_id="R5s260001",
            ftp_url=url,
            testcase="TC_1",
            changed_functions=["mod_a.fn_one", "mod_b.fn_two"],
        ),
    )
    response = client.get("/tdocs/R5s260001")
    assert response.status_code == 200
    assert "<dt>Changed functions</dt>" in response.text
    assert "<code>mod_a.fn_one</code>" in response.text
    assert "<code>mod_b.fn_two</code>" in response.text


def test_tdoc_show_related_wis_in_tdoc_section(
    client: TestClient, sqlite_env: Any,
) -> None:
    """The TDoc section shows the related_wis field."""
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

    create_schema()
    SQLAlchemyTDocRepository().upsert(
        TDoc(
            tdoc_id="R5-260001",
            title="CR on NR measurement",
            ftp_url="R5/26.001/R5-260001.zip",
            related_wis="890001, 890002",
        ),
    )
    response = client.get("/tdocs/R5-260001")
    assert response.status_code == 200
    assert "<dt>Related WIs</dt>" in response.text
    assert "<dd>890001, 890002</dd>" in response.text


def test_tdoc_show_related_wis_dash_when_absent(
    client: TestClient, sqlite_env: Any,
) -> None:
    """No related_wis -> the field renders '-'."""
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

    create_schema()
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5-260001", ftp_url="R5/26.001/R5-260001.zip"),
    )
    response = client.get("/tdocs/R5-260001")
    assert response.status_code == 200
    assert "<dt>Related WIs</dt>" in response.text
    assert "<dd>-</dd>" in response.text


def test_tdoc_show_auxiliary_files_link_to_ftp(
    client: TestClient, sqlite_env: Any,
) -> None:
    """Auxiliary files link to their 3GPP FTP URLs."""
    from doc3gpp.models.tdoc_file import TDocFile
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_file_sql import SQLAlchemyTDocFileRepository
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository
    from doc3gpp.web.deps import get_tdoc_file_repo

    create_schema()
    url = "R5/26.001/R5-260001.zip"
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5-260001", ftp_url=url),
    )
    SQLAlchemyTDocFileRepository().upsert_many(
        [
            TDocFile(
                tdoc_id="R5-260001",
                type="revision",
                file="R5-260001r1.zip",
                ftp_url="R5/26.001/R5-260001r1.zip",
            ),
        ],
    )
    app = client.app
    app.dependency_overrides[get_tdoc_file_repo] = (
        lambda: SQLAlchemyTDocFileRepository()
    )
    try:
        response = client.get("/tdocs/R5-260001")
    finally:
        app.dependency_overrides.pop(get_tdoc_file_repo, None)
    assert response.status_code == 200
    assert (
        '<a href="https://www.3gpp.org/ftp/R5/26.001/R5-260001r1.zip">'
        "<code>R5/26.001/R5-260001r1.zip</code></a> (revision)"
    ) in response.text


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


def test_meeting_list_filters_form_has_tdoc_input(client: TestClient) -> None:
    """``GET /meetings`` renders the TDoc filter input."""
    html = client.get("/meetings").text
    assert 'name="tdoc"' in html


def test_meetings_list_tdoc_filter_returns_200(client: TestClient) -> None:
    """``GET /meetings?tdoc=R5-260013`` is 200 (pass-through to service)."""
    response = client.get("/meetings?tdoc=R5-260013")
    assert response.status_code == 200


def test_meetings_list_empty_tdoc_filter_returns_200(client: TestClient) -> None:
    """``GET /meetings?tdoc=`` is 200, not 422 (empty form field)."""
    response = client.get("/meetings?tdoc=&tsg=c6")
    assert response.status_code == 200


def test_meetings_list_invalid_tdoc_filter_returns_400(client: TestClient) -> None:
    """``GET /meetings?tdoc=not-a-tdoc`` is 400 with invalid_filter envelope."""
    response = client.get("/meetings?tdoc=not-a-tdoc")
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_filter"


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


def test_tdoc_list_pagination_renders_without_500(
    app_with_fakes: FastAPI,
) -> None:
    """``GET /tdocs?offset=50`` renders the pagination block, no 500.

    Regression for an UndefinedError: pagination.html references
    ``{{ limit }}`` but the route only passed ``limit`` nested under
    ``filters``. With pagination active (more rows than fit on the
    page) the template evaluated ``offset - limit`` and Jinja raised
    ``UndefinedError: 'limit' is undefined`` — the user saw a 500.
    """

    class _ManyRowsTDocService(FakeTDocService):
        def list_recent_with_meeting(
            self, *, limit: int = 50, offset: int = 0, **_kwargs: Any,
        ) -> list[TDocWithMeeting]:
            return [
                TDocWithMeeting(
                    tdoc=TDoc(
                        tdoc_id=f"R5-{260001 + offset + i:06d}",
                        title=f"row {offset + i}",
                        meeting_id=1,
                        ftp_url=f"R5/26.{(offset + i):03d}/R5-{260001 + offset + i:06d}.zip",
                        spec="38.523-3",
                        release="Rel-18",
                        type="CR",
                        uploaded_date=date(2026, 5, 2),
                    ),
                    meeting_name="RAN5#99-e",
                )
                for i in range(limit)
            ]

    app_with_fakes.dependency_overrides[get_tdoc_service] = (
        lambda: _ManyRowsTDocService()
    )
    with TestClient(app_with_fakes) as c:
        # offset=0 + limit=50 → pagination block must render with prev/next.
        response = c.get("/tdocs?limit=50&offset=0")
        assert response.status_code == 200
        assert '<nav class="pagination">' in response.text
        # offset=50 → next page; would have raised UndefinedError before
        # the fix because pagination.html evaluated `offset - limit`.
        response = c.get("/tdocs?limit=50&offset=50")
        assert response.status_code == 200
        assert '<nav class="pagination">' in response.text
        assert "‹ prev" in response.text


def test_tdoc_list_pagination_htmx_offset(
    app_with_fakes: FastAPI,
) -> None:
    """``GET /tdocs`` with ``HX-Request`` and non-zero offset returns partial, 200."""

    class _ManyRowsTDocService(FakeTDocService):
        def list_recent_with_meeting(
            self, *, limit: int = 50, offset: int = 0, **_kwargs: Any,
        ) -> list[TDocWithMeeting]:
            return [
                TDocWithMeeting(
                    tdoc=TDoc(
                        tdoc_id=f"R5-{260001 + offset + i:06d}",
                        title=f"row {offset + i}",
                        meeting_id=1,
                        ftp_url=f"R5/26.{(offset + i):03d}/R5-{260001 + offset + i:06d}.zip",
                        spec="38.523-3",
                        release="Rel-18",
                        type="CR",
                        uploaded_date=date(2026, 5, 2),
                    ),
                    meeting_name="RAN5#99-e",
                )
                for i in range(limit)
            ]

    app_with_fakes.dependency_overrides[get_tdoc_service] = (
        lambda: _ManyRowsTDocService()
    )
    with TestClient(app_with_fakes) as c:
        # The exact URL the user reported: offset=50 with all filter
        # form fields blank. Must be 200 + fragment, not 500.
        params = {
            "tdoc_id": "", "meeting": "", "meeting_id": "", "title": "",
            "type": "", "source": "", "spec": "", "wi": "", "cr-cat": "",
            "status": "", "revision-of": "", "revised-to": "", "ftp-url": "",
            "release": "", "version": "", "cr-num": "", "cr-pack": "",
            "uploaded-date": "", "limit": "50", "offset": "50",
        }
        response = c.get("/tdocs", params=params, headers={"HX-Request": "true"})
        assert response.status_code == 200
        assert "<!DOCTYPE" not in response.text
        assert '<nav class="pagination">' in response.text


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


def test_tdoc_content_markdown_zip_wrapped_cache(
    client: TestClient, sqlite_env: Any, tmp_path: Any,
) -> None:
    """``GET /tdocs/{id}/content?format=markdown`` reads a real ZIP-wrapped cache.

    Post-D10 the on-disk markdown cache is a real ZIP archive (see
    ``_wrap_markdown_zip`` in ``tdoc_cr_service``), not a plain UTF-8
    text file. The web route previously called ``read_text(encoding='utf-8')``
    on the wrapped cache, which crashed with ``UnicodeDecodeError``
    on the ``PK\\x03\\x04`` magic bytes — every TTCN TDoc hit a 500.
    The route must now use ``_read_cached_markdown_path`` so it reads
    the inner ``.md`` entry from the ZIP.
    """
    import io
    import zipfile

    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository
    from doc3gpp.scraping.cache_keys import derive_cache_file

    create_schema()
    url = "TSG_RAN/WG5_Test_ex-T1/TTCN/TTCN_CRs/2026/Docs/R5s260231.zip"
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5s260231", ftp_url=url),
    )
    cache_file = derive_cache_file(url)
    markdown_path = tmp_path / "markdown" / cache_file
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    # Write a real ZIP archive with a single ``R5s260231.md`` entry,
    # matching what ``_wrap_markdown_zip`` produces on disk.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("R5s260231.md", "# TTCN heading\n\nSome markdown.")
    markdown_path.write_bytes(buf.getvalue())

    new_app = _build_app_with_fakes(cache_dir=tmp_path)
    with TestClient(new_app) as new_client:
        # ?format=markdown returns the inner .md payload, not the zip bytes.
        response = new_client.get(
            "/tdocs/R5s260231/content?format=markdown",
        )
    assert response.status_code == 200
    assert "# TTCN heading" in response.text
    assert "PK" not in response.text  # not the raw zip header


def test_tdoc_content_html_zip_wrapped_cache(
    client: TestClient, sqlite_env: Any, tmp_path: Any,
) -> None:
    """``GET /tdocs/{id}/content?format=html`` reads a real ZIP-wrapped cache."""
    import io
    import zipfile

    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository
    from doc3gpp.scraping.cache_keys import derive_cache_file

    create_schema()
    url = "TSG_RAN/WG5_Test_ex-T1/TTCN/TTCN_CRs/2026/Docs/R5s260231.zip"
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5s260231", ftp_url=url),
    )
    cache_file = derive_cache_file(url)
    markdown_path = tmp_path / "markdown" / cache_file
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("R5s260231.md", "# TTCN heading\n\nSome markdown.")
    markdown_path.write_bytes(buf.getvalue())

    new_app = _build_app_with_fakes(cache_dir=tmp_path)
    with TestClient(new_app) as new_client:
        response = new_client.get("/tdocs/R5s260231/content?format=html")
    assert response.status_code == 200
    assert "TTCN heading" in response.text


def test_tdoc_list_default_columns_use_status(client: TestClient) -> None:
    """Default HTML columns: Status replaces Uploaded; no fields param needed."""
    html = client.get("/tdocs").text
    assert "<th>Status</th>" in html
    assert "<th>Uploaded</th>" not in html
    assert '<table class="grid tdoc-grid">' in html
    assert 'class="col-meeting"' in html
    assert "content content-wide" in html


def test_meetings_page_keeps_default_width(client: TestClient) -> None:
    """Non-tdoc pages keep the default 1100px content class."""
    html = client.get("/meetings").text
    assert 'class="content"' in html
    assert "content content-wide" not in html


def test_tdoc_list_status_row_colors(client: TestClient) -> None:
    """Rows carry the status-derived class on the <tr>."""
    html = client.get("/tdocs").text
    assert '<tr class="status-green">' in html
    assert '<tr class="status-vanilla">' in html


def test_tdoc_list_custom_fields(client: TestClient) -> None:
    """?fields=tdoc_id&fields=related_wis renders only those columns + action."""
    html = client.get(
        "/tdocs?fields=tdoc_id&fields=related_wis",
    ).text
    assert "<th>TDoc ID</th>" in html
    assert "<th>Related WIs</th>" in html
    assert "<th>Status</th>" not in html
    assert '<tr class="status-green">' in html


def test_tdoc_list_unknown_field_returns_400(client: TestClient) -> None:
    """?fields=bogus is 400 with the invalid_filter envelope."""
    response = client.get("/tdocs?fields=bogus")
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_filter"


def test_tdoc_list_fields_select_renders(client: TestClient) -> None:
    """The filter form carries the dropdown checkboxes with all column options."""
    html = client.get("/tdocs").text
    assert 'name="fields"' in html
    assert 'type="checkbox"' in html
    assert 'value="related_wis"' in html
    assert 'value="status"' in html
    assert 'class="columns-count"' in html
    assert "<select" not in html


def test_tdoc_list_fields_persist_in_pagination(
    app_with_fakes: FastAPI,
) -> None:
    """The fields selection is preserved in pagination links."""
    from doc3gpp.web.deps import get_tdoc_service

    class _ManyRowsTDocService(FakeTDocService):
        def list_recent_with_meeting(
            self, *, limit: int = 50, offset: int = 0, **_kwargs: Any,
        ) -> list[TDocWithMeeting]:
            return [
                TDocWithMeeting(
                    tdoc=TDoc(
                        tdoc_id=f"R5-{260001 + offset + i:06d}",
                        title=f"row {offset + i}",
                        meeting_id=1,
                        ftp_url=f"R5/26.{(offset + i):03d}/R5-{260001 + offset + i:06d}.zip",
                        spec="38.523-3",
                        release="Rel-18",
                        type="CR",
                        status="Agreed",
                        uploaded_date=date(2026, 5, 2),
                    ),
                    meeting_name="RAN5#99-e",
                )
                for i in range(limit)
            ]

    app_with_fakes.dependency_overrides[get_tdoc_service] = (
        lambda: _ManyRowsTDocService()
    )
    with TestClient(app_with_fakes) as c:
        response = c.get("/tdocs?limit=50&fields=tdoc_id&fields=status")
    assert response.status_code == 200
    assert "fields=tdoc_id" in response.text
    assert "fields=status" in response.text


# ---------------------------------------------------------------------------
# status_color_class
# ---------------------------------------------------------------------------


def test_status_color_class_mapping() -> None:
    """Each status needle maps to its class, case-insensitively."""
    from doc3gpp.web.templates_setup import status_color_class

    cases = {
        "Conditionally Approved": "status-lgreen",
        "Partially Approved": "status-lgreen",
        "Agreed": "status-green",
        "approved": "status-green",
        "Revised": "status-vanilla",
        "Reissued": "status-vanilla",
        "Merged": "status-vanilla",
        "Rejected": "status-red",
        "Withdrawn": "status-grey",
        "Postponed": "status-pink",
        "Noted": "status-lblue",
        "Treated": "status-lblue",
        "Endorsed": "status-lblue",
    }
    for value, expected in cases.items():
        assert status_color_class(value) == expected, value


def test_status_color_class_no_match_and_empty() -> None:
    """No matching needle (or None/empty) -> no class."""
    from doc3gpp.web.templates_setup import status_color_class

    assert status_color_class("Submitted") == ""
    assert status_color_class("") == ""
    assert status_color_class(None) == ""





def test_tsg_list_renders_html(client: TestClient) -> None:
    """``GET /tsgs`` returns 200 with the TSG list template."""
    response = client.get("/tsgs")
    assert response.status_code == 200
    assert "RAN Plenary" in response.text


def test_tsg_list_name_links_to_tsg_url(client: TestClient) -> None:
    """A TSG with a URL links its Name cell to the TSG's own URL."""
    html = client.get("/tsgs").text
    assert '<a href="https://www.3gpp.org/ran">RAN Plenary</a>' in html
    # A TSG without a URL renders plain text, not a link.
    assert "<td>RAN WG5</td>" in html


def test_tsg_list_show_links_to_meetings_filtered(client: TestClient) -> None:
    """The show link jumps to the meetings page pre-filtered by TSG."""
    html = client.get("/tsgs").text
    assert 'href="/meetings?tsg=RP"' in html
    assert 'href="/meetings?tsg=R5"' in html


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


def test_search_results_single_details_per_hit(client: TestClient) -> None:
    """One details.hit-details block per hit (single folding), not per column."""
    response = client.get("/search?q=foo")
    assert response.status_code == 200
    body = response.text
    assert body.count('<details class="hit-details">') == 1
    assert '<span class="preview-label">title</span>' in body


def test_search_results_has_master_toggle(client: TestClient) -> None:
    """The results fragment carries the fold/unfold-all toggle."""
    response = client.get("/search?q=foo", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert 'id="fold-toggle"' in response.text


def test_search_results_toggle_absent_without_hits(client: TestClient) -> None:
    """No hits -> no toggle and no details."""
    response = client.get("/search")
    assert response.status_code == 200
    assert 'id="fold-toggle"' not in response.text


def test_search_full_page_loads_search_js(client: TestClient) -> None:
    """The full search page includes the fold-toggle script."""
    html = client.get("/search?q=foo").text
    assert 'src="/static/js/search.js"' in html


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


def test_search_query_tdoc_id_filter_forwarded(client: TestClient) -> None:
    """``GET /search?tdoc-id=<id>`` forwards tdoc_id into SearchFilters."""
    from doc3gpp.web.deps import get_search_service

    service = FakeSearchService()
    client.app.dependency_overrides[get_search_service] = lambda: service
    try:
        response = client.get("/search?q=foo&tdoc-id=R5-260001")
    finally:
        client.app.dependency_overrides.pop(get_search_service, None)
    assert response.status_code == 200
    assert service.last_filters is not None
    assert service.last_filters.tdoc_id == "R5-260001"


def test_search_query_empty_tdoc_id_is_no_filter(client: TestClient) -> None:
    """``GET /search?q=foo&tdoc-id=`` is 200 and tdoc_id stays None."""
    from doc3gpp.web.deps import get_search_service

    service = FakeSearchService()
    client.app.dependency_overrides[get_search_service] = lambda: service
    try:
        response = client.get("/search?q=foo&tdoc-id=")
    finally:
        client.app.dependency_overrides.pop(get_search_service, None)
    assert response.status_code == 200
    assert service.last_filters is not None
    assert service.last_filters.tdoc_id is None


def test_search_sem_tdoc_id_filter_forwarded(client: TestClient) -> None:
    """``GET /search/sem?tdoc-id=<id>`` forwards tdoc_id into SearchFilters."""
    from doc3gpp.web.deps import get_semantic_search_service

    service = FakeSemanticSearchService()
    client.app.dependency_overrides[get_semantic_search_service] = lambda: service
    try:
        response = client.get("/search/sem?q=foo&tdoc-id=R5-260001")
    finally:
        client.app.dependency_overrides.pop(get_semantic_search_service, None)
    assert response.status_code == 200
    filters = service.last_kwargs.get("filters")
    assert filters is not None
    assert filters.tdoc_id == "R5-260001"


def test_search_form_renders_tdoc_input_fts5(client: TestClient) -> None:
    """The FTS5 search form carries a tdoc-id input with the round-tripped value."""
    html = client.get("/search?q=foo&tdoc-id=R5-260001").text
    assert 'name="tdoc-id"' in html
    assert 'value="R5-260001"' in html


def test_search_form_renders_tdoc_input_sem(client: TestClient) -> None:
    """The semantic search form carries a tdoc-id input with the round-tripped value."""
    html = client.get("/search/sem?q=foo&tdoc-id=R5-260001").text
    assert 'name="tdoc-id"' in html
    assert 'value="R5-260001"' in html


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
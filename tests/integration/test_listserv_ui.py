"""Integration coverage for the read-only LISTSERV browser UI."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from doc3gpp.models.jobs import JobKind
from doc3gpp.models.listserv import (
    ArchivePage,
    ListservAttachment,
    ListservDetail,
    ListservMessage,
    parse_date_set,
)
from doc3gpp.scraping.listserv_source import ListservSource
from doc3gpp.services import factory
from doc3gpp.settings.loader import get_settings
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.repositories.jobs_sql import SQLAlchemyJobRepository
from doc3gpp.storage.repositories.listserv_sql import SQLAlchemyListservRepository
from doc3gpp.web.app import build_app
from doc3gpp.web.deps import get_listserv_search_service

LIST_ID = "3GPP_TSG_RAN_WG1"
PARSED_URL = f"https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L={LIST_ID}&P=1"
UNPARSED_URL = f"https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L={LIST_ID}&P=2"
EMPTY_URL = f"https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L={LIST_ID}&P=3"
NONSTANDARD_PORT_URL = (
    f"https://list.etsi.org:8443/scripts/wa.exe?A2=ind2610A&L={LIST_ID}&P=4"
)


def _message(url: str, subject: str, day: int) -> ListservMessage:
    return ListservMessage(
        url=url,
        list_id=LIST_ID,
        subject=subject,
        sender="Alice <alice@example.test>",
        date=datetime(2026, 10, day, 12, tzinfo=timezone.utc),
        date_precision="datetime",
        week="2026-10-W1",
    )


@pytest.fixture()
def client(sqlite_env, disable_auto_sync, monkeypatch) -> TestClient:
    def no_network(*args, **kwargs):
        raise AssertionError("LISTSERV UI read attempted network access")

    monkeypatch.setattr(ListservSource, "fetch", no_network)
    monkeypatch.setenv("DOC3GPP_SERVER__POLL_INTERVAL_SECONDS", "60")
    get_settings.cache_clear()
    create_schema("main")
    repo = SQLAlchemyListservRepository()
    repo.commit_page(
        ArchivePage(
            list_id=LIST_ID,
            url=f"https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L={LIST_ID}",
            period="2026-10-W1",
        ),
        [
            _message(PARSED_URL, "<script>alert(1)</script> Topic", 1),
            _message(UNPARSED_URL, "Unparsed topic", 15),
            _message(EMPTY_URL, "Parsed empty", 20),
            _message(NONSTANDARD_PORT_URL, "Nonstandard port", 21),
        ],
        parse_date_set(["= 2026-10-01"]),
        (),
    )
    repo.save_detail(
        ListservDetail(
            url=PARSED_URL,
            attachments=(
                ListservAttachment(
                    name="<script>attachment</script>",
                    url="javascript:alert(2)",
                ),
                ListservAttachment(
                    name="R1-2610793_report.zip",
                    url="https://list.etsi.org/attachments/report.zip",
                ),
            ),
            tdocs=("R1-2610793",),
            content=(
                "A" * 220
                + "\n<script>alert(3)</script>\n[unsafe](javascript:alert(4))"
            ),
            content_truncated=True,
            parser_version="test-parser",
            parsed_at=datetime(2026, 10, 2, tzinfo=timezone.utc),
        )
    )
    repo.save_detail(
        ListservDetail(
            url=EMPTY_URL,
            attachments=(),
            tdocs=(),
            content="",
            content_truncated=False,
            parser_version="test-parser",
            parsed_at=datetime(2026, 10, 21, tzinfo=timezone.utc),
        )
    )
    factory.build_listserv_search_service().upsert(PARSED_URL)
    with TestClient(build_app(get_settings())) as test_client:
        yield test_client


def test_listserv_page_renders_filters_preview_truncation_and_pagination(
    client: TestClient,
) -> None:
    response = client.get(
        "/listserv",
        params=[
            ("list_id", LIST_ID),
            ("date", "= 2026-10-01"),
            ("limit", "1"),
            ("offset", "0"),
        ],
    )

    assert response.status_code == 200

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(response.text, "html.parser")
    results = soup.select_one("#listserv-results ul.listserv-results")
    pagination = soup.select_one("#listserv-results nav.pagination")
    assert results is not None
    assert pagination is not None
    assert pagination.find_next_sibling("ul", class_="listserv-results") is results

    assert LIST_ID in response.text
    assert "Alice &lt;alice@example.test&gt;" in response.text
    assert 'action="/api/listserv/sync"' in response.text
    assert 'action="/api/listserv/parse"' in response.text
    assert 'action="/api/listserv/index"' in response.text
    assert "/jobs" in response.text
    assert "A" * 200 in response.text
    assert "Preview truncated" in response.text
    assert "Stored content was truncated" in response.text
    assert "next" in response.text
    assert "offset=1" in response.text


def test_listserv_show_escapes_upstream_values_and_preserves_parse_states(
    client: TestClient,
) -> None:
    api_record = client.get("/api/listserv/show", params={"url": PARSED_URL})
    assert api_record.status_code == 200, api_record.text
    parsed = client.get("/listserv/show", params={"url": PARSED_URL})

    assert parsed.status_code == 200, parsed.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in parsed.text
    assert "&lt;script&gt;alert(3)&lt;/script&gt;" in parsed.text
    assert "&lt;script&gt;attachment&lt;/script&gt;" in parsed.text
    assert "<script>" not in parsed.text
    assert 'href="javascript:' not in parsed.text
    assert "Stored content was truncated" in parsed.text
    assert "unsafe](javascript:alert(4))" in parsed.text
    assert "A" * 220 in parsed.text

    unparsed = client.get("/listserv/show", params={"url": UNPARSED_URL})
    empty = client.get("/listserv/show", params={"url": EMPTY_URL})
    assert "Not parsed" in unparsed.text
    assert "Parsed content is empty" not in unparsed.text
    assert "Parsed content is empty" in empty.text

    nonstandard_port = client.get(
        "/listserv/show", params={"url": NONSTANDARD_PORT_URL}
    )
    assert nonstandard_port.status_code == 200
    assert 'href="https://list.etsi.org:8443/' not in nonstandard_port.text
    assert "https://list.etsi.org:8443/" in nonstandard_port.text


def test_listserv_show_links_safe_attachment_urls_but_leaves_unsafe_schemes_unlinked(
    client: TestClient,
) -> None:
    response = client.get("/listserv/show", params={"url": PARSED_URL})

    assert response.status_code == 200, response.text
    safe_url = "https://list.etsi.org/attachments/report.zip"
    assert f'<a href="{safe_url}"><code>{safe_url}</code></a>' in response.text
    assert "<code>javascript:alert(2)</code>" in response.text
    assert 'href="javascript:' not in response.text


def test_listserv_ui_uses_shared_job_endpoints_and_payload_modes(
    client: TestClient,
) -> None:
    page = client.get("/listserv")
    assert 'name="text"' in page.text
    assert 'name="semantic"' in page.text
    assert 'name="date"' in page.text
    assert '/static/js/job_poller.js' in page.text
    assert '/static/js/listserv.js' in page.text

    sync = client.post(
        "/api/listserv/sync",
        json={"list_id": LIST_ID, "dates": ["= 2026-10-01"], "force": True},
    )
    parse = client.post(
        "/api/listserv/parse",
        json={"list_id": LIST_ID, "dates": ["= 2026-10-01"], "force": True},
    )
    rebuild = client.post(
        "/api/listserv/index",
        json={"rebuild": True, "rebuild_embeddings": False},
    )

    assert sync.status_code == parse.status_code == rebuild.status_code == 202
    jobs = SQLAlchemyJobRepository()
    assert jobs.get(sync.json()["job_id"]).kind is JobKind.LISTSERV_SYNC
    assert jobs.get(parse.json()["job_id"]).kind is JobKind.LISTSERV_PARSE
    assert jobs.get(rebuild.json()["job_id"]).kind is JobKind.LISTSERV_INDEX
    jobs.mark_failed(
        sync.json()["job_id"],
        error="1 source failure; 1 derived-index failure (source data was committed)",
        summary={
            "message": "1 source failure; 1 derived-index failure (source data was committed)",
            "processed": 1,
            "skipped": 0,
            "failed": 1,
            "index_failed": 1,
            "failures": {UNPARSED_URL: "HTTP 503"},
            "index_failures": {PARSED_URL: "vector index update failed (RuntimeError)"},
        },
    )
    job_view = client.get(f"/jobs/{sync.json()['job_id']}?format=html")
    assert f'href="/jobs/{sync.json()["job_id"]}"' in job_view.text
    assert "Derived-index failures" in job_view.text
    assert "Source failures" in job_view.text
    assert "HTTP 503" in job_view.text
    assert "source data was committed" in job_view.text

    filtered = client.get(
        "/listserv",
        params=[("subject", "%Topic%"), ("date", "= 2026-10-01")],
    )
    assert filtered.status_code == 200
    assert "Topic" in filtered.text
    assert "Preview truncated" in filtered.text

    fts = client.get("/listserv", params={"text": "Topic"})
    assert fts.status_code == 200
    assert "Search match (subject)" in fts.text

    calls = []

    class CapturingSearchService:
        def search(self, *, text, semantic, filters):
            calls.append((text, semantic, filters))
            return []

    client.app.dependency_overrides[get_listserv_search_service] = (
        lambda: CapturingSearchService()
    )
    semantic = client.get(
        "/listserv",
        params=[("semantic", "meaning"), ("date", ">= 2026-10-01")],
    )
    hybrid = client.get(
        "/listserv",
        params=[("text", "literal"), ("semantic", "meaning"), ("list_id", LIST_ID)],
    )
    assert semantic.status_code == hybrid.status_code == 200
    assert calls[0][0:2] == (None, "meaning")
    assert calls[0][2].dates is not None
    assert calls[1][0:2] == ("literal", "meaning")
    assert calls[1][2].list_id == LIST_ID



def test_empty_listserv_store_allows_entering_sync_and_parse_list_ids(
    sqlite_env, disable_auto_sync, monkeypatch
) -> None:
    def no_network(*args, **kwargs):
        raise AssertionError("LISTSERV UI read attempted network access")

    monkeypatch.setattr(ListservSource, "fetch", no_network)
    monkeypatch.setenv("DOC3GPP_SERVER__POLL_INTERVAL_SECONDS", "60")
    get_settings.cache_clear()
    create_schema("main")

    with TestClient(build_app(get_settings())) as client:
        response = client.get("/listserv")

    assert response.status_code == 200
    assert "No stored lists" in response.text
    for form_id in ("listserv-sync-form", "listserv-parse-form"):
        form = response.text.split(f'id="{form_id}"', 1)[1].split("</form>", 1)[0]
        assert '<input type="text"' in form
        assert 'name="list_id"' in form
        assert 'name="since"' in form
        assert 'name="until"' in form
        assert 'name="dates"' not in form
        assert "required" in form


@pytest.mark.parametrize("blank_value", ["", "   "], ids=["empty", "whitespace"])
@pytest.mark.parametrize(
    ("endpoint", "json_response"),
    [("/listserv", False), ("/api/listserv/search", True)],
    ids=["page", "api"],
)
@pytest.mark.parametrize(
    ("search_field", "search_value"),
    [("text", "Topic"), ("subject", "%alert(1)%"), ("tdocs", "R1-2610793")],
    ids=["text", "subject", "tdocs"],
)
def test_listserv_http_search_ignores_blank_filter_inputs(
    client: TestClient,
    blank_value: str,
    endpoint: str,
    json_response: bool,
    search_field: str,
    search_value: str,
) -> None:
    params = {
        "list_id": blank_value,
        "sender": blank_value,
        "subject": blank_value,
        "tdocs": blank_value,
        "date": blank_value,
        "text": blank_value,
        "semantic": blank_value,
        "limit": "20",
    }
    params[search_field] = search_value

    response = client.get(endpoint, params=params)

    assert response.status_code == 200, response.text
    if json_response:
        assert [item["url"] for item in response.json()] == [PARSED_URL]
    else:
        assert "1 result" in response.text
        assert "Topic" in response.text


def test_listserv_search_form_uses_rich_list_id_input_and_stored_suggestions(
    client: TestClient,
) -> None:
    from bs4 import BeautifulSoup

    repository = SQLAlchemyListservRepository()
    messages = []
    for list_id, subject in (("SA1", "SA1 handover"), ("SA2", "SA2 handover"), ("CT1", "CT1 handover")):
        message_url = (
            "https://list.etsi.org/scripts/wa.exe?A2=ind2610A"
            f"&L={list_id}&P=1"
        )
        page = ArchivePage(
            list_id=list_id,
            url=(
                "https://list.etsi.org/scripts/wa.exe?A1=ind2610A"
                f"&L={list_id}"
            ),
            period="2026-10-W1",
        )
        repository.commit_page(
            page,
            [
                ListservMessage(
                    url=message_url,
                    list_id=list_id,
                    subject=subject,
                    sender="Alice <alice@example.test>",
                    date=datetime(2026, 10, 5, 12, tzinfo=timezone.utc),
                    date_precision="datetime",
                    week="2026-10-W1",
                )
            ],
            parse_date_set(["= 2026-10-05"]),
            (),
        )
        messages.append((message_url, subject))

    response = client.get("/listserv", params={"list_id": "SA%", "limit": 150})
    assert response.status_code == 200
    soup = BeautifulSoup(response.text, "html.parser")
    search_form = soup.find("form", attrs={"method": "get", "action": "/listserv"})
    assert search_form is not None
    list_input = search_form.find("input", attrs={"name": "list_id"})
    assert list_input is not None
    assert list_input.get("type") == "text"
    assert list_input.get("list") == "listserv-list-id-suggestions"
    assert list_input.get("value") == "SA%"

    suggestions = soup.find("datalist", id="listserv-list-id-suggestions")
    assert suggestions is not None
    suggested_ids = {option.get("value") for option in suggestions.find_all("option")}
    assert {"SA1", "SA2", "CT1"} <= suggested_ids

    rendered_subjects = {
        heading.get_text(strip=True)
        for heading in soup.select("ul.listserv-results h3")
    }
    assert rendered_subjects == {"SA1 handover", "SA2 handover"}


def test_listserv_search_form_preserves_rich_tokens_and_ignores_blank(
    client: TestClient,
) -> None:
    calls = []

    class CapturingSearchService:
        def search(self, *, text, semantic, filters):
            calls.append(filters)
            return []

    client.app.dependency_overrides[get_listserv_search_service] = (
        lambda: CapturingSearchService()
    )
    for value in ("SA%", "!SA%", "null", "not-null"):
        response = client.get("/listserv", params=[("list_id", value)])
        assert response.status_code == 200
        assert calls[-1].list_id == value

    for value in ("", "   "):
        response = client.get("/listserv", params=[("list_id", value)])
        assert response.status_code == 200
        assert calls[-1].list_id is None

"""Offline ASGI integration tests for the LISTSERV HTTP surface."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from doc3gpp.models.jobs import JobKind
from doc3gpp.models.listserv import (
    ArchivePage,
    ListservDetail,
    ListservFilters,
    ListservMessage,
    parse_date_set,
)
from doc3gpp.models.listserv_payloads import (
    listserv_record_payload,
    listserv_search_payload,
)
from doc3gpp.scraping.listserv_source import ListservSource
from doc3gpp.services import factory
from doc3gpp.settings.loader import get_settings
from doc3gpp.settings.schema import SearchSettings, SemanticSearchSettings, Settings
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.repositories.jobs_sql import SQLAlchemyJobRepository
from doc3gpp.storage.repositories.listserv_search_sql import (
    SQLAlchemyListservSearchRepository,
)
from doc3gpp.storage.repositories.listserv_sql import SQLAlchemyListservRepository
from doc3gpp.web.app import build_app
from doc3gpp.web.deps import get_listserv_search_service
from tests.integration.test_listserv_semantic_sqlite import (
    _DeterministicEmbedder,
    _require_task8_modules,
)

LIST_ID = "3GPP_TSG_RAN_WG1"
PARSED_URL = f"https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L={LIST_ID}&P=1"
UNPARSED_URL = f"https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L={LIST_ID}&P=2"
EMPTY_URL = f"https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L={LIST_ID}&P=3"
MISSING_URL = f"https://list.etsi.org/scripts/wa.exe?A2=ind2611A&L={LIST_ID}&P=9"


def _message(url: str, subject: str, day: int, month: int = 10) -> ListservMessage:
    return ListservMessage(
        url=url,
        list_id=LIST_ID,
        subject=subject,
        sender="Alice <x>",
        date=datetime(2026, month, day, 12, tzinfo=timezone.utc),
        date_precision="datetime",
        week=f"2026-{month:02d}-W1",
        subject_tdocs=("R1-2610793",) if "R1-" in subject else (),
    )


@pytest.fixture()
def client(sqlite_env, disable_auto_sync, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(ListservSource, "fetch", boom)
    # Keep enqueued jobs queued: the worker's next poll is far away.
    monkeypatch.setenv("DOC3GPP_SERVER__POLL_INTERVAL_SECONDS", "60")
    get_settings.cache_clear()
    create_schema("main")
    repo = SQLAlchemyListservRepository()
    page = ArchivePage(
        list_id=LIST_ID,
        url=f"https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L={LIST_ID}",
        period="2026-10-W1",
    )
    repo.commit_page(
        page,
        [
            _message(PARSED_URL, "Topic R1-2610793", 1),
            _message(UNPARSED_URL, "Other thing", 15),
            _message(EMPTY_URL, "Empty body", 20),
        ],
        parse_date_set(["= 2026-10-01"]),
        (),
    )
    repo.save_detail(
        ListservDetail(
            url=PARSED_URL, attachments=(), tdocs=(), content="b" * 250,
            content_truncated=False, parser_version="1",
            parsed_at=datetime(2026, 10, 2, tzinfo=timezone.utc),
        )
    )
    repo.save_detail(
        ListservDetail(
            url=EMPTY_URL, attachments=(), tdocs=(), content="",
            content_truncated=False, parser_version="1",
            parsed_at=datetime(2026, 10, 21, tzinfo=timezone.utc),
        )
    )
    with TestClient(build_app(get_settings())) as c:
        yield c


def test_http_show_matches_sdk_payload(client):
    service = factory.build_listserv_service()
    for url in (PARSED_URL, UNPARSED_URL):
        response = client.get("/api/listserv/show", params={"url": url})
        assert response.status_code == 200
        assert response.json() == listserv_record_payload(service.show(url))
    assert client.get("/api/listserv/show", params={"url": PARSED_URL}).json()[
        "content"
    ] == "b" * 250
    assert client.get("/api/listserv/show", params={"url": UNPARSED_URL}).json()[
        "content"
    ] is None


def test_http_show_empty_content_matches_sdk_and_differs_from_unparsed(client):
    service = factory.build_listserv_service()
    response = client.get("/api/listserv/show", params={"url": EMPTY_URL})
    assert response.status_code == 200
    assert response.json() == listserv_record_payload(service.show(EMPTY_URL))
    assert response.json()["content"] == ""
    unparsed = client.get("/api/listserv/show", params={"url": UNPARSED_URL}).json()
    assert unparsed["content"] is None

    results = client.get(
        "/api/listserv/search", params={"subject": "Empty body"}
    ).json()
    filters = ListservFilters(subject="Empty body")
    expected = [
        listserv_search_payload(r)
        for r in factory.build_listserv_search_service().search(filters=filters)
    ]
    assert results == expected and [r["url"] for r in results] == [EMPTY_URL]


def test_http_show_not_found_is_404(client):
    response = client.get("/api/listserv/show", params={"url": MISSING_URL})
    assert response.status_code == 404
    assert response.json()["error"] == "listserv_message_not_found"


def test_http_lists_matches_sdk(client):
    from doc3gpp.storage.db.models import ListservListORM
    from doc3gpp.storage.db.session import get_session_factory

    with get_session_factory()() as session:
        session.add(ListservListORM(list_id="LEGACY_NULL_LIST"))
        session.commit()

    response = client.get("/api/listserv/lists")
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"list_ids", "lists"}

    records = factory.build_listserv_service().list_lists()
    expected_lists = [
        {
            "list_id": record.list_id,
            "tsg": record.tsg,
            "description": record.description,
        }
        for record in records
    ]
    assert payload == {
        "list_ids": [record.list_id for record in records],
        "lists": expected_lists,
    }
    assert payload["list_ids"] == [entry["list_id"] for entry in payload["lists"]]
    assert payload["list_ids"] == sorted(payload["list_ids"])
    assert LIST_ID in payload["list_ids"]

    lists = {entry["list_id"]: entry for entry in payload["lists"]}
    assert lists["3GPP_TSG_CT"] == {
        "list_id": "3GPP_TSG_CT",
        "tsg": "CP",
        "description": "3GPP_TSG_CT - Core Network and Terminals",
    }
    assert lists["3GPP_TSG_RAN_WG6"] == {
        "list_id": "3GPP_TSG_RAN_WG6",
        "tsg": "R6",
        "description": "3GPP_TSG_RAN_WG6 Close 14/08/2020 JMS ticket 2020-103474",
    }
    assert lists["LEGACY_NULL_LIST"] == {
        "list_id": "LEGACY_NULL_LIST",
        "tsg": None,
        "description": None,
    }


def test_http_search_matches_sdk_with_repeated_dates_and_paging(client):
    service = factory.build_listserv_search_service()
    params = [("date", ">= 2026-10-10"), ("date", "<= 2026-10-16"), ("list_id", LIST_ID)]
    response = client.get("/api/listserv/search", params=params)
    assert response.status_code == 200
    filters = ListservFilters(
        list_id=LIST_ID,
        dates=parse_date_set([">= 2026-10-10", "<= 2026-10-16"]),
    )
    expected = [listserv_search_payload(r) for r in service.search(filters=filters)]
    assert response.json() == expected
    assert [item["url"] for item in expected] == [UNPARSED_URL]
    assert expected[0]["content_preview"] is None

    both = client.get("/api/listserv/search", params={"limit": 1, "offset": 0}).json()
    rest = client.get("/api/listserv/search", params={"limit": 1, "offset": 1}).json()
    assert len(both) == 1 and len(rest) == 1 and both != rest
    assert client.get(
        "/api/listserv/search", params={"date": "= 2030-01-01"}
    ).json() == []
    parsed = client.get("/api/listserv/search", params={"subject": "Topic%"}).json()
    assert parsed[0]["content_preview"] == "b" * 200
    assert "content" not in parsed[0]


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 151},
        {"limit": -1},
        {"offset": -1},
        {"date": "not a date"},
    ],
)
def test_http_search_bad_filters_are_400(client, params):
    response = client.get("/api/listserv/search", params=params)
    assert response.status_code == 400
    assert "detail" in response.json()


def test_http_search_text_without_fts_is_error_not_network(client, monkeypatch):
    from doc3gpp.settings.schema import SearchSettings, Settings

    monkeypatch.setattr(
        factory, "get_settings", lambda: Settings(search=SearchSettings(enabled=False))
    )
    # Fresh app so the lazily-built service sees the patched settings.
    with TestClient(build_app(Settings(search=SearchSettings(enabled=False)))) as c:
        response = c.get("/api/listserv/search", params={"text": "topic"})
    assert response.status_code >= 400


def test_http_index_status_matches_sdk(client):
    response = client.get("/api/listserv/index/status")
    assert response.status_code == 200
    assert response.json() == factory.build_listserv_index_service().status().to_dict()


def _job(job_id):
    return SQLAlchemyJobRepository().get(job_id)


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("sync", JobKind.LISTSERV_SYNC),
        ("parse", JobKind.LISTSERV_PARSE),
    ],
)
def test_post_sync_parse_enqueue_normalized_params(client, path, kind):
    response = client.post(
        f"/api/listserv/{path}",
        json={"list_id": " 3gpp_tsg_ran_wg1 ", "dates": [">= 2026-10-01"], "force": True},
    )
    assert response.status_code == 202
    job = _job(response.json()["job_id"])
    assert job.kind is kind
    assert dict(job.params) == {
        "list_id": LIST_ID,
        "dates": [">= 2026-10-01"],
        "force": True,
    }


def test_post_index_enqueues_distinct_kind(client):
    response = client.post(
        "/api/listserv/index", json={"rebuild": True, "batch": 5, "stale_only": True}
    )
    assert response.status_code == 202
    job = _job(response.json()["job_id"])
    assert job.kind is JobKind.LISTSERV_INDEX
    assert job.params["rebuild"] is True and job.params["batch"] == 5
    assert job.params["stale_only"] is True and job.params["rebuild_all"] is False


@pytest.mark.parametrize("path", ["sync", "parse"])
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"list_id": ""},
        {"list_id": "bad id/with space"},
        {"list_id": LIST_ID, "dates": ["garbage"]},
    ],
)
def test_post_invalid_requests_create_no_job(client, path, body):
    response = client.post(f"/api/listserv/{path}", json=body)
    assert response.status_code in (400, 422)
    assert SQLAlchemyJobRepository().list(limit=10) == []


def test_post_index_invalid_flags_create_no_job(client):
    response = client.post(
        "/api/listserv/index", json={"rebuild_all": True, "rebuild": True}
    )
    assert response.status_code == 400
    assert SQLAlchemyJobRepository().list(limit=10) == []


def test_http_successful_search_modes_match_sdk_payload_without_network(
    sqlite_env, disable_auto_sync, monkeypatch
):
    VectorRepository, SemanticService, _, SearchService, _ = (
        _require_task8_modules()
    )
    monkeypatch.setenv("DOC3GPP_SERVER__POLL_INTERVAL_SECONDS", "60")
    get_settings.cache_clear()

    fetched_urls = []

    def forbid_network(_source, url):
        fetched_urls.append(url)
        raise AssertionError(f"search attempted network access: {url}")

    monkeypatch.setattr(ListservSource, "fetch", forbid_network)
    create_schema("main")
    repository = SQLAlchemyListservRepository()
    page = ArchivePage(
        list_id=LIST_ID,
        url=f"https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L={LIST_ID}",
        period="2026-10-W1",
    )
    outside_date_url = (
        f"https://list.etsi.org/scripts/wa.exe?A2=ind2609A&L={LIST_ID}&P=9"
    )
    third_url = f"https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L={LIST_ID}&P=4"
    other_list_id = "3GPP_TSG_RAN_WG2"
    other_list_url = (
        "https://list.etsi.org/scripts/wa.exe?A2=ind2610A&"
        f"L={other_list_id}&P=5"
    )
    repository.commit_page(
        page,
        [
            _message(PARSED_URL, "Archive marker parsed R1-2610793", 3),
            _message(UNPARSED_URL, "Archive marker unparsed", 1),
            _message(EMPTY_URL, "Archive marker parsed middle", 2),
            _message(third_url, "Archive marker parsed third", 4),
            _message(outside_date_url, "Archive marker outside date", 30, month=9),
        ],
        parse_date_set(["= 2026-10-04"]),
        (),
    )
    other_page = ArchivePage(
        list_id=other_list_id,
        url=(
            "https://list.etsi.org/scripts/wa.exe?A1=ind2610A&"
            f"L={other_list_id}"
        ),
        period="2026-10-W1",
    )
    repository.commit_page(
        other_page,
        [
            ListservMessage(
                url=other_list_url,
                list_id=other_list_id,
                subject="Archive marker other list",
                sender="Alice <x>",
                date=datetime(2026, 10, 3, 12, tzinfo=timezone.utc),
                date_precision="datetime",
                week="2026-10-W1",
                subject_tdocs=(),
            )
        ],
        parse_date_set(["= 2026-10-03"]),
        (),
    )

    details = {
        PARSED_URL: ("beamforming semantic body " * 12, True),
        EMPTY_URL: ("beamforming semantic middle body " * 10, False),
        third_url: ("beamforming semantic third body " * 10, False),
        outside_date_url: ("beamforming semantic outside date " * 10, False),
        other_list_url: ("beamforming semantic other list " * 10, False),
    }
    for url, (content, truncated) in details.items():
        repository.save_detail(
            ListservDetail(
                url=url,
                attachments=(),
                tdocs=(),
                content=content,
                content_truncated=truncated,
                parser_version="1",
                parsed_at=datetime(2026, 10, 5, tzinfo=timezone.utc),
            )
        )

    settings = Settings(
        search=SearchSettings(enabled=True),
        semantic_search=SemanticSearchSettings(
            enabled=True,
            chunk_size=64,
            chunk_overlap=0,
        ),
    )
    embedder = _DeterministicEmbedder()
    fts_repository = SQLAlchemyListservSearchRepository(
        source_repository=repository,
        settings=settings,
    )
    fts_repository.rebuild()
    vector_repository = VectorRepository(
        expected_model=embedder.model_name,
        expected_dim=embedder.dim,
        chunk_settings=settings.semantic_search,
    )
    semantic_service = SemanticService(
        embedder=embedder,
        vector_repo=vector_repository,
        fts_repository=fts_repository,
        source_repository=repository,
        settings=settings,
    )
    for url in details:
        record = repository.get(url)
        assert record is not None
        assert semantic_service.index_record(record) >= 1
    search_service = SearchService(
        repository=repository,
        fts_repository=fts_repository,
        semantic_service=semantic_service,
        settings=settings,
    )

    app = build_app(settings)
    app.dependency_overrides[get_listserv_search_service] = lambda: search_service
    date_values = [">= 2026-10-01", "<= 2026-10-04"]
    search_dates = parse_date_set(date_values)
    cases = (
        ("fts", "archive", None, 1),
        ("semantic", None, "beamforming", 1),
        ("hybrid", "archive", "beamforming", 1),
        ("filter", None, None, 3),
    )

    def compared_fields(payloads):
        return [
            (
                payload["url"],
                payload["score"],
                payload["matched_source"],
                payload["snippet"],
                payload["content_truncated"],
                payload["preview_truncated"],
            )
            for payload in payloads
        ]

    with TestClient(app) as http_client:
        responses = {}
        for mode, text_query, semantic_query, limit in cases:
            filters = ListservFilters(
                list_id=LIST_ID,
                dates=search_dates,
                limit=limit,
                offset=1,
            )
            sdk_results = search_service.search(
                text=text_query,
                semantic=semantic_query,
                filters=filters,
            )
            expected = [listserv_search_payload(result) for result in sdk_results]
            params = [
                ("list_id", LIST_ID),
                ("date", date_values[0]),
                ("date", date_values[1]),
                ("limit", str(limit)),
                ("offset", "1"),
            ]
            if text_query is not None:
                params.append(("text", text_query))
            if semantic_query is not None:
                params.append(("semantic", semantic_query))

            response = http_client.get("/api/listserv/search", params=params)
            assert response.status_code == 200, f"{mode}: {response.text}"
            actual = response.json()
            assert expected, mode
            assert [item["url"] for item in actual] == [
                item["url"] for item in expected
            ], mode
            assert compared_fields(actual) == compared_fields(expected), mode
            assert actual == expected, mode
            responses[mode] = actual

    filter_urls = {item["url"] for item in responses["filter"]}
    assert UNPARSED_URL in filter_urls
    assert PARSED_URL in filter_urls
    assert fetched_urls == []



def test_http_listserv_search_corruption_uses_listserv_rebuild_hint(
    client, monkeypatch
) -> None:
    from doc3gpp.models.search import SearchIndexCorruptError
    from doc3gpp.services.listserv_search import ListservSearchService

    def corrupt(*_args, **_kwargs):
        raise SearchIndexCorruptError("broken LISTSERV FTS index")

    monkeypatch.setattr(ListservSearchService, "search", corrupt)

    response = client.get("/api/listserv/search", params={"text": "handover"})

    assert response.status_code == 500
    assert response.json() == {
        "error": "search_index_corrupt",
        "detail": "broken LISTSERV FTS index",
        "resource": "listserv",
        "hint": "run: doc3gpp listserv index --rebuild",
    }


@pytest.mark.parametrize(
    ("list_filter", "expected_urls"),
    [
        ("3GPP_TSG_RAN_WG%", {PARSED_URL, UNPARSED_URL, EMPTY_URL}),
        ("!3GPP_TSG_RAN_WG%", set()),
        ("null", set()),
        ("not-null", {PARSED_URL, UNPARSED_URL, EMPTY_URL}),
    ],
)
def test_http_search_preserves_rich_list_id_filter_grammar(
    client, list_filter, expected_urls
) -> None:
    response = client.get(
        "/api/listserv/search",
        params={"list_id": list_filter, "limit": 150},
    )

    assert response.status_code == 200, response.text
    assert {result["url"] for result in response.json()} == expected_urls

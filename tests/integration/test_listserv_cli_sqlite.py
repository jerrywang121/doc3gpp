"""Offline SQLite CLI integration tests for ``doc3gpp listserv``."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.models.listserv import (
    ArchivePage,
    ListservDetail,
    ListservFilters,
    ListservMessage,
    parse_date_set,
)
from doc3gpp.models.listserv_payloads import listserv_search_payload
from doc3gpp.scraping.listserv_source import FetchedPage, ListservSource
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.repositories.listserv_search_sql import (
    SQLAlchemyListservSearchRepository,
)
from doc3gpp.storage.repositories.listserv_sql import SQLAlchemyListservRepository
from tests.integration.test_listserv_parse_sqlite import _detail_html
from tests.integration.test_listserv_semantic_sqlite import (
    _DeterministicEmbedder,
    _make_message,
    _make_page,
    _require_task8_modules,
)

runner = CliRunner()
URL = "https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=3GPP_TSG_RAN_WG1&P=1"


@pytest.fixture()
def stored(sqlite_env, disable_auto_sync, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(ListservSource, "fetch", boom)
    create_schema("main")
    repo = SQLAlchemyListservRepository()
    page = ArchivePage(
        list_id="3GPP_TSG_RAN_WG1",
        url="https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=3GPP_TSG_RAN_WG1",
        period="2026-10-W1",
    )
    msg = ListservMessage(
        url=URL,
        list_id="3GPP_TSG_RAN_WG1",
        subject="Topic R1-2610793",
        sender="Alice <x>",
        date=datetime(2026, 10, 1, 12, tzinfo=timezone.utc),
        date_precision="datetime",
        week="2026-10-W1",
        subject_tdocs=("R1-2610793",),
    )
    repo.commit_page(page, [msg], parse_date_set(["= 2026-10-01"]), ())
    repo.save_detail(
        ListservDetail(
            url=URL, attachments=(), tdocs=(), content="b" * 250,
            content_truncated=False, parser_version="1",
            parsed_at=datetime(2026, 10, 2, tzinfo=timezone.utc),
        )
    )
    return repo


def test_list_returns_distinct_ids(stored):
    from doc3gpp.storage.db.models import ListservListORM
    from doc3gpp.storage.db.session import get_session_factory

    with get_session_factory()() as session:
        session.add(ListservListORM(list_id="LEGACY_NULL_LIST"))
        session.commit()

    result = runner.invoke(app, ["listserv", "list"])
    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert lines[0].split("\t") == ["list_id", "tsg", "description"]
    rows = [line.split("\t") for line in lines[1:]]
    ids = [row[0] for row in rows]
    assert ids == sorted(ids)
    assert {"3GPP_TSG_RAN_WG1", "LEGACY_NULL_LIST"} <= set(ids)
    assert [
        "3GPP_TSG_CT_WG3_108E_MAIN",
        "C3",
        "3GPP_TSG_CT_WG3_108e_main Closed 02/12/2020, #done by JMS by request Anna ticket n°2020-105066",
    ] in rows
    assert ["LEGACY_NULL_LIST", "-", "-"] in rows



def test_list_lists_and_ids_include_seeded_catalog_without_messages_and_preserve_null_metadata(
    sqlite_env, disable_auto_sync
):
    from dataclasses import FrozenInstanceError

    import pytest

    import doc3gpp.models.listserv as listserv_models
    from doc3gpp.models.listserv import ListservFilters
    from doc3gpp.services.listserv_service import ListservService
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.db.models import ListservListORM
    from doc3gpp.storage.db.session import get_session_factory
    from doc3gpp.storage.repositories.listserv_sql import SQLAlchemyListservRepository

    create_schema("main")
    repository = SQLAlchemyListservRepository()
    service = ListservService(repository=repository)

    assert callable(getattr(repository, "list_lists", None))
    assert callable(getattr(service, "list_lists", None))
    assert hasattr(listserv_models, "ListservList")
    list_type = listserv_models.ListservList

    seeded = repository.list_lists()
    seeded_ids = [record.list_id for record in seeded]
    assert len(seeded) == 97
    assert seeded_ids == sorted(seeded_ids)
    assert service.list_lists() == seeded
    assert service.list_ids() == seeded_ids
    assert repository.select(ListservFilters()) == []

    with get_session_factory()() as session:
        session.add(ListservListORM(list_id="LEGACY_NULL_LIST"))
        session.commit()

    records = service.list_lists()
    legacy = next(record for record in records if record.list_id == "LEGACY_NULL_LIST")
    assert isinstance(legacy, list_type)
    assert legacy == list_type(
        list_id="LEGACY_NULL_LIST", tsg=None, description=None
    )
    assert service.list_ids() == [record.list_id for record in records]
    assert service.list_ids() == sorted(service.list_ids())
    with pytest.raises(FrozenInstanceError):
        legacy.tsg = "R1"


def test_filter_only_search_json_preview_and_nullable(stored):
    result = runner.invoke(
        app, ["listserv", "search", "--subject", "Topic%", "--format", "json"]
    )
    assert result.exit_code == 0, result.output
    (item,) = json.loads(result.output)
    assert item["url"] == URL
    assert item["content_preview"] == "b" * 200 and item["preview_truncated"] is True
    assert item["score"] is None and item["snippet"] is None
    assert "content" not in item


def test_search_text_without_fts_is_explicit_error(stored, monkeypatch):
    from doc3gpp.services import factory
    from doc3gpp.settings.schema import SearchSettings, Settings

    monkeypatch.setattr(
        factory, "get_settings", lambda: Settings(search=SearchSettings(enabled=False))
    )
    result = runner.invoke(app, ["listserv", "search", "--text", "topic"])
    assert result.exit_code == 1


def test_show_all_content_and_not_found(stored):
    result = runner.invoke(app, ["listserv", "show", URL, "--format", "json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["content"] == "b" * 250
    missing = runner.invoke(app, ["listserv", "show", "https://nope"])
    assert missing.exit_code == 1 and "not found" in missing.output.lower()


def test_index_status_no_download(stored):
    result = runner.invoke(app, ["listserv", "index"])
    assert result.exit_code == 0, result.output
    assert "index status" in result.output.lower()


def test_index_rebuild_no_download(stored, monkeypatch):
    from doc3gpp.services import factory
    from doc3gpp.settings.schema import SearchSettings, SemanticSearchSettings, Settings

    monkeypatch.setattr(
        factory,
        "get_settings",
        lambda: Settings(
            search=SearchSettings(enabled=True),
            semantic_search=SemanticSearchSettings(enabled=False),
        ),
    )
    result = runner.invoke(app, ["listserv", "index", "--rebuild", "--quiet"])
    assert result.exit_code == 0, result.output
    assert "network access" not in result.output

    search = runner.invoke(
        app, ["listserv", "search", "--text", "Topic", "--format", "json"]
    )
    assert search.exit_code == 0, search.output
    assert [item["url"] for item in json.loads(search.output)] == [URL]


def test_index_rebuild_without_fts_reports_actionable_failure(stored, monkeypatch):
    from doc3gpp.services import factory
    from doc3gpp.settings.schema import SearchSettings, SemanticSearchSettings, Settings

    monkeypatch.setattr(
        factory,
        "get_settings",
        lambda: Settings(
            search=SearchSettings(enabled=False),
            semantic_search=SemanticSearchSettings(enabled=False),
        ),
    )
    result = runner.invoke(app, ["listserv", "index", "--rebuild", "--quiet"])
    assert result.exit_code == 1, result.output
    assert "LISTSERV FTS5 index is not available" in result.output


def test_hybrid_search_cli_deep_offset_matches_sdk_payload(stored, monkeypatch):
    (
        vector_repository_type,
        semantic_service_type,
        _,
        search_service_type,
        _,
    ) = _require_task8_modules()
    from doc3gpp.services import factory
    from doc3gpp.settings.schema import SearchSettings, SemanticSearchSettings, Settings

    messages = [
        _make_message(
            f"https://list.etsi.org/msg/cli-fts-only-{index:03d}",
            subject=f"CLI paginationmarker FTS-only record {index:03d}",
            dt=datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc),
            subject_tdocs=(),
        )
        for index in range(205)
    ]
    stored.commit_page(
        _make_page(), messages, parse_date_set(["= 2026-10-01"]), ()
    )

    settings = Settings(
        search=SearchSettings(enabled=True),
        semantic_search=SemanticSearchSettings(
            enabled=True, chunk_size=64, chunk_overlap=0
        ),
    )
    fts_repository = SQLAlchemyListservSearchRepository(settings=settings)
    fts_repository.rebuild()
    embedder = _DeterministicEmbedder()
    vector_repository = vector_repository_type(
        expected_model=embedder.model_name,
        expected_dim=embedder.dim,
        chunk_settings=settings.semantic_search,
    )
    semantic_service = semantic_service_type(
        embedder=embedder,
        vector_repo=vector_repository,
        fts_repository=fts_repository,
        source_repository=stored,
        settings=settings,
    )
    search_service = search_service_type(
        repository=stored,
        fts_repository=fts_repository,
        semantic_service=semantic_service,
        settings=settings,
    )
    sdk_results = search_service.search(
        text="paginationmarker",
        semantic="beamforming",
        filters=ListservFilters(limit=7, offset=170),
    )
    expected_payload = [listserv_search_payload(item) for item in sdk_results]
    assert len(expected_payload) == 7

    monkeypatch.setattr(
        factory,
        "build_listserv_search_service",
        lambda *args, **kwargs: search_service,
    )
    # Support either a module-level builder or a local factory import in the CLI adapter.
    from doc3gpp import cli_listserv

    monkeypatch.setattr(
        cli_listserv,
        "build_listserv_search_service",
        lambda *args, **kwargs: search_service,
        raising=False,
    )
    result = runner.invoke(
        app,
        [
            "listserv",
            "search",
            "--text",
            "paginationmarker",
            "--semantic",
            "beamforming",
            "--offset",
            "170",
            "--limit",
            "7",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    actual_payload = json.loads(result.output)
    assert len(actual_payload) == 7
    assert [item["url"] for item in actual_payload] == [
        item["url"] for item in expected_payload
    ]
    assert [item["score"] for item in actual_payload] == [
        item["score"] for item in expected_payload
    ]
    assert [item["matched_source"] for item in actual_payload] == [
        item["matched_source"] for item in expected_payload
    ]
    assert actual_payload == expected_payload


def test_parse_cli_uses_exact_list_id_with_underscore(stored, monkeypatch):
    team_a_url = "https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=TEAM_A&P=1"
    team_x_url = "https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=TEAMXA&P=2"
    for list_id, url in (("TEAM_A", team_a_url), ("TEAMXA", team_x_url)):
        stored.commit_page(
            ArchivePage(
                list_id=list_id,
                url=f"https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L={list_id}",
                period="2026-10-W1",
            ),
            [
                _make_message(
                    url,
                    list_id=list_id,
                    subject=f"Unparsed {list_id}",
                    subject_tdocs=(),
                )
            ],
            parse_date_set(["= 2026-10-01"]),
            (),
        )

    fetched_urls = []

    def fake_fetch(_source, url):
        fetched_urls.append(url)
        return FetchedPage(
            text=_detail_html(inline_body_html="<p>Selected body for R1-2610793</p>"),
            content_type="text/html",
            final_url=url,
        )

    monkeypatch.setattr(ListservSource, "fetch", fake_fetch)
    result = runner.invoke(app, ["listserv", "parse", "TEAM_A"])
    assert result.exit_code == 0, result.output
    assert fetched_urls == [team_a_url]
    assert stored.get(team_a_url).detail is not None
    assert stored.get(team_x_url).detail is None


@pytest.mark.parametrize("operation", ["sync", "parse"])
def test_wildcard_list_id_is_rejected_before_io(stored, monkeypatch, operation):
    from doc3gpp import cli
    from doc3gpp.storage.db import migrate

    def forbidden(*_args, **_kwargs):
        raise AssertionError("database or network access happened before validation")

    monkeypatch.setattr(cli, "create_schema", forbidden)
    monkeypatch.setattr(migrate, "create_schema", forbidden)
    monkeypatch.setattr(SQLAlchemyListservRepository, "__init__", forbidden)
    monkeypatch.setattr(ListservSource, "fetch", forbidden)

    result = runner.invoke(app, ["listserv", operation, "TEAM%"])
    assert result.exit_code != 0
    assert "TEAM%" in result.output

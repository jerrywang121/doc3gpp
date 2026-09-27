"""Typer tests for the unified TDoc and spec-document search CLI."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.models.index import (
    IndexComponentStatus,
    IndexRebuildResult,
    IndexStatus,
)
from doc3gpp.models.search import (
    SearchError,
    SearchFilters,
    SearchIndexCorruptError,
    SearchIndexStatus,
    SearchQueryError,
)
from doc3gpp.models.semantic_search import (
    EmbedderUnavailableError,
    SemanticSearchUnavailableError,
    VectorIndexUnavailableError,
)
from doc3gpp.models.unified_search import (
    SearchMode,
    SpecDocSearchResult,
    TDocSearchResult,
)

runner = CliRunner()

TDOC_KEYS = [
    "tdoc_id",
    "score",
    "search_mode",
    "previews",
    "title",
    "meeting",
    "tsg",
    "uploaded_date",
    "ftp_url",
    "wis",
    "type",
    "status",
    "best_chunk_id",
]

SPEC_DOC_KEYS = [
    "chunk_id",
    "score",
    "search_mode",
    "previews",
    "spec_id",
    "version",
    "release",
    "sections",
    "tables",
    "chunk_index",
    "text",
]


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        log_level="INFO",
        output=SimpleNamespace(
            format="table",
            compact=False,
            fields=SimpleNamespace(
                spec_doc=[
                    "spec_id",
                    "version",
                    "release",
                    "sections",
                    "tables",
                    "chunk_index",
                    "text",
                ]
            ),
        ),
        search=SimpleNamespace(
            enabled=True,
            bm25_weights=(5.0, 5.0, 5.0, 1.0, 1.0, 1.0, 1.0, 1.0),
        ),
        semantic_search=SimpleNamespace(fts5_weight=0.35),
    )


def _tdoc_result(mode: SearchMode, *, previews: dict[str, str] | None) -> TDocSearchResult:
    return TDocSearchResult(
        tdoc_id="R5-000001",
        score=1.25 if mode is not SearchMode.FILTER else None,
        search_mode=mode,
        previews=previews,
        title="Handover procedure",
        meeting="RAN1#120",
        tsg="RAN",
        uploaded_date=date(2025, 1, 10),
        ftp_url="https://www.3gpp.org/ftp/R5-000001.zip",
        wis="38.331",
        type="CR",
        status="Agreed",
        best_chunk_id="R5-000001#0" if mode in {SearchMode.SEMANTIC, SearchMode.HYBRID} else None,
    )


def _spec_doc_result(mode: SearchMode, *, previews: dict[str, str] | None) -> SpecDocSearchResult:
    return SpecDocSearchResult(
        chunk_id="38.331@18.5.0#0",
        score=2.5 if mode is not SearchMode.FILTER else None,
        search_mode=mode,
        previews=previews,
        spec_id="38.331",
        version="18.5.0",
        release="Rel-18",
        sections="5.1 Handover",
        tables="Table 1 Values",
        chunk_index=0,
        text="handover procedure",
    )


def _status() -> IndexStatus:
    fts_status = SearchIndexStatus(
        enabled=True,
        row_count=12,
        last_rebuild_at=None,
        last_indexed_uploaded_date=None,
        latest_tdocs_uploaded_date=None,
        is_stale=False,
    )
    return IndexStatus(
        fts5=IndexComponentStatus(available=True, status=fts_status),
        vector=IndexComponentStatus(available=False, error="unavailable"),
    )


def test_top_level_search_command_is_removed() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "search" not in result.output


def test_top_level_search_invocation_is_rejected() -> None:
    result = runner.invoke(app, ["search", "query", "--help"])
    assert result.exit_code != 0


def test_tdoc_search_direct_uses_facade_and_emits_exact_fts5_payload(monkeypatch) -> None:
    facade = MagicMock()
    facade.search.return_value = [
        _tdoc_result(SearchMode.FTS5, previews={"title": "<<handover>> procedure"})
    ]
    facade.status.return_value = SimpleNamespace(is_stale=False)
    settings = _settings()
    monkeypatch.setattr("doc3gpp.cli.get_settings", lambda: settings)
    monkeypatch.setattr(
        "doc3gpp.services.factory.build_tdoc_search_facade",
        lambda: facade,
    )

    result = runner.invoke(
        app,
        ["tdoc", "search", "--text", "handover", "--format", "json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert list(payload[0]) == TDOC_KEYS
    assert payload[0]["score"] == 1.25
    assert payload[0]["search_mode"] == "fts5"
    assert payload[0]["previews"] == {"title": "<<handover>> procedure"}
    assert payload[0]["type"] == "CR"
    assert payload[0]["status"] == "Agreed"
    assert payload[0]["best_chunk_id"] is None
    call = facade.search.call_args.kwargs
    assert call["text"] == "handover"
    assert call["semantic"] is None
    assert call["snippet_tokens"] == 8


def test_tdoc_search_stale_hint_uses_real_facade_status_boundary(monkeypatch) -> None:
    from doc3gpp.models.search import SearchIndexStatus
    from doc3gpp.models.tdoc import TDoc, TDocWithMeeting
    from doc3gpp.services.tdoc_search_facade import TDocSearchFacade

    class Source:
        def list_for_search(self, _filters):
            return [
                TDocWithMeeting(
                    tdoc=TDoc(tdoc_id="R5-000001", title="Filtered"),
                    meeting_name="RAN#1",
                    meeting_tsg="RAN",
                )
            ]

    class Fts:
        def search(self, _query, _filters, *, snippet_tokens=None):
            from doc3gpp.models.search import SearchHit

            return [
                SearchHit(
                    tdoc_id="R5-000001",
                    score=-1.0,
                    previews={"title": "<<Filtered>>"},
                    title="Filtered",
                    meeting="RAN#1",
                    tsg="RAN",
                    uploaded_date=None,
                    ftp_url=None,
                    wis=None,
                )
            ]

        def status(self):
            return SearchIndexStatus(
                enabled=True,
                row_count=1,
                last_rebuild_at=None,
                last_indexed_uploaded_date=None,
                latest_tdocs_uploaded_date=None,
                is_stale=True,
            )

    settings = _settings()
    facade = TDocSearchFacade(
        fts5_service=Fts(),
        semantic_service=None,
        source_repo=Source(),
        settings=settings,
    )
    monkeypatch.setattr("doc3gpp.cli.get_settings", lambda: settings)
    monkeypatch.setattr(
        "doc3gpp.services.factory.build_tdoc_search_facade",
        lambda: facade,
    )

    result = runner.invoke(
        app, ["tdoc", "search", "--text", "filtered", "--format", "json"]
    )

    assert result.exit_code == 0, result.output
    assert "doc3gpp tdoc index --rebuild" in result.output


def test_tdoc_semantic_search_does_not_probe_unavailable_fts5(monkeypatch) -> None:
    from doc3gpp.models.search import SearchUnavailableError
    from doc3gpp.services import factory

    settings = _settings()
    semantic = MagicMock()
    semantic.search.return_value = []

    def unavailable_fts5(*_args, **_kwargs):
        raise SearchUnavailableError("FTS5 unavailable")

    monkeypatch.setattr(factory, "build_search_service", unavailable_fts5)
    monkeypatch.setattr(
        factory,
        "build_semantic_search_service",
        lambda *_args, **_kwargs: semantic,
    )
    facade = factory.build_tdoc_search_facade(
        settings,
        source_repo=MagicMock(),
        embedder=MagicMock(),
    )
    monkeypatch.setattr("doc3gpp.cli.get_settings", lambda: settings)
    monkeypatch.setattr(
        "doc3gpp.services.factory.build_tdoc_search_facade",
        lambda: facade,
    )
    monkeypatch.setattr("doc3gpp.cli._stale_index_hint_emitted", False)

    result = runner.invoke(
        app,
        ["tdoc", "search", "--semantic", "handover", "--format", "json"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == []


def test_tdoc_filter_search_does_not_probe_fts5_status(monkeypatch) -> None:
    from doc3gpp.models.search import SearchUnavailableError
    from doc3gpp.services import factory

    settings = _settings()
    settings.semantic_search.embedding_base_url = None

    def unavailable_fts5(*_args, **_kwargs):
        raise SearchUnavailableError("FTS5 unavailable")

    monkeypatch.setattr(factory, "build_search_service", unavailable_fts5)
    source = MagicMock()
    source.list_for_search.return_value = []
    facade = factory.build_tdoc_search_facade(settings, source_repo=source)
    monkeypatch.setattr("doc3gpp.cli.get_settings", lambda: settings)
    monkeypatch.setattr(
        "doc3gpp.services.factory.build_tdoc_search_facade",
        lambda: facade,
    )
    monkeypatch.setattr("doc3gpp.cli._stale_index_hint_emitted", False)

    result = runner.invoke(app, ["tdoc", "search", "--format", "json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == []


def test_tdoc_search_ignores_unavailable_stale_status(monkeypatch) -> None:
    from doc3gpp.models.search import SearchUnavailableError

    settings = _settings()
    facade = MagicMock()
    facade.search.return_value = [_tdoc_result(SearchMode.FTS5, previews=None)]
    facade.status.side_effect = SearchUnavailableError("FTS5 unavailable")
    monkeypatch.setattr("doc3gpp.cli.get_settings", lambda: settings)
    monkeypatch.setattr(
        "doc3gpp.services.factory.build_tdoc_search_facade",
        lambda: facade,
    )
    monkeypatch.setattr("doc3gpp.cli._stale_index_hint_emitted", False)

    result = runner.invoke(
        app,
        ["tdoc", "search", "--text", "handover", "--format", "json"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)[0]["search_mode"] == "fts5"


def test_tdoc_filter_result_preserves_meeting_tsg() -> None:
    from doc3gpp.models.tdoc import TDoc, TDocWithMeeting
    from doc3gpp.services.tdoc_search_facade import TDocSearchFacade

    row = TDocWithMeeting(
        tdoc=TDoc(tdoc_id="R5-000002", title="Filtered"),
        meeting_name="RAN#2",
        meeting_tsg="RAN",
    )
    facade = TDocSearchFacade(
        fts5_service=None,
        semantic_service=None,
        source_repo=SimpleNamespace(list_for_search=lambda _filters: [row]),
        settings=_settings(),
    )

    result = facade.search(text=None, semantic=None, filters=SearchFilters())

    assert result[0].tsg == "RAN"


def test_tdoc_search_dispatches_semantic_hybrid_and_filter_modes(monkeypatch) -> None:
    settings = _settings()
    monkeypatch.setattr("doc3gpp.cli.get_settings", lambda: settings)

    cases = [
        (["--semantic", "handover"], SearchMode.SEMANTIC),
        (["--text", "handover", "--semantic", "procedure"], SearchMode.HYBRID),
        ([], SearchMode.FILTER),
    ]
    for options, mode in cases:
        facade = MagicMock()
        facade.search.return_value = [_tdoc_result(mode, previews={"title": "hidden"})]
        monkeypatch.setattr(
            "doc3gpp.services.factory.build_tdoc_search_facade",
            lambda facade=facade: facade,
        )

        result = runner.invoke(app, ["tdoc", "search", *options, "--format", "json"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)[0]
        assert list(payload) == TDOC_KEYS
        assert payload["search_mode"] == mode.value
        assert payload["previews"] is None
        assert payload["score"] is None if mode is SearchMode.FILTER else payload["score"] == 1.25
        assert facade.search.call_args.kwargs["text"] == (
            "handover" if "--text" in options else None
        )


def test_tdoc_search_table_and_markdown_show_score_mode_metadata_and_previews(monkeypatch) -> None:
    settings = _settings()
    monkeypatch.setattr("doc3gpp.cli.get_settings", lambda: settings)
    facade = MagicMock()
    facade.search.return_value = [
        _tdoc_result(SearchMode.FTS5, previews={"title": "<<handover>>"})
    ]
    monkeypatch.setattr(
        "doc3gpp.services.factory.build_tdoc_search_facade",
        lambda: facade,
    )

    table = runner.invoke(app, ["tdoc", "search", "--text", "handover"])
    markdown = runner.invoke(
        app,
        ["tdoc", "search", "--text", "handover", "--format", "markdown"],
    )

    assert table.exit_code == 0, table.output
    assert "score" in table.output
    assert "fts5" in table.output
    assert "CR" in table.output and "Agreed" in table.output
    assert "<<handover>>" in table.output
    assert markdown.exit_code == 0, markdown.output
    assert "| search_mode |" in markdown.output
    assert "| 1.25 |" in markdown.output
    assert "title: <<handover>>" in markdown.output


def test_tdoc_search_errors_keep_query_and_rebuild_paths(monkeypatch) -> None:
    settings = _settings()
    monkeypatch.setattr("doc3gpp.cli.get_settings", lambda: settings)
    facade = MagicMock()
    monkeypatch.setattr(
        "doc3gpp.services.factory.build_tdoc_search_facade",
        lambda: facade,
    )

    facade.search.side_effect = SearchQueryError("bad expression")
    query_result = runner.invoke(app, ["tdoc", "search", "--text", "["])
    assert query_result.exit_code == 2
    assert "bad query" in query_result.output

    facade.search.side_effect = SearchIndexCorruptError("broken")
    corrupt_result = runner.invoke(app, ["tdoc", "search", "--text", "handover"])
    assert corrupt_result.exit_code == 3
    assert "doc3gpp tdoc index --rebuild" in corrupt_result.output


def test_tdoc_search_preserves_established_unavailable_messages(monkeypatch) -> None:
    settings = _settings()
    monkeypatch.setattr("doc3gpp.cli.get_settings", lambda: settings)
    facade = MagicMock()
    facade.status.return_value = SimpleNamespace(is_stale=False)
    monkeypatch.setattr(
        "doc3gpp.services.factory.build_tdoc_search_facade",
        lambda: facade,
    )

    cases = (
        (EmbedderUnavailableError("load failed"), "embedding model load failed: load failed", 1),
        (VectorIndexUnavailableError("vector missing"), "vector index unavailable: vector missing", 1),
        (SemanticSearchUnavailableError("semantic missing"), "search sem unavailable: semantic missing", 1),
        (SearchError("broken index"), "doc3gpp tdoc index --rebuild", 3),
    )
    for error, expected, exit_code in cases:
        facade.search.side_effect = error
        result = runner.invoke(app, ["tdoc", "search", "--semantic", "handover"])
        assert result.exit_code == exit_code, result.output
        assert expected in result.output


def test_tdoc_index_uses_coordinator_for_status_and_actions(monkeypatch) -> None:
    settings = _settings()
    monkeypatch.setattr("doc3gpp.cli.get_settings", lambda: settings)
    coordinator = MagicMock()
    coordinator.rebuild.return_value = _status()
    monkeypatch.setattr(
        "doc3gpp.services.factory.build_tdoc_index_service",
        lambda: coordinator,
    )

    status_result = runner.invoke(app, ["tdoc", "index"])
    assert status_result.exit_code == 0, status_result.output
    assert "FTS5" in status_result.output
    assert "FTS5 rows: 12" in status_result.output
    assert "Vector" in status_result.output
    request = coordinator.rebuild.call_args.args[0]
    assert request.rebuild is False
    assert request.rebuild_embeddings is False

    coordinator.rebuild.return_value = IndexRebuildResult(
        fts5_processed=4,
        vector_processed=0,
    )
    action_result = runner.invoke(
        app,
        ["tdoc", "index", "--rebuild", "--batch", "7", "--resume", "--quiet"],
    )
    assert action_result.exit_code == 0, action_result.output
    request = coordinator.rebuild.call_args.args[0]
    assert request.rebuild is True
    assert request.batch == 7
    assert request.resume is True
    assert "FTS5 processed: 4" in action_result.output
    assert "Vector processed: 0" in action_result.output


def test_tdoc_index_status_renders_index_timestamps(monkeypatch) -> None:
    settings = _settings()
    monkeypatch.setattr("doc3gpp.cli.get_settings", lambda: settings)
    status = IndexStatus(
        fts5=IndexComponentStatus(
            available=True,
            status=SearchIndexStatus(
                enabled=True,
                row_count=12,
                last_rebuild_at=datetime(2026, 9, 1, 1, 2, 3, tzinfo=timezone.utc),
                last_indexed_uploaded_date=datetime(
                    2026, 9, 2, 4, 5, 6, tzinfo=timezone.utc
                ),
                latest_tdocs_uploaded_date=datetime(
                    2026, 9, 3, 7, 8, 9, tzinfo=timezone.utc
                ),
                is_stale=False,
            ),
        ),
        vector=IndexComponentStatus(available=False, error="unavailable"),
    )
    coordinator = MagicMock()
    coordinator.rebuild.return_value = status
    monkeypatch.setattr(
        "doc3gpp.services.factory.build_tdoc_index_service",
        lambda: coordinator,
    )

    result = runner.invoke(app, ["tdoc", "index"])

    assert result.exit_code == 0, result.output
    assert "Last rebuild: 2026-09-01 01:02:03" in result.output
    assert "Last indexed: 2026-09-02 04:05:06" in result.output
    assert "Latest tdocs: 2026-09-03 07:08:09" in result.output




def test_spec_doc_search_supports_output_file(monkeypatch, tmp_path) -> None:
    settings = _settings()
    monkeypatch.setattr("doc3gpp.cli.get_settings", lambda: settings)
    create_schema = MagicMock()
    monkeypatch.setattr("doc3gpp.cli.create_schema", create_schema)
    facade = MagicMock()
    facade.search.return_value = [_spec_doc_result(SearchMode.FILTER, previews=None)]
    monkeypatch.setattr(
        "doc3gpp.services.factory.build_spec_doc_search_facade",
        lambda: facade,
    )
    output = tmp_path / "spec-doc-search.json"

    result = runner.invoke(
        app,
        [
            "spec", "doc", "search", "--format", "json",
            "--output", str(output), "--compact",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(output.read_text(encoding="utf-8"))[0]["search_mode"] == "filter"
    assert result.stdout == ""
    create_schema.assert_not_called()


def test_spec_doc_search_direct_uses_facade_offset_fields_and_flat_payload(monkeypatch) -> None:
    settings = _settings()
    monkeypatch.setattr("doc3gpp.cli.get_settings", lambda: settings)
    monkeypatch.setattr("doc3gpp.cli.create_schema", lambda _scope: None)
    facade = MagicMock()
    facade.search.return_value = [
        _spec_doc_result(SearchMode.FTS5, previews={"text": "<<handover>>"})
    ]
    monkeypatch.setattr(
        "doc3gpp.services.factory.build_spec_doc_search_facade",
        lambda: facade,
    )

    result = runner.invoke(
        app,
        [
            "spec",
            "doc",
            "search",
            "--text",
            "handover",
            "--spec",
            "38.331",
            "--version",
            "18.5.0",
            "--sections",
            "%5%",
            "--tables",
            "%UE%",
            "--limit",
            "3",
            "--offset",
            "2",
            "--fields",
            "spec_id,text",
            "--format",
            "json",
            "--compact",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(result.stdout.splitlines()) == 1
    payload = json.loads(result.stdout)
    assert list(payload[0]) == SPEC_DOC_KEYS
    assert payload[0]["score"] == 2.5
    assert payload[0]["search_mode"] == "fts5"
    assert payload[0]["previews"] == {"text": "<<handover>>"}
    filters = facade.search.call_args.kwargs["filters"]
    assert filters.spec_id == "38.331"
    assert filters.version == "18.5.0"
    assert filters.sections == "%5%"
    assert filters.tables == "%UE%"
    assert filters.limit == 3
    assert filters.offset == 2


def test_spec_doc_search_accepts_semantic_and_field_only_modes(monkeypatch) -> None:
    settings = _settings()
    monkeypatch.setattr("doc3gpp.cli.get_settings", lambda: settings)
    monkeypatch.setattr("doc3gpp.cli.create_schema", lambda _scope: None)
    for options, mode in ((["--semantic", "handover"], SearchMode.SEMANTIC), ([], SearchMode.FILTER)):
        facade = MagicMock()
        facade.search.return_value = [_spec_doc_result(mode, previews={"text": "hidden"})]
        monkeypatch.setattr(
            "doc3gpp.services.factory.build_spec_doc_search_facade",
            lambda facade=facade: facade,
        )
        result = runner.invoke(
            app,
            ["spec", "doc", "search", *options, "--format", "json"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)[0]
        assert list(payload) == SPEC_DOC_KEYS
        assert payload["search_mode"] == mode.value
        assert payload["previews"] is None


def test_spec_doc_table_and_markdown_honor_explicit_fields(monkeypatch) -> None:
    settings = _settings()
    monkeypatch.setattr("doc3gpp.cli.get_settings", lambda: settings)
    monkeypatch.setattr("doc3gpp.cli.create_schema", lambda _scope: None)
    facade = MagicMock()
    facade.search.return_value = [
        _spec_doc_result(SearchMode.FTS5, previews={"text": "<<handover>>"})
    ]
    monkeypatch.setattr(
        "doc3gpp.services.factory.build_spec_doc_search_facade",
        lambda: facade,
    )

    for fmt in ("table", "markdown"):
        result = runner.invoke(
            app,
            [
                "spec", "doc", "search", "--text", "handover",
                "--fields", "spec_id,text", "--format", fmt,
            ],
        )
        assert result.exit_code == 0, result.output
        assert "spec_id" in result.output
        assert "text" in result.output
        assert "score" in result.output
        assert "search_mode" in result.output
        assert "previews" not in result.output


def test_spec_doc_search_preserves_established_unavailable_messages(monkeypatch) -> None:
    settings = _settings()
    monkeypatch.setattr("doc3gpp.cli.get_settings", lambda: settings)
    monkeypatch.setattr("doc3gpp.cli.create_schema", lambda _scope: None)
    facade = MagicMock()
    facade.status.return_value = SimpleNamespace(is_stale=False)
    monkeypatch.setattr(
        "doc3gpp.services.factory.build_spec_doc_search_facade",
        lambda: facade,
    )

    cases = (
        (EmbedderUnavailableError("load failed"), "embedding model load failed: load failed", 1),
        (VectorIndexUnavailableError("vector missing"), "vector index unavailable: vector missing", 1),
        (SemanticSearchUnavailableError("semantic missing"), "search sem unavailable: semantic missing", 1),
        (SearchError("broken index"), "doc3gpp spec doc index --rebuild", 3),
    )
    for error, expected, exit_code in cases:
        facade.search.side_effect = error
        result = runner.invoke(
            app,
            ["spec", "doc", "search", "--semantic", "handover"],
        )
        assert result.exit_code == exit_code, result.output
        assert expected in result.output


def test_spec_doc_index_uses_coordinator(monkeypatch) -> None:
    settings = _settings()
    monkeypatch.setattr("doc3gpp.cli.get_settings", lambda: settings)
    monkeypatch.setattr("doc3gpp.cli.create_schema", lambda _scope: None)
    coordinator = MagicMock()
    coordinator.rebuild.return_value = _status()
    monkeypatch.setattr(
        "doc3gpp.services.factory.build_spec_doc_index_service",
        lambda: coordinator,
    )

    result = runner.invoke(app, ["spec", "doc", "index"])

    assert result.exit_code == 0, result.output
    assert "FTS5" in result.output
    assert "Vector" in result.output
    assert coordinator.rebuild.call_args.args[0].rebuild_all is False


def test_unified_help_removes_old_nested_commands_and_options() -> None:
    tdoc_help = runner.invoke(app, ["tdoc", "--help"])
    spec_doc_help = runner.invoke(app, ["spec", "doc", "--help"])

    assert tdoc_help.exit_code == 0, tdoc_help.output
    assert "search" in tdoc_help.output
    assert "index" in tdoc_help.output
    assert spec_doc_help.exit_code == 0, spec_doc_help.output
    assert "search" in spec_doc_help.output
    assert "index" in spec_doc_help.output

    for args in (
        ["tdoc", "search", "query", "handover"],
        ["tdoc", "search", "sem", "handover"],
        ["tdoc", "search", "index"],
        ["spec", "doc", "search", "query", "handover"],
        ["spec", "doc", "search", "sem", "handover"],
        ["tdoc", "search", "--fts5-weight", "0.5"],
        ["tdoc", "search", "--fts5-query", "handover"],
        ["tdoc", "search", "--sem-query", "handover"],
    ):
        assert runner.invoke(app, args).exit_code != 0, args

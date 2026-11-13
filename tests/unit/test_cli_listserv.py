"""Unit tests for the ``doc3gpp listserv`` Typer adapter (services mocked)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.models.index import IndexRebuildResult
from doc3gpp.models.listserv import (
    ListservBatchResult,
    ListservMessage,
    ListservRecord,
    ListservSearchResult,
)
from doc3gpp.models.listserv_payloads import listserv_search_payload
from doc3gpp.models.search import SearchUnavailableError

runner = CliRunner()


def _record() -> ListservRecord:
    msg = ListservMessage(
        url="https://list.etsi.org/m/1",
        list_id="3GPP_TSG_RAN_WG1",
        subject="S R1-2610793",
        sender="A <x>",
        date=datetime(2026, 10, 1, 12, tzinfo=timezone.utc),
        date_precision="datetime",
        week="2026-10-W1",
        subject_tdocs=("R1-2610793",),
    )
    return ListservRecord(message=msg, detail=None, tdocs=("R1-2610793",), source_revision=1)


class _Spy:
    def __init__(self, result=None):
        self.calls: list[tuple] = []
        self.result = result

    def sync(self, list_id, **kw):
        self.calls.append(("sync", list_id, kw))
        return self.result

    parse = sync


@pytest.fixture()
def built(monkeypatch, sqlite_env, disable_auto_sync):
    from doc3gpp.services import factory

    state: dict[str, object] = {"built": []}

    def mark(name, obj):
        def _b(*a, **k):
            state["built"].append(name)
            return obj

        monkeypatch.setattr(factory, name, _b)

    state["sync"] = _Spy(ListservBatchResult(1, 0, {}))
    state["parse"] = _Spy(ListservBatchResult(1, 0, {}))
    mark("build_listserv_sync_service", state["sync"])
    mark("build_listserv_parse_service", state["parse"])
    return state


def test_listserv_help_has_six_operations():
    result = runner.invoke(app, ["listserv", "--help"])
    assert result.exit_code == 0
    assert all(n in result.output for n in ("sync", "parse", "list", "search", "show", "index"))


def test_search_help_lists_options():
    out = runner.invoke(app, ["listserv", "search", "--help"]).output
    for opt in ("--date", "--list-id", "--from", "--subject", "--tdocs", "--text",
                "--semantic", "--limit", "--offset", "--format"):
        assert opt in out
    out = runner.invoke(app, ["listserv", "sync", "--help"]).output
    assert "--date" in out and "--force" in out


@pytest.mark.parametrize("cmd", ["sync", "parse"])
@pytest.mark.parametrize(
    "args",
    [["BAD ID%"], ["3GPP_%"], ["OK", "--date", "not-a-date"]],
)
def test_invalid_input_rejected_before_service_build(built, cmd, args):
    if args[0] == "OK":
        args = ["3GPP_TSG_RAN_WG1", *args[1:]]
    result = runner.invoke(app, ["listserv", cmd, *args])
    assert result.exit_code == 2
    assert built["built"] == []


@pytest.mark.parametrize("cmd", ["sync", "parse"])
def test_dates_and_force_reach_service_unchanged(built, cmd):
    result = runner.invoke(
        app,
        ["listserv", cmd, "3gpp_tsg_ran_wg1", "--date", ">= 2026-01-01",
         "--date", ">= 2026-01-01", "--force"],
    )
    assert result.exit_code == 0, result.output
    _name, list_id, kw = built[cmd].calls[0]
    assert list_id == "3gpp_tsg_ran_wg1"
    assert list(kw["dates"]) == [">= 2026-01-01", ">= 2026-01-01"]
    assert kw["force"] is True


def test_sync_default_dates_empty_and_force_false(built):
    assert runner.invoke(app, ["listserv", "sync", "X"]).exit_code == 0
    kw = built["sync"].calls[0][2]
    assert list(kw["dates"]) == [] and kw["force"] is False


def test_index_failures_reported_distinctly_and_exit_nonzero(built):
    built["sync"].result = ListservBatchResult(
        3, 0, {"u1": "Index update failed (RuntimeError)"}
    )
    result = runner.invoke(app, ["listserv", "sync", "X"])
    assert result.exit_code == 1
    assert "index" in result.output.lower()
    assert "source data was committed" in result.output.lower()


def test_source_failures_exit_nonzero(built):
    built["parse"].result = ListservBatchResult(1, 0, {"u1": "HTTP 500"})
    result = runner.invoke(app, ["listserv", "parse", "X"])
    assert result.exit_code == 1
    assert "u1" in result.output and "HTTP 500" in result.output


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("iNdEx update failed (RuntimeError)", True),
        ("fTs5 index update failed (OperationalError)", True),
        ("VeCtOr index update failed (ValueError)", True),
        ("HTTP 500 while INDEX update failed (RuntimeError)", False),
    ],
)
def test_cli_and_job_share_listserv_failure_classifier(reason, expected):
    import importlib.util

    from doc3gpp import cli_listserv
    from doc3gpp.web.workers import listserv_jobs

    module_name = "doc3gpp.models.listserv_failures"
    assert importlib.util.find_spec(module_name) is not None
    from doc3gpp.models import listserv_failures

    assert cli_listserv._is_index_failure is listserv_jobs.is_index_failure
    assert cli_listserv._is_index_failure is listserv_failures.is_index_failure
    assert listserv_jobs.INDEX_FAILURE_PREFIXES is listserv_failures.INDEX_FAILURE_PREFIXES
    assert cli_listserv._is_index_failure(reason) is expected
    assert listserv_jobs.is_index_failure(reason) is expected


def test_list_prints_ids(monkeypatch, sqlite_env, disable_auto_sync):
    from types import SimpleNamespace

    from doc3gpp.services import factory

    class S:
        def list_ids(self):
            return ["A", "B"]

        def list_lists(self):
            return [
                SimpleNamespace(list_id="ALPHA", tsg="R1", description="Radio access"),
                SimpleNamespace(list_id="BETA", tsg=None, description=None),
            ]

    monkeypatch.setattr(factory, "build_listserv_service", lambda *a, **k: S())
    result = runner.invoke(app, ["listserv", "list"])
    assert result.exit_code == 0
    assert result.output.splitlines() == [
        "list_id\ttsg\tdescription",
        "ALPHA\tR1\tRadio access",
        "BETA\t-\t-",
    ]


def test_search_passes_filters_and_emits_shared_payload(monkeypatch, sqlite_env, disable_auto_sync):
    from doc3gpp.services import factory

    seen = {}
    close_calls = []
    res = ListservSearchResult(
        record=_record(), score=None, snippet=None, matched_source=None,
        content_preview=None, preview_truncated=None,
    )

    class S:
        def search(self, *, text=None, semantic=None, filters=None):
            seen.update(text=text, semantic=semantic, filters=filters)
            return [res]

        def close(self):
            close_calls.append(True)

    service = S()
    monkeypatch.setattr(factory, "build_listserv_search_service", lambda *a, **k: service)
    result = runner.invoke(
        app,
        ["listserv", "search", "--date", ">= 2026-10-01", "--list-id", "L", "--from", "bob",
         "--subject", "s", "--tdocs", "R1-1", "--text", "q", "--semantic", "m",
         "--limit", "5", "--offset", "2", "--format", "json"],
    )
    assert result.exit_code == 0, result.output
    f = seen["filters"]
    assert (f.list_id, f.sender, f.subject, f.tdocs, f.limit, f.offset) == ("L", "bob", "s", "R1-1", 5, 2)
    assert f.dates is not None
    assert (seen["text"], seen["semantic"]) == ("q", "m")
    assert json.loads(result.output) == json.loads(json.dumps([listserv_search_payload(res)]))
    assert close_calls == [True]


def test_search_unavailable_exits_1(monkeypatch, sqlite_env, disable_auto_sync):
    from doc3gpp.services import factory

    close_calls = []

    class S:
        def search(self, **kw):
            raise SearchUnavailableError("FTS5 search is not available")

        def close(self):
            close_calls.append(True)

    monkeypatch.setattr(factory, "build_listserv_search_service", lambda *a, **k: S())
    result = runner.invoke(app, ["listserv", "search", "--text", "q"])
    assert result.exit_code == 1
    assert "not available" in result.output
    assert close_calls == [True]


def test_search_invalid_limit_or_date_rejected(sqlite_env, disable_auto_sync):
    assert runner.invoke(app, ["listserv", "search", "--limit", "999"]).exit_code == 2
    assert runner.invoke(app, ["listserv", "search", "--date", "zzz"]).exit_code == 2


def test_show_not_found(monkeypatch, sqlite_env, disable_auto_sync):
    from doc3gpp.services import factory
    from doc3gpp.services.listserv_service import ListservMessageNotFoundError

    class S:
        def show(self, url):
            raise ListservMessageNotFoundError(url)

    monkeypatch.setattr(factory, "build_listserv_service", lambda *a, **k: S())
    result = runner.invoke(app, ["listserv", "show", "https://x/y"])
    assert result.exit_code == 1
    assert "not found" in result.output.lower()


def test_index_rebuild_wired(monkeypatch, sqlite_env, disable_auto_sync):
    from doc3gpp.services import factory

    seen = {}
    close_calls = []

    class S:
        def rebuild(self, request, **kw):
            seen["req"] = request
            seen["kwargs"] = kw
            if kw["on_progress"] is not None:
                kw["on_progress"]("quiet progress must not be emitted")
            return IndexRebuildResult(2, 0)

        def close(self):
            close_calls.append(True)

    monkeypatch.setattr(factory, "build_listserv_index_service", lambda *a, **k: S())
    result = runner.invoke(app, ["listserv", "index", "--rebuild", "--batch", "5", "--quiet"])
    assert result.exit_code == 0, result.output
    assert seen["req"].rebuild is True and seen["req"].batch == 5
    assert seen["kwargs"]["quiet"] is True
    assert seen["kwargs"]["on_progress"] is None
    assert "quiet progress must not be emitted" not in result.output
    assert "FTS5 processed: 2" in result.output
    assert close_calls == [True]


@pytest.mark.parametrize(
    "reason",
    [
        "iNdEx update failed (RuntimeError)",
        "fTs5 index update failed (OperationalError)",
        "VeCtOr index update failed (ValueError)",
    ],
)
def test_service_format_index_failure_is_labeled_index(built, reason):
    built["sync"].result = ListservBatchResult(1, 0, {"u1": reason})
    result = runner.invoke(app, ["listserv", "sync", "X"])
    assert result.exit_code == 1
    assert "INDEX FAILED u1" in result.output
    assert "FAILED u1" not in result.output.replace("INDEX FAILED u1", "")
    assert "source data was committed" in result.output.lower()
    assert "failed=0 index_failed=1" in result.output


def test_source_failure_containing_phrase_not_misclassified(built):
    built["sync"].result = ListservBatchResult(
        0, 0, {"u1": "HTTP 500 while Index update failed (x) text"}
    )
    result = runner.invoke(app, ["listserv", "sync", "X"])
    assert result.exit_code == 1
    assert "INDEX FAILED" not in result.output
    assert "source data was committed" not in result.output.lower()
    assert "failed=1 index_failed=0" in result.output



def test_index_closes_service_after_rebuild_error(monkeypatch, sqlite_env, disable_auto_sync):
    from doc3gpp.models.search import SearchUnavailableError
    from doc3gpp.services import factory

    close_calls = []

    class S:
        def rebuild(self, request, **kwargs):
            raise SearchUnavailableError("LISTSERV index is not available")

        def close(self):
            close_calls.append(True)

    monkeypatch.setattr(factory, "build_listserv_index_service", lambda *a, **k: S())
    result = runner.invoke(app, ["listserv", "index", "--rebuild"])

    assert result.exit_code == 1
    assert "not available" in result.output
    assert close_calls == [True]

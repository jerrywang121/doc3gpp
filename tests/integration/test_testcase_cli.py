"""Integration tests for ``doc3gpp testcase`` CLI commands.

Uses a stubbed :class:`TestCaseService` injected via monkeypatch on
``doc3gpp.cli.build_testcase_service`` so the CLI commands can be
exercised without any network or schema bootstrap.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.models.sync import SyncOutcome
from doc3gpp.models.testcase import TestCase, TestCaseWithStatuses

runner = CliRunner()


def test_testcase_list_json_shape(monkeypatch) -> None:
    from doc3gpp.models.testcase import TestCaseStatus
    svc = MagicMock()
    svc.list_recent.return_value = [
        TestCaseWithStatuses(
            testcase=TestCase(testcase_id="TC_1", title="T", spec="38.523-1", group="5G", release="Rel-17"),
            statuses=[TestCaseStatus(testcase_id="TC_1", group="5G", path="FR1", gcf_ptcrb="Approved", ttcn_status="Approved")],
        )
    ]
    monkeypatch.setattr("doc3gpp.cli.build_testcase_service", lambda: svc)
    result = runner.invoke(app, ["testcase", "list", "--format", "json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload[0]["testcase_id"] == "TC_1"
    assert payload[0]["group"] == "5G"
    assert payload[0]["statuses"] == [{"path": "FR1", "gcf_ptcrb": "Approved", "ttcn_status": "Approved"}]
    assert "testcase" not in payload[0]


def test_testcase_show_multi_group_json(monkeypatch) -> None:
    """``testcase show`` without ``--group`` emits one flat entry per group."""
    from doc3gpp.models.testcase import TestCase, TestCaseDetail, TestCaseStatus
    svc = MagicMock()
    svc.get_all.return_value = [
        TestCaseDetail(
            testcase=TestCase(testcase_id="TC_D", group="IMS", title="I"),
            statuses=[TestCaseStatus(testcase_id="TC_D", group="IMS", path="default", ttcn_status="Approved")],
        ),
        TestCaseDetail(
            testcase=TestCase(testcase_id="TC_D", group="UTRA", title="U"),
            statuses=[TestCaseStatus(testcase_id="TC_D", group="UTRA", path="default", ttcn_status="Rejected")],
        ),
    ]
    monkeypatch.setattr("doc3gpp.cli.build_testcase_service", lambda: svc)
    result = runner.invoke(app, ["testcase", "show", "--testcase", "TC_D", "--format", "json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert isinstance(payload, list) and len(payload) == 2
    assert [entry["group"] for entry in payload] == ["IMS", "UTRA"]
    assert payload[0]["statuses"] == [{"path": "default", "gcf_ptcrb": None, "ttcn_status": "Approved"}]
    assert all("testcase" not in entry and "group" not in entry["statuses"][0] for entry in payload)


def test_testcase_show_group_scoped(monkeypatch) -> None:
    """``testcase show --group`` scopes to a single flat ``(id, group)`` object."""
    from doc3gpp.models.testcase import TestCase, TestCaseDetail
    svc = MagicMock()
    svc.get.return_value = TestCaseDetail(
        testcase=TestCase(testcase_id="TC_D", group="UTRA", title="U"),
        statuses=[],
    )
    monkeypatch.setattr("doc3gpp.cli.build_testcase_service", lambda: svc)
    result = runner.invoke(
        app, ["testcase", "show", "--testcase", "TC_D", "--group", "UTRA", "--format", "json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert isinstance(payload, list) and len(payload) == 1
    assert payload[0]["group"] == "UTRA"
    assert payload[0]["statuses"] == []
    _, kwargs = svc.get.call_args
    assert kwargs.get("group") == "UTRA" or svc.get.call_args.args[1:] == ("UTRA",)


def test_testcase_group_validation(monkeypatch) -> None:
    _ = monkeypatch
    result = runner.invoke(app, ["testcase", "list", "--group", "NOPE"])
    assert result.exit_code != 0


def test_testcase_sync_help_shows_force() -> None:
    """``testcase sync --help`` documents the ``--force`` flag."""
    result = runner.invoke(app, ["testcase", "sync", "--help"])
    assert result.exit_code == 0, result.output
    assert "--force" in result.output


def test_testcase_sync_echoes_reason(monkeypatch) -> None:
    """``testcase sync`` with a stubbed service echoes the outcome reason."""
    svc = MagicMock()
    svc.sync.return_value = SyncOutcome(
        status="synced",
        reason="Testcase sync complete: 2 testcases, 5 statuses from f.zip",
        synced_count=2,
    )
    monkeypatch.setattr("doc3gpp.cli.build_testcase_service", lambda: svc)
    monkeypatch.setattr("doc3gpp.cli.create_schema", lambda *args, **kwargs: None)
    result = runner.invoke(app, ["testcase", "sync"])
    assert result.exit_code == 0, result.output
    assert "Testcase sync complete" in result.output
    svc.sync.assert_called_once()


def test_testcase_sync_force_flag_passed_through(monkeypatch) -> None:
    """``testcase sync --force`` forwards ``force=True`` to the service."""
    svc = MagicMock()
    svc.sync.return_value = SyncOutcome(
        status="synced",
        reason="Testcase sync complete: 1 testcases, 1 statuses from f.zip",
        synced_count=1,
    )
    monkeypatch.setattr("doc3gpp.cli.build_testcase_service", lambda: svc)
    monkeypatch.setattr("doc3gpp.cli.create_schema", lambda *args, **kwargs: None)
    result = runner.invoke(app, ["testcase", "sync", "--force"])
    assert result.exit_code == 0, result.output
    _, kwargs = svc.sync.call_args
    assert kwargs.get("force") is True


def test_testcase_show_missing(monkeypatch) -> None:
    """``testcase show --testcase NOPE`` exits non-zero with 'not found'."""
    svc = MagicMock()
    svc.get_all.return_value = []
    monkeypatch.setattr("doc3gpp.cli.build_testcase_service", lambda: svc)
    result = runner.invoke(app, ["testcase", "show", "--testcase", "NOPE"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower()

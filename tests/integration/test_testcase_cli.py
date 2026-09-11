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
from doc3gpp.models.testcase import TestCase, TestCaseWithStatuses

runner = CliRunner()


def test_testcase_list_json_shape(monkeypatch) -> None:
    svc = MagicMock()
    svc.list_recent.return_value = [
        TestCaseWithStatuses(
            testcase=TestCase(
                testcase_id="TC_1",
                title="T",
                spec="38.523-1",
                group="5G",
                release="Rel-17",
            ),
            statuses={"FR1": "Approved"},
        )
    ]
    monkeypatch.setattr("doc3gpp.cli.build_testcase_service", lambda: svc)
    result = runner.invoke(app, ["testcase", "list", "--format", "json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload[0]["statuses"] == {"FR1": "Approved"}


def test_testcase_group_validation(monkeypatch) -> None:
    _ = monkeypatch
    result = runner.invoke(app, ["testcase", "list", "--group", "NOPE"])
    assert result.exit_code != 0

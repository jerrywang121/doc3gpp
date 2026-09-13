"""Parity + HTML tests for the resource `schema` surfaces."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from tests.integration.test_web_end_to_end import app_with_deps  # noqa: F401,F811


def test_rest_schema_json_matches_cli_compact(app_with_deps, sqlite_env) -> None:  # noqa: F811
    """``GET /<r>/schema?format=json`` bytes equal CLI ``schema --format json --compact``."""
    from doc3gpp.cli import app as cli_app

    app, _ = app_with_deps
    routes = {
        "tsg": "/tsgs/schema",
        "meeting": "/meetings/schema",
        "tdoc": "/tdocs/schema",
        "wi": "/wis/schema",
        "spec": "/specs/schema",
        "testcase": "/testcases/schema",
    }
    runner = CliRunner()
    with TestClient(app) as client:
        for resource, route in routes.items():
            http_resp = client.get(route, params={"format": "json"})
            assert http_resp.status_code == 200, (resource, http_resp.text)
            cli_result = runner.invoke(cli_app, [resource, "schema", "--format", "json", "--compact"])
            assert cli_result.exit_code == 0, (resource, cli_result.output)
            assert http_resp.content.decode("utf-8") == cli_result.output, resource
            assert isinstance(json.loads(http_resp.content), list)


def test_schema_html_and_partial(app_with_deps, sqlite_env) -> None:  # noqa: F811
    app, _ = app_with_deps
    with TestClient(app) as client:
        full = client.get("/tdocs/schema")
        assert full.status_code == 200
        assert "<!DOCTYPE" in full.text
        assert "tdoc_cr_cover_page" in full.text
        assert "changed_functions" in full.text
        partial = client.get("/tdocs/schema", headers={"HX-Request": "true"})
        assert partial.status_code == 200
        assert 'id="results"' in partial.text
        assert "<!DOCTYPE" not in partial.text

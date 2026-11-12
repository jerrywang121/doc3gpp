"""Smoke-test real LISTSERV form submission and job polling in Node."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_JS_PATH = Path(__file__).resolve().parent / "_listserv_forms_smoke.js"


def test_listserv_forms_build_json_and_attach_job_status() -> None:
    if shutil.which("node") is None:
        pytest.skip("Node.js is required for the LISTSERV JavaScript smoke test")

    proc = subprocess.run(
        ["node", str(_JS_PATH)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"node LISTSERV form smoke test failed (rc={proc.returncode})\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert "PASS" in proc.stdout

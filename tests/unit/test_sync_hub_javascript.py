"""Smoke-test the /sync hub's page-local JavaScript body builders."""
from __future__ import annotations

import subprocess
from pathlib import Path

_JS_PATH = Path(__file__).resolve().parent / "_sync_hub_smoke.js"


def test_sync_hub_body_builders() -> None:
    proc = subprocess.run(
        ["node", str(_JS_PATH)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"node smoke test failed (rc={proc.returncode})\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert "PASS" in proc.stdout

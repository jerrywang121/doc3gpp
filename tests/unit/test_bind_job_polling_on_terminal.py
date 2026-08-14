"""Test the ``onTerminal`` callback option on ``bindJobPolling``.

Playwright is not available in this environment, so the assertions run in
a self-contained node smoke script (``_job_polling_smoke.js``) that loads
``src/doc3gpp/web/static/js/job_poller.js`` against hand-rolled DOM mocks
and drives the full bind->submit->poll->finish cycle deterministically.
It exits 0 on success and non-zero on failure.

The acceptance criteria exercised:

* when ``onTerminal`` is passed, the callback fires and
  ``window.location.reload`` is NOT called;
* when ``onTerminal`` is omitted, ``window.location.reload`` IS called.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

_JS_PATH = Path(__file__).resolve().parent / "_job_polling_smoke.js"


def test_bind_job_polling_on_terminal() -> None:
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

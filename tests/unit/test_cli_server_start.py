"""Behavioural tests for ``doc3gpp server start`` crash detection and pid-file guards.

The T11 implementation in :mod:`doc3gpp.cli_server` launches uvicorn as a
detached child process and reports success once the health URL responds
200. The original behaviour had two silent failure modes that this
suite locks down:

1. **Crashed child on bind failure** — when the chosen port is already
   in use, the new uvicorn process exits with ``[Errno 98]`` almost
   immediately, but the *old* server (still listening on the port) keeps
   answering the health check. ``start`` then wrote a fresh pid file
   pointing at a now-dead process and printed ``server running at
   http://...`` — a complete lie. The fix adds an early
   ``proc.poll() is not None`` check so a crashed child is surfaced as
   a non-zero exit, the pid file is not left behind, and the log tail
   is shown.

2. **No guard against a live pid file** — ``start`` would overwrite an
   existing pid file unconditionally, which races against the running
   server. The fix refuses to start when a pid file already points to a
   live process, with ``--force`` to opt in to overwriting.
"""
from __future__ import annotations

import signal
import socket
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.settings.loader import get_settings


def _write_config(
    tmp_path: Path,
    *,
    enabled: bool = True,
    port: int = 8765,
) -> Path:
    cfg = tmp_path / "doc3gpp.toml"
    cfg.write_text(
        "[server]\n"
        f"enabled = {str(enabled).lower()}\n"
        f"port = {port}\n"
        f'pid_file = "{tmp_path / "server.pid"}"\n'
        f'log_file = "{tmp_path / "server.log"}"\n',
        encoding="utf-8",
    )
    return cfg


@pytest.fixture
def isolated_config(monkeypatch, tmp_path):
    """Point ``get_settings()`` at a per-test empty config so the ambient
    user-wide TOML cannot override the test's server settings."""
    cfg = _write_config(tmp_path)
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))
    get_settings.cache_clear()
    yield cfg
    get_settings.cache_clear()


def test_start_refuses_when_pid_file_alive(isolated_config, tmp_path) -> None:
    """If the pid file points to a live process, ``start`` refuses.

    Spawns a long-lived child (the test process's own ``sleep``), writes
    its pid into the pid file, and asserts that ``server start`` exits
    non-zero with an error message naming the existing pid.
    """
    import click

    pid_path = tmp_path / "server.pid"
    proc = subprocess.Popen(["sleep", "30"])  # noqa: S603,S607
    try:
        pid_path.write_text(f"{proc.pid}\n", encoding="utf-8")
        # Verify the helper considers this pid alive (the same check the
        # guard uses) so the test's premise is sound.
        from doc3gpp.cli_server import _is_pid_alive

        assert _is_pid_alive(proc.pid) is True

        runner = CliRunner()
        result = runner.invoke(app, ["server", "start"])
        assert result.exit_code != 0, (
            f"start must refuse when pid file is alive; output:\n{result.output}"
        )
        # click.ClickException routes its message to stderr; CliRunner
        # surfaces it via ``result.exception`` rather than the captured
        # streams.
        assert isinstance(result.exception, click.ClickException), (
            f"expected ClickException; got {type(result.exception).__name__}: "
            f"{result.exception!r}"
        )
        msg = str(result.exception)
        assert "already running" in msg.lower(), (
            f"error must mention the running server; got:\n{msg}"
        )
        assert f"pid {proc.pid}" in msg, (
            f"error must name the live pid {proc.pid}; got:\n{msg}"
        )
        # The pid file must NOT have been clobbered with a new value.
        assert pid_path.read_text(encoding="utf-8") == f"{proc.pid}\n"
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=5)


def test_start_with_force_overrides_live_pid(isolated_config, tmp_path) -> None:
    """``--force`` allows start to proceed when the pid file is live.

    We can't easily let the real ``start`` actually launch uvicorn inside
    a unit test, so this test patches ``_launch_server`` to return a fake
    process object that the rest of the pipeline treats as live, and
    patches ``_wait_healthy`` to return True. The point of the test is
    that the *guard* is bypassed by ``--force`` — the rest of the
    pipeline runs to completion.
    """
    pid_path = tmp_path / "server.pid"
    proc = subprocess.Popen(["sleep", "30"])  # noqa: S603,S607
    try:
        pid_path.write_text(f"{proc.pid}\n", encoding="utf-8")

        from doc3gpp import cli_server

        class _FakeProc:
            pid = 12345

            def poll(self) -> None:
                return None

        original_launch = cli_server._launch_server
        original_wait = cli_server._wait_healthy
        original_open = cli_server._open_browser

        try:
            cli_server._launch_server = lambda *a, **kw: _FakeProc()
            cli_server._wait_healthy = lambda url, timeout=15.0: True
            cli_server._open_browser = lambda url: None
            runner = CliRunner()
            result = runner.invoke(app, ["server", "start", "--force"])
        finally:
            cli_server._launch_server = original_launch
            cli_server._wait_healthy = original_wait
            cli_server._open_browser = original_open
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=5)

        assert result.exit_code == 0, (
            f"start --force must succeed; output:\n{result.output}\n"
            f"exception: {result.exception!r}"
        )
        assert "server running at" in result.output
        # The pid file now points at the fake process's pid, not the live one.
        assert pid_path.read_text(encoding="utf-8") == "12345\n"
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=5)


def test_start_fails_when_child_crashes_on_bind(isolated_config, tmp_path) -> None:
    """If the spawned child exits before the health check, start fails.

    Simulates the user's reported scenario: a real listener is already
    bound to the chosen port. We pin a real Python ``socket`` listener
    on a random port and patch ``_launch_server`` to spawn a uvicorn
    command aimed at that port. The child crashes with
    ``[Errno 98] address already in use`` almost immediately.

    Asserts that ``start`` exits non-zero, that the pid file is *not*
    left behind pointing at the dead child, and that the user gets a
    useful error message naming the log file.
    """
    pid_path = tmp_path / "server.pid"
    log_path = tmp_path / "server.log"

    # Pin a real listener on a random port so the child's uvicorn will
    # fail to bind.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        # Rewrite the config to point at the occupied port.
        cfg = tmp_path / "doc3gpp.toml"
        cfg.write_text(
            "[server]\n"
            "enabled = true\n"
            f"port = {port}\n"
            f'pid_file = "{pid_path}"\n'
            f'log_file = "{log_path}"\n',
            encoding="utf-8",
        )
        get_settings.cache_clear()

        from doc3gpp import cli_server

        original_launch = cli_server._launch_server

        def _real_launch(bind_host, bind_port, env, log_handle):
            return original_launch(bind_host, bind_port, env, log_handle)

        try:
            cli_server._launch_server = _real_launch
            runner = CliRunner()
            result = runner.invoke(app, ["server", "start"])
        finally:
            cli_server._launch_server = original_launch
            get_settings.cache_clear()

        assert result.exit_code != 0, (
            f"start must fail when child crashes; output:\n{result.output}"
        )
        assert "child process exited" in result.output.lower(), (
            f"error must explain the child failed; got:\n{result.output}"
        )
        # The pid file must NOT exist — the dead child's pid would be misleading.
        assert not pid_path.exists(), (
            "start must not leave a pid file pointing at a dead child"
        )
        # The log file must exist and contain the bind error.
        assert log_path.exists(), "log file should have been written by the child"
        log_text = log_path.read_text(encoding="utf-8")
        assert "address already in use" in log_text, (
            f"log should contain the bind error; got:\n{log_text[-2000:]}"
        )
    finally:
        listener.close()


def test_is_pid_alive_recognises_running_and_dead() -> None:
    """``_is_pid_alive`` returns True for a live child and False for a dead one."""
    from doc3gpp.cli_server import _is_pid_alive

    proc = subprocess.Popen(["sleep", "5"])  # noqa: S603,S607
    try:
        assert _is_pid_alive(proc.pid) is True
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=5)

    assert _is_pid_alive(proc.pid) is False

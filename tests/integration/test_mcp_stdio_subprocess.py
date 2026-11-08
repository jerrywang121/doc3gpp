"""Exercise the MCP stdio CLI over a real JSON-RPC subprocess."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest


def test_mcp_stdio_subprocess_lists_shared_tools(
    sqlite_env, disable_auto_sync, tmp_path: Path, monkeypatch
):
    for module in (
        "typer",
        "mcp.server.mcpserver",
        "fastapi",
        "uvicorn",
        "jinja2",
        "markdown_it",
        "pygments",
        "humanfriendly",
    ):
        pytest.importorskip(module)

    from doc3gpp.settings.loader import get_settings
    from doc3gpp.storage.db.migrate import create_schema

    config = tmp_path / "doc3gpp.toml"
    config.write_text("[server]\nenabled = false\n[mcp]\nenabled = true\n", encoding="utf-8")
    monkeypatch.setenv("DOC3GPP_CONFIG", str(config))
    monkeypatch.setenv("DOC3GPP_CACHE__DIR", str(tmp_path / "cache"))
    get_settings.cache_clear()
    create_schema()
    marker = tmp_path / "cache" / "markdown" / "stdio-test.md"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("offline cache entry", encoding="utf-8")
    env = os.environ.copy()
    # Override an editable installation (including one pointing at another worktree).
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")

    # A file, rather than a pipe, prevents unconsumed stderr from blocking the child.
    with tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(
            [sys.executable, "-m", "doc3gpp.cli", "mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            cwd=tmp_path,
            env=env,
        )
        assert process.stdin is not None and process.stdout is not None
        lines: queue.Queue[bytes | None] = queue.Queue()

        def read_stdout() -> None:
            buffer = b""
            try:
                while chunk := os.read(process.stdout.fileno(), 65536):
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        lines.put(line)
                if buffer:
                    lines.put(buffer)
            finally:
                lines.put(None)

        reader = threading.Thread(target=read_stdout, daemon=True)
        reader.start()
        deadline = time.monotonic() + 20
        failure: Exception | None = None

        def remaining() -> float:
            seconds = deadline - time.monotonic()
            if seconds <= 0:
                raise TimeoutError("MCP subprocess deadline expired")
            return seconds

        def send(message: dict) -> None:
            process.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
            process.stdin.flush()

        def next_message() -> dict | None:
            while True:
                try:
                    line = lines.get(timeout=remaining())
                except queue.Empty:
                    raise TimeoutError("timed out waiting for stdout") from None
                if line is None:
                    return None
                if line.strip():
                    message = json.loads(line)
                    assert isinstance(message, dict), f"not a JSON-RPC object: {message!r}"
                    assert message.get("jsonrpc") == "2.0", message
                    return message

        def response(request_id: int) -> dict:
            while True:
                message = next_message()
                assert message is not None, f"stdout closed before response {request_id}"
                if message.get("id") == request_id:
                    assert "error" not in message, message
                    return message

        try:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "stdio-test", "version": "1"},
                    },
                }
            )
            initialize = response(1)
            assert initialize["result"]["serverInfo"]["name"] == "doc3gpp"
            send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
            send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            tools = response(2)["result"]["tools"]
            assert {"get_skill", "list_meetings", "get_tdoc", "sync_meetings", "get_job"} <= {
                tool["name"] for tool in tools
            }
            send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "purge_cache", "arguments": {"scope": "markdown", "yes": True},
            }})
            submission = response(3)["result"]
            assert not submission.get("isError"), submission
            queued = json.loads(submission["content"][0]["text"])
            assert queued["status"] == "queued"
            job_id = queued["job_id"]
            request_id = 4
            while True:
                send({"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {
                    "name": "get_job", "arguments": {"job_id": job_id},
                }})
                result = response(request_id)["result"]
                assert not result.get("isError"), result
                job = json.loads(result["content"][0]["text"])
                assert job["job_id"] == job_id
                if job["status"] in {"succeeded", "failed", "cancelled"}:
                    break
                request_id += 1
                time.sleep(0.05)
            assert job["status"] == "succeeded", job
            assert job["result"]["deleted"] == 1
            assert not marker.exists()
            process.stdin.close()
            assert process.wait(timeout=remaining()) == 0, "child did not exit cleanly"
            # Parse any remaining output as well, including lines emitted during shutdown.
            while next_message() is not None:
                pass
        except Exception as exc:  # noqa: BLE001 - include child stderr for any test failure
            failure = exc
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            if not process.stdin.closed:
                try:
                    process.stdin.close()
                except BrokenPipeError:
                    pass
            reader.join(timeout=5)
            process.stdout.close()

        if failure is not None:
            stderr.seek(0)
            diagnostic = stderr.read().decode("utf-8", errors="replace")
            pytest.fail(
                f"{type(failure).__name__}: {failure}\nchild stderr:\n{diagnostic}", pytrace=False
            )

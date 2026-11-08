import asyncio
from contextlib import asynccontextmanager
from threading import Event
from types import SimpleNamespace

import pytest

from doc3gpp.settings.schema import Settings
from doc3gpp.web import mcp_stdio
from doc3gpp.web.state import JobWorkerHandle


@pytest.mark.parametrize("mode", ["eof", "cancel", "cancel-twice", "request-cancel-eof"])
def test_real_stdio_sync_tool_lifetime(monkeypatch, mode):
    anyio = pytest.importorskip("anyio")
    sdk_server = pytest.importorskip("mcp.server.mcpserver.server")
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCNotification, JSONRPCRequest, JSONRPCResponse

    from doc3gpp.web.mcp_server import build_mcp_server

    settings = Settings()
    events = []
    started, release = Event(), Event()

    def list_recent(**kwargs):
        started.set()
        assert release.wait(10)
        events.append("tool finished")
        return []

    state = SimpleNamespace(
        settings=settings, jobs=None,
        services=SimpleNamespace(tdoc_content=object(), meeting=SimpleNamespace(list_recent=list_recent)),
        close_docx_pool=lambda: events.append("docx"),
        close_embedder=lambda: events.append("embedder"),
        engine=SimpleNamespace(dispose=lambda: events.append("engine")),
        testcase_engine=SimpleNamespace(dispose=lambda: events.append("testcase")),
        specdata_engine=None,
    )

    class Worker:
        def __init__(self, _state):
            pass

        async def run(self):
            await asyncio.Event().wait()

    monkeypatch.setattr(mcp_stdio, "_load_dependencies", lambda: mcp_stdio._StdioDependencies(
        build_state=lambda _: state, build_mcp_server=build_mcp_server,
        worker_type=Worker, handle_type=JobWorkerHandle,
    ))

    async def exercise():
        client_write, server_read = anyio.create_memory_object_stream(100)
        server_write, client_read = anyio.create_memory_object_stream(100)

        @asynccontextmanager
        async def transport():
            yield server_read, server_write

        monkeypatch.setattr(sdk_server, "stdio_server", transport)
        runner = asyncio.create_task(mcp_stdio.run_stdio(settings))
        try:
            await client_write.send(SessionMessage(JSONRPCRequest(
                jsonrpc="2.0", id=1, method="initialize", params={
                    "protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "probe", "version": "1"},
                },
            )))
            response = await asyncio.wait_for(client_read.receive(), 5)
            assert response.message.id == 1
            await client_write.send(SessionMessage(JSONRPCNotification(
                jsonrpc="2.0", method="notifications/initialized", params={},
            )))
            await client_write.send(SessionMessage(JSONRPCRequest(
                jsonrpc="2.0", id=2, method="tools/call",
                params={"name": "list_meetings", "arguments": {}},
            )))
            async def wait_started():
                while not started.is_set():
                    await asyncio.sleep(0.001)
            await asyncio.wait_for(wait_started(), 5)
            if mode == "request-cancel-eof":
                await client_write.send(SessionMessage(JSONRPCNotification(
                    jsonrpc="2.0", method="notifications/cancelled",
                    params={"requestId": 2, "reason": "probe"},
                )))
                # MCP notifications have no acknowledgement. A later ping
                # response confirms the SDK has processed the cancellation.
                await client_write.send(SessionMessage(JSONRPCRequest(
                    jsonrpc="2.0", id=3, method="ping", params={},
                )))
                ping = await asyncio.wait_for(client_read.receive(), 5)
                assert isinstance(ping.message, JSONRPCResponse)
                assert ping.message.id == 3 and ping.message.result == {}
            if mode in {"cancel", "cancel-twice"}:
                runner.cancel()
                if mode == "cancel-twice":
                    await asyncio.sleep(0.05)
                    runner.cancel()
            else:
                await client_write.aclose()
            done, _ = await asyncio.wait({runner}, timeout=0.25)
            assert not done, f"stdio exited while sync tool was running: {events}"
            assert "engine" not in events
        finally:
            release.set()
            await client_write.aclose()
            result = (await asyncio.gather(runner, return_exceptions=True))[0]
            if mode == "request-cancel-eof":
                async for message in client_read:
                    assert getattr(message.message, "id", None) != 2
            await client_read.aclose()
        if mode in {"cancel", "cancel-twice"}:
            assert isinstance(result, asyncio.CancelledError)
        else:
            assert result is None
        assert events.index("tool finished") < events.index("engine")

    asyncio.run(exercise())

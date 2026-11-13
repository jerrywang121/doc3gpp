import asyncio
from contextlib import asynccontextmanager
from threading import Event
from types import SimpleNamespace

import pytest

from doc3gpp.settings.schema import Settings
from doc3gpp.web import mcp_stdio
from doc3gpp.web.state import JobWorkerHandle


@pytest.mark.parametrize("mode", ["eof", "cancel", "cancel-twice", "request-cancel-eof"])
@pytest.mark.parametrize("tool_name", ["list_meetings", "listserv_sync"])
def test_real_stdio_sync_tool_lifetime(monkeypatch, mode, tool_name):
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

    from doc3gpp.models.jobs import JobKind

    def create_job(kind, params):
        assert kind is JobKind.LISTSERV_SYNC
        assert params == {"list_id": "RAN-WG1", "dates": [], "force": False}
        started.set()
        assert release.wait(10)
        events.append("tool finished")
        return SimpleNamespace(id="listserv-job", status=SimpleNamespace(value="queued"))

    state = SimpleNamespace(
        settings=settings, jobs=None,
        services=SimpleNamespace(
            tdoc_content=object(),
            meeting=SimpleNamespace(list_recent=list_recent),
            listserv=SimpleNamespace(list_ids=list),
            listserv_search=SimpleNamespace(search=lambda **_kwargs: []),
            listserv_index=SimpleNamespace(status=lambda: SimpleNamespace(to_dict=dict)),
            job_repo=SimpleNamespace(create=create_job),
        ),
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
                params={
                    "name": tool_name,
                    "arguments": (
                        {} if tool_name == "list_meetings" else {"list_id": "RAN-WG1"}
                    ),
                },
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


def test_stdio_drains_a_blocked_listserv_sync_job_before_closing_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from doc3gpp.models.jobs import JobKind
    from doc3gpp.web.workers import handlers

    settings = Settings()
    events: list[str] = []
    started, release = Event(), Event()

    class Service:
        def sync(self, list_id, *, dates, force, on_progress, cancelled):
            assert list_id == "RAN-WG1"
            assert dates == []
            assert force is False
            started.set()
            assert release.wait(10)
            events.append("sync finished")
            return SimpleNamespace(processed=0, skipped=0, failures={})

        def close(self):
            events.append("source closed")

    monkeypatch.setattr(handlers.factory, "build_listserv_sync_service", lambda _settings: Service())
    state = SimpleNamespace(
        settings=settings,
        jobs=None,
        services=SimpleNamespace(pipeline=None),
        engine=SimpleNamespace(dispose=lambda: events.append("engine")),
        testcase_engine=SimpleNamespace(dispose=lambda: events.append("testcase")),
        specdata_engine=None,
        close_docx_pool=lambda: events.append("docx"),
        close_embedder=lambda: events.append("embedder"),
    )

    class Worker:
        def __init__(self, _state):
            pass

        async def run(self):
            job = SimpleNamespace(
                kind=JobKind.LISTSERV_SYNC,
                params={"list_id": "RAN-WG1", "dates": [], "force": False},
            )
            await handlers._listserv_sync(
                job,
                state.services,
                settings,
                progress=lambda *_args, **_kwargs: None,
                cancel_event=asyncio.Event(),
            )

    class Server:
        async def run_stdio_async(self):
            while not started.is_set() and not state.jobs.task.done():
                await asyncio.sleep(0.001)

    monkeypatch.setattr(
        mcp_stdio,
        "_load_dependencies",
        lambda: mcp_stdio._StdioDependencies(
            build_state=lambda _settings: state,
            build_mcp_server=lambda _state: Server(),
            worker_type=Worker,
            handle_type=JobWorkerHandle,
        ),
    )

    async def exercise():
        runner = asyncio.create_task(mcp_stdio.run_stdio(settings))
        try:
            for _ in range(5000):
                if started.is_set():
                    break
                await asyncio.sleep(0.001)
            assert started.is_set(), "LISTSERV sync handler did not start"
            await asyncio.sleep(0.05)
            assert not runner.done(), "stdio closed resources before LISTSERV sync drained"
            assert events == []
        finally:
            release.set()
            result = (await asyncio.gather(runner, return_exceptions=True))[0]
        assert result is None
        assert events.index("sync finished") < events.index("source closed")
        assert events.index("source closed") < events.index("engine")

    asyncio.run(exercise())

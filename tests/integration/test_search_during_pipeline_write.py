import sqlite3
from contextlib import closing


def test_reader_returns_while_wal_writer_transaction_is_open(tmp_path):
    path = tmp_path / "search.db"
    with closing(sqlite3.connect(path)) as setup:
        setup.execute("PRAGMA journal_mode=WAL")
        setup.execute("CREATE TABLE docs (id INTEGER PRIMARY KEY, body TEXT)")
        setup.execute("INSERT INTO docs VALUES (1, 'searchable')")
        setup.commit()

    with closing(sqlite3.connect(path, timeout=0.1)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE docs SET body = 'updated' WHERE id = 1")
        try:
            with closing(sqlite3.connect(path, timeout=0.1)) as reader:
                assert reader.execute(
                    "SELECT body FROM docs WHERE body LIKE '%search%'"
                ).fetchone() == ("searchable",)
        finally:
            writer.rollback()


def test_search_read_completes_while_serialized_write_is_delayed(tmp_path):
    import asyncio
    import threading
    from contextlib import suppress

    from doc3gpp.services.pipeline_runtime import PipelineRuntime
    from doc3gpp.services.tdoc_pipeline import PipelineItem, TDocPipeline

    path = tmp_path / "pipeline.db"
    with closing(sqlite3.connect(path)) as setup:
        setup.execute("PRAGMA journal_mode=WAL")
        setup.execute("CREATE TABLE docs (id INTEGER PRIMARY KEY, body TEXT)")
        setup.execute("INSERT INTO docs VALUES (1, 'searchable')")
        setup.commit()

    runtime = PipelineRuntime(prepare_workers=1)
    runner = TDocPipeline(runtime)
    write_open = threading.Event()
    release_write = threading.Event()

    def persist(value):
        with closing(sqlite3.connect(path, timeout=0.1)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE docs SET body = 'updated' WHERE id = 1")
            write_open.set()
            assert release_write.wait(timeout=5)
            conn.commit()
        return value

    def search():
        with closing(sqlite3.connect(path, timeout=0.1)) as reader:
            return reader.execute("SELECT body FROM docs").fetchone()

    async def main():
        run = asyncio.create_task(
            runner.run_async(
                [PipelineItem(key="a", value="a")],
                lambda v: v,
                persist=persist,
                is_complete=lambda _v: False,
            )
        )
        while not write_open.is_set():
            await asyncio.sleep(0)
        ticker_ready = asyncio.Event()
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                ticks += 1
                ticker_ready.set()
                await asyncio.sleep(0)

        tick_task = asyncio.create_task(ticker())
        try:
            await ticker_ready.wait()
            found = await asyncio.to_thread(search)
        finally:
            tick_task.cancel()
            with suppress(asyncio.CancelledError):
                await tick_task
        assert not run.done()  # writer is still blocked mid-transaction
        assert found == ("searchable",)
        assert ticks > 0  # event loop kept running during the read
        release_write.set()
        outcomes = await run
        assert [o.status for o in outcomes] == ["success"]

    try:
        asyncio.run(main())
    finally:
        release_write.set()
        runtime.shutdown()

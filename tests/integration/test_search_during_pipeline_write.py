import sqlite3


def test_reader_returns_while_wal_writer_transaction_is_open(tmp_path):
    path = tmp_path / "search.db"
    setup = sqlite3.connect(path)
    setup.execute("PRAGMA journal_mode=WAL")
    setup.execute("CREATE TABLE docs (id INTEGER PRIMARY KEY, body TEXT)")
    setup.execute("INSERT INTO docs VALUES (1, 'searchable')")
    setup.commit()
    setup.close()

    writer = sqlite3.connect(path, timeout=0.1)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("UPDATE docs SET body = 'updated' WHERE id = 1")
    try:
        reader = sqlite3.connect(path, timeout=0.1)
        try:
            assert reader.execute("SELECT body FROM docs WHERE body LIKE '%search%'").fetchone() == (
                "searchable",
            )
        finally:
            reader.close()
    finally:
        writer.rollback()
        writer.close()


def test_search_read_completes_while_serialized_write_is_delayed(tmp_path):
    import asyncio
    import threading

    from doc3gpp.services.pipeline_runtime import PipelineRuntime
    from doc3gpp.services.tdoc_pipeline import PipelineItem, TDocPipeline

    path = tmp_path / "pipeline.db"
    setup = sqlite3.connect(path)
    setup.execute("PRAGMA journal_mode=WAL")
    setup.execute("CREATE TABLE docs (id INTEGER PRIMARY KEY, body TEXT)")
    setup.execute("INSERT INTO docs VALUES (1, 'searchable')")
    setup.commit()
    setup.close()

    runtime = PipelineRuntime(prepare_workers=1)
    runner = TDocPipeline(runtime)
    write_open = threading.Event()
    release_write = threading.Event()

    def persist(value):
        conn = sqlite3.connect(path, timeout=0.1)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE docs SET body = 'updated' WHERE id = 1")
            write_open.set()
            assert release_write.wait(timeout=5)
            conn.commit()
        finally:
            conn.close()
        return value

    def search():
        reader = sqlite3.connect(path, timeout=0.1)
        try:
            return reader.execute("SELECT body FROM docs").fetchone()
        finally:
            reader.close()

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
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0)

        tick_task = asyncio.create_task(ticker())
        found = await asyncio.to_thread(search)
        tick_task.cancel()
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

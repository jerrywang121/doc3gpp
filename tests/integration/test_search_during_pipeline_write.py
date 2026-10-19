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

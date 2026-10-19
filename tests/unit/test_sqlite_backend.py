from sqlalchemy import create_engine, text

from doc3gpp.storage.backends.sqlite import configure_sqlite_engine


def test_sqlite_backend_configures_concurrent_reader_pragmas(tmp_path):
    url = f"sqlite+pysqlite:///{tmp_path / 'corpus.db'}"
    engine = create_engine(url, **configure_sqlite_engine(url, db_echo=False))
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA journal_mode")).scalar_one() == "wal"
        assert connection.execute(text("PRAGMA busy_timeout")).scalar_one() > 0


def test_non_sqlite_backend_configuration_is_not_sqlite_specific():
    options = configure_sqlite_engine("postgresql+psycopg://localhost/doc3gpp", False)
    assert options["connect_args"] == {"check_same_thread": False}

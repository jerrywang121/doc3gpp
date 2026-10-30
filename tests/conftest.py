from __future__ import annotations

import pytest
from sqlalchemy import event
from sqlalchemy.pool import Pool

from doc3gpp.settings.loader import get_settings
from doc3gpp.storage.db.session import (
    get_engine,
    get_specdata_engine,
    get_testcase_engine,
)

_opened_records = []


def _track_connection(_dbapi_connection, connection_record) -> None:
    _opened_records.append(connection_record)


event.listen(Pool, "connect", _track_connection)


@pytest.fixture(autouse=True)
def _close_opened_connections():
    """Invalidate pooled connections a test opened, so GC never closes them late."""
    _opened_records.clear()
    yield
    for connection_record in _opened_records:
        connection_record.invalidate()
    _opened_records.clear()


def _dispose_cached_engines() -> None:
    """Close pooled sqlite connections before dropping the cached engines.

    ``cache_clear`` alone leaves the connections open until garbage collection,
    which surfaces as ``ResourceWarning: unclosed database``.
    """
    for cached in (get_engine, get_testcase_engine, get_specdata_engine):
        if cached.cache_info().currsize:
            cached().dispose()
        cached.cache_clear()


@pytest.fixture()
def sqlite_env(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    testcase_db_path = tmp_path / "test_testcase.db"
    specdata_db_path = tmp_path / "test_specdata.db"
    monkeypatch.setenv("DOC3GPP_DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv(
        "DOC3GPP_TESTCASE_DATABASE_URL", f"sqlite+pysqlite:///{testcase_db_path}"
    )
    monkeypatch.setenv(
        "DOC3GPP_SPECDATA_DATABASE_URL", f"sqlite+pysqlite:///{specdata_db_path}"
    )
    _dispose_cached_engines()
    get_settings.cache_clear()
    yield db_path
    _dispose_cached_engines()
    get_settings.cache_clear()


@pytest.fixture()
def disable_auto_sync(monkeypatch):
    """Keep CLI output tests independent of upstream sync responses."""
    monkeypatch.setenv("DOC3GPP_SYNC__AUTO_SYNC", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def search_corpus(sqlite_env):
    """Populate a sqlite engine with the FTS5 search-corpus rows.

    Depends on the existing ``sqlite_env`` fixture (which points the
    engine at a temporary file). Yields the engine so callers can
    also run their own asserts against it. The FTS5 index is
    pre-populated so tests that don't care about the indexing loop
    can go straight to ``search``.
    """
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.search_sql import (
        SQLAlchemySearchIndexRepository,
    )
    from tests.fixtures.search_corpus import build_corpus

    create_schema()
    engine = get_engine()
    tdoc_ids = build_corpus(engine)
    repo = SQLAlchemySearchIndexRepository()
    for tdoc_id in tdoc_ids:
        repo.upsert(tdoc_id)
    yield engine


@pytest.fixture()
def semantic_search_corpus(sqlite_env):
    """Populate a sqlite engine with the semantic-search corpus rows.

    Mirrors :func:`search_corpus` but in addition to FTS5 indexing
    inserts pre-computed embedding vectors through
    :class:`SQLAlchemyVectorIndexRepository` so KNN lookups return
    deterministic results without calling a remote embeddings API.

    Yields the sqlite engine so callers can layer more assertions on
    top.
    """
    from doc3gpp.models.semantic_search import EmbeddedChunk
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.search_sql import (
        SQLAlchemySearchIndexRepository,
    )
    from doc3gpp.storage.repositories.vector_sql import (
        SQLAlchemyVectorIndexRepository,
    )
    from tests.fixtures.semantic_search_corpus import (
        PRECOMPUTED_EMBEDDINGS,
        build_semantic_corpus,
    )

    create_schema()
    engine = get_engine()
    tdoc_ids = build_semantic_corpus(engine)

    fts5_repo = SQLAlchemySearchIndexRepository()
    for tdoc_id in tdoc_ids:
        fts5_repo.upsert(tdoc_id)

    vec_repo = SQLAlchemyVectorIndexRepository()
    for tdoc_id in tdoc_ids:
        vec_repo.upsert_chunks(
            tdoc_id,
            [
                EmbeddedChunk(chunk_id=f"{tdoc_id}#{i}", chunk_index=i, embedding=vec)
                for i, vec in enumerate(PRECOMPUTED_EMBEDDINGS[tdoc_id])
            ],
        )

    yield engine

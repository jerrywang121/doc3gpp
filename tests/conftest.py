from __future__ import annotations

import pytest

from doc3gpp.settings.loader import get_settings
from doc3gpp.storage.db.session import get_engine


@pytest.fixture()
def sqlite_env(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DOC3GPP_DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    get_settings.cache_clear()
    get_engine.cache_clear()
    yield db_path
    get_engine.cache_clear()
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

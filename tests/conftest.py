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

"""Unit tests for testcase DB URL resolution (no engine, no I/O)."""

from __future__ import annotations

import pytest

from doc3gpp.settings.schema import ALLOWED_ENV_VARS, Settings
from doc3gpp.storage.db.session import resolve_testcase_database_url


@pytest.fixture()
def _clean_db_env(monkeypatch: pytest.MonkeyPatch):
    """Strip DB URL env vars so direct ``Settings(...)`` construction is hermetic."""
    for key in (
        "DOC3GPP_DATABASE_URL",
        "DOC3GPP_TESTCASE_DATABASE_URL",
        "DOC3GPP_CONFIG",
    ):
        monkeypatch.delenv(key, raising=False)


def test_testcase_env_var_is_allowlisted() -> None:
    assert "DOC3GPP_TESTCASE_DATABASE_URL" in ALLOWED_ENV_VARS


def test_sibling_derivation_appends_testcase_stem(_clean_db_env) -> None:
    s = Settings(database_url="sqlite+pysqlite:////tmp/x/doc3gpp.db")
    assert s.testcase_database_url is None
    assert (
        resolve_testcase_database_url(s)
        == "sqlite+pysqlite:////tmp/x/doc3gpp_testcase.db"
    )


def test_sibling_derivation_preserves_suffixless_name(_clean_db_env) -> None:
    s = Settings(database_url="sqlite+pysqlite:////tmp/x/doc3gpp")
    assert (
        resolve_testcase_database_url(s)
        == "sqlite+pysqlite:////tmp/x/doc3gpp_testcase"
    )


def test_memory_main_gives_private_memory_testcase(_clean_db_env) -> None:
    s = Settings(database_url="sqlite+pysqlite:///:memory:")
    assert resolve_testcase_database_url(s) == "sqlite+pysqlite:///:memory:"


def test_explicit_url_wins_over_derivation(
    _clean_db_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "DOC3GPP_TESTCASE_DATABASE_URL", "sqlite+pysqlite:////tmp/x/custom-tc.db"
    )
    from doc3gpp.settings.loader import get_settings

    get_settings.cache_clear()
    try:
        s = get_settings()
        assert s.testcase_database_url == "sqlite+pysqlite:////tmp/x/custom-tc.db"
        assert resolve_testcase_database_url(s) == "sqlite+pysqlite:////tmp/x/custom-tc.db"
    finally:
        get_settings.cache_clear()


def test_non_sqlite_main_without_override_raises(_clean_db_env) -> None:
    s = Settings(database_url="oracle://user:pass@localhost/db")
    with pytest.raises(ValueError, match="testcase_database_url"):
        resolve_testcase_database_url(s)

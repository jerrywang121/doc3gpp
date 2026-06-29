from __future__ import annotations

from doc3gpp.settings.schema import Settings


def test_default_database_url_is_sqlite() -> None:
    settings = Settings()
    assert settings.database_url.startswith("sqlite")

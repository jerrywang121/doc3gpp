"""Dialect behavior for the TDoc extraction-completeness migration."""

from __future__ import annotations

from types import SimpleNamespace

from doc3gpp.storage.db import migrate


class _RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement) -> None:
        sql = getattr(statement, "text", str(statement))
        assert "sqlite_master" not in sql.lower()
        assert "pragma" not in sql.lower()
        self.statements.append(sql)


class _FakeTransaction:
    def __init__(self, connection: _RecordingConnection) -> None:
        self._connection = connection

    def __enter__(self) -> _RecordingConnection:
        return self._connection

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return False


class _FakeInspector:
    def __init__(self) -> None:
        self.checked_tables: list[str] = []

    def has_table(self, table_name: str) -> bool:
        self.checked_tables.append(table_name)
        return table_name == "tdoc_extracts"

    def get_columns(self, table_name: str) -> list[dict[str, str]]:
        assert table_name == "tdoc_extracts"
        return [{"name": "ftp_url"}, {"name": "tdoc_id"}]


class _FakeEngine:
    dialect = SimpleNamespace(name="postgresql")

    def __init__(self, connection: _RecordingConnection) -> None:
        self._connection = connection

    def begin(self) -> _FakeTransaction:
        return _FakeTransaction(self._connection)


def test_extract_completeness_migration_uses_dialect_safe_inspection(
    monkeypatch,
) -> None:
    connection = _RecordingConnection()
    inspector = _FakeInspector()
    engine = _FakeEngine(connection)
    monkeypatch.setattr(migrate, "get_engine", lambda: engine)
    monkeypatch.setattr(migrate, "inspect", lambda conn: inspector, raising=False)

    migrate._migrate_tdoc_extract_completeness()

    assert inspector.checked_tables == ["tdoc_extracts"]
    assert connection.statements == [
        (
            "ALTER TABLE tdoc_extracts ADD COLUMN extraction_version "
            "INTEGER NOT NULL DEFAULT 1"
        ),
        (
            "ALTER TABLE tdoc_extracts ADD COLUMN document_count "
            "INTEGER NOT NULL DEFAULT 0"
        ),
        "ALTER TABLE tdoc_extracts ADD COLUMN skipped_sources TEXT",
    ]

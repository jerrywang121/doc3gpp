from sqlalchemy import create_engine, inspect

from doc3gpp.storage.db.base import Base
from doc3gpp.storage.db import models as m  # noqa: F401 - registers metadata


def test_spec_tables_created() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert "specs" in tables
    assert "spec_versions" in tables


def test_tsgs_has_spec_last_sync_column() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    cols = {c["name"] for c in inspector.get_columns("tsgs")}
    assert "spec_last_sync" in cols

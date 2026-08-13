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

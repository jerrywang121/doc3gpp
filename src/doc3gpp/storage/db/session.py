from __future__ import annotations

from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from doc3gpp.config import get_settings
from doc3gpp.storage.backends import configure_sqlite_engine


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    settings = get_settings()
    return create_engine(
        settings.database_url,
        **configure_sqlite_engine(
            database_url=settings.database_url,
            db_echo=settings.db_echo,
        ),
    )


def get_session_factory() -> sessionmaker:
    return sessionmaker(bind=get_engine(), autoflush=False, autocommit=False)

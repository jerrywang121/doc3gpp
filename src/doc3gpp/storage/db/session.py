from __future__ import annotations

from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from doc3gpp.config import get_settings
from doc3gpp.storage.backends import (
    configure_mysql_engine,
    configure_postgres_engine,
    configure_sqlite_engine,
)


def _engine_kwargs(database_url: str) -> dict:
    settings = get_settings()

    if database_url.startswith("sqlite"):
        return configure_sqlite_engine(database_url=database_url, db_echo=settings.db_echo)
    if database_url.startswith("mysql"):
        return configure_mysql_engine(db_echo=settings.db_echo, db_pool_size=settings.db_pool_size)
    if database_url.startswith("postgresql"):
        return configure_postgres_engine(db_echo=settings.db_echo, db_pool_size=settings.db_pool_size)

    return {"echo": settings.db_echo, "future": True}


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    settings = get_settings()
    return create_engine(settings.database_url, **_engine_kwargs(settings.database_url))


def get_session_factory() -> sessionmaker:
    return sessionmaker(bind=get_engine(), autoflush=False, autocommit=False)

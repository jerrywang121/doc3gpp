from __future__ import annotations

from functools import lru_cache
from pathlib import PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import sessionmaker

from doc3gpp.config import get_settings
from doc3gpp.storage.backends import configure_sqlite_engine

if TYPE_CHECKING:
    from doc3gpp.settings.schema import Settings


def resolve_testcase_database_url(settings: Settings | None = None) -> str:
    """Return the effective testcase database URL.

    Explicit ``testcase_database_url`` wins. Otherwise derive a sibling
    of ``database_url`` in the same directory with ``_testcase``
    suffixed to the stem (``doc3gpp.db`` → ``doc3gpp_testcase.db``).
    ``:memory:`` mains map to a private ``:memory:`` URL (each engine
    keeps its own in-memory DB). A non-sqlite main URL cannot yield a
    sibling — raises :class:`ValueError` telling the operator to set
    ``testcase_database_url`` explicitly.
    """
    if settings is None:
        settings = get_settings()
    if settings.testcase_database_url:
        return settings.testcase_database_url
    parsed = make_url(settings.database_url)
    if parsed.database in (None, ":memory:"):
        return "sqlite+pysqlite:///:memory:"
    if not parsed.drivername.startswith("sqlite"):
        raise ValueError(
            "cannot derive a testcase database from non-sqlite "
            f"database_url {settings.database_url!r}; set "
            "testcase_database_url explicitly (TOML key "
            "'testcase_database_url' or DOC3GPP_TESTCASE_DATABASE_URL)."
        )
    path_type = PureWindowsPath if PureWindowsPath(parsed.database).is_absolute() else PurePosixPath
    db_path = path_type(parsed.database)
    sibling = db_path.with_name(f"{db_path.stem}_testcase{db_path.suffix}")
    return f"sqlite+pysqlite:///{sibling.as_posix()}"


def resolve_specdata_database_url(settings: Settings | None = None) -> str:
    """Return the effective spec-document database URL.

    Explicit ``specdata_database_url`` wins. Otherwise derive a sibling
    of ``database_url`` in the same directory with ``_specdata``
    suffixed to the stem (``doc3gpp.db`` → ``doc3gpp_specdata.db``).
    ``:memory:`` mains map to a private ``:memory:`` URL (each engine
    keeps its own in-memory DB). A non-sqlite main URL cannot yield a
    sibling — raises :class:`ValueError` telling the operator to set
    ``specdata_database_url`` explicitly.
    """
    if settings is None:
        settings = get_settings()
    if settings.specdata_database_url:
        return settings.specdata_database_url
    parsed = make_url(settings.database_url)
    if parsed.database in (None, ":memory:"):
        return "sqlite+pysqlite:///:memory:"
    if not parsed.drivername.startswith("sqlite"):
        raise ValueError(
            "cannot derive a specdata database from non-sqlite "
            f"database_url {settings.database_url!r}; set "
            "specdata_database_url explicitly (TOML key "
            "'specdata_database_url' or DOC3GPP_SPECDATA_DATABASE_URL)."
        )
    path_type = PureWindowsPath if PureWindowsPath(parsed.database).is_absolute() else PurePosixPath
    db_path = path_type(parsed.database)
    sibling = db_path.with_name(f"{db_path.stem}_specdata{db_path.suffix}")
    return f"sqlite+pysqlite:///{sibling.as_posix()}"


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


@lru_cache(maxsize=1)
def get_testcase_engine() -> Engine:
    """Return the cached engine for the testcase corpus DB."""
    settings = get_settings()
    database_url = resolve_testcase_database_url(settings)
    return create_engine(
        database_url,
        **configure_sqlite_engine(
            database_url=database_url,
            db_echo=settings.db_echo,
        ),
    )


def get_session_factory() -> sessionmaker:
    return sessionmaker(bind=get_engine(), autoflush=False, autocommit=False)


@lru_cache(maxsize=1)
def get_specdata_engine() -> Engine:
    """Return the cached engine for the spec-document corpus DB."""
    settings = get_settings()
    database_url = resolve_specdata_database_url(settings)
    return create_engine(
        database_url,
        **configure_sqlite_engine(
            database_url=database_url,
            db_echo=settings.db_echo,
        ),
    )


def get_testcase_session_factory() -> sessionmaker:
    """Return a session factory bound to the testcase engine."""
    return sessionmaker(bind=get_testcase_engine(), autoflush=False, autocommit=False)


def get_specdata_session_factory() -> sessionmaker:
    """Return a session factory bound to the specdata engine."""
    return sessionmaker(bind=get_specdata_engine(), autoflush=False, autocommit=False)

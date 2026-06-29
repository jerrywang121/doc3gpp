from __future__ import annotations


def configure_mysql_engine(db_echo: bool, db_pool_size: int) -> dict:
    """Return SQLAlchemy engine kwargs for mysql backends."""

    return {
        "echo": db_echo,
        "future": True,
        "pool_pre_ping": True,
        "pool_recycle": 3600,
        "pool_size": db_pool_size,
        "max_overflow": 10,
    }

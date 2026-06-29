from __future__ import annotations


def configure_postgres_engine(db_echo: bool, db_pool_size: int) -> dict:
    """Return SQLAlchemy engine kwargs for postgresql backends."""

    return {
        "echo": db_echo,
        "future": True,
        "pool_pre_ping": True,
        "pool_size": db_pool_size,
        "max_overflow": 10,
    }

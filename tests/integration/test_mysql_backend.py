from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.mysql


@pytest.mark.skipif(not os.getenv("DOC3GPP_TEST_MYSQL_URL"), reason="mysql url not provided")
def test_mysql_connects() -> None:
    engine = create_engine(os.environ["DOC3GPP_TEST_MYSQL_URL"], pool_pre_ping=True)
    with engine.connect() as conn:
        value = conn.execute(text("SELECT 1")).scalar_one()
    assert value == 1

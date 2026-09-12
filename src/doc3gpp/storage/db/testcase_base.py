from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class TestCaseBase(DeclarativeBase):
    """Separate declarative base for the RAN5 testcase corpus.

    The ``testcases`` / ``testcase_status`` / ``testcase_sources``
    tables live in their own sqlite file (see
    :func:`doc3gpp.storage.db.session.get_testcase_engine`), so their
    ORM classes hang off this base instead of the main ``Base``. That
    way ``Base.metadata.create_all`` never touches them and
    ``TestCaseBase.metadata.create_all`` creates exactly those three.
    """

    __test__ = False  # pytest must not collect this as a test class

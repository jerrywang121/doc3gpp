from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class SpecDataBase(DeclarativeBase):
    """Separate declarative base for the spec-document corpus.

    Mirrors TestCaseBase: spec_doc_* tables live in their own sqlite
    file so Base.metadata.create_all never touches them.
    """

    __test__ = False  # pytest must not collect this as a test class

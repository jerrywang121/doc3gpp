"""Integration tests for :class:`SQLAlchemyLSParserRepository`.

Mirrors the sibling sidecar-repo suites (``test_tdoc_cr_ttcn_sqlite.py``)
for the LS sidecar table ``tdoc_cr_ls_details``:

- Upsert + ``get_by_url`` round-trips the header fields and the
  gzip-compressed ``attachments`` blob.
- ``get_by_tdoc_id`` returns every revision row for a TDoc id.
- ``get_by_variant`` filters by the format-family tag.

Uses the same ``sqlite_env`` fixture as the rest of the integration
suite; the parent ``tdocs`` row is seeded so the FK target exists.
"""

from __future__ import annotations

import pytest

from doc3gpp.models.tdoc_ls import TDocLSDetails
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.models import TDocORM
from doc3gpp.storage.db.session import get_session_factory
from doc3gpp.storage.repositories.tdoc_cr_ls_sql import SQLAlchemyLSParserRepository


@pytest.fixture
def repo(sqlite_env):
    return SQLAlchemyLSParserRepository()


@pytest.fixture
def tdoc_row(sqlite_env):
    create_schema()
    sf = get_session_factory()
    with sf() as s:
        s.add(TDocORM(tdoc_id="R5-240001", ftp_url="tsg/ls/R5-240001.doc", type="LS", source="3GPP TSG"))
        s.commit()
    yield "R5-240001"


def test_upsert_then_get_by_url(repo, tdoc_row):
    repo.upsert(TDocLSDetails(
        tdoc_id=tdoc_row,
        ftp_url="tsg/ls/R5-240001.doc",
        variant="3gpp",
        title="LS on foo",
        source="3GPP TSG",
        attachments=( {"doc_number": "TR 38.901 v0.1.0", "description": "draft"}, ),
    ))
    got = repo.get_by_url("tsg/ls/R5-240001.doc")
    assert got is not None
    assert got.title == "LS on foo"
    assert got.variant == "3gpp"
    assert got.attachments[0]["doc_number"] == "TR 38.901 v0.1.0"


def test_get_by_tdoc_id_returns_all_revisions(repo, tdoc_row):
    for i in range(2):
        repo.upsert(TDocLSDetails(
            tdoc_id=tdoc_row,
            ftp_url=f"tsg/ls/R5-240001-{i}.doc",
            variant="3gpp",
            title=f"rev {i}",
        ))
    rows = repo.get_by_tdoc_id(tdoc_row)
    assert len(rows) == 2


def test_get_by_variant_filters(repo, tdoc_row):
    repo.upsert(TDocLSDetails(
        tdoc_id=tdoc_row, ftp_url="tsg/ls/R5-240001-a.doc",
        variant="3gpp", title="a",
    ))
    repo.upsert(TDocLSDetails(
        tdoc_id=tdoc_row, ftp_url="tsg/ls/R5-240001-b.doc",
        variant="ieee", title="b",
    ))
    assert repo.get_by_variant("tsg/ls/R5-240001-a.doc", "3gpp").title == "a"
    assert repo.get_by_variant("tsg/ls/R5-240001-b.doc", "ieee").title == "b"
    assert repo.get_by_variant("tsg/ls/R5-240001-a.doc", "ieee") is None

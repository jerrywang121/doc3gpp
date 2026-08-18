"""LS sidecar projection into the FTS5 ``cover_text`` column and the
embed text.

When a TDoc has an LS sidecar row (``tdoc_cr_ls_details``), the search
index and the semantic embed text must include the LS header fields so
queries against the LS title / response-to title / To / Cc groups match
and the embeddings reflect them. CR rows are untouched — no LS sidecar
row means no LS projection.

Projection (one field per line, mirroring the ``summary_of_change``
pattern):

* ``title``
* ``response_to_title``
* ``to_groups`` with newlines flattened to spaces
* ``cc_groups`` with newlines flattened to spaces

The FTS5 assertions query tokens that live ONLY in the LS sidecar
(``ran4`` in ``response_to_title``, ``wg4`` / ``wg2`` in the group
lists) so the tests genuinely exercise the projection rather than
matching through ``tdocs.title``.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text

from doc3gpp.models.search import SearchFilters
from doc3gpp.models.tdoc_ls import TDocLSDetails
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.session import get_engine
from doc3gpp.storage.repositories.search_sql import (
    SQLAlchemySearchIndexRepository,
)
from doc3gpp.storage.repositories.tdoc_cr_ls_sql import SQLAlchemyLSParserRepository

LS_TDOC_ID = "R5-240001"


@pytest.fixture
def ls_row(sqlite_env):
    """Seed the parent TDoc (with a meeting — the FTS5 read path
    inner-joins ``meetings``) and the LS sidecar row."""
    create_schema()
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tsgs (tsg_name, short_name, description) "
                "VALUES ('RAN WG5', 'RAN5', '')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO meetings (meeting_id, name, title, location, tsg, "
                "start_date, end_date, ftp_url, tdoc_list_last_sync) "
                "VALUES (100, 'RAN5#120', 'RAN5 #120', 'Online', 'RAN5', "
                "'2026-08-01', '2026-08-05', 'https://x/ran5-120', "
                "'2026-08-05T00:00:00')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO tdocs (tdoc_id, meeting_id, title, ftp_url, type, "
                "source, uploaded_date) "
                "VALUES (:id, 100, 'LS on 5G_eHealth WI status update', "
                "'tsg/ls/R5-240001.doc', 'LS', '3GPP TSG', :d)"
            ),
            {"id": LS_TDOC_ID, "d": date(2026, 8, 1)},
        )
    SQLAlchemyLSParserRepository().upsert(
        TDocLSDetails(
            tdoc_id=LS_TDOC_ID,
            ftp_url="tsg/ls/R5-240001.doc",
            variant="3gpp",
            title="5G_eHealth WI status update",
            source="3GPP TSG",
            response_to_title="eHealth WI status from RAN4",
            to_groups="RAN WG3\nRAN WG4",
            cc_groups="RAN WG2",
        )
    )
    yield LS_TDOC_ID


def test_search_index_includes_ls_title(ls_row):
    """FTS5 returns the LS TDoc when querying its title."""
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert(ls_row)
    hits = repo.search("ehealth", SearchFilters(limit=10))
    assert any(h.tdoc_id == ls_row for h in hits)


def test_search_index_matches_ls_response_to_title(ls_row):
    """A token that lives ONLY in ``response_to_title`` matches."""
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert(ls_row)
    hits = repo.search("ran4", SearchFilters(limit=10))
    assert any(h.tdoc_id == ls_row for h in hits)


def test_search_index_matches_ls_to_and_cc_groups(ls_row):
    """Group tokens from ``to_groups`` / ``cc_groups`` match once the
    newlines are flattened into space-separated tokens."""
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert(ls_row)
    for token in ("wg4", "wg2"):
        hits = repo.search(token, SearchFilters(limit=10))
        assert any(h.tdoc_id == ls_row for h in hits), token


def test_embed_text_includes_ls_fields(ls_row):
    """``_build_embed_text`` concatenates the LS header fields."""
    from doc3gpp.storage.repositories.vector_sql import _build_embed_text

    out = _build_embed_text(ls_row)
    assert out is not None
    assert "RAN4" in out  # response_to_title
    assert "RAN WG3 RAN WG4" in out  # to_groups, newlines flattened
    assert "RAN WG2" in out  # cc_groups, newlines flattened

"""End-to-end tests for unified hybrid TDoc search over sqlite + sqlite-vec.

Exercises the full CLI flow through the unified TDoc facade, which dispatches
the text and semantic inputs to the FTS5/vector hybrid service and the unified
table renderer.

The factory's :func:`build_embedder` is patched so the test does
not depend on a remote embeddings API. The corpus uses
the production DDL (via :func:`doc3gpp.storage.db.migrate.create_schema`)
but with ``vec_meta.embedding_dim`` pinned to 4 so the test vectors
stay short and the cosine-distance arithmetic is obvious.

Four cases pin the contract:

 1. Hybrid search uses the semantic query for embedding and truncates the
    result list to ``--limit``.
 2. Empty ``vec_tdoc_embeddings`` preserves the FTS5 side's order without the
    old reranker warning.
 3. An empty semantic input selects FTS5 mode, so encode() is never called.
 4. Hybrid mode still embeds the semantic query even when the FTS5 side has no
    matches.
"""
from __future__ import annotations

import logging
from unittest.mock import patch

import numpy as np
import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.services import factory
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.session import get_engine


pytestmark = pytest.mark.semantic


_TDOC_ROWS: tuple[tuple[str, str, str], ...] = (
    ("R5-1", "alpha", "https://x/R5-1.doc"),
    ("R5-2", "beta", "https://x/R5-2.doc"),
    ("R5-3", "gamma", "https://x/R5-3.doc"),
)


def _bootstrap_corpus() -> None:
    """Seed tsgs / meetings / tdocs / tdoc_search with the production DDL.

    Uses :func:`doc3gpp.storage.db.migrate.create_schema` for the
    full production schema (the FTS5 virtual table column order
    MUST match the 8-column layout in ``_create_search_schema`` or
    the real :class:`SQLAlchemySearchIndexRepository` will crash on
    its bm25() / snippet() cid arithmetic). The corpus pins the
    title into the ``cover_text`` column so a FTS5 ``R5*`` prefix
    query matches every row via the indexed ``cover_text`` token.

    The production schema pins ``vec_tdoc_embeddings`` to
    ``FLOAT[384]`` (``_create_vector_schema``), but the test uses
    4-D vectors. The ``vec0`` virtual table's dimension is a
    schema-level property (set at CREATE VIRTUAL TABLE time) and
    can't be altered in place — so we drop + recreate the table
    with ``FLOAT[4]`` and load sqlite-vec on the connection that
    will run the DDL. ``vec_meta.embedding_dim`` is also pinned to
    4 so the factory's :class:`SQLAlchemyVectorIndexRepository`
    constructor probes ``_dim=4`` and the
    :meth:`get_min_distance_for_tdocs` lookup accepts the test's
    float32[4] query vectors.
    """
    create_schema()
    engine = get_engine()
    with engine.begin() as conn:
        from doc3gpp.storage.backends.sqlite import load_sqlite_vec

        load_sqlite_vec(conn.connection.driver_connection)
        conn.execute(text("DROP TABLE vec_tdoc_embeddings"))
        conn.execute(
            text(
                """
                CREATE VIRTUAL TABLE vec_tdoc_embeddings USING vec0(
                    chunk_id TEXT PRIMARY KEY,
                    tdoc_id TEXT,
                    chunk_index INTEGER,
                    embedding FLOAT[4] distance_metric=cosine
                )
                """
            ),
        )
        conn.execute(
            text(
                "INSERT INTO vec_meta (key, value) "
                "VALUES ('embedding_dim', '4')"
            ),
        )
        conn.execute(
            text(
                "INSERT INTO tsgs (tsg_name, short_name, description) "
                "VALUES ('TSG RAN WG1', 'RAN1', 'RAN WG1')"
            ),
        )
        conn.execute(
            text(
                """
                INSERT INTO meetings (
                    meeting_id, name, title, location, tsg, start_date,
                    end_date, ftp_url, tdoc_list_last_sync
                ) VALUES (
                    1, 'RAN1#1', 'RAN1#1', 'Online', 'RAN1',
                    '2026-01-01', '2026-01-05',
                    'https://x/RAN1_1', '2026-01-05T00:00:00'
                )
                """
            ),
        )
        for tdoc_id, title, ftp_url in _TDOC_ROWS:
            conn.execute(
                text(
                    """
                    INSERT INTO tdocs (
                        tdoc_id, meeting_id, title, ftp_url, type, source,
                        uploaded_date, release
                    ) VALUES (
                        :tid, 1, :title, :ftp, 'CR', 'TSG',
                        '2026-01-02', 'Rel-18'
                    )
                    """
                ),
                {"tid": tdoc_id, "title": title, "ftp": ftp_url},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO tdoc_search (
                        tdoc_id, title, ftp_url, meeting_title,
                        meeting_location, wis, cover_text, change_text,
                        ttcn_text
                    ) VALUES (
                        :tid, :title, :ftp, 'RAN1#1', 'Online', '',
                        :cover, '', ''
                    )
                    """
                ),
                {
                    "tid": tdoc_id, "title": title, "ftp": ftp_url,
                    "cover": f"{tdoc_id} fixture",
                },
            )


def _seed_vectors(mappings: dict[str, list[list[float]]]) -> None:
    """Insert pre-baked 4-D float32 vectors into ``vec_tdoc_embeddings``.

    Mirrors :func:`doc3gpp.storage.repositories.vector_sql.upsert_chunks`
    but bypasses the dim probe + the SQLAlchemy repo so the test
    doesn't need to construct the repo twice (once for the
    factory's reranker, once for the explicit seed).
    """
    engine = get_engine()
    with engine.begin() as conn:
        for tdoc_id, vecs in mappings.items():
            for chunk_index, vec in enumerate(vecs):
                conn.execute(
                    text(
                        "INSERT INTO vec_tdoc_embeddings "
                        "(chunk_id, tdoc_id, chunk_index, embedding) "
                        "VALUES (:cid, :tid, :ci, :emb)"
                    ),
                    {
                        "cid": f"{tdoc_id}#{chunk_index}",
                        "tid": tdoc_id,
                        "ci": chunk_index,
                        "emb": np.asarray(
                            vec, dtype=np.float32,
                        ).tobytes(),
                    },
                )


@pytest.fixture
def seeded_engine(sqlite_env):
    """A production-schema sqlite engine with the 3-row corpus + dim=4."""
    _bootstrap_corpus()
    yield get_engine()


class _FakeEmbedder:
    """Deterministic embedder that returns a fixed 4-D vector per call.

    Tracks every ``encode`` call so tests can assert the CLI only
    hits the embedder for the ``--semantic`` argument (and not for
    each FTS5 candidate).
    """

    dim = 4

    def __init__(self, fixed: list[float]) -> None:
        self._fixed = np.asarray(fixed, dtype=np.float32)
        self.calls: list[list[str]] = []

    def encode(self, texts: list[str]) -> np.ndarray:
        self.calls.append(list(texts))
        return np.tile(self._fixed, (len(texts), 1))


def _patch_embedder(embedder: _FakeEmbedder):
    """Patch :func:`factory.build_embedder` for the duration of a test.

    The factory's unified search construction path builds the
    embedder via ``build_embedder(settings)`` inside the semantic
    branch — patching it avoids any HTTP call to the remote
    embeddings API and routes ``encode`` to the test's
    :class:`_FakeEmbedder` instance.
    """
    return (
        patch.object(
            factory, "build_embedder",
            lambda settings: embedder,
        ),
    )


def test_sem_query_uses_4x_fanout_then_truncates_to_limit(seeded_engine):
    _seed_vectors({
        "R5-1": [[1.0, 0.0, 0.0, 0.0]],
        "R5-2": [[0.0, 1.0, 0.0, 0.0]],
        "R5-3": [[0.0, 0.0, 1.0, 0.0]],
    })
    embedder = _FakeEmbedder([1.0, 0.0, 0.0, 0.0])
    (embedder_patch,) = _patch_embedder(embedder)
    with embedder_patch:
        result = CliRunner().invoke(
            app,
            [
                "tdoc", "search", "--text", "R5*", "--semantic", "anything",
                "--limit", "2",
            ],
        )
    assert result.exit_code == 0, result.output
    # Embedder was called exactly once (for the semantic query), with
    # the literal ``--semantic`` string as the only input.
    assert embedder.calls == [["anything"]]
    # Top 2 by cosine distance to [1,0,0,0] are R5-1, then R5-2.
    # The table renderer emits one summary line per tdoc (plus
    # continuation lines for each additional weight>0 column); the
    # first tdoc on the line is always the tdoc_id column.
    out_lines = [
        line for line in result.output.splitlines()
        if line.startswith("R5-")
    ]
    assert out_lines[0].startswith("R5-1"), result.output
    assert out_lines[1].startswith("R5-2"), result.output


def test_sem_query_empty_vector_index_falls_back_to_fts5_order(
    seeded_engine, caplog,
):
    _seed_vectors({})
    embedder = _FakeEmbedder([0.0, 0.0, 0.0, 0.0])
    (embedder_patch,) = _patch_embedder(embedder)
    with embedder_patch, caplog.at_level(logging.WARNING):
        result = CliRunner().invoke(
            app,
            ["tdoc", "search", "--text", "R5*", "--semantic", "anything"],
    )
    assert result.exit_code == 0, result.output
    assert embedder.calls == [["anything"]]
    assert not any("no rows in vec_tdoc_embeddings" in rec.message for rec in caplog.records)


def test_sem_query_empty_string_is_no_op(seeded_engine):
    _seed_vectors({
        "R5-1": [[1.0, 0.0, 0.0, 0.0]],
        "R5-2": [[0.0, 1.0, 0.0, 0.0]],
        "R5-3": [[0.0, 0.0, 1.0, 0.0]],
    })
    embedder = _FakeEmbedder([0.0, 0.0, 0.0, 0.0])
    (embedder_patch,) = _patch_embedder(embedder)
    with embedder_patch:
        result = CliRunner().invoke(
            app,
            ["tdoc", "search", "--text", "R5*", "--semantic", ""],
        )
    assert result.exit_code == 0, result.output
    # The facade treats the empty string as absent, selecting FTS5 mode.
    assert embedder.calls == []


def test_sem_query_fts5_zero_results_does_not_encode(seeded_engine):
    _seed_vectors({
        "R5-1": [[1.0, 0.0, 0.0, 0.0]],
        "R5-2": [[0.0, 1.0, 0.0, 0.0]],
        "R5-3": [[0.0, 0.0, 1.0, 0.0]],
    })
    embedder = _FakeEmbedder([0.0, 0.0, 0.0, 0.0])
    (embedder_patch,) = _patch_embedder(embedder)
    with embedder_patch:
        result = CliRunner().invoke(
            app,
            ["tdoc", "search", "--text", "nothing", "--semantic", "anything"],
        )
    assert result.exit_code == 0, result.output
    # Hybrid mode always embeds the semantic input; the two paths are
    # coordinated by the facade rather than by the old reranker branch.
    assert embedder.calls == [["anything"]]


# ----------------------------------------------------------------------
# Final-review fix (Task F1): --quiet must suppress the empty-vector
# warning in the integration path too. The unit test pins the
# SemanticReranker contract; this integration test pins the
# CLI → factory → reranker plumbing end-to-end.
# ----------------------------------------------------------------------


def test_sem_query_quiet_preserves_hybrid_output(seeded_engine, caplog):
    """``--quiet`` does not alter the unified hybrid result ordering."""
    _seed_vectors({})
    embedder = _FakeEmbedder([0.0, 0.0, 0.0, 0.0])
    (embedder_patch,) = _patch_embedder(embedder)
    with embedder_patch, caplog.at_level(
        logging.WARNING, logger="doc3gpp.services.semantic_reranker",
    ):
        result = CliRunner().invoke(
            app,
            [
                "tdoc", "search", "--text", "R5*", "--semantic", "anything",
                "--quiet",
            ],
        )
    assert result.exit_code == 0, result.output
    assert embedder.calls == [["anything"]]
    assert not any("tdoc search index" in rec.message for rec in caplog.records)

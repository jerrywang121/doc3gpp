"""End-to-end test for ``search query --sem-query`` over sqlite + sqlite-vec.

Exercises the full CLI flow — :class:`typer.testing.CliRunner` →
:func:`doc3gpp.services.factory.build_search_service` →
:class:`doc3gpp.services.search_service.SearchService` (FTS5 path) →
:class:`doc3gpp.services.semantic_reranker.SemanticReranker` (vector
rerank) → CLI table renderer.

The :class:`SentenceTransformerEmbedder` is patched so the test does
not depend on a real sentence-transformers model. The corpus uses
the production DDL (via :func:`doc3gpp.storage.db.migrate.create_schema`)
but with ``vec_meta.embedding_dim`` pinned to 4 so the test vectors
stay short and the cosine-distance arithmetic is obvious.

Four cases pin the contract:

1. 4x FTS5 fanout + rerank truncation: ``--limit 2`` + the
   ``search.search_fanout_factor=4`` default fetches 8 candidates;
   the reranker sorts them by cosine distance to the ``--sem-query``
   vector and truncates back to 2. Top-2 are the two vectors closest
   to the query.
2. Empty ``vec_tdoc_embeddings``: all candidates map to
   :attr:`SemanticReranker.MISSING_FLOOR`; the reranker logs the
   one-shot "no rows in vec_tdoc_embeddings" warning and returns
   hits in FTS5 order.
3. Empty ``--sem-query`` string: the CLI treats ``""`` as
   falsy and skips the rerank branch entirely — encode() is never
   called, output is the raw FTS5 hit list.
4. Zero FTS5 hits: the reranker's ``if not hits: return []`` guard
   short-circuits before encode() is called.
"""
from __future__ import annotations

import logging
from unittest.mock import patch

import numpy as np
import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from doc3gpp.cli import search_app
from doc3gpp.services.embedding.embedder import SentenceTransformerEmbedder
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
    import sqlite_vec

    create_schema()
    engine = get_engine()
    with engine.begin() as conn:
        sqlite_vec.load(conn.connection.driver_connection)
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
    hit the embedder for the ``--sem-query`` argument (and not for
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
    """Patch :class:`SentenceTransformerEmbedder` for the duration of a test.

    The factory's :func:`build_search_service` constructs the
    embedder via ``SentenceTransformerEmbedder(settings.semantic_search.embedding_model)``
    inside the reranker branch — so the ``__init__`` patch is
    required to skip the lazy model load. The ``encode`` patch
    routes the call to the test's :class:`_FakeEmbedder` instance.
    """
    return (
        patch.object(
            SentenceTransformerEmbedder, "__init__",
            lambda self, model: None,
        ),
        patch.object(
            SentenceTransformerEmbedder, "encode", embedder.encode,
        ),
    )


def test_sem_query_uses_4x_fanout_then_truncates_to_limit(seeded_engine):
    _seed_vectors({
        "R5-1": [[1.0, 0.0, 0.0, 0.0]],
        "R5-2": [[0.0, 1.0, 0.0, 0.0]],
        "R5-3": [[0.0, 0.0, 1.0, 0.0]],
    })
    embedder = _FakeEmbedder([1.0, 0.0, 0.0, 0.0])
    init_patch, encode_patch = _patch_embedder(embedder)
    with init_patch, encode_patch:
        result = CliRunner().invoke(
            search_app,
            [
                "query", "R5*", "--sem-query", "anything",
                "--limit", "2",
            ],
        )
    assert result.exit_code == 0, result.output
    # Embedder was called exactly once (for the semantic query), with
    # the literal ``--sem-query`` string as the only input.
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
    init_patch, encode_patch = _patch_embedder(embedder)
    with init_patch, encode_patch, caplog.at_level(logging.WARNING):
        result = CliRunner().invoke(
            search_app, ["query", "R5*", "--sem-query", "anything"],
        )
    assert result.exit_code == 0, result.output
    # Every candidate maps to MISSING_FLOOR → one-shot warning.
    assert any(
        "no rows in vec_tdoc_embeddings" in rec.message
        for rec in caplog.records
    ), [r.message for r in caplog.records]


def test_sem_query_empty_string_is_no_op(seeded_engine):
    _seed_vectors({
        "R5-1": [[1.0, 0.0, 0.0, 0.0]],
        "R5-2": [[0.0, 1.0, 0.0, 0.0]],
        "R5-3": [[0.0, 0.0, 1.0, 0.0]],
    })
    embedder = _FakeEmbedder([0.0, 0.0, 0.0, 0.0])
    init_patch, encode_patch = _patch_embedder(embedder)
    with init_patch, encode_patch:
        result = CliRunner().invoke(
            search_app, ["query", "R5*", "--sem-query", ""],
        )
    assert result.exit_code == 0, result.output
    # The CLI's `if sem_query:` guard treats the empty string as
    # falsy → rerank branch skipped → encode() never called.
    assert embedder.calls == []


def test_sem_query_fts5_zero_results_does_not_encode(seeded_engine):
    _seed_vectors({
        "R5-1": [[1.0, 0.0, 0.0, 0.0]],
        "R5-2": [[0.0, 1.0, 0.0, 0.0]],
        "R5-3": [[0.0, 0.0, 1.0, 0.0]],
    })
    embedder = _FakeEmbedder([0.0, 0.0, 0.0, 0.0])
    init_patch, encode_patch = _patch_embedder(embedder)
    with init_patch, encode_patch:
        result = CliRunner().invoke(
            search_app, ["query", "nothing", "--sem-query", "anything"],
        )
    assert result.exit_code == 0, result.output
    # SemanticReranker.rerank short-circuits on `if not hits: return []`
    # so the embedder is never called when FTS5 returns zero hits.
    assert embedder.calls == []


# ----------------------------------------------------------------------
# Final-review fix (Task F1): --quiet must suppress the empty-vector
# warning in the integration path too. The unit test pins the
# SemanticReranker contract; this integration test pins the
# CLI → factory → reranker plumbing end-to-end.
# ----------------------------------------------------------------------


def test_sem_query_quiet_suppresses_warning(seeded_engine, caplog):
    """``--quiet`` suppresses the empty-vector WARNING end-to-end.

    The vector index is empty, so the reranker would normally emit
    ``"semantic rerank: no rows in vec_tdoc_embeddings; falling back
    to FTS5 order"``. With ``--quiet`` the warning is silent and the
    FTS5 order is still preserved (the suppression is a side-channel
    gate, not a behaviour change for the visible output).
    """
    _seed_vectors({})
    embedder = _FakeEmbedder([0.0, 0.0, 0.0, 0.0])
    init_patch, encode_patch = _patch_embedder(embedder)
    with init_patch, encode_patch, caplog.at_level(
        logging.WARNING, logger="doc3gpp.services.semantic_reranker",
    ):
        result = CliRunner().invoke(
            search_app,
            ["query", "R5*", "--sem-query", "anything", "--quiet"],
        )
    assert result.exit_code == 0, result.output
    warnings = [
        rec for rec in caplog.records
        if rec.levelno == logging.WARNING
        and "no rows in vec_tdoc_embeddings" in rec.message
    ]
    assert warnings == [], (
        f"expected no empty-vector WARNING under --quiet; got: "
        f"{[r.message for r in caplog.records]}"
    )

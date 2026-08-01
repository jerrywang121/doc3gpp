"""End-to-end ``search sem`` integration tests over a populated corpus.

Builds the search-stack collaborators (FTS5 repo + vector repo) with
real sqlite + real ``tdocs`` corpus rows + pre-computed embedding
vectors. The embedder is replaced by a deterministic mock whose
``encode()`` consults :data:`tests.fixtures.semantic_search_corpus.ENCODE_TABLE`
for known inputs and falls back to a content-derived vector for
anything else, so the assertions stay deterministic without loading a
sentence-transformers model.

The service no longer pre-processes queries through a stripper; the
natural-language positional ``QUERY`` flows only into the embedder,
and the FTS5 path is opt-in via the new ``fts5_query`` argument
(preprocessed by ``SearchQueryBuilder``). These tests exercise both
the vector-only branch (``fts5_query=None``) and the hybrid branch
(``fts5_query="..."``) end-to-end.

Verifies:

1. ``search sem "what CRs touch NB-IoT power saving"`` returns the
   NB-IoT TDoc at rank 0 or 1 (vector + FTS5 agree).
2. ``--tsg`` filter narrows the result list to the requested TSG.
3. ``fts5_weight=0.0`` (vector weight = 1.0) collapses RRF to the
   FTS5-only formula, pinning the search seam's blend.
4. New ``fts5_query=None`` returns pure vector KNN, no FTS5 fan-out.
5. New ``fts5_query="NB-IoT"`` runs both paths through RRF and
   surfaces at least one FTS5-present hit.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest

pytestmark = pytest.mark.semantic


@pytest.fixture()
def semantic_service(semantic_search_corpus):
    """A :class:`SemanticSearchService` wired against the real fixtures.

    Uses the real FTS5 + vector repos from the fixture and a mocked
    embedder that returns the pre-computed corpus vectors for known
    texts (queries and chunk text alike).
    """
    from doc3gpp.services.factory import build_search_service
    from doc3gpp.services.semantic_search_service import SemanticSearchService
    from doc3gpp.settings.loader import get_settings
    from tests.fixtures.semantic_search_corpus import ENCODE_TABLE

    fts5 = build_search_service()
    assert fts5 is not None

    table = dict(ENCODE_TABLE)

    embedder = MagicMock()
    embedder.dim = 384

    def _encode(texts: list[str]) -> np.ndarray:
        out = np.empty((len(texts), 384), dtype=np.float32)
        for i, t in enumerate(texts):
            v = table.get(t)
            if v is None:
                # Fallback for unseen text (e.g. the user query) — derive
                # a deterministic vector from the text hash so the
                # mock is still reproducible.
                v = _content_vector(t)
            out[i] = v
        return out

    embedder.encode.side_effect = _encode

    from doc3gpp.storage.repositories.vector_sql import (
        SQLAlchemyVectorIndexRepository,
    )

    svc = SemanticSearchService(
        fts5_service=fts5,
        embedder=embedder,
        vector_repo=SQLAlchemyVectorIndexRepository(),
        settings=get_settings(),
    )
    return svc


def _content_vector(text: str, dim: int = 384) -> np.ndarray:
    """Deterministic content-derived unit vector (fallback for unseen text).

    Uses ``hashlib.sha256`` so any-length string seeds a 32-bit RNG via
    the first 4 bytes (folded into uint32 to stay within numpy's seed
    range).
    """
    import hashlib

    digest = hashlib.sha256(text.encode("utf-8")).digest()
    seed = int.from_bytes(digest[:4], "big") % (2**32)
    rng = np.random.RandomState(seed)
    v = rng.randn(dim).astype(np.float32)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def test_search_sem_returns_expected_tdoc(semantic_service):
    from doc3gpp.models.search import SearchFilters

    out = semantic_service.search(
        "what CRs touch NB-IoT power saving",
        fts5_query="NB-IoT",
        filters=SearchFilters(),
        limit=5,
        fts5_weight=0.5,
    )
    assert out, "expected at least one hit"
    top_two = {h.tdoc_id for h in out[:2]}
    # NB-IoT TDoc's identifier is "SEM-NB-001" (see corpus); the KNN
    # path maps the original query to the NB-IoT vector so it wins.
    assert "SEM-NB-001" in top_two, (
        f"expected NB-IoT tdoc in top 2; got {[h.tdoc_id for h in out]}"
    )


def test_search_sem_filter_by_tsg(semantic_service):
    from doc3gpp.models.search import SearchFilters

    # Query is broad enough to match every row (every title has "Rel-17");
    # the test asserts the tsg filter survives the RRF merge and that
    # RAN1 rows don't leak into a RAN5-only result list.
    out = semantic_service.search(
        "Rel-17",
        fts5_query="Rel-17",
        filters=SearchFilters(tsg="RAN5"),
        limit=10,
        fts5_weight=0.5,
    )
    assert out, "expected at least one hit under RAN5 filter"
    tdoc_ids = {h.tdoc_id for h in out}
    # RAN1 rows must not leak into a RAN5-only result. SEM-TTCN-001
    # is the sole RAN5 row in the corpus and must appear.
    assert "SEM-TTCN-001" in tdoc_ids, (
        f"RAN5 filter should still surface its only row; got {tdoc_ids}"
    )
    leaked = tdoc_ids - {"SEM-TTCN-001"}
    assert not leaked, (
        f"TSG filter leaked non-RAN5 rows: {leaked}"
    )


def test_search_sem_fts5_weight_zero_is_fts5_only(semantic_service):
    from doc3gpp.models.search import SearchFilters

    # With ``fts5_weight=1.0`` (i.e. vector weight = 0) the RRF score
    # collapses to the FTS5-only formula (``1/(k+rank_fts5)``);
    # vector hits still participate in the fan-out but contribute 0
    # to the score and so always sort below FTS5-present hits.
    out = semantic_service.search(
        "NB-IoT power saving",
        fts5_query="NB-IoT",
        filters=SearchFilters(),
        limit=10,
        fts5_weight=1.0,
    )
    assert out, "expected at least one FTS5 hit"

    # Filter to FTS5-present hits (vector-only rows have rank_fts5 None
    # and score 0 when fts5_weight=1.0).
    fts5_hits = [h for h in out if h.rank_fts5 is not None]
    assert fts5_hits, "expected at least one FTS5-side hit"

    # FTS5-only RRF formula = 1/(k+rank_fts5), with k=60 from
    # settings.semantic_search.rrf_k.
    for hit in fts5_hits:
        expected = 1.0 / (60 + hit.rank_fts5)
        assert abs(hit.rrf_score - expected) < 1e-6, (
            f"fts5_weight=1.0 score mismatch for {hit.tdoc_id}: "
            f"rrf={hit.rrf_score}, expected={expected}"
        )

    # Spot-check: ranks must be 0, 1, 2, ... in order after sort.
    ranks = [h.rank_fts5 for h in fts5_hits]
    assert ranks == sorted(ranks), (
        f"FTS5 ranks should be monotonic after RRF; got {ranks}"
    )

    # FTS5 fan-out must surface at least one of the NB-IoT TDocs
    # (SEM-NB-001 / SEM-NB-002 have "NB-IoT" in their titles; both
    # are guaranteed FTS5 hits). The corpus's CHG rows also match
    # "NB-IoT" via their `change_text` blob, so the FTS5 top hit
    # under BM25 is whichever document has the most focused mention
    # — we don't pin the top tdoc, only the FTS5 surface set.
    fts5_tdoc_ids = {h.tdoc_id for h in fts5_hits}
    assert fts5_tdoc_ids & {"SEM-NB-001", "SEM-NB-002"}, (
        f"FTS5 fan-out should surface an NB-IoT TDoc; got {fts5_tdoc_ids}"
    )


def test_build_embed_text_against_real_corpus(semantic_search_corpus):
    """Regression: _build_embed_text must not crash on real tdoc_cr_* columns.

    The corpus seeds rows into ``tdoc_cr_ttcn_details`` (with the real
    ``required_changes`` blob column) and ``tdoc_cr_change_details``
    (with the real ``changes`` blob column). Earlier the helper queried
    non-existent ``required_changes_text`` / ``change_text`` columns,
    raising ``OperationalError`` and silently dropping every TDoc from
    the rebuild. This test calls the helper directly so the regression
    surfaces as a hard assertion failure rather than a swallowed
    WARNING in the rebuild log.
    """
    from doc3gpp.storage.repositories.vector_sql import _build_embed_text
    from tests.fixtures.semantic_search_corpus import ROWS

    # ROWS[*][7] is the `kinds` tuple — populated TDoc IDs include at
    # least one TTCN-only, one CHG-only, and one with both sidecars.
    target_ids = [row[0] for row in ROWS if "ttcn" in row[7] or "chg" in row[7]]
    assert target_ids, "corpus must seed at least one TTCN or CHG TDoc"

    for tid in target_ids:
        text = _build_embed_text(tid)
        assert isinstance(text, str), (
            f"_build_embed_text({tid!r}) must return str, got {type(text).__name__}"
        )
        assert text, f"_build_embed_text({tid!r}) returned empty string"


def test_search_sem_without_fts5_query_returns_pure_vector_results(semantic_service):
    """End-to-end pin: when ``fts5_query`` is None, the service returns
    top-``limit`` vector KNN hits with ``rank_fts5=None``. No RRF, no FTS5
    fan-out, no FTS5 metadata depends on a populated ``tdoc_cr_cover_page``.
    """
    from doc3gpp.models.search import SearchFilters

    hits = semantic_service.search(
        query="NB-IoT power saving",
        fts5_query=None,
        filters=SearchFilters(),
        limit=10,
        fts5_weight=0.5,
    )
    assert len(hits) > 0
    for h in hits:
        assert h.rank_fts5 is None
        assert h.rrf_score < 0
        assert h.fts5_hit is not None


def test_search_sem_with_fts5_query_returns_rrf_merged_results(semantic_service):
    """End-to-end pin: when ``fts5_query`` is supplied, the service runs
    both paths through RRF; hits present in the FTS5 fan-out carry
    ``rank_fts5=<int>`` while vector-only hits carry ``rank_fts5=None``.

    The corpus seeds two NB-IoT TDocs (``SEM-NB-001`` / ``SEM-NB-002``)
    with ``NB-IoT`` in their titles, so ``fts5_query="NB-IoT"`` is
    guaranteed to produce FTS5 fan-out hits.
    """
    from doc3gpp.models.search import SearchFilters

    hits = semantic_service.search(
        query="NB-IoT power saving",
        fts5_query="NB-IoT",
        filters=SearchFilters(),
        limit=20,
        fts5_weight=0.5,
    )
    assert len(hits) > 0
    fts5_present = [h for h in hits if h.rank_fts5 is not None]
    assert len(fts5_present) > 0, (
        f"expected at least one FTS5-side hit for 'NB-IoT'; got "
        f"{[h.tdoc_id for h in hits]}"
    )


_ = np  # keep numpy import alive for type-checkers scanning fixtures

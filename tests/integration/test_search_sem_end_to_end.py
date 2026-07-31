"""End-to-end ``search sem`` integration tests over a populated corpus.

Builds the search-stack collaborators (FTS5 repo + vector repo) with
real sqlite + real ``tdocs`` corpus rows + pre-computed embedding
vectors. The embedder is replaced by a deterministic mock whose
``encode()`` consults :data:`tests.fixtures.semantic_search_corpus.ENCODE_TABLE`
for known inputs and falls back to a content-derived vector for
anything else, so the assertions stay deterministic without loading a
sentence-transformers model.

``strip_stopwords`` is monkeypatched to a pure-Python stand-in (no
spaCy dependency at runtime) — the FTS5 query string is what the
search service passes to ``SearchService.search``, and we're testing
the search-stack plumbing, not the stopword lexer.

Verifies:

1. ``search sem "what CRs touch NB-IoT power saving"`` returns the
   NB-IoT TDoc at rank 0 or 1 (vector + FTS5 agree).
2. ``--tsg`` filter narrows the result list to the requested TSG.
3. ``vector_weight=0.0`` makes the FTS5 ranking dominate and produces
   the FTS5-only top hit (proves the search seam is even-blended).
"""

from __future__ import annotations

import re
from unittest.mock import MagicMock

import numpy as np
import pytest

pytestmark = pytest.mark.semantic


_PURE_STOPWORDS = re.compile(
    r"\b(the|a|an|of|for|to|in|on|what|which|that|are|is|do|does|"
    r"touch|cr|crs)\b",
    re.IGNORECASE,
)


def _pure_strip(text: str) -> str:
    """Pure-Python stopword stripper — drops ``the/a/an/of/for/...`` and
    lowercases the rest. Used in place of the spaCy-backed
    :func:`strip_stopwords` so the test has no heavy extras.
    """
    return " ".join(_PURE_STOPWORDS.sub(" ", text.lower()).split())


@pytest.fixture(autouse=True)
def _patch_strip(monkeypatch):
    monkeypatch.setattr(
        "doc3gpp.services.semantic_search_service.strip_stopwords",
        _pure_strip,
    )


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
        SearchFilters(),
        limit=5,
        vector_weight=0.7,
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
        SearchFilters(tsg="RAN5"),
        limit=10,
        vector_weight=0.7,
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


def test_search_sem_vector_weight_zero_is_fts5_only(semantic_service):
    from doc3gpp.models.search import SearchFilters
    from doc3gpp.services.semantic_search_service import rrf_merge

    # With ``vector_weight=0.0`` the RRF score collapses to the
    # FTS5-only formula (``1/(k+rank_fts5)``); vector hits still
    # participate in the fan-out but contribute 0 to the score and
    # so always sort below FTS5-present hits.
    out = semantic_service.search(
        "NB-IoT power saving",
        SearchFilters(),
        limit=10,
        vector_weight=0.0,
    )
    assert out, "expected at least one FTS5 hit"

    # Filter to FTS5-present hits (vector-only rows have rank_fts5 None
    # and score 0 at vector_weight=0).
    fts5_hits = [h for h in out if h.rank_fts5 is not None]
    assert fts5_hits, "expected at least one FTS5-side hit"

    # FTS5-only RRF formula = 1/(k+rank_fts5), with k=60 from
    # settings.semantic_search.rrf_k.
    for hit in fts5_hits:
        expected = 1.0 / (60 + hit.rank_fts5)
        assert abs(hit.rrf_score - expected) < 1e-6, (
            f"vector_weight=0 score mismatch for {hit.tdoc_id}: "
            f"rrf={hit.rrf_score}, expected={expected}"
        )

    # Spot-check: ranks must be 0, 1, 2, ... in order after sort.
    ranks = [h.rank_fts5 for h in fts5_hits]
    assert ranks == sorted(ranks), (
        f"FTS5 ranks should be monotonic after RRF; got {ranks}"
    )

    # Top-ranked FTS5 hit is the NB-IoT TDoc — text strongest match.
    assert fts5_hits[0].tdoc_id == "SEM-NB-001", (
        f"FTS5 top hit should be the NB-IoT TDoc; got {fts5_hits[0].tdoc_id}"
    )
    _ = rrf_merge  # silence unused-import warning


_ = np  # keep numpy import alive for type-checkers scanning fixtures

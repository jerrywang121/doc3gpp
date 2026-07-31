"""Auto-embed hook integration with ``TDocCrService``.

Verifies:

1. ``TDocCrService._embed_after_parse(tdoc_id)`` calls
   ``SemanticSearchService.index_for_tdoc(tdoc_id)`` when both the
   semantic service is wired AND
   ``Settings.semantic_search.auto_embed_on_parse`` is True.
2. ``Settings.semantic_search.auto_embed_on_parse = False`` skips the
   hook.
3. ``_semantic_service = None`` skips the hook silently.
4. Defensive contract: when a reparse finds the embed text gone, the
   underlying service falls through to ``remove_for_tdoc`` so the
   stale vector rows are purged. Validates the delete-before-residual
   contract :class:`SemanticSearchService` relies on (no leftover
   embeddings on a vanished source).

Mirrors :mod:`tests.integration.test_search_after_parse` (the FTS5
sibling) and uses ``MagicMock`` for the wiring to stay independent of
the ``[semantic]`` extra at test time.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from doc3gpp.services.factory import build_tdoc_cr_service
from doc3gpp.services.semantic_search_service import SemanticSearchService
from doc3gpp.services.search_service import PassthroughReranker, SearchService
from doc3gpp.settings.schema import SemanticSearchSettings, Settings


def test_embed_hook_fires_after_parse(sqlite_env) -> None:
    mock = MagicMock()
    service = build_tdoc_cr_service()
    service._semantic_service = mock  # type: ignore[attr-defined]
    service._settings = Settings()  # type: ignore[attr-defined]
    service._embed_after_parse("R5-1234567")  # type: ignore[attr-defined]
    mock.index_for_tdoc.assert_called_once_with("R5-1234567")


def test_embed_hook_skipped_when_auto_embed_disabled(sqlite_env) -> None:
    mock = MagicMock()
    service = build_tdoc_cr_service()
    service._semantic_service = mock  # type: ignore[attr-defined]
    service._settings = Settings(  # type: ignore[attr-defined]
        semantic_search=SemanticSearchSettings(auto_embed_on_parse=False),
    )
    service._embed_after_parse("R5-1234567")  # type: ignore[attr-defined]
    mock.index_for_tdoc.assert_not_called()


def test_embed_hook_skipped_when_service_none(sqlite_env) -> None:
    service = build_tdoc_cr_service()
    service._semantic_service = None  # type: ignore[attr-defined]
    # Should not raise even when no service is wired.
    service._embed_after_parse("R5-1234567")  # type: ignore[attr-defined]


def test_embed_hook_swallows_semantic_service_exception(sqlite_env) -> None:
    """A failing embed never aborts a successful parse."""
    mock = MagicMock()
    mock.index_for_tdoc.side_effect = RuntimeError("vector store offline")
    service = build_tdoc_cr_service()
    service._semantic_service = mock  # type: ignore[attr-defined]
    service._settings = Settings()  # type: ignore[attr-defined]
    # No raise — the hook is best-effort.
    service._embed_after_parse("R5-1234567")  # type: ignore[attr-defined]
    mock.index_for_tdoc.assert_called_once_with("R5-1234567")


def test_reparse_with_vanished_embed_text_clears_vector_rows(sqlite_env) -> None:
    """When re-parsing finds the embed text gone, ``index_for_tdoc``
    routes through ``vector_repo.remove_for_tdoc`` so stale chunks
    don't linger. Validates the contract the
    ``_embed_after_parse`` hook relies on — a successful parse whose
    source text disappears downstream must purge the index, not leave
    orphan embeddings behind.
    """
    vec_repo = MagicMock()
    embedder = MagicMock()
    fts5 = SearchService(repo=MagicMock(), reranker=PassthroughReranker())
    semantic = SemanticSearchService(
        fts5_service=fts5,
        embedder=embedder,
        vector_repo=vec_repo,
        settings=Settings(),
    )

    with patch(
        "doc3gpp.services.semantic_search_service._build_embed_text",
        return_value=None,
    ):
        semantic.index_for_tdoc("R5-empty")

    vec_repo.remove_for_tdoc.assert_called_once_with("R5-empty")
    vec_repo.upsert_chunks.assert_not_called()

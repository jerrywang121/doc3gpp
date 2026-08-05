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

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from doc3gpp.models.tdoc import TDoc
from doc3gpp.models.tdoc_cr import TDocCRDetails, TDocCRParseResult
from doc3gpp.scraping.cache import TDocCache
from doc3gpp.services.factory import build_tdoc_cr_service
from doc3gpp.services.semantic_search_service import SemanticSearchService
from doc3gpp.services.search_service import PassthroughReranker, SearchService
from doc3gpp.services.tdoc_cr_service import TDocCrService
from doc3gpp.settings.schema import SemanticSearchSettings, Settings
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.repositories.tdoc_cr_change_details_sql import (
    SQLAlchemyTDocCrChangeDetailsRepository,
)
from doc3gpp.storage.repositories.tdoc_cr_sql import SQLAlchemyTDocCrRepository
from doc3gpp.storage.repositories.tdoc_cr_ttcn_sql import SQLAlchemyTDocCrTtcnRepository
from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository


FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "tdoc_cr_doc"


def _docx_available() -> bool:
    """Return True iff ``python-docx`` imports cleanly.

    Mirrors the helper used in :mod:`tests.integration.test_tdoc_cr_sqlite`
    so the same skip guard can gate both integration suites.
    """
    try:
        from docx import Document  # noqa: F401
    except ImportError:
        return False
    return True


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


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_web_job_path_parse_auto_indexes_fts5_and_embeddings(
    sqlite_env, tmp_path, monkeypatch,
) -> None:
    """A parse enqueued via the web job path (same params the tdoc
    detail page sends) auto-indexes FTS5 + embeddings on success."""
    create_schema()
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5-260001", type="CR", ftp_url="R5/26.001/R5-260001.zip"),
    )

    search_service = MagicMock()
    semantic_service = MagicMock()

    cache = TDocCache(root=tmp_path / "cache", size_limit_bytes=0)
    scraper_mock = MagicMock()
    fixture = FIXTURES_DIR / "R5-227476.zip"
    assert fixture.exists(), f"fixture missing: {fixture}"
    scraper_mock.get_bytes.return_value = fixture.read_bytes()

    cr_repo = SQLAlchemyTDocCrRepository()
    cr_ttcn_repo = SQLAlchemyTDocCrTtcnRepository()
    cr_change_details_repo = SQLAlchemyTDocCrChangeDetailsRepository()
    tdoc_repo = SQLAlchemyTDocRepository()

    parser = MagicMock()
    parser.supports.return_value = True
    parser.parser_version = "1.0.0"
    parser.parse.return_value = TDocCRParseResult(
        cover=TDocCRDetails(tdoc_id="R5-260001"),
    )

    service = TDocCrService(
        cache=cache,
        scraper_client=scraper_mock,
        cr_repository=cr_repo,
        cr_ttcn_repository=cr_ttcn_repo,
        cr_change_details_repository=cr_change_details_repo,
        tdoc_repository=tdoc_repo,
        parser=parser,
        search_service=search_service,
        semantic_service=semantic_service,
    )

    result = service.extract_many(["R5-260001"], force=True, full=True)
    assert "R5-260001" in result.successes

    search_service.upsert_for_tdoc.assert_called_once_with("R5-260001")
    semantic_service.index_for_tdoc.assert_called_once_with("R5-260001")

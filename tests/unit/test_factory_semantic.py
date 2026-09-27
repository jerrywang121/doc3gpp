from __future__ import annotations

from unittest.mock import MagicMock


def test_build_embedder_returns_none_when_url_unset():
    from doc3gpp.services import factory
    from doc3gpp.settings.schema import SemanticSearchSettings, Settings
    settings = Settings(semantic_search=SemanticSearchSettings(embedding_base_url=None))
    assert factory.build_embedder(settings) is None


def test_build_embedder_returns_remote_when_url_set():
    from doc3gpp.services import factory
    from doc3gpp.services.embedding.remote_embedder import OpenAICompatibleEmbedder
    from doc3gpp.settings.schema import SemanticSearchSettings, Settings
    settings = Settings(semantic_search=SemanticSearchSettings(
        embedding_base_url="http://localhost:11434/v1",
        embedding_model="nomic-embed-text",
    ))
    emb = factory.build_embedder(settings)
    assert isinstance(emb, OpenAICompatibleEmbedder)
    assert emb.model_name == "nomic-embed-text"
    emb.close()


def test_semantic_service_none_when_url_unset():
    from doc3gpp.services import factory
    from doc3gpp.settings.schema import SemanticSearchSettings, Settings
    settings = Settings(semantic_search=SemanticSearchSettings(embedding_base_url=None))
    assert factory.build_semantic_search_service(settings) is None


def test_semantic_service_none_when_url_blank(sqlite_env=None):
    from doc3gpp.services import factory
    from doc3gpp.settings.schema import SemanticSearchSettings, Settings
    settings = Settings(semantic_search=SemanticSearchSettings(embedding_base_url="   "))
    assert factory.build_semantic_search_service(settings) is None



def test_semantic_service_does_not_require_fts5(monkeypatch):
    from doc3gpp.services import factory
    build_search = MagicMock(return_value=None)
    monkeypatch.setattr(factory, "build_search_service", build_search)
    embedder = MagicMock(dim=3, model_name="fake-model")
    vector_repo = MagicMock()

    out = factory.build_semantic_search_service(
        MagicMock(),
        fts5_service=None,
        embedder=embedder,
        vector_repo=vector_repo,
    )

    assert out is not None
    assert out._fts5 is None
    assert out._embedder is embedder
    assert out._vec is vector_repo
    build_search.assert_not_called()


def test_spec_doc_semantic_service_does_not_require_fts5(monkeypatch):
    from doc3gpp.services import factory

    build_search = MagicMock(return_value=None)
    monkeypatch.setattr(factory, "build_spec_doc_search_service", build_search)
    embedder = MagicMock(dim=3, model_name="fake-model")
    vector_repo = MagicMock()

    out = factory.build_spec_doc_semantic_service(
        MagicMock(),
        vector_repo=vector_repo,
        embedder=embedder,
        fts5_service=None,
    )

    assert out is not None
    assert out._fts5 is None
    assert out._embedder is embedder
    assert out._vec is vector_repo
    build_search.assert_not_called()


def test_tdoc_facade_filter_mode_does_not_construct_optional_services(monkeypatch):
    from doc3gpp.services import factory

    build_search = MagicMock(side_effect=AssertionError("FTS5 must stay lazy"))
    build_semantic = MagicMock(side_effect=AssertionError("vector must stay lazy"))
    monkeypatch.setattr(factory, "build_search_service", build_search)
    monkeypatch.setattr(factory, "build_semantic_search_service", build_semantic)
    source = MagicMock()
    source.list_for_search.return_value = []

    facade = factory.build_tdoc_search_facade(
        MagicMock(),
        source_repo=source,
    )

    assert facade.search(text=None, semantic=None, filters=MagicMock()) == []
    build_search.assert_not_called()
    build_semantic.assert_not_called()


def test_tdoc_facade_semantic_mode_can_use_vector_without_fts5(monkeypatch):
    from doc3gpp.services import factory

    monkeypatch.setattr(
        factory,
        "build_search_service",
        MagicMock(side_effect=AssertionError("semantic mode must not build FTS5")),
    )
    semantic = MagicMock()
    semantic.search.return_value = []
    monkeypatch.setattr(factory, "build_semantic_search_service", lambda *a, **kw: semantic)

    facade = factory.build_tdoc_search_facade(
        MagicMock(),
        source_repo=MagicMock(),
        embedder=MagicMock(),
    )

    assert facade.search(text=None, semantic="meaning", filters=MagicMock()) == []
    semantic.search.assert_called_once()


def test_spec_doc_service_does_not_build_embedder_when_semantic_is_injected(monkeypatch):
    from doc3gpp.services import factory
    from doc3gpp.settings.schema import Settings

    build_embedder = MagicMock(side_effect=AssertionError("embedder must stay lazy"))
    monkeypatch.setattr(factory, "build_embedder", build_embedder)
    search_service = MagicMock()
    semantic_service = MagicMock()

    service = factory.build_spec_doc_service(
        settings=Settings(),
        search_service=search_service,
        semantic_service=semantic_service,
    )

    assert service._embedder is None
    assert service._search is search_service
    assert service._semantic is semantic_service
    build_embedder.assert_not_called()


def test_returns_none_when_vector_repo_raises(monkeypatch):
    from doc3gpp.models.semantic_search import VectorIndexUnavailableError
    from doc3gpp.services import factory
    monkeypatch.setattr(factory, "build_search_service", lambda *a, **kw: MagicMock())
    monkeypatch.setattr(
        "doc3gpp.services.factory.OpenAICompatibleEmbedder",
        lambda *a, **kw: MagicMock(),
    )
    def boom(*a, **kw):
        raise VectorIndexUnavailableError("no sqlite-vec")
    monkeypatch.setattr(
        "doc3gpp.storage.repositories.vector_sql.SQLAlchemyVectorIndexRepository",
        lambda *a, **kw: (_ for _ in ()).throw(boom()),
    )
    out = factory.build_semantic_search_service(MagicMock())
    assert out is None


def test_returns_service_when_all_present(monkeypatch):
    from doc3gpp.services import factory
    monkeypatch.setattr(factory, "build_search_service", lambda *a, **kw: MagicMock())
    monkeypatch.setattr(
        "doc3gpp.services.factory.OpenAICompatibleEmbedder",
        lambda *a, **kw: MagicMock(),
    )
    fake_repo = MagicMock()
    monkeypatch.setattr(
        "doc3gpp.storage.repositories.vector_sql.SQLAlchemyVectorIndexRepository",
        lambda *a, **kw: fake_repo,
    )
    out = factory.build_semantic_search_service(MagicMock())
    assert out is not None

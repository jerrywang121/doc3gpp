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



def test_returns_none_when_fts5_unavailable(monkeypatch):
    from doc3gpp.services import factory
    monkeypatch.setattr(factory, "build_search_service", lambda *a, **kw: None)
    out = factory.build_semantic_search_service(MagicMock())
    assert out is None


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

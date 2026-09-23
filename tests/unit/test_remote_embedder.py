import numpy as np


def _fake_response(payload):
    import httpx
    return httpx.Response(200, json=payload)


def test_encode_posts_openai_shape_and_returns_float32(monkeypatch):
    import httpx
    from doc3gpp.services.embedding.remote_embedder import OpenAICompatibleEmbedder
    seen = {}

    def fake_post(self, url, json=None, headers=None):
        seen["url"] = url
        seen["json"] = json
        seen["headers"] = headers
        return _fake_response({"data": [
            {"embedding": [0.1, 0.2, 0.3, 0.4]},
            {"embedding": [0.5, 0.6, 0.7, 0.8]},
        ]})

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    emb = OpenAICompatibleEmbedder(base_url="http://localhost:11434/v1/", model="nomic-embed-text")
    out = emb.encode(["a", "b"])
    assert seen["url"] == "http://localhost:11434/v1/embeddings"
    assert seen["json"] == {"model": "nomic-embed-text", "input": ["a", "b"]}
    assert "Authorization" not in seen["headers"]
    assert out.shape == (2, 4) and out.dtype == np.float32


def test_encode_empty_returns_empty_without_http(monkeypatch):
    import httpx
    from doc3gpp.services.embedding.remote_embedder import OpenAICompatibleEmbedder

    def boom(*a, **kw):
        raise AssertionError("no HTTP for empty input")

    monkeypatch.setattr(httpx.Client, "post", boom)
    emb = OpenAICompatibleEmbedder(base_url="http://x/v1", model="m")
    out = emb.encode([])
    assert out.shape == (0, 0)


def test_bearer_header_and_batching(monkeypatch):
    import httpx
    from doc3gpp.services.embedding.remote_embedder import OpenAICompatibleEmbedder
    calls = []

    def fake_post(self, url, json=None, headers=None):
        calls.append((json, headers))
        n = len(json["input"])
        return _fake_response({"data": [{"embedding": [float(i)] * 2} for i in range(n)]})

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    emb = OpenAICompatibleEmbedder(base_url="http://x/v1", model="m", api_key="k", batch_size=2)
    out = emb.encode(["a", "b", "c"])
    assert len(calls) == 2
    assert all(h["Authorization"] == "Bearer k" for _, h in calls)
    assert out.shape == (3, 2)


def test_dim_probes_once_and_caches(monkeypatch):
    import httpx
    from doc3gpp.services.embedding.remote_embedder import OpenAICompatibleEmbedder
    calls = []

    def fake_post(self, url, json=None, headers=None):
        calls.append(json)
        return _fake_response({"data": [{"embedding": [0.0] * 8}]})

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    emb = OpenAICompatibleEmbedder(base_url="http://x/v1", model="m")
    assert emb.dim == 8
    assert emb.dim == 8
    assert len(calls) == 1


def test_http_error_maps_to_embedder_unavailable(monkeypatch):
    import httpx
    from doc3gpp.models.semantic_search import EmbedderUnavailableError
    from doc3gpp.services.embedding.remote_embedder import OpenAICompatibleEmbedder
    import pytest

    def fake_post(self, url, json=None, headers=None):
        return httpx.Response(401, json={"error": "bad key"})

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    emb = OpenAICompatibleEmbedder(base_url="http://x/v1", model="m", api_key="wrong")
    with pytest.raises(EmbedderUnavailableError, match="401"):
        emb.encode(["a"])


def test_ragged_dimensions_map_to_embedder_unavailable(monkeypatch):
    import httpx
    from doc3gpp.models.semantic_search import EmbedderUnavailableError
    from doc3gpp.services.embedding.remote_embedder import OpenAICompatibleEmbedder
    import pytest

    def fake_post(self, url, json=None, headers=None):
        return _fake_response({"data": [
            {"embedding": [0.1, 0.2]},
            {"embedding": [0.3]},  # ragged: shorter than the first row
        ]})

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    emb = OpenAICompatibleEmbedder(base_url="http://x/v1", model="m")
    with pytest.raises(EmbedderUnavailableError, match="malformed"):
        emb.encode(["a", "b"])

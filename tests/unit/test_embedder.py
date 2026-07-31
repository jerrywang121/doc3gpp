from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from doc3gpp.models.semantic_search import EmbedderUnavailableError
from doc3gpp.services.embedding import Embedder, SentenceTransformerEmbedder


def test_embedder_protocol_is_re_exported():
    """Embedder must be importable from the public embedding package."""
    assert Embedder is not None


def test_encode_returns_float32_with_expected_shape():
    emb = SentenceTransformerEmbedder("fake-model")
    fake_model = MagicMock()
    fake_model.get_sentence_embedding_dimension.return_value = 384
    fake_model.encode.return_value = np.zeros((3, 384), dtype=np.float32)
    with patch.object(SentenceTransformerEmbedder, "_load_model", return_value=fake_model):
        out = emb.encode(["a", "b", "c"])
    assert out.shape == (3, 384)
    assert out.dtype == np.float32


def test_dim_property_reads_from_model():
    emb = SentenceTransformerEmbedder("fake-model")
    fake_model = MagicMock()
    fake_model.get_sentence_embedding_dimension.return_value = 768
    with patch.object(SentenceTransformerEmbedder, "_load_model", return_value=fake_model):
        assert emb.dim == 768


def test_lazy_model_load_on_first_encode():
    emb = SentenceTransformerEmbedder("fake-model")
    # Construction must NOT load the model.
    assert emb._model is None
    fake_model = MagicMock()
    fake_model.get_sentence_embedding_dimension.return_value = 384
    fake_model.encode.return_value = np.zeros((1, 384), dtype=np.float32)
    with patch.object(
        SentenceTransformerEmbedder, "_load_model", return_value=fake_model
    ) as loader:
        emb.encode(["x"])
        loader.assert_called_once()


def test_load_failure_raises_embedder_unavailable():
    emb = SentenceTransformerEmbedder("bad-model")
    with patch.object(
        SentenceTransformerEmbedder,
        "_load_model",
        side_effect=OSError("network down"),
    ):
        with pytest.raises(EmbedderUnavailableError):
            emb.encode(["x"])


def test_empty_input_returns_empty_array():
    emb = SentenceTransformerEmbedder("fake-model")
    with patch.object(SentenceTransformerEmbedder, "_load_model", return_value=MagicMock()):
        out = emb.encode([])
    assert out.shape == (0, 0)

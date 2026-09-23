"""OpenAI-compatible remote embedder for the semantic search subsystem.

Single client for OpenAI, Ollama's ``/v1`` endpoint, vLLM, TEI, and any
other OpenAI-compatible server: ``POST {base_url}/embeddings`` with
``{"model", "input"}``. No new dependencies (httpx is already core).
"""

from __future__ import annotations

import threading

import httpx
import numpy as np

from doc3gpp.models.semantic_search import EmbedderUnavailableError


class OpenAICompatibleEmbedder:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout_s: float = 30.0,
        batch_size: int = 32,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model_name = model
        self._api_key = api_key
        self._batch_size = max(1, batch_size)
        self._client = httpx.Client(timeout=timeout_s)
        self._lock = threading.Lock()
        self._dim: int | None = None

    @property
    def model_name(self) -> str:
        return self._model_name

    def _headers(self) -> dict[str, str]:
        if self._api_key:
            return {"Authorization": f"Bearer {self._api_key}"}
        return {}

    def _post_batch(self, batch: list[str]) -> list[list[float]]:
        try:
            resp = self._client.post(
                f"{self._base_url}/embeddings",
                json={"model": self._model_name, "input": batch},
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            raise EmbedderUnavailableError(
                f"embedding API request failed for model {self._model_name!r}: {exc}"
            ) from exc
        if resp.status_code >= 400:
            raise EmbedderUnavailableError(
                f"embedding API error HTTP {resp.status_code} for model {self._model_name!r}"
            )
        try:
            data = resp.json()["data"]
            return [row["embedding"] for row in data]
        except (ValueError, KeyError, TypeError) as exc:
            raise EmbedderUnavailableError(
                f"malformed embedding API response for model {self._model_name!r}: {exc}"
            ) from exc

    def encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        rows: list[list[float]] = []
        for i in range(0, len(texts), self._batch_size):
            rows.extend(self._post_batch(texts[i:i + self._batch_size]))
        if len(rows) != len(texts):
            raise EmbedderUnavailableError(
                f"embedding API returned {len(rows)} vectors for {len(texts)} inputs "
                f"(model {self._model_name!r})"
            )
        try:
            return np.asarray(rows, dtype=np.float32)
        except (ValueError, TypeError) as exc:
            # Ragged / non-numeric rows: the remote server is untrusted
            # input, so funnel every malformed shape into the disabled
            # path instead of crashing the caller with a raw ValueError.
            raise EmbedderUnavailableError(
                f"malformed embedding API response for model {self._model_name!r}: {exc}"
            ) from exc

    @property
    def dim(self) -> int:
        if self._dim is None:
            with self._lock:
                if self._dim is None:
                    self._dim = int(self.encode(["probe"])[0].shape[-1])
        return self._dim

    def close(self) -> None:
        self._client.close()

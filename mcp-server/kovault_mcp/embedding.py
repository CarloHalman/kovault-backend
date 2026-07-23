"""Embedding endpoint client.

The server composes deterministic text from a row (embedding_text.py), calls the endpoint
named in the `embedding` setting, MRL-truncates the vector to `dims` (4000 for the halfvec
hnsw ceiling), renormalises, and returns a pgvector literal string ready to cast ::halfvec.

Default wire format is OpenAI-compatible (`POST /v1/embeddings {input, model}` ->
`{data:[{embedding:[...]}]}`), which TEI / vLLM / most Qwen3 servers expose. Swap the model
by repointing the endpoint in settings, then /janitor -embed.
"""
from __future__ import annotations

import math

import httpx


class EmbeddingClient:
    def __init__(self, endpoint: str, model: str, dims: int = 4000, timeout: float = 60.0):
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.dims = dims
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def _raw(self, text: str) -> list[float]:
        resp = self._client.post(
            f"{self.endpoint}/v1/embeddings",
            json={"input": text, "model": self.model},
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]

    def _raw_many(self, texts: list[str]) -> list[list[float]]:
        """Embed a whole batch in ONE request. The OpenAI wire format returns a `data` list with a
        per-item `index`; sort by it rather than trusting positional order."""
        resp = self._client.post(
            f"{self.endpoint}/v1/embeddings",
            json={"input": texts, "model": self.model},
        )
        resp.raise_for_status()
        data = sorted(resp.json()["data"], key=lambda d: d.get("index", 0))
        return [d["embedding"] for d in data]

    @staticmethod
    def _mrl_normalise(vec: list[float], dims: int) -> list[float]:
        """MRL-truncate to `dims`, then L2-normalise (cosine-friendly)."""
        v = vec[:dims]
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    def embed(self, text: str) -> str:
        """Return a pgvector literal '[a,b,c]' truncated/normalised to `dims`."""
        vec = self._mrl_normalise(self._raw(text), self.dims)
        return "[" + ",".join(repr(x) for x in vec) + "]"

    def embed_many(self, texts: list[str]) -> list[str]:
        """Batch variant of embed() — one endpoint round-trip for the whole list, results aligned
        to the input order. Used by the background embed worker."""
        return ["[" + ",".join(repr(x) for x in self._mrl_normalise(v, self.dims)) + "]"
                for v in self._raw_many(texts)]

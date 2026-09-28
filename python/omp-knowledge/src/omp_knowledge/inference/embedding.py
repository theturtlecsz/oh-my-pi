"""Loopback HTTP embedding client.

The profile pins model identity, revision, dimensions, pooling, prefixes, and
normalization. ``generation_id`` is the sha256 of that profile's canonical JSON,
so a same-dimension model swap is a different generation. The HTTP client posts
to a loopback ``/v1/embeddings`` endpoint and does not retry or fall back.
"""

from __future__ import annotations

import hashlib
import json
import math
import urllib.error
import urllib.request
from collections.abc import Sequence
from typing import Literal, Protocol, runtime_checkable
from urllib.parse import urlsplit

from pydantic import Field
from omp_work.v1.canonical import canonical_json
from omp_work.v1.models import StrictModel

from omp_knowledge.errors import KnowledgeError

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class EmbeddingUnavailable(KnowledgeError):
    """Raised when the embedding service is unreachable, times out, or returns an unusable response."""

    code = "embedding_unavailable"
    status_code = 503

    def __init__(
        self, message: str = "embedding_unavailable: embedding service unavailable"
    ) -> None:
        super().__init__(message)


class EmbeddingProfile(StrictModel):
    """Pinned embedding identity. ``generation_id`` hashes the canonical JSON of every field."""

    model: str
    model_revision: str
    dimensions: int = Field(gt=0)
    pooling: Literal["mean", "last", "cls"]
    query_prefix: str
    document_prefix: str
    normalize: bool

    @property
    def generation_id(self) -> str:
        return hashlib.sha256(
            canonical_json(self.model_dump(mode="json")).encode()
        ).hexdigest()


@runtime_checkable
class Embedder(Protocol):
    """Replaceable document and query embedder bound to one profile."""

    profile: EmbeddingProfile

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed documents with the profile's document prefix, in input order."""
        ...

    def embed_query(self, text: str) -> list[float]:
        """Embed one query with the profile's query prefix."""
        ...


def _loopback_endpoint(endpoint: str) -> str:
    host = urlsplit(endpoint).hostname
    if host is None or host.lower() not in LOOPBACK_HOSTS:
        raise ValueError(
            f"loopback host required (127.0.0.1, localhost, ::1); refusing endpoint {endpoint!r}"
        )
    return endpoint.rstrip("/")


def _finite_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EmbeddingUnavailable(
            "embedding_unavailable: embedding value must be numeric"
        )
    number = float(value)
    if not math.isfinite(number):
        raise EmbeddingUnavailable(
            "embedding_unavailable: embedding value must be finite"
        )
    return number


def _l2_normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(component * component for component in vector))
    if norm == 0.0 or not math.isfinite(norm):
        raise EmbeddingUnavailable(
            "embedding_unavailable: embedding norm is not a positive finite number"
        )
    return [component / norm for component in vector]


class HttpEmbedder:
    """OpenAI-compatible embeddings client for a loopback llama.cpp server.

    Posts ``{"model", "input"}`` to ``{endpoint}/v1/embeddings`` in batches.
    Responses are ordered by ``data[].index``. A declared ``normalize`` profile
    L2-normalizes each vector. Transport and payload failures raise
    ``EmbeddingUnavailable`` once; there is no retry and no fallback route.
    """

    def __init__(
        self,
        endpoint: str,
        profile: EmbeddingProfile,
        timeout_s: float = 30,
        batch_size: int = 64,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        self.endpoint = _loopback_endpoint(endpoint)
        self.profile = profile
        self.timeout_s = timeout_s
        self.batch_size = batch_size
        # Empty proxy map keeps a loopback call from following http_proxy off-box.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        prefixed = [self.profile.document_prefix + text for text in texts]
        vectors: list[list[float]] = []
        for start in range(0, len(prefixed), self.batch_size):
            vectors.extend(self._embed_batch(prefixed[start : start + self.batch_size]))
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return self._embed_batch([self.profile.query_prefix + text])[0]

    def _embed_batch(self, inputs: list[str]) -> list[list[float]]:
        payload = {"model": self.profile.model, "input": inputs}
        request = urllib.request.Request(
            f"{self.endpoint}/v1/embeddings",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=self.timeout_s) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise EmbeddingUnavailable(
                f"embedding_unavailable: HTTP error {exc.code}: {exc.reason}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise EmbeddingUnavailable(
                f"embedding_unavailable: connection/timeout error: {exc}"
            ) from exc
        return self._parse_embeddings(raw, len(inputs))

    def _parse_embeddings(self, raw: bytes, size: int) -> list[list[float]]:
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise EmbeddingUnavailable(
                f"embedding_unavailable: response is not valid JSON: {exc}"
            ) from exc
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise EmbeddingUnavailable(
                "embedding_unavailable: response is missing a data list"
            )
        data = body["data"]
        if len(data) != size:
            raise EmbeddingUnavailable(
                f"embedding_unavailable: response length {len(data)} does not match input length {size}"
            )

        seen: set[int] = set()
        ordered: list[list[float] | None] = [None] * size
        for entry in data:
            if not isinstance(entry, dict):
                raise EmbeddingUnavailable(
                    "embedding_unavailable: data entry is not an object"
                )
            index = self._entry_index(entry, size, seen)
            seen.add(index)
            ordered[index] = self._entry_vector(entry)
        if seen != set(range(size)) or any(vector is None for vector in ordered):
            raise EmbeddingUnavailable("embedding_unavailable: missing index")
        return [vector for vector in ordered if vector is not None]

    def _entry_index(self, entry: dict[str, object], size: int, seen: set[int]) -> int:
        if "index" not in entry:
            raise EmbeddingUnavailable("embedding_unavailable: missing index")
        index = entry["index"]
        if isinstance(index, bool) or not isinstance(index, int):
            raise EmbeddingUnavailable(
                "embedding_unavailable: index must be an integer"
            )
        if index < 0 or index >= size:
            raise EmbeddingUnavailable(
                f"embedding_unavailable: index {index} out of range (0..{size - 1})"
            )
        if index in seen:
            raise EmbeddingUnavailable(
                f"embedding_unavailable: duplicate index {index}"
            )
        return index

    def _entry_vector(self, entry: dict[str, object]) -> list[float]:
        raw = entry.get("embedding")
        if not isinstance(raw, list):
            raise EmbeddingUnavailable(
                "embedding_unavailable: embedding must be a list"
            )
        if len(raw) != self.profile.dimensions:
            raise EmbeddingUnavailable(
                "embedding_unavailable: embedding length "
                f"{len(raw)} does not match dimensions {self.profile.dimensions}"
            )
        vector = [_finite_float(component) for component in raw]
        if self.profile.normalize:
            return _l2_normalize(vector)
        return vector


__all__ = [
    "Embedder",
    "EmbeddingProfile",
    "EmbeddingUnavailable",
    "HttpEmbedder",
]

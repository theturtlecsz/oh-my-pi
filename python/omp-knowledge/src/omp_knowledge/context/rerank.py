"""Reranking implementations for context items."""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from omp_knowledge.context.models import ContextItem
from omp_knowledge.errors import KnowledgeError


class RerankerUnavailable(KnowledgeError):
    """Raised when the reranker is unreachable, times out, or returns invalid responses."""

    code = "reranker_unavailable"
    status_code = 503

    def __init__(self, message: str = "reranker_unavailable: reranker service unavailable") -> None:
        super().__init__(message)


@runtime_checkable
class Reranker(Protocol):
    """Protocol for scoring and reranking candidate context items."""

    def rerank(self, query: str, items: Sequence[ContextItem]) -> list[ContextItem]:
        """Rerank items given a query. Mandatory items must be left untouched."""
        ...


class OrderReranker:
    """Deterministic reranker that sets score = -position (maintaining input order)."""

    def rerank(self, query: str, items: Sequence[ContextItem]) -> list[ContextItem]:
        output: list[ContextItem] = []
        for pos, item in enumerate(items):
            if item.mandatory:
                output.append(item)
            else:
                output.append(item.model_copy(update={"score": float(-pos)}))
        return output


class HttpReranker:
    """HTTP reranker targeting llama.cpp server `/v1/rerank` shape via stdlib urllib."""

    def __init__(self, url: str, model: str, timeout_s: float = 10.0) -> None:
        self.url = url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s

    def rerank(self, query: str, items: Sequence[ContextItem]) -> list[ContextItem]:
        if not items:
            return []

        documents = [item.text for item in items]
        payload = {
            "model": self.model,
            "query": query,
            "documents": documents,
        }
        payload_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.url}/v1/rerank",
            data=payload_bytes,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                resp_bytes = resp.read()
        except urllib.error.HTTPError as exc:
            raise RerankerUnavailable(f"reranker HTTP error {exc.code}: {exc.reason}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RerankerUnavailable(f"reranker connection/timeout error: {exc}") from exc

        try:
            data = json.loads(resp_bytes.decode("utf-8"))
        except Exception as exc:
            raise RerankerUnavailable(f"reranker response is not valid JSON: {exc}") from exc

        if not isinstance(data, dict):
            raise RerankerUnavailable("reranker response is not a JSON object")
        if "results" not in data or not isinstance(data["results"], list):
            raise RerankerUnavailable("reranker response missing 'results' list")

        results = data["results"]
        if len(results) != len(documents):
            raise RerankerUnavailable(
                f"reranker returned {len(results)} results, expected {len(documents)}"
            )

        seen_indices: set[int] = set()
        scores: dict[int, float] = {}
        for entry in results:
            if not isinstance(entry, dict):
                raise RerankerUnavailable("reranker result entry is not a JSON object")
            if "index" not in entry or "relevance_score" not in entry:
                raise RerankerUnavailable("reranker result entry missing 'index' or 'relevance_score'")
            idx = entry["index"]
            if isinstance(idx, bool) or not isinstance(idx, int):
                raise RerankerUnavailable("reranker result 'index' must be an integer")
            if idx < 0 or idx >= len(documents):
                raise RerankerUnavailable(
                    f"reranker result index {idx} out of range (0..{len(documents)-1})"
                )
            if idx in seen_indices:
                raise RerankerUnavailable(f"duplicate index {idx} in reranker results")
            seen_indices.add(idx)

            raw_score = entry["relevance_score"]
            if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
                raise RerankerUnavailable("reranker 'relevance_score' must be a numeric value")
            score_val = float(raw_score)
            if math.isnan(score_val) or math.isinf(score_val):
                raise RerankerUnavailable("reranker 'relevance_score' must be finite")
            scores[idx] = round(score_val, 6)

        if seen_indices != set(range(len(documents))):
            raise RerankerUnavailable("missing index in reranker results")

        output: list[ContextItem] = []
        for idx, item in enumerate(items):
            if item.mandatory:
                output.append(item)
            else:
                output.append(item.model_copy(update={"score": scores[idx]}))
        return output


__all__ = [
    "HttpReranker",
    "OrderReranker",
    "Reranker",
    "RerankerUnavailable",
]

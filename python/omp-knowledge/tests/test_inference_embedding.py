"""Loopback HTTP embedding client: profile identity, batching, and failure mapping."""

from __future__ import annotations

import hashlib
import http.server
import json
import threading
import time
from typing import Any, ClassVar, Literal

import pytest
from omp_work.v1.canonical import canonical_json

from omp_knowledge.inference import (
    Embedder,
    EmbeddingProfile,
    EmbeddingUnavailable,
    HttpEmbedder,
)


def _profile(
    *,
    model: str = "qwen3-embedding-0.6b",
    model_revision: str = "rev-1",
    dimensions: int = 4,
    pooling: Literal["mean", "last", "cls"] = "mean",
    query_prefix: str = "query: ",
    document_prefix: str = "doc: ",
    normalize: bool = False,
) -> EmbeddingProfile:
    return EmbeddingProfile(
        model=model,
        model_revision=model_revision,
        dimensions=dimensions,
        pooling=pooling,
        query_prefix=query_prefix,
        document_prefix=document_prefix,
        normalize=normalize,
    )


class MockEmbeddingHandler(http.server.BaseHTTPRequestHandler):
    mode: ClassVar[str] = "ok"
    dimensions: ClassVar[int] = 4
    requests: ClassVar[list[dict[str, Any]]] = []
    paths: ClassVar[list[str]] = []

    def log_message(self, format: str, *args: Any) -> None:
        return None

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        MockEmbeddingHandler.requests.append(body)
        MockEmbeddingHandler.paths.append(self.path)

        if self.mode == "500":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"internal server error")
            return
        if self.mode == "slow":
            time.sleep(0.3)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")
            return
        if self.mode == "bad_json":
            self._send(200, b"not-json")
            return
        if self.mode == "fixed_3_4":
            raw = json.dumps({"data": [{"index": 0, "embedding": [3.0, 4.0]}]}).encode(
                "utf-8"
            )
            self._send(200, raw)
            return

        inputs = body["input"]
        width = self.dimensions + 1 if self.mode == "wrong_length" else self.dimensions
        data: list[dict[str, Any]] = []
        for index in range(len(inputs) - 1, -1, -1):
            vector = [float(index)] + [1.0] * (width - 1)
            if self.mode == "missing_index":
                data.append({"embedding": vector})
            elif self.mode == "duplicate_index":
                data.append({"index": 0, "embedding": vector})
            else:
                data.append({"index": index, "embedding": vector})
        self._send(200, json.dumps({"data": data}).encode("utf-8"))

    def _send(self, status: int, raw: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture
def embedding_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), MockEmbeddingHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    MockEmbeddingHandler.mode = "ok"
    MockEmbeddingHandler.dimensions = 4
    MockEmbeddingHandler.requests = []
    MockEmbeddingHandler.paths = []
    try:
        yield f"http://127.0.0.1:{port}", MockEmbeddingHandler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_generation_id_is_stable_and_changes_with_every_field() -> None:
    profile = _profile()
    again = _profile()
    digest = hashlib.sha256(
        canonical_json(profile.model_dump(mode="json")).encode()
    ).hexdigest()
    assert profile.generation_id == again.generation_id == digest
    assert len(profile.generation_id) == 64

    variants = {
        "model": "other-model",
        "model_revision": "rev-2",
        "dimensions": 8,
        "pooling": "cls",
        "query_prefix": "q: ",
        "document_prefix": "d: ",
        "normalize": True,
    }
    seen = {profile.generation_id}
    for field, value in variants.items():
        changed = profile.model_copy(update={field: value})
        assert changed.generation_id not in seen
        seen.add(changed.generation_id)


def test_prefixes_and_batched_requests_stay_ordered(
    embedding_server: tuple[str, type[MockEmbeddingHandler]],
) -> None:
    endpoint, handler = embedding_server
    profile = _profile()
    embedder = HttpEmbedder(endpoint, profile)
    assert isinstance(embedder, Embedder)

    texts = [f"chunk-{index}" for index in range(130)]
    vectors = embedder.embed_documents(texts)

    assert handler.paths == ["/v1/embeddings"] * 3
    assert [len(request["input"]) for request in handler.requests] == [64, 64, 2]
    assert handler.requests[0]["model"] == profile.model
    assert handler.requests[0]["input"] == ["doc: " + text for text in texts[:64]]
    assert handler.requests[1]["input"] == ["doc: " + text for text in texts[64:128]]
    assert handler.requests[2]["input"] == ["doc: " + text for text in texts[128:]]
    assert len(vectors) == 130
    # The server returns each batch reversed; the client restores index order.
    assert vectors[0][0] == pytest.approx(0.0)
    assert vectors[63][0] == pytest.approx(63.0)
    assert vectors[64][0] == pytest.approx(0.0)
    assert vectors[129][0] == pytest.approx(1.0)
    assert vectors[0] == [0.0, 1.0, 1.0, 1.0]

    handler.requests = []
    query = embedder.embed_query("where is the lease")
    assert handler.requests[0]["input"] == ["query: where is the lease"]
    assert query[0] == pytest.approx(0.0)


def test_normalize_l2_scales_the_vector(
    embedding_server: tuple[str, type[MockEmbeddingHandler]],
) -> None:
    endpoint, handler = embedding_server
    handler.mode = "fixed_3_4"
    embedder = HttpEmbedder(endpoint, _profile(dimensions=2, normalize=True))
    vector = embedder.embed_query("q")
    assert vector == pytest.approx([0.6, 0.8])


@pytest.mark.parametrize(
    ("mode", "dimensions"),
    [
        ("wrong_length", 4),
        ("missing_index", 4),
        ("duplicate_index", 4),
        ("bad_json", 4),
        ("500", 4),
    ],
)
def test_bad_responses_raise_embedding_unavailable(
    embedding_server: tuple[str, type[MockEmbeddingHandler]],
    mode: str,
    dimensions: int,
) -> None:
    endpoint, handler = embedding_server
    handler.mode = mode
    handler.dimensions = dimensions
    embedder = HttpEmbedder(endpoint, _profile(dimensions=dimensions))
    with pytest.raises(EmbeddingUnavailable) as excinfo:
        embedder.embed_documents(["one", "two"])
    assert excinfo.value.code == "embedding_unavailable"
    assert excinfo.value.status_code == 503
    assert len(handler.requests) == 1


def test_timeout_raises_embedding_unavailable(
    embedding_server: tuple[str, type[MockEmbeddingHandler]],
) -> None:
    endpoint, handler = embedding_server
    handler.mode = "slow"
    embedder = HttpEmbedder(endpoint, _profile(), timeout_s=0.05)
    with pytest.raises(EmbeddingUnavailable):
        embedder.embed_query("q")


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://api.openai.com/v1",
        "https://embeddings.example.com:8080",
        "http://10.0.0.5:8080",
        "http://[::2]:8080",
    ],
)
def test_non_loopback_endpoint_is_rejected(endpoint: str) -> None:
    with pytest.raises(ValueError, match="loopback"):
        HttpEmbedder(endpoint, _profile())


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1:9",
        "http://localhost:9",
        "http://[::1]:9",
        "http://LocalHost:9",
    ],
)
def test_loopback_endpoint_is_accepted(endpoint: str) -> None:
    embedder = HttpEmbedder(endpoint, _profile())
    assert embedder.endpoint == endpoint.rstrip("/")

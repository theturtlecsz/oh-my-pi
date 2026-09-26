"""Tests for semantic retrieval (FK-3) and reranking (OMP-311-s05)."""

from __future__ import annotations

import asyncio
import http.server
import json
import threading
from typing import Any
from uuid import uuid4

import pytest
from omp_knowledge.context.compiler import compile_bundle
from omp_knowledge.context.models import (
    CompileRequest,
    ContextItem,
    StageIdentity,
)
from omp_knowledge.context.rerank import (
    HttpReranker,
    OrderReranker,
    Reranker,
    RerankerUnavailable,
)
from omp_knowledge.context.semantic import semantic_items
from support.fixtures import load_staged_fixture
from support.null_engine import NullEngine


class WordCounter:
    profile = "test-words-v1"

    def count(self, texts: Any) -> list[int]:
        return [len(text.split()) for text in texts]


class SpyEngine(NullEngine):
    """Spy over NullEngine to observe query invocations."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.query_called = False

    async def query(self, *args: Any, **kwargs: Any) -> Any:
        self.query_called = True
        return await super().query(*args, **kwargs)


class MockRerankHandler(http.server.BaseHTTPRequestHandler):
    mode: str = "ok"
    received_payload: dict[str, Any] | None = None

    def log_message(self, format: str, *args: Any) -> None:
        pass  # suppress logging during tests

    def do_POST(self) -> None:
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len)
        MockRerankHandler.received_payload = json.loads(body.decode("utf-8"))

        if self.mode == "ok":
            docs = MockRerankHandler.received_payload.get("documents", [])
            results = [
                {"index": i, "relevance_score": 0.1234567 + (i * 0.1)}
                for i in range(len(docs))
            ]
            resp_body = json.dumps({"results": results}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)
        elif self.mode == "missing_index":
            docs = MockRerankHandler.received_payload.get("documents", [])
            results = [
                {"index": i, "relevance_score": 0.5}
                for i in range(1, len(docs))
            ]
            resp_body = json.dumps({"results": results}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)
        elif self.mode == "duplicate_index":
            docs = MockRerankHandler.received_payload.get("documents", [])
            results = [
                {"index": 0, "relevance_score": 0.5}
                for _ in range(len(docs))
            ]
            resp_body = json.dumps({"results": results}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)
        elif self.mode == "500":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"internal server error")
        elif self.mode == "slow":
            import time
            time.sleep(0.3)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"results": []}')


@pytest.fixture
def rerank_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), MockRerankHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    MockRerankHandler.mode = "ok"
    MockRerankHandler.received_payload = None
    try:
        yield f"http://127.0.0.1:{port}", MockRerankHandler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


# ===========================================================================
# 1. Semantic retrieval tests
# ===========================================================================


def test_null_engine_with_fixture_a_returns_current_items() -> None:
    async def _run() -> None:
        engine = NullEngine()
        workspace_id = uuid4()
        repository_id = uuid4()
        snapshot_id = "a" * 64

        _, _, _, raw_facts = load_staged_fixture("A")
        await engine.ingest_snapshot(
            workspace_id=workspace_id,
            repository_id=repository_id,
            snapshot_id=snapshot_id,
            facts=raw_facts,
        )
        engine.publish(
            workspace_id=workspace_id,
            repository_id=repository_id,
            snapshot_id=snapshot_id,
        )

        items = await semantic_items(
            engine=engine,
            selection=[(workspace_id, repository_id, snapshot_id)],
            permitted_repo_ids={repository_id},
            query_text="normalize",
            limit=10,
        )

        assert len(items) > 0
        for item in items:
            assert item.status == "current"
            assert item.section == "semantic"
            assert item.detail == snapshot_id
            assert not item.mandatory
            assert item.score is None

        # Verify formatting `name kind file:line`
        alpha_norm = next(it for it in items if "py/pkg/alpha.normalize" in it.text)
        assert alpha_norm.text == "py/pkg/alpha.normalize symbol py/pkg/alpha.py:1"
        assert alpha_norm.ref == "950270693f0b0c8255a1dbb940c4247d"

    asyncio.run(_run())


def test_unpublished_snapshot_returns_missing_item() -> None:
    async def _run() -> None:
        engine = NullEngine()
        workspace_id = uuid4()
        repository_id = uuid4()
        snapshot_id = "b" * 64

        # Ingested but NOT published
        _, _, _, raw_facts = load_staged_fixture("A")
        await engine.ingest_snapshot(
            workspace_id=workspace_id,
            repository_id=repository_id,
            snapshot_id=snapshot_id,
            facts=raw_facts,
        )

        items = await semantic_items(
            engine=engine,
            selection=[(workspace_id, repository_id, snapshot_id)],
            permitted_repo_ids={repository_id},
            query_text="*",
            limit=10,
        )

        assert len(items) == 1
        assert items[0].status == "missing"
        assert items[0].section == "semantic"
        assert items[0].detail == "snapshot_not_published"
        assert items[0].ref == snapshot_id

    asyncio.run(_run())


def test_engine_unavailable_returns_missing_with_code() -> None:
    async def _run() -> None:
        engine = NullEngine(available=False)
        workspace_id = uuid4()
        repository_id = uuid4()
        snapshot_id = "c" * 64

        items = await semantic_items(
            engine=engine,
            selection=[(workspace_id, repository_id, snapshot_id)],
            permitted_repo_ids={repository_id},
            query_text="*",
            limit=10,
        )

        assert len(items) == 1
        assert items[0].status == "missing"
        assert items[0].section == "semantic"
        assert items[0].detail == "engine_unavailable"
        assert items[0].ref == snapshot_id

    asyncio.run(_run())


def test_unpermitted_repo_returns_denied_and_engine_not_queried() -> None:
    async def _run() -> None:
        engine = SpyEngine()
        workspace_id = uuid4()
        repository_id = uuid4()
        snapshot_id = "d" * 64
        other_repo_id = uuid4()

        items = await semantic_items(
            engine=engine,
            selection=[(workspace_id, repository_id, snapshot_id)],
            permitted_repo_ids={other_repo_id},  # repository_id is NOT permitted
            query_text="*",
            limit=10,
        )

        assert engine.query_called is False
        assert len(items) == 1
        assert items[0].status == "denied"
        assert items[0].section == "semantic"
        assert items[0].detail == "repo not permitted"
        assert items[0].ref == str(repository_id)

    asyncio.run(_run())


def test_engine_timeout_returns_missing_with_code() -> None:
    class TimeoutEngine(NullEngine):
        async def query(self, *args: Any, **kwargs: Any) -> Any:
            from omp_knowledge.errors import EngineTimeoutError
            raise EngineTimeoutError("timeout exceeded")

    async def _run() -> None:
        engine = TimeoutEngine()
        workspace_id = uuid4()
        repository_id = uuid4()
        snapshot_id = "e" * 64

        items = await semantic_items(
            engine=engine,
            selection=[(workspace_id, repository_id, snapshot_id)],
            permitted_repo_ids={repository_id},
            query_text="*",
            limit=10,
        )

        assert len(items) == 1
        assert items[0].status == "missing"
        assert items[0].section == "semantic"
        assert items[0].detail == "engine_timeout"
        assert items[0].ref == snapshot_id

    asyncio.run(_run())


# ===========================================================================
# 2. Reranker tests
# ===========================================================================


def test_order_reranker_stable_and_preserves_order() -> None:
    reranker = OrderReranker()
    assert isinstance(reranker, Reranker)

    items = [
        ContextItem(
            section="semantic",
            source="retrieval",
            ref="fact-0",
            text="first item",
            status="current",
            score=None,
        ),
        ContextItem(
            section="exact",
            source="work",
            ref="mandatory-fact",
            text="mandatory item",
            status="current",
            mandatory=True,
            score=None,
        ),
        ContextItem(
            section="semantic",
            source="retrieval",
            ref="fact-1",
            text="second item",
            status="current",
            score=None,
        ),
    ]

    res1 = reranker.rerank("query", items)
    res2 = reranker.rerank("query", items)

    assert res1 == res2
    assert res1[0].score == 0.0
    assert res1[1].score is None  # Mandatory item untouched
    assert res1[2].score == -2.0


def test_http_reranker_assigns_scores(rerank_server: tuple[str, type[MockRerankHandler]]) -> None:
    url, handler_cls = rerank_server
    handler_cls.mode = "ok"

    reranker = HttpReranker(url=url, model="test-reranker-v1")
    assert isinstance(reranker, Reranker)

    items = [
        ContextItem(
            section="semantic",
            source="retrieval",
            ref="fact-0",
            text="apple pie recipe",
            status="current",
            score=None,
        ),
        ContextItem(
            section="exact",
            source="work",
            ref="mandatory-rev",
            text="mandatory title",
            status="current",
            mandatory=True,
            score=None,
        ),
        ContextItem(
            section="semantic",
            source="retrieval",
            ref="fact-1",
            text="banana bread recipe",
            status="current",
            score=None,
        ),
    ]

    scored = reranker.rerank("baking dessert", items)

    assert len(scored) == 3
    # 0.1234567 rounded to 6 decimals is 0.123457
    assert scored[0].score == 0.123457
    assert scored[1].score is None  # Mandatory untouched
    # 0.1234567 + 0.2 = 0.3234567 -> rounded is 0.323457
    assert scored[2].score == 0.323457


def test_http_reranker_missing_index_raises(rerank_server: tuple[str, type[MockRerankHandler]]) -> None:
    url, handler_cls = rerank_server
    handler_cls.mode = "missing_index"

    reranker = HttpReranker(url=url, model="test-reranker-v1")
    items = [
        ContextItem(
            section="semantic",
            source="retrieval",
            ref="fact-0",
            text="doc1",
            status="current",
        ),
        ContextItem(
            section="semantic",
            source="retrieval",
            ref="fact-1",
            text="doc2",
            status="current",
        ),
    ]

    with pytest.raises(RerankerUnavailable):
        reranker.rerank("query", items)


def test_http_reranker_duplicate_index_raises(rerank_server: tuple[str, type[MockRerankHandler]]) -> None:
    url, handler_cls = rerank_server
    handler_cls.mode = "duplicate_index"

    reranker = HttpReranker(url=url, model="test-reranker-v1")
    items = [
        ContextItem(
            section="semantic",
            source="retrieval",
            ref="fact-0",
            text="doc1",
            status="current",
        ),
        ContextItem(
            section="semantic",
            source="retrieval",
            ref="fact-1",
            text="doc2",
            status="current",
        ),
    ]

    with pytest.raises(RerankerUnavailable):
        reranker.rerank("query", items)


def test_http_reranker_500_raises(rerank_server: tuple[str, type[MockRerankHandler]]) -> None:
    url, handler_cls = rerank_server
    handler_cls.mode = "500"

    reranker = HttpReranker(url=url, model="test-reranker-v1")
    items = [
        ContextItem(
            section="semantic",
            source="retrieval",
            ref="fact-0",
            text="doc1",
            status="current",
        ),
    ]

    with pytest.raises(RerankerUnavailable):
        reranker.rerank("query", items)


def test_http_reranker_timeout_raises(rerank_server: tuple[str, type[MockRerankHandler]]) -> None:
    url, handler_cls = rerank_server
    handler_cls.mode = "slow"

    reranker = HttpReranker(url=url, model="test-reranker-v1", timeout_s=0.05)
    items = [
        ContextItem(
            section="semantic",
            source="retrieval",
            ref="fact-0",
            text="doc1",
            status="current",
        ),
    ]

    with pytest.raises(RerankerUnavailable):
        reranker.rerank("query", items)


def test_http_reranker_rejects_non_http_scheme() -> None:
    for bad_url in ("file:///etc/passwd", "ftp://example.com/rerank", "localhost:8080", ""):
        with pytest.raises(ValueError, match="HttpReranker URL must use http or https scheme"):
            HttpReranker(url=bad_url, model="test-model")


# ===========================================================================
# 3. Compiler integration test
# ===========================================================================


def test_semantic_items_integrate_with_compiler() -> None:
    async def _run() -> None:
        engine = NullEngine()
        workspace_id = uuid4()
        repository_id = uuid4()
        snapshot_id = "a" * 64

        _, _, _, raw_facts = load_staged_fixture("A")
        await engine.ingest_snapshot(
            workspace_id=workspace_id,
            repository_id=repository_id,
            snapshot_id=snapshot_id,
            facts=raw_facts,
        )
        engine.publish(
            workspace_id=workspace_id,
            repository_id=repository_id,
            snapshot_id=snapshot_id,
        )

        items = await semantic_items(
            engine=engine,
            selection=[(workspace_id, repository_id, snapshot_id)],
            permitted_repo_ids={repository_id},
            query_text="normalize",
            limit=5,
        )
        reranked = OrderReranker().rerank("normalize", items)

        identity = StageIdentity(
            work_id=str(uuid4()),
            work_key="OMP-311",
            revision_id=str(uuid4()),
            stage="implement",
            attempt_id=str(uuid4()),
            candidate_id=str(uuid4()),
        )
        req = CompileRequest(
            identity=identity,
            token_budget=10_000,
            encoding="utf-8",
            items=tuple(reranked),
        )

        bundle = compile_bundle(req, WordCounter())
        assert bundle.tokens <= 10_000
        assert "## semantic" in bundle.text
        for it in reranked:
            assert it.ref in [included.ref for included in bundle.included]

    asyncio.run(_run())

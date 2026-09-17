"""Opt-in live evaluations R0-R8 for the reranking stage (``rerank_v1``).

Runs against live Neo4j, PostgreSQL (pgvector), the local Qwen3 embedding
endpoint (18081) and the qualified local Qwen3 reranker (18082, ``POST
/v1/rerank`` answering ``results[].index`` + ``results[].relevance_score``).
This module never runs by default; it is gated like
``tests/test_semantic_pgvector_integration.py`` (whose helpers it imports) plus
``OMP_KNOWLEDGE_RERANK_INTEGRATION=1``. ``MOCK_EMBEDDING`` fails the module
immediately; nothing here uses a mock model or synthetic scores.

Adapter-level evals (one asyncio loop per module):
- R0: reranker qualification: ``/v1/models`` lists the model, a direct
      ``/v1/rerank`` probe answers in the qualified shape, the adapter's readiness
      check passes; evidence JSON written to ``OMP_KNOWLEDGE_RERANK_EVIDENCE_PATH``.
- R1: fixture A on live stores: ``status == applied``, the request body is exactly
      raw query + indexed ``semantic_text`` documents, ``request_sha256`` recomputes
      from the returned facts, post order is the ``(-score, pre-index)`` sort, and a
      rerank-disabled adapter on the same snapshot returns the pre-rerank order.
- R2: withdrawn (``correct``) and control-snapshot facts never reach the reranker.
- R3: unreachable reranker, optional policy: ``fallback`` with identical candidates
      and order to the rerank-disabled adapter, ``retrieval.mode == semantic``.
- R5: real hydration + ``httpx.MockTransport`` malformed answers ->
      ``RerankContractError`` (never fallback / exact_fallback); programming errors
      propagate.
- R6: endpoint userinfo / bolt scheme rejected at config; model absent from the
      endpoint's ``/v1/models`` (bogus model on 18082, reranker model on 18081) ->
      ``RerankBindingError`` before any ``/v1/rerank`` call.

Server-level evals (synchronous ``TestClient``, need the ``pg_cluster`` /
``dual_pg_cluster`` ledger fixtures):
- R4: required policy through ``/v1/query``: 503 ``engine_unavailable``, no secret.
- R7: deterministic replay through ``/v1/query`` on one adapter and after a fresh
      adapter is swapped in; ``/v1/status`` reports the sanitized reranker binding.
- R8: ``/v1/context/compile`` is byte-identical with rerank enabled, on replay and
      with rerank disabled (the compiler sorts facts by fact_id).

Loop note: see the semantic module; the synchronous evals evict Cognee's cached
engines before and after running.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from omp_knowledge.config import KnowledgeConfig
from omp_knowledge.engine import cognee_adapter
from omp_knowledge.engine.backends import (
    SnapshotScope,
    rerank_documents,
    rerank_request_sha256,
    semantic_text,
)
from omp_knowledge.engine.cognee_adapter import RealCogneeAdapter
from omp_knowledge.engine.protocol import (
    EngineUnavailableError,
    RerankBindingError,
    RerankContractError,
)
from omp_knowledge.server import create_app
from omp_work.knowledge_contracts import JobState
from support.fake_cognee import RecordingRerankTransport, install_fake_reranker, rerank_response
from support.fixtures import load_staged_fixture, make_synthetic_enola_snapshot_ref
from support.native_fixture import insert_test_receipt
from test_semantic_pgvector_integration import (
    OUTAGE_TIMEOUT_SECONDS,
    QUALIFIED_EMBEDDING_ENDPOINT,
    _assert_no_secret,
    _await_redacted,
    _canonical_snapshot_id,
    _check_gates_and_mock,
    _evict_loop_bound_engines,
    _fail_redacted,
    _ingest_payload,
    _portal_call,
    _scope_node_ids,
    _secrets_tuple,
    _semantic_config,
    _semantic_config_for_cluster,
    _semantic_values,
    _server_setup,
)

GATE_RERANK_ENV = "OMP_KNOWLEDGE_RERANK_INTEGRATION"
EVIDENCE_ENV = "OMP_KNOWLEDGE_RERANK_EVIDENCE_PATH"

QUALIFIED_RERANK_ENDPOINT = "http://127.0.0.1:18082"
QUALIFIED_RERANK_MODEL = "qwen3-reranker-0.6b-q8"
UNREACHABLE_RERANK_ENDPOINT = "http://127.0.0.1:19999"

RERANK_QUERY = "typescript function that normalizes input"


def _rerank_gate() -> str | None:
    reason = _check_gates_and_mock()  # fails hard on MOCK_EMBEDDING, never skips it
    if reason is not None:
        return reason
    if os.environ.get(GATE_RERANK_ENV) != "1":
        return f"set {GATE_RERANK_ENV}=1 to run reranker integration"
    return None


_SKIP_REASON = _rerank_gate()
pytestmark = pytest.mark.skipif(_SKIP_REASON is not None, reason=_SKIP_REASON or "")


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _rerank_values(
    *,
    reranking_required: bool = False,
    reranking_endpoint: str | None = None,
    reranking_model: str | None = None,
    reranking_timeout_seconds: float | None = None,
    **semantic_kwargs: Any,
) -> dict[str, Any]:
    values = _semantic_values(**semantic_kwargs)
    values.update(
        reranking_provider="rerank_v1",
        reranking_model=reranking_model or os.environ.get("OMP_KNOWLEDGE_RERANKING_MODEL", QUALIFIED_RERANK_MODEL),
        reranking_endpoint=reranking_endpoint or os.environ.get("OMP_KNOWLEDGE_RERANKING_ENDPOINT", QUALIFIED_RERANK_ENDPOINT),
        reranking_required=reranking_required,
    )
    if reranking_timeout_seconds is not None:
        values["reranking_timeout_seconds"] = reranking_timeout_seconds
    return values


def _rerank_config(state_root: Path, **kwargs: Any) -> KnowledgeConfig:
    return KnowledgeConfig(state_dir=state_root / "state", config_dir=state_root / "config", **_rerank_values(**kwargs))


def _rerank_config_for_cluster(cluster: KnowledgeConfig, **kwargs: Any) -> KnowledgeConfig:
    merged = cluster.model_dump()
    merged.update(_rerank_values(**kwargs))
    return KnowledgeConfig(**merged)


# ---------------------------------------------------------------------------
# Evidence and request recording
# ---------------------------------------------------------------------------


def _evidence_path(tmp_path: Path) -> Path:
    return Path(os.environ.get(EVIDENCE_ENV, str(tmp_path / "rerank-evidence.json")))


def _write_evidence(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def _append_evidence(path: Path, key: str, data: dict[str, Any]) -> None:
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            existing = loaded if isinstance(loaded, dict) else {}
        except ValueError:
            existing = {}
    existing.setdefault("evals", {})[key] = data
    _write_evidence(path, existing)


class _RecordingRealTransport(httpx.AsyncBaseTransport):
    """Records what the adapter sends and forwards it over a real HTTP transport.

    The adapter opens one client per call and closes it, so each client gets its
    own inner transport while every instance shares ``log``.
    """

    def __init__(self, log: list[dict[str, Any]]) -> None:
        self._inner = httpx.AsyncHTTPTransport()
        self.log = log

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body: Any = None
        if request.content:
            try:
                body = json.loads(request.content)
            except ValueError:
                body = None
        self.log.append({"method": request.method, "path": request.url.path, "url": str(request.url), "json": body})
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def _record_real_rerank_requests(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    log: list[dict[str, Any]] = []

    def _client(timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=_RecordingRealTransport(log), timeout=timeout)

    monkeypatch.setattr(cognee_adapter, "rerank_http_client", _client)
    return log


def _rerank_posts(log: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry for entry in log if entry["path"] == "/v1/rerank"]


def _models_gets(log: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry for entry in log if entry["path"] == "/v1/models"]


def _http_json(url: str, *, body: dict[str, Any] | None = None, timeout: float = 30.0) -> tuple[int, Any]:
    """Direct probe without the adapter: (status, decoded JSON or raw text)."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "omp-knowledge-r0"},
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        status = exc.code
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw


# ---------------------------------------------------------------------------
# Shared adapter-level helpers
# ---------------------------------------------------------------------------


async def _ingest_fixture(adapter: RealCogneeAdapter, *, ws_id: UUID, repo_id: UUID, snap_id: str, fixture: str, secrets: tuple[str, ...]) -> list[dict[str, Any]]:
    _, receipt_bytes, insights_bytes, raw_facts = load_staged_fixture(fixture)
    snap_ref = make_synthetic_enola_snapshot_ref(repository_id=repo_id, fixture_name=fixture, snapshot_id=snap_id)
    result = await _await_redacted(
        f"ingest_snapshot ({fixture}) failed",
        adapter.ingest_snapshot(
            workspace_id=ws_id,
            repository_id=repo_id,
            snapshot_ref=snap_ref,
            facts=raw_facts,
            receipt=json.loads(receipt_bytes),
            insights=json.loads(insights_bytes),
        ),
        secrets,
    )
    assert result.semantic is not None and result.semantic.status == "indexed", "live semantic ingest must index"
    return raw_facts


async def _query(adapter: RealCogneeAdapter, *, ws_id: UUID, repo_id: UUID, snap_id: str, text: str, limit: int, secrets: tuple[str, ...]) -> Any:
    return await _await_redacted(
        f"query ({text!r}) failed",
        adapter.query(workspace_id=ws_id, repository_id=repo_id, snapshot_id=snap_id, query_text=text, limit=limit),
        secrets,
    )


async def _retire_quietly(adapter: RealCogneeAdapter, ws_id: UUID, repo_id: UUID, *snapshot_ids: str) -> None:
    for snapshot_id in snapshot_ids:
        try:
            await adapter.retire(workspace_id=ws_id, repository_id=repo_id, snapshot_id=snapshot_id)
        except Exception:
            pass


def _node_ids(facts: Any) -> list[str]:
    return [str(fact.node_id) for fact in facts]


def _assert_applied_receipt(res: Any, query_text: str, log: list[dict[str, Any]] | None = None) -> Any:
    """Checks every self-describing property of an ``applied`` RerankReport
    against the returned facts (and the recorded wire body when ``log`` is given)."""
    assert res.retrieval is not None
    rr = res.retrieval.rerank
    assert rr is not None, "reranking route must always carry a rerank receipt"
    assert rr.status == "applied", rr
    assert rr.provider == "rerank_v1"
    assert rr.pair_format == "rerank_v1/semantic_text"
    assert rr.document_count == len(res.facts) >= 2
    assert rr.reason is None
    # Identity: same set, same count, total_matched untouched, facts in post order.
    fact_ids = _node_ids(res.facts)
    assert list(rr.post_rerank_order) == fact_ids
    assert set(rr.pre_rerank_order) == set(rr.post_rerank_order) == set(fact_ids)
    assert len(rr.pre_rerank_order) == len(rr.post_rerank_order) == len(fact_ids)
    assert res.total_matched == len(res.facts)
    # Pair format readback: documents are the indexed semantic_text in pre order.
    by_node = {str(fact.node_id): fact for fact in res.facts}
    pre_facts = [by_node[node_id] for node_id in rr.pre_rerank_order]
    documents = rerank_documents(query_text, pre_facts)
    assert rr.request_sha256 == rerank_request_sha256(query_text, documents)
    # Post order is the (-score, pre-index) sort and scores align with it.
    assert len(rr.scores) == len(fact_ids)
    pre_index = {node_id: index for index, node_id in enumerate(rr.pre_rerank_order)}
    keys = [(-score, pre_index[node_id]) for node_id, score in zip(rr.post_rerank_order, rr.scores)]
    assert keys == sorted(keys)
    # Retrieval scores stay aligned with their facts (semantic modes).
    if res.retrieval.mode in ("semantic", "semantic_empty"):
        assert len(res.retrieval.scores) == len(res.facts)
    if log is not None:
        posts = _rerank_posts(log)
        assert len(posts) == 1, posts
        assert posts[0]["json"] == {"model": rr.model_id, "query": query_text, "documents": list(documents)}
        assert posts[0]["url"] == f"{rr.endpoint}/v1/rerank"
    return rr


# ---------------------------------------------------------------------------
# R0: reranker qualification and evidence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="module")
async def test_r0_reranker_qualification_and_evidence(tmp_path: Path) -> None:
    cfg = _rerank_config(tmp_path)
    secrets = _secrets_tuple(cfg)
    endpoint = cfg.reranking_endpoint
    assert endpoint is not None

    # 1. /v1/models lists the configured reranker model.
    try:
        status, models = _http_json(f"{endpoint}/v1/models")
    except Exception as exc:  # noqa: BLE001 - surfaced redacted
        _fail_redacted(f"GET {endpoint}/v1/models failed", exc, secrets)
        raise AssertionError("unreachable")
    assert status == 200, f"/v1/models returned {status}"
    available = [item["id"] for item in models.get("data", []) if isinstance(item, dict) and "id" in item]
    assert cfg.reranking_model in available, f"{cfg.reranking_model} not in {available}"

    # 2. Direct /v1/rerank probe with two documents in the adapter's pair format.
    probe_query = "python function that normalizes a string"
    probe_documents = [
        semantic_text("symbol", "py/pkg/alpha.normalize", "py/pkg/alpha.py", {"language": "python", "symbol_kind": "function"}),
        semantic_text("module", "ts/src", "ts/src", {"language": "typescript", "package_name": "enola-fixture"}),
    ]
    try:
        probe_status, probe = _http_json(
            f"{endpoint}/v1/rerank",
            body={"model": cfg.reranking_model, "query": probe_query, "documents": probe_documents},
        )
    except Exception as exc:  # noqa: BLE001 - surfaced redacted
        _fail_redacted(f"POST {endpoint}/v1/rerank failed", exc, secrets)
        raise AssertionError("unreachable")
    assert probe_status == 200, f"/v1/rerank returned {probe_status}: {probe!r}"
    assert isinstance(probe, dict) and isinstance(probe.get("results"), list)
    results = probe["results"]
    assert len(results) == 2
    for item in results:
        assert isinstance(item, dict)
        assert isinstance(item.get("index"), int) and not isinstance(item.get("index"), bool)
        assert isinstance(item.get("relevance_score"), (int, float)) and not isinstance(item.get("relevance_score"), bool)
    assert {item["index"] for item in results} == {0, 1}
    model_echoed = probe.get("model")
    if model_echoed is not None:
        assert str(model_echoed) == cfg.reranking_model

    # 3. The embedding endpoint (18081) is not a reranker: record what it answers.
    try:
        emb_status, _ = _http_json(
            f"{QUALIFIED_EMBEDDING_ENDPOINT}/v1/rerank",
            body={"model": cfg.reranking_model, "query": probe_query, "documents": probe_documents},
            timeout=10.0,
        )
        response_status_from_18081_probe: int | str = emb_status
    except Exception as exc:  # noqa: BLE001 - recorded, not asserted
        response_status_from_18081_probe = f"unreachable: {type(exc).__name__}"
    try:
        _, emb_models = _http_json(f"{QUALIFIED_EMBEDDING_ENDPOINT}/v1/models", timeout=10.0)
        embedding_endpoint_models = [item["id"] for item in emb_models.get("data", []) if isinstance(item, dict) and "id" in item]
    except Exception as exc:  # noqa: BLE001 - recorded, not asserted
        embedding_endpoint_models = [f"unreachable: {type(exc).__name__}"]

    # 4. The adapter's own readiness check passes against the live endpoint.
    try:
        adapter = RealCogneeAdapter(cfg)
    except Exception as exc:  # noqa: BLE001 - surfaced redacted
        _fail_redacted("RealCogneeAdapter construction failed", exc, secrets)
        raise AssertionError("unreachable")
    await _await_redacted("adapter rerank readiness failed", adapter._ensure_rerank_ready("R0 readiness"), secrets)
    assert adapter._rerank_ready_verified is True

    evidence = {
        "eval": "R0",
        "endpoint": endpoint,
        "model": cfg.reranking_model,
        "available_models": available,
        "probe_query": probe_query,
        "probe_documents": probe_documents,
        "probe_results": results,
        "model_echoed": model_echoed,
        "response_status_from_18081_probe": response_status_from_18081_probe,
        "embedding_endpoint": QUALIFIED_EMBEDDING_ENDPOINT,
        "embedding_endpoint_models": embedding_endpoint_models,
        "adapter_readiness": "verified",
        "pair_format": "rerank_v1/semantic_text",
    }
    _write_evidence(_evidence_path(tmp_path), evidence)


# ---------------------------------------------------------------------------
# R1: applied receipt on live stores; disabled adapter returns the pre order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="module")
async def test_r1_applied_receipt_and_pre_order_matches_disabled_adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _rerank_config(tmp_path / "rr")
    secrets = _secrets_tuple(cfg)
    log = _record_real_rerank_requests(monkeypatch)
    adapter = RealCogneeAdapter(cfg)
    plain = RealCogneeAdapter(_semantic_config(tmp_path / "plain"))

    ws_id, repo_id = uuid4(), uuid4()
    snap_id = _canonical_snapshot_id("r1", ws_id, repo_id)
    scope = SnapshotScope(workspace_id=ws_id, repository_id=repo_id, snapshot_id=snap_id)
    try:
        raw_facts = await _ingest_fixture(adapter, ws_id=ws_id, repo_id=repo_id, snap_id=snap_id, fixture="A", secrets=secrets)
        scope_ids = _scope_node_ids(scope, raw_facts)

        res = await _query(adapter, ws_id=ws_id, repo_id=repo_id, snap_id=snap_id, text=RERANK_QUERY, limit=5, secrets=secrets)
        assert res.retrieval.mode == "semantic"
        rr = _assert_applied_receipt(res, RERANK_QUERY, log)
        assert rr.model_id == cfg.reranking_model
        assert rr.endpoint == QUALIFIED_RERANK_ENDPOINT
        assert set(rr.pre_rerank_order) <= scope_ids
        assert len(_models_gets(log)) == 1

        # The rerank-disabled adapter on the same snapshot yields the pre-rerank order
        # and the same cosine scores per fact.
        baseline = await _query(plain, ws_id=ws_id, repo_id=repo_id, snap_id=snap_id, text=RERANK_QUERY, limit=5, secrets=secrets)
        assert baseline.retrieval is not None and baseline.retrieval.rerank is None
        assert baseline.retrieval.mode == "semantic"
        assert _node_ids(baseline.facts) == list(rr.pre_rerank_order)
        assert baseline.total_matched == res.total_matched
        baseline_scores = dict(zip(_node_ids(baseline.facts), baseline.retrieval.scores))
        rerank_scores = dict(zip(_node_ids(res.facts), res.retrieval.scores))
        assert set(baseline_scores) == set(rerank_scores)
        for node_id, score in baseline_scores.items():
            other = rerank_scores[node_id]
            if score is None or other is None:
                assert score is other
            else:
                assert abs(score - other) < 1e-6

        # Second call on the same adapter: readiness is cached, one more POST only.
        again = await _query(adapter, ws_id=ws_id, repo_id=repo_id, snap_id=snap_id, text=RERANK_QUERY, limit=5, secrets=secrets)
        assert again.retrieval.rerank.request_sha256 == rr.request_sha256
        assert len(_models_gets(log)) == 1 and len(_rerank_posts(log)) == 2

        node_to_fact = {str(fact.node_id): fact.fact_id for fact in res.facts}
        _append_evidence(
            _evidence_path(tmp_path),
            "R1",
            {
                "query": RERANK_QUERY,
                "limit": 5,
                "request_sha256": rr.request_sha256,
                "pre_rerank_fact_ids": [node_to_fact[n] for n in rr.pre_rerank_order],
                "post_rerank_fact_ids": [node_to_fact[n] for n in rr.post_rerank_order],
                "post_rerank_node_ids": list(rr.post_rerank_order),
                "relevance_scores": list(rr.scores),
                "cosine_scores_post_order": list(res.retrieval.scores),
                "disabled_adapter_order_equals_pre_order": True,
                "documents": list(_rerank_posts(log)[0]["json"]["documents"]),
            },
        )
    except AssertionError:
        raise
    except Exception as exc:  # noqa: BLE001 - surfaced redacted
        _fail_redacted("R1 failed", exc, secrets)
    finally:
        await _retire_quietly(adapter, ws_id, repo_id, snap_id)


# ---------------------------------------------------------------------------
# R2: withdrawn and control facts never reach the reranker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="module")
async def test_r2_withdrawn_and_control_facts_never_reranked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _rerank_config(tmp_path)
    secrets = _secrets_tuple(cfg)
    log = _record_real_rerank_requests(monkeypatch)
    adapter = RealCogneeAdapter(cfg)

    ws_id, repo_id = uuid4(), uuid4()
    snap_id = _canonical_snapshot_id("r2", ws_id, repo_id)
    ctrl_id = _canonical_snapshot_id("r2-control", ws_id, repo_id)
    scope = SnapshotScope(workspace_id=ws_id, repository_id=repo_id, snapshot_id=snap_id)
    ctrl_scope = SnapshotScope(workspace_id=ws_id, repository_id=repo_id, snapshot_id=ctrl_id)
    try:
        raw_facts = await _ingest_fixture(adapter, ws_id=ws_id, repo_id=repo_id, snap_id=snap_id, fixture="A", secrets=secrets)
        ctrl_facts = await _ingest_fixture(adapter, ws_id=ws_id, repo_id=repo_id, snap_id=ctrl_id, fixture="B", secrets=secrets)
        primary_ids = _scope_node_ids(scope, raw_facts)
        ctrl_ids = _scope_node_ids(ctrl_scope, ctrl_facts)
        assert primary_ids.isdisjoint(ctrl_ids)

        f1 = next(f for f in raw_facts if f.get("name") == "py/pkg/alpha.normalize")
        f1_node = str(scope.fact_node_id(f1["id"]))
        f1_document = semantic_text("symbol", f1["name"], f1["file"], f1.get("props") or {})

        query_text = "alpha normalize"
        before = await _query(adapter, ws_id=ws_id, repo_id=repo_id, snap_id=snap_id, text=query_text, limit=20, secrets=secrets)
        rr_before = _assert_applied_receipt(before, query_text)
        assert f1_node in rr_before.pre_rerank_order, "f1 must be a candidate before withdrawal"
        assert f1_document in _rerank_posts(log)[-1]["json"]["documents"]

        withdrawal = await _await_redacted(
            "correct (withdraw f1) failed",
            adapter.correct(
                workspace_id=ws_id,
                repository_id=repo_id,
                snapshot_id=snap_id,
                fact_id=f1["id"],
                properties_update={"withdrawn": True, "withdrawn_by": str(uuid4())},
            ),
            secrets,
        )
        assert withdrawal.success is True and withdrawal.semantic_status == "deleted"

        log.clear()
        after = await _query(adapter, ws_id=ws_id, repo_id=repo_id, snap_id=snap_id, text=query_text, limit=20, secrets=secrets)
        rr = _assert_applied_receipt(after, query_text, log)
        sent_documents = _rerank_posts(log)[0]["json"]["documents"]
        assert rr.document_count == len(after.facts) == len(sent_documents)
        assert f1_node not in rr.pre_rerank_order and f1_node not in rr.post_rerank_order
        assert f1["id"] not in {fact.fact_id for fact in after.facts}
        assert f1_document not in sent_documents
        assert all("withdrawn" not in document for document in sent_documents)
        assert set(rr.pre_rerank_order) <= primary_ids
        assert set(rr.pre_rerank_order).isdisjoint(ctrl_ids)
        assert all(fact.snapshot_id == snap_id for fact in after.facts)
        # Control snapshot in the other scope keeps its own candidates only.
        ctrl = await _query(adapter, ws_id=ws_id, repo_id=repo_id, snap_id=ctrl_id, text=query_text, limit=20, secrets=secrets)
        rr_ctrl = _assert_applied_receipt(ctrl, query_text)
        assert set(rr_ctrl.pre_rerank_order) <= ctrl_ids
        assert set(rr_ctrl.pre_rerank_order).isdisjoint(primary_ids)
    except AssertionError:
        raise
    except Exception as exc:  # noqa: BLE001 - surfaced redacted
        _fail_redacted("R2 failed", exc, secrets)
    finally:
        await _retire_quietly(adapter, ws_id, repo_id, snap_id, ctrl_id)


# ---------------------------------------------------------------------------
# R3: unreachable reranker, optional policy -> fallback with identical order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="module")
async def test_r3_optional_outage_falls_back_to_disabled_order(tmp_path: Path) -> None:
    cfg = _rerank_config(
        tmp_path / "rr",
        reranking_required=False,
        reranking_endpoint=UNREACHABLE_RERANK_ENDPOINT,
        reranking_timeout_seconds=OUTAGE_TIMEOUT_SECONDS,
    )
    secrets = _secrets_tuple(cfg)
    adapter = RealCogneeAdapter(cfg)
    plain = RealCogneeAdapter(_semantic_config(tmp_path / "plain"))

    ws_id, repo_id = uuid4(), uuid4()
    snap_id = _canonical_snapshot_id("r3", ws_id, repo_id)
    try:
        await _ingest_fixture(adapter, ws_id=ws_id, repo_id=repo_id, snap_id=snap_id, fixture="A", secrets=secrets)

        degraded = await _query(adapter, ws_id=ws_id, repo_id=repo_id, snap_id=snap_id, text=RERANK_QUERY, limit=5, secrets=secrets)
        baseline = await _query(plain, ws_id=ws_id, repo_id=repo_id, snap_id=snap_id, text=RERANK_QUERY, limit=5, secrets=secrets)

        assert degraded.retrieval is not None and degraded.retrieval.mode == "semantic"
        rr = degraded.retrieval.rerank
        assert rr is not None and rr.status == "fallback"
        assert rr.reason is not None and "backend unreachable during query (rerank" in rr.reason
        _assert_no_secret(rr.reason, secrets, "fallback reason")
        assert rr.endpoint == UNREACHABLE_RERANK_ENDPOINT
        assert rr.scores == ()
        assert rr.pre_rerank_order == rr.post_rerank_order == tuple(_node_ids(degraded.facts))
        assert rr.document_count == len(degraded.facts) >= 2
        assert rr.request_sha256 is not None

        assert degraded.facts == baseline.facts, "optional outage must keep candidate identity and order"
        assert degraded.total_matched == baseline.total_matched
        assert degraded.retrieval.scores == baseline.retrieval.scores
        assert baseline.retrieval.rerank is None
    except AssertionError:
        raise
    except Exception as exc:  # noqa: BLE001 - surfaced redacted
        _fail_redacted("R3 failed", exc, secrets)
    finally:
        await _retire_quietly(adapter, ws_id, repo_id, snap_id)


# ---------------------------------------------------------------------------
# R5: real hydration, malformed reranker answers are never degraded
# ---------------------------------------------------------------------------


def _malformed_cases() -> list[tuple[str, Any]]:
    return [
        ("missing_results", lambda body: httpx.Response(200, json={"object": "list"})),
        ("index_out_of_range", lambda body: rerank_response([0.5] * len(body["documents"]), indices=list(range(1, len(body["documents"]) + 1)))),
        ("duplicate_index", lambda body: rerank_response([0.5] * len(body["documents"]), indices=[0] * len(body["documents"]))),
        ("string_score", lambda body: rerank_response(["high"] * len(body["documents"]))),
        ("short_results", lambda body: rerank_response([0.5] * (len(body["documents"]) - 1))),
        ("non_json_200", lambda body: httpx.Response(200, content=b"<html>not json</html>")),
        ("http_400", lambda body: httpx.Response(400, json={"error": "bad request"})),
    ]


@pytest.mark.asyncio(loop_scope="module")
async def test_r5_malformed_responses_fail_closed_under_optional_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _rerank_config(tmp_path, reranking_required=False)
    secrets = _secrets_tuple(cfg)
    adapter = RealCogneeAdapter(cfg)

    ws_id, repo_id = uuid4(), uuid4()
    snap_id = _canonical_snapshot_id("r5", ws_id, repo_id)
    query_text = "normalize"
    try:
        await _ingest_fixture(adapter, ws_id=ws_id, repo_id=repo_id, snap_id=snap_id, fixture="A", secrets=secrets)

        # Sanity: the real reranker applies on this snapshot (>= 2 candidates).
        healthy = await _query(adapter, ws_id=ws_id, repo_id=repo_id, snap_id=snap_id, text=query_text, limit=5, secrets=secrets)
        _assert_applied_receipt(healthy, query_text)

        for case, responder in _malformed_cases():
            transport = RecordingRerankTransport(models=(cfg.reranking_model or "",), rerank=responder)
            install_fake_reranker(monkeypatch, transport)
            probe = RealCogneeAdapter(_rerank_config(tmp_path / case, reranking_required=False))
            with pytest.raises(RerankContractError) as exc_info:
                await probe.query(workspace_id=ws_id, repository_id=repo_id, snapshot_id=snap_id, query_text=query_text, limit=5)
            assert str(exc_info.value).startswith("engine_error:"), case
            assert not isinstance(exc_info.value, EngineUnavailableError), case
            _assert_no_secret(str(exc_info.value), secrets, f"R5 {case}")
            assert len(transport.rerank_requests) == 1, f"{case}: real hydration must have reached the endpoint"
            assert len(transport.rerank_requests[0]["json"]["documents"]) >= 2, case

        # Echoed model mismatch is a binding error, not a fallback.
        mismatch = RecordingRerankTransport(
            models=(cfg.reranking_model or "",),
            rerank=lambda body: rerank_response([0.5] * len(body["documents"]), model="another-reranker"),
        )
        install_fake_reranker(monkeypatch, mismatch)
        with pytest.raises(RerankBindingError):
            await RealCogneeAdapter(_rerank_config(tmp_path / "mismatch")).query(
                workspace_id=ws_id, repository_id=repo_id, snapshot_id=snap_id, query_text=query_text, limit=5
            )

        # Programming error inside the pair formatter propagates unchanged.
        install_fake_reranker(monkeypatch, RecordingRerankTransport(models=(cfg.reranking_model or "",)))

        def broken(query: str, facts: Any) -> tuple[str, ...]:
            raise TypeError("pair formatter broke")

        monkeypatch.setattr(cognee_adapter, "rerank_documents", broken)
        with pytest.raises(TypeError, match="pair formatter broke"):
            await RealCogneeAdapter(_rerank_config(tmp_path / "typeerror")).query(
                workspace_id=ws_id, repository_id=repo_id, snapshot_id=snap_id, query_text=query_text, limit=5
            )
        monkeypatch.undo()

        # The real endpoint still applies afterwards: nothing above was cached.
        after = await _query(adapter, ws_id=ws_id, repo_id=repo_id, snap_id=snap_id, text=query_text, limit=5, secrets=secrets)
        _assert_applied_receipt(after, query_text)
    except AssertionError:
        raise
    except Exception as exc:  # noqa: BLE001 - surfaced redacted
        _fail_redacted("R5 failed", exc, secrets)
    finally:
        monkeypatch.undo()
        await _retire_quietly(adapter, ws_id, repo_id, snap_id)


# ---------------------------------------------------------------------------
# R6: endpoint and model binding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="module")
async def test_r6_endpoint_and_model_binding_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    password = "rr-secret-3b9d"
    with pytest.raises(ValidationError) as userinfo_exc:
        _rerank_config(tmp_path / "userinfo", reranking_endpoint=f"http://rr-user:{password}@127.0.0.1:18082")
    # The validator's own message names the setting and the sanitized endpoint only
    # (pydantic's rendering of the raw input is not part of this contract).
    messages = [error["msg"] for error in userinfo_exc.value.errors()]
    assert any("reranking_endpoint must not embed userinfo" in msg and "http://127.0.0.1:18082" in msg for msg in messages)
    assert all(password not in msg and "rr-user" not in msg for msg in messages)
    with pytest.raises(ValueError, match="reranking_endpoint must use one of"):
        _rerank_config(tmp_path / "bolt", reranking_endpoint="bolt://127.0.0.1:18082")

    cfg = _rerank_config(tmp_path / "ok")
    secrets = _secrets_tuple(cfg)
    log = _record_real_rerank_requests(monkeypatch)
    adapter = RealCogneeAdapter(cfg)
    ws_id, repo_id = uuid4(), uuid4()
    snap_id = _canonical_snapshot_id("r6", ws_id, repo_id)
    try:
        await _ingest_fixture(adapter, ws_id=ws_id, repo_id=repo_id, snap_id=snap_id, fixture="A", secrets=secrets)

        # 18082 with a model it does not serve.
        log.clear()
        bogus = RealCogneeAdapter(_rerank_config(tmp_path / "bogus", reranking_model="not-served"))
        with pytest.raises(RerankBindingError) as bogus_exc:
            await bogus.query(workspace_id=ws_id, repository_id=repo_id, snapshot_id=snap_id, query_text=RERANK_QUERY, limit=5)
        assert "reranking_model 'not-served' is not served by http://127.0.0.1:18082" in str(bogus_exc.value)
        assert _rerank_posts(log) == [], "no /v1/rerank call may precede a failed binding check"
        assert len(_models_gets(log)) == 1
        assert bogus._rerank_ready_verified is False

        # 18081 (embedding server) with the reranker model: absent from its /v1/models.
        log.clear()
        wrong_host = RealCogneeAdapter(_rerank_config(tmp_path / "emb", reranking_endpoint=QUALIFIED_EMBEDDING_ENDPOINT))
        with pytest.raises(RerankBindingError) as host_exc:
            await wrong_host.query(workspace_id=ws_id, repository_id=repo_id, snapshot_id=snap_id, query_text=RERANK_QUERY, limit=5)
        assert f"is not served by {QUALIFIED_EMBEDDING_ENDPOINT}" in str(host_exc.value)
        assert _rerank_posts(log) == []
        assert len(_models_gets(log)) == 1
        _assert_no_secret(str(host_exc.value), secrets, "R6 binding error")

        # Binding errors are never degraded: the optional policy is on for both adapters.
        assert bogus.config.reranking_required is False and wrong_host.config.reranking_required is False

        # The correctly bound adapter still applies on the same snapshot.
        log.clear()
        res = await _query(adapter, ws_id=ws_id, repo_id=repo_id, snap_id=snap_id, text=RERANK_QUERY, limit=5, secrets=secrets)
        _assert_applied_receipt(res, RERANK_QUERY, log)
    except AssertionError:
        raise
    except Exception as exc:  # noqa: BLE001 - surfaced redacted
        _fail_redacted("R6 failed", exc, secrets)
    finally:
        await _retire_quietly(adapter, ws_id, repo_id, snap_id)


# ---------------------------------------------------------------------------
# Server-level helpers (sync, TestClient)
# ---------------------------------------------------------------------------


def _ingest_and_publish(client: TestClient, headers: dict[str, str], payload: dict[str, Any], ws_id: UUID, repo_id: UUID, snap_id: str) -> None:
    res = client.post("/v1/ingest", headers=headers, json=payload)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["state"] == JobState.COMPLETED.value
    assert "semantic_index_indexed" in body["diagnostics"]
    pub = client.post(
        "/v1/publish",
        headers=headers,
        json={"workspace_id": str(ws_id), "repository_id": str(repo_id), "snapshot_id": snap_id},
    )
    assert pub.status_code == 200, pub.text
    assert pub.json()["published"] is True


def _http_query(client: TestClient, headers: dict[str, str], ws_id: UUID, repo_id: UUID, snap_id: str, text: str, limit: int) -> Any:
    return client.get(
        "/v1/query",
        headers=headers,
        params={"workspace_id": str(ws_id), "repository_id": str(repo_id), "snapshot_id": snap_id, "query": text, "limit": limit},
    )


# ---------------------------------------------------------------------------
# R4: required policy through /v1/query
# ---------------------------------------------------------------------------


def test_r4_required_outage_refuses_at_adapter_and_over_http(pg_cluster: KnowledgeConfig) -> None:
    _evict_loop_bound_engines()
    cfg = _rerank_config_for_cluster(
        pg_cluster,
        reranking_required=True,
        reranking_endpoint=UNREACHABLE_RERANK_ENDPOINT,
        reranking_timeout_seconds=OUTAGE_TIMEOUT_SECONDS,
    )
    secrets = _secrets_tuple(cfg)
    try:
        adapter = RealCogneeAdapter(cfg)
    except Exception as exc:  # noqa: BLE001 - surfaced redacted
        _fail_redacted("RealCogneeAdapter construction failed", exc, secrets)
        raise AssertionError("unreachable")

    ws_id, repo_id, headers = _server_setup(pg_cluster)
    snap_id = _canonical_snapshot_id("r4", ws_id, repo_id)
    payload, _ = _ingest_payload(op_id=uuid4(), ws_id=ws_id, repo_id=repo_id, snap_id=snap_id, fixture_name="A")

    app = create_app(cfg, engine=adapter)
    try:
        with TestClient(app) as client:
            try:
                # Ingest and publish never touch the reranker.
                _ingest_and_publish(client, headers, payload, ws_id, repo_id, snap_id)

                # Adapter level, on the request loop.
                with pytest.raises(EngineUnavailableError) as exc_info:
                    _portal_call(
                        client, adapter.query, workspace_id=ws_id, repository_id=repo_id, snapshot_id=snap_id, query_text=RERANK_QUERY, limit=5
                    )
                message = str(exc_info.value)
                assert message.startswith("engine_unavailable:")
                assert "backend unreachable during query (rerank" in message
                assert not isinstance(exc_info.value, (RerankBindingError, RerankContractError))
                _assert_no_secret(message, secrets, "R4 adapter error")

                # HTTP level: the existing handler maps it to 503 engine_unavailable.
                res = _http_query(client, headers, ws_id, repo_id, snap_id, RERANK_QUERY, 5)
                assert res.status_code == 503, res.text
                err = res.json()["error"]
                assert err["code"] == "engine_unavailable"
                assert err["message"].startswith("engine_unavailable:")
                assert "backend unreachable during query (rerank" in err["message"]
                _assert_no_secret(res.text, secrets, "R4 query response")

                # '*' bypasses ranking and still answers under the required policy.
                star = _http_query(client, headers, ws_id, repo_id, snap_id, "*", 100)
                assert star.status_code == 200, star.text
                assert star.json()["retrieval"]["mode"] == "exact"
                assert star.json()["retrieval"]["rerank"] is None
            finally:
                try:
                    _portal_call(client, adapter.retire, workspace_id=ws_id, repository_id=repo_id, snapshot_id=snap_id)
                except Exception:
                    pass
    finally:
        _evict_loop_bound_engines()


# ---------------------------------------------------------------------------
# R7: deterministic replay through /v1/query, restart, /v1/status
# ---------------------------------------------------------------------------


def test_r7_http_replay_is_deterministic_across_adapter_restart(pg_cluster: KnowledgeConfig) -> None:
    _evict_loop_bound_engines()
    cfg = _rerank_config_for_cluster(pg_cluster)
    secrets = _secrets_tuple(cfg)
    try:
        adapter = RealCogneeAdapter(cfg)
    except Exception as exc:  # noqa: BLE001 - surfaced redacted
        _fail_redacted("RealCogneeAdapter construction failed", exc, secrets)
        raise AssertionError("unreachable")

    ws_id, repo_id, headers = _server_setup(pg_cluster)
    snap_id = _canonical_snapshot_id("r7", ws_id, repo_id)
    payload, _ = _ingest_payload(op_id=uuid4(), ws_id=ws_id, repo_id=repo_id, snap_id=snap_id, fixture_name="A")

    def query_body(client: TestClient) -> dict[str, Any]:
        res = _http_query(client, headers, ws_id, repo_id, snap_id, RERANK_QUERY, 5)
        assert res.status_code == 200, res.text
        _assert_no_secret(res.text, secrets, "R7 query response")
        body = res.json()
        assert body["retrieval"]["mode"] == "semantic"
        rr = body["retrieval"]["rerank"]
        assert rr["status"] == "applied", rr
        assert rr["pair_format"] == "rerank_v1/semantic_text"
        assert [fact["node_id"] for fact in body["facts"]] == rr["post_rerank_order"]
        assert set(rr["pre_rerank_order"]) == set(rr["post_rerank_order"])
        assert len(rr["scores"]) == len(body["facts"]) >= 2
        assert body["total_matched"] == len(body["facts"])
        return body

    def same(a: dict[str, Any], b: dict[str, Any], context: str) -> None:
        assert [f["fact_id"] for f in a["facts"]] == [f["fact_id"] for f in b["facts"]], context
        assert a["retrieval"]["rerank"]["post_rerank_order"] == b["retrieval"]["rerank"]["post_rerank_order"], context
        assert a["retrieval"]["rerank"]["pre_rerank_order"] == b["retrieval"]["rerank"]["pre_rerank_order"], context
        assert a["retrieval"]["rerank"]["request_sha256"] == b["retrieval"]["rerank"]["request_sha256"], context
        for x, y in zip(a["retrieval"]["rerank"]["scores"], b["retrieval"]["rerank"]["scores"]):
            assert abs(x - y) <= 1e-6, context

    app = create_app(cfg, engine=adapter)
    try:
        with TestClient(app) as client:
            try:
                _ingest_and_publish(client, headers, payload, ws_id, repo_id, snap_id)

                first = query_body(client)
                second = query_body(client)
                same(first, second, "same adapter replay")

                # Restart: a fresh adapter on the same config and loop.
                fresh = RealCogneeAdapter(cfg)
                app.state.engine = fresh
                third = query_body(client)
                same(first, third, "fresh adapter replay")

                status = client.get("/v1/status", headers=headers)
                assert status.status_code == 200, status.text
                details = status.json()["engine"]["details"]
                assert details["reranking_provider"] == "rerank_v1"
                assert details["reranking_model"] == cfg.reranking_model
                assert details["reranking_endpoint"] == QUALIFIED_RERANK_ENDPOINT
                assert details["reranking"] == {
                    "provider": "rerank_v1",
                    "model": cfg.reranking_model,
                    "endpoint": QUALIFIED_RERANK_ENDPOINT,
                    "required": False,
                    "timeout_seconds": cfg.reranking_timeout_seconds,
                }
                assert "@" not in details["reranking_endpoint"]
                assert status.json()["engine"]["active_route"]["model_id"] == cfg.embedding_model
                _assert_no_secret(status.text, secrets, "R7 status response")
            finally:
                try:
                    _portal_call(client, app.state.engine.retire, workspace_id=ws_id, repository_id=repo_id, snapshot_id=snap_id)
                except Exception:
                    pass
    finally:
        _evict_loop_bound_engines()


# ---------------------------------------------------------------------------
# R8: context bundles are unaffected by reranking (compiler is sole authority)
# ---------------------------------------------------------------------------


def test_r8_context_compile_identical_with_rerank_enabled_replayed_and_disabled(dual_pg_cluster: KnowledgeConfig) -> None:
    _evict_loop_bound_engines()
    cfg = _rerank_config_for_cluster(dual_pg_cluster)
    secrets = _secrets_tuple(cfg)
    try:
        adapter_rr = RealCogneeAdapter(cfg)
        adapter_plain = RealCogneeAdapter(_semantic_config_for_cluster(dual_pg_cluster))
    except Exception as exc:  # noqa: BLE001 - surfaced redacted
        _fail_redacted("RealCogneeAdapter construction failed", exc, secrets)
        raise AssertionError("unreachable")

    ws_id, repo_id, headers = _server_setup(dual_pg_cluster)
    snap_id = _canonical_snapshot_id("r8", ws_id, repo_id)
    payload, _ = _ingest_payload(op_id=uuid4(), ws_id=ws_id, repo_id=repo_id, snap_id=snap_id, fixture_name="A")

    work_id, rev_id, cand_id = uuid4(), uuid4(), uuid4()
    with psycopg.connect(
        host=dual_pg_cluster.native_pg_host,
        port=dual_pg_cluster.native_pg_port,
        dbname=dual_pg_cluster.native_pg_database,
        user="postgres",
        autocommit=True,
    ) as native_conn:
        insert_test_receipt(native_conn, workspace_id=ws_id, work_id=work_id, revision_id=rev_id, candidate_id=cand_id, verdict="PASS")

    compile_body = {
        "workspace_id": str(ws_id),
        "repository_id": str(repo_id),
        "work_id": str(work_id),
        "revision_id": str(rev_id),
        "candidate_id": str(cand_id),
        "snapshot_id": snap_id,
        "stage": "planning",
        "budget": {"method": "utf8_bytes", "limit": 200000},
    }

    def compile_bundle(client: TestClient, context: str) -> dict[str, Any]:
        res = client.post("/v1/context/compile", headers=headers, json=compile_body)
        assert res.status_code == 200, f"{context}: {res.text}"
        _assert_no_secret(res.text, secrets, f"R8 compile ({context})")
        bundle = res.json()
        assert bundle["enrichment_status"] == "applied", context
        assert "fact:0" in bundle["optional"], context
        assert bundle["budget"]["method"] == "utf8_bytes"
        return bundle

    app = create_app(cfg, engine=adapter_rr)
    try:
        with TestClient(app) as client:
            try:
                _ingest_and_publish(client, headers, payload, ws_id, repo_id, snap_id)

                # The rerank adapter really reranks the compiler's query (stage text, limit 10).
                engine_view = _portal_call(
                    client, adapter_rr.query, workspace_id=ws_id, repository_id=repo_id, snapshot_id=snap_id, query_text="planning", limit=10
                )
                _assert_applied_receipt(engine_view, "planning")

                first = compile_bundle(client, "rerank enabled")
                replay = compile_bundle(client, "rerank enabled, replay")
                assert replay["bundle_id"] == first["bundle_id"]
                assert replay["bundle_sha256"] == first["bundle_sha256"]

                app.state.engine = adapter_plain
                disabled = compile_bundle(client, "rerank disabled")
                assert disabled["bundle_sha256"] == first["bundle_sha256"]
                assert disabled["bundle_id"] == first["bundle_id"]
                assert disabled["optional"] == first["optional"]
                assert disabled["budget"] == first["budget"]
                assert disabled["mandatory"] == first["mandatory"]
                # The facts inside the bundle are in fact_id order regardless of the engine order.
                fact_keys = [key for key in first["optional"] if key.startswith("fact:")]
                fact_ids = [first["optional"][key]["fact_id"] for key in fact_keys]
                assert fact_ids == sorted(fact_ids)
                assert len(fact_ids) >= 2

                stored = client.get(f"/v1/context/bundles/{first['bundle_id']}", headers=headers)
                assert stored.status_code == 200, stored.text
                assert stored.json()["bundle_sha256"] == first["bundle_sha256"]
            finally:
                for engine in (adapter_rr, adapter_plain):
                    try:
                        _portal_call(client, engine.retire, workspace_id=ws_id, repository_id=repo_id, snapshot_id=snap_id)
                        break
                    except Exception:
                        continue
    finally:
        _evict_loop_bound_engines()

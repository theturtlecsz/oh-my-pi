"""Unit tests for the optional post-hydration reranking stage (``rerank_v1``).

Seam-double tests only: graph rows come from ``FakeGraphEngine`` (tuple shape of
the embedded engine), vector hits from ``FakeVectorEngine`` and the reranker
endpoint from ``RecordingRerankTransport`` over ``httpx.MockTransport`` (see
``tests/support/fake_cognee.py``). Nothing here is evidence of reranking
capability; the live evals are in ``tests/test_rerank_integration.py``.

Covered contracts:

* config: disabled by default; ``rerank_v1`` requires the semantic route, a
  model and a credential-free http(s) endpoint; env parsing;
* route / status: reranking fields on ``BackendRoute``; graph-only and embedded
  routes report ``reranking`` False and carry no reranking details;
* readiness: the configured model must be listed by ``/v1/models`` before any
  ``/v1/rerank`` call (``RerankBindingError`` otherwise); listing outages follow
  the policy;
* healthy path: exact request body (raw query + indexed ``semantic_text``
  documents), permutation by ``(-score, pre-index)``, aligned retrieval scores,
  recomputable ``request_sha256``, identity and ``total_matched`` unchanged,
  withdrawn rows never present, readiness cached per adapter;
* outage policy: transport failures, deadline expiry, 429 / 503 -> ``fallback``
  with the pre-rerank order when optional, ``EngineUnavailableError`` when
  required;
* contract: non-outage statuses and malformed bodies -> ``RerankContractError``,
  echoed model mismatch -> ``RerankBindingError``, programming errors propagate,
  all under both policies;
* ``*`` and single-candidate queries issue no HTTP request;
* exact_fallback candidates are still reranked when the reranker is healthy;
* the API key travels as a bearer header and never leaks into reasons or errors.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError

from omp_knowledge.config import KnowledgeConfig, load_config
from omp_knowledge.engine import cognee_adapter
from omp_knowledge.engine.backends import (
    RERANK_PAIR_FORMAT,
    BackendConfigError,
    SnapshotScope,
    rerank_documents,
    rerank_request_sha256,
    resolve_backend_route,
    semantic_text,
)
from omp_knowledge.engine.cognee_adapter import RealCogneeAdapter
from omp_knowledge.engine.protocol import (
    EngineUnavailableError,
    RerankBindingError,
    RerankContractError,
)
from support.fake_cognee import (
    FAKE_RERANK_MODEL,
    FakeCogneeConfig,
    FakeVectorEngine,
    RecordingRerankTransport,
    install_fake_cognee,
    install_fake_reranker,
    install_fake_vector_engine,
    rerank_response,
    secret_file,
)
from test_semantic_mode import (
    EMBEDDING_MODEL,
    FakeGraphEngine,
    _graph_row,
    _make_semantic_config,
    _point,
    _scope,
    _wire_graph_seam,
)

RERANK_MODEL = FAKE_RERANK_MODEL
RERANK_ENDPOINT = "http://127.0.0.1:18082"
QUERY = "apple"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rerank_config(tmp_path: Path, *, semantic: dict[str, Any] | None = None, **rerank_overrides: Any) -> KnowledgeConfig:
    """The semantic unit config with ``rerank_v1`` enabled (overrides win)."""
    values = _make_semantic_config(tmp_path, **(semantic or {})).model_dump()
    values.update(
        {
            "reranking_provider": "rerank_v1",
            "reranking_model": RERANK_MODEL,
            "reranking_endpoint": RERANK_ENDPOINT,
            **rerank_overrides,
        }
    )
    return KnowledgeConfig(**values)


def _rerank_adapter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    fake_engine: FakeVectorEngine | None = None,
    transport: RecordingRerankTransport | None = None,
    semantic: dict[str, Any] | None = None,
    **rerank_overrides: Any,
) -> tuple[RealCogneeAdapter, FakeVectorEngine, RecordingRerankTransport]:
    engine = fake_engine if fake_engine is not None else FakeVectorEngine(vector_dimension=1024)
    install_fake_cognee(monkeypatch, FakeCogneeConfig())
    install_fake_vector_engine(monkeypatch, engine)
    recorder = install_fake_reranker(monkeypatch, transport if transport is not None else RecordingRerankTransport())
    assert isinstance(recorder, RecordingRerankTransport)
    adapter = RealCogneeAdapter(_rerank_config(tmp_path, semantic=semantic, **rerank_overrides), available=True)
    return adapter, engine, recorder


FACTS: tuple[tuple[str, str, str, dict[str, Any]], ...] = (
    ("fact-0", "apple_pie", "src/a.py", {"lang": "py", "arity": 1}),
    ("fact-1", "apple_juice", "src/b.py", {"lang": "py", "arity": 2}),
    ("fact-2", "apple_tart", "src/c.py", {"lang": "ts"}),
)


async def _seed(
    monkeypatch: pytest.MonkeyPatch,
    fake_engine: FakeVectorEngine,
    scope: SnapshotScope,
    *,
    count: int = 3,
    with_vectors: bool = True,
) -> tuple[list[UUID], UUID]:
    """``count`` valid facts plus one withdrawn fact in the graph; every one of
    them (including the withdrawn one, as a stale row) in the vector store.
    Pre-rerank semantic order is the vector insertion order (fact-0, fact-1, ...)."""
    ids = [scope.fact_node_id(fact_id) for fact_id, _, _, _ in FACTS[:count]]
    withdrawn = scope.fact_node_id("fact-withdrawn")
    rows = [
        _graph_row(
            node_id,
            name,
            {"enola_id": fact_id, "kind": "symbol", "file_path": file_path, "fact_properties": props},
        )
        for node_id, (fact_id, name, file_path, props) in zip(ids, FACTS[:count])
    ]
    rows.append(
        _graph_row(
            withdrawn,
            "apple_withdrawn",
            {"enola_id": "fact-withdrawn", "kind": "symbol", "status": "withdrawn", "fact_properties": {"withdrawn": True}},
        )
    )
    _wire_graph_seam(monkeypatch, FakeGraphEngine(rows=rows))
    if with_vectors:
        await fake_engine.fake_index_data_points([_point(node_id, scope) for node_id in ids] + [_point(withdrawn, scope)])
    return ids, withdrawn


def _expected_documents(count: int = 3) -> list[str]:
    return [semantic_text("symbol", name, file_path, props) for _, name, file_path, props in FACTS[:count]]


async def _query(adapter: RealCogneeAdapter, scope: SnapshotScope, text: str = QUERY, limit: int = 10) -> Any:
    return await adapter.query(
        workspace_id=scope.workspace_id,
        repository_id=scope.repository_id,
        snapshot_id=scope.snapshot_id,
        query_text=text,
        limit=limit,
    )


def _scores(*values: float) -> Callable[[dict[str, Any]], httpx.Response]:
    return lambda body: rerank_response(list(values))


def _status(code: int) -> Callable[[dict[str, Any]], httpx.Response]:
    return lambda body: httpx.Response(code, json={"error": {"code": code}})


# ---------------------------------------------------------------------------
# 1. Config
# ---------------------------------------------------------------------------


def test_default_config_has_reranking_disabled(tmp_path: Path) -> None:
    cfg = KnowledgeConfig(state_dir=tmp_path / "state", config_dir=tmp_path / "config")
    assert cfg.reranking_provider == "none"
    assert cfg.reranking_model is None
    assert cfg.reranking_endpoint is None
    assert cfg.reranking_api_key_file is None
    assert cfg.reranking_required is False
    assert cfg.reranking_timeout_seconds == 15.0
    # The semantic config without the switch is unchanged as well.
    semantic = _make_semantic_config(tmp_path)
    assert semantic.reranking_provider == "none"
    assert resolve_backend_route(semantic).reranking is False


def test_rerank_config_valid(tmp_path: Path) -> None:
    cfg = _rerank_config(tmp_path)
    assert cfg.reranking_provider == "rerank_v1"
    assert cfg.reranking_model == RERANK_MODEL
    assert cfg.reranking_endpoint == RERANK_ENDPOINT
    assert cfg.reranking_required is False


def test_rerank_config_requires_semantic_route(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="reranking_provider requires graph_only=False"):
        KnowledgeConfig(
            state_dir=tmp_path / "state",
            config_dir=tmp_path / "config",
            graph_only=True,
            reranking_provider="rerank_v1",
            reranking_model=RERANK_MODEL,
            reranking_endpoint=RERANK_ENDPOINT,
        )


def test_rerank_config_rejects_unknown_provider(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _rerank_config(tmp_path, reranking_provider="cohere")


@pytest.mark.parametrize("model", ["", None])
def test_rerank_config_requires_model(tmp_path: Path, model: str | None) -> None:
    with pytest.raises(ValueError, match="requires a non-empty reranking_model"):
        _rerank_config(tmp_path, reranking_model=model)


@pytest.mark.parametrize("endpoint", ["", None])
def test_rerank_config_requires_endpoint(tmp_path: Path, endpoint: str | None) -> None:
    with pytest.raises(ValueError, match="requires a non-empty reranking_endpoint"):
        _rerank_config(tmp_path, reranking_endpoint=endpoint)


def test_rerank_config_rejects_userinfo_with_sanitized_message(tmp_path: Path) -> None:
    password = "hunter2-9c1d"
    with pytest.raises(ValidationError) as exc_info:
        _rerank_config(tmp_path, reranking_endpoint=f"http://rr-user:{password}@127.0.0.1:18082")
    # The validator's own message names the setting and the sanitized endpoint only
    # (pydantic's rendering of the raw input is not part of this contract).
    messages = [error["msg"] for error in exc_info.value.errors()]
    assert any("reranking_endpoint must not embed userinfo" in msg and "http://127.0.0.1:18082" in msg for msg in messages)
    assert all(password not in msg and "rr-user" not in msg for msg in messages)


def test_route_boundary_rejects_reranking_userinfo_when_validation_is_bypassed(tmp_path: Path) -> None:
    password = "bypass-pass-4e2f"
    data = _rerank_config(tmp_path).model_dump()
    data["reranking_endpoint"] = f"http://bypass-user:{password}@127.0.0.1:18082"
    bypassed = KnowledgeConfig.model_construct(**data)
    with pytest.raises(BackendConfigError) as exc_info:
        resolve_backend_route(bypassed)
    assert "reranking_endpoint must not embed userinfo" in str(exc_info.value)
    assert password not in str(exc_info.value)

    graph_only = KnowledgeConfig(state_dir=tmp_path / "g", config_dir=tmp_path / "gc").model_dump()
    graph_only.update({"reranking_provider": "rerank_v1", "reranking_model": RERANK_MODEL, "reranking_endpoint": RERANK_ENDPOINT})
    with pytest.raises(BackendConfigError, match="reranking_provider requires the semantic route"):
        resolve_backend_route(KnowledgeConfig.model_construct(**graph_only))


@pytest.mark.parametrize("endpoint", ["bolt://127.0.0.1:18082", "ftp://127.0.0.1:18082", "127.0.0.1:18082"])
def test_rerank_config_rejects_non_http_scheme(tmp_path: Path, endpoint: str) -> None:
    with pytest.raises(ValueError, match="reranking_endpoint must use one of"):
        _rerank_config(tmp_path, reranking_endpoint=endpoint)


def test_rerank_config_rejects_missing_host(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="reranking_endpoint must name a host"):
        _rerank_config(tmp_path, reranking_endpoint="http:///v1")


def test_load_config_parses_reranking_env_vars(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pw = secret_file(tmp_path, "pg.secret", "secret-pw")
    key_file = secret_file(tmp_path, "rerank.key", "rerank-key")
    monkeypatch.setenv("OMP_KNOWLEDGE_GRAPH_ONLY", "0")
    monkeypatch.setenv("OMP_KNOWLEDGE_VECTOR_STORE", "pgvector")
    monkeypatch.setenv("OMP_KNOWLEDGE_COGNEE_PG_PASSWORD_FILE", str(pw))
    monkeypatch.setenv("OMP_KNOWLEDGE_EMBEDDING_PROVIDER", "openai_compatible")
    monkeypatch.setenv("OMP_KNOWLEDGE_EMBEDDING_MODEL", EMBEDDING_MODEL)
    monkeypatch.setenv("OMP_KNOWLEDGE_EMBEDDING_ENDPOINT", "http://127.0.0.1:18081")
    monkeypatch.setenv("OMP_KNOWLEDGE_RERANKING_PROVIDER", "rerank_v1")
    monkeypatch.setenv("OMP_KNOWLEDGE_RERANKING_MODEL", "custom-reranker")
    monkeypatch.setenv("OMP_KNOWLEDGE_RERANKING_ENDPOINT", "http://127.0.0.1:28082")
    monkeypatch.setenv("OMP_KNOWLEDGE_RERANKING_API_KEY_FILE", str(key_file))
    monkeypatch.setenv("OMP_KNOWLEDGE_RERANKING_REQUIRED", "1")
    monkeypatch.setenv("OMP_KNOWLEDGE_RERANKING_TIMEOUT_SECONDS", "7.5")

    cfg = load_config()
    assert cfg.reranking_provider == "rerank_v1"
    assert cfg.reranking_model == "custom-reranker"
    assert cfg.reranking_endpoint == "http://127.0.0.1:28082"
    assert cfg.reranking_api_key_file == key_file
    assert cfg.reranking_required is True
    assert cfg.reranking_timeout_seconds == 7.5

    # Without the variables the loader keeps the disabled default.
    for name in (
        "OMP_KNOWLEDGE_RERANKING_PROVIDER",
        "OMP_KNOWLEDGE_RERANKING_MODEL",
        "OMP_KNOWLEDGE_RERANKING_ENDPOINT",
        "OMP_KNOWLEDGE_RERANKING_API_KEY_FILE",
        "OMP_KNOWLEDGE_RERANKING_REQUIRED",
        "OMP_KNOWLEDGE_RERANKING_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name)
    plain = load_config()
    assert plain.reranking_provider == "none"
    assert plain.reranking_required is False
    assert plain.reranking_timeout_seconds == 15.0


# ---------------------------------------------------------------------------
# 2. Route and status
# ---------------------------------------------------------------------------


def test_route_carries_reranking_fields(tmp_path: Path) -> None:
    route = resolve_backend_route(_rerank_config(tmp_path))
    assert route.reranking is True
    assert route.reranking_provider == "rerank_v1"
    assert route.reranking_model == RERANK_MODEL
    assert route.reranking_endpoint == RERANK_ENDPOINT
    as_dict = route.as_dict()
    assert as_dict["reranking"] is True
    assert as_dict["reranking_provider"] == "rerank_v1"
    assert as_dict["reranking_model"] == RERANK_MODEL
    assert as_dict["reranking_endpoint"] == RERANK_ENDPOINT
    # The embedding binding is untouched by the switch.
    assert as_dict["embedding_model"] == EMBEDDING_MODEL
    assert as_dict["binding_tag"] == f"omp-emb:{EMBEDDING_MODEL}@1024"


@pytest.mark.asyncio
async def test_status_details_reranking_only_when_enabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    adapter, _, _ = _rerank_adapter(monkeypatch, tmp_path, reranking_required=True, reranking_timeout_seconds=3.5)
    status = await adapter.status()
    assert status.details["reranking"] == {
        "provider": "rerank_v1",
        "model": RERANK_MODEL,
        "endpoint": RERANK_ENDPOINT,
        "required": True,
        "timeout_seconds": 3.5,
    }
    assert status.details["reranking_provider"] == "rerank_v1"
    # ProviderRoute keeps naming the embedding model; the reranker is a stage, not a route.
    assert status.active_route.model_id == EMBEDDING_MODEL
    assert status.active_route.graph_only is False

    # Semantic route without the switch: bool False, no details mapping.
    install_fake_cognee(monkeypatch, FakeCogneeConfig())
    plain = RealCogneeAdapter(_make_semantic_config(tmp_path / "plain"), available=True)
    plain_status = await plain.status()
    assert plain_status.details["reranking"] is False
    assert plain_status.details["reranking_provider"] == "none"
    assert plain_status.details["reranking_model"] is None
    assert plain_status.details["reranking_endpoint"] is None


@pytest.mark.asyncio
async def test_graph_only_and_embedded_routes_report_reranking_false(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    embedded = RealCogneeAdapter(KnowledgeConfig(state_dir=tmp_path / "e", config_dir=tmp_path / "ec"), available=False)
    embedded_status = await embedded.status()
    assert embedded_status.details["reranking"] is False
    assert embedded_status.details["reranking_provider"] == "none"
    assert embedded_status.details["vector_indexing"] is False
    assert embedded_status.active_route.graph_only is True
    assert "embedding" not in embedded_status.details

    install_fake_cognee(monkeypatch, FakeCogneeConfig())
    graph_only = RealCogneeAdapter(
        KnowledgeConfig(
            state_dir=tmp_path / "g",
            config_dir=tmp_path / "gc",
            graph_engine="neo4j",
            neo4j_password_file=secret_file(tmp_path, "neo.pw", "neo-pw"),
        ),
        available=True,
    )
    graph_status = await graph_only.status()
    assert graph_status.details["reranking"] is False
    assert graph_status.details["reranking_model"] is None
    assert graph_status.active_route.graph_only is True
    assert "embedding" not in graph_status.details


# ---------------------------------------------------------------------------
# 3. Readiness (/v1/models binding)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("required", [False, True])
async def test_model_not_listed_raises_binding_error_before_any_rerank(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, required: bool
) -> None:
    transport = RecordingRerankTransport(models=("some-other-model",))
    adapter, engine, recorder = _rerank_adapter(monkeypatch, tmp_path, transport=transport, reranking_required=required)
    scope = _scope(uuid4(), uuid4())
    await _seed(monkeypatch, engine, scope)

    with pytest.raises(RerankBindingError) as exc_info:
        await _query(adapter, scope)
    assert "is not served by http://127.0.0.1:18082" in str(exc_info.value)
    assert "some-other-model" in str(exc_info.value)
    assert isinstance(exc_info.value, EngineUnavailableError)
    assert recorder.rerank_requests == []
    assert len(recorder.models_requests) == 1
    assert adapter._rerank_ready_verified is False


@pytest.mark.asyncio
async def test_models_listing_outage_optional_falls_back(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    transport = RecordingRerankTransport(models_responder=httpx.ConnectError("connection refused"))
    adapter, engine, recorder = _rerank_adapter(monkeypatch, tmp_path, transport=transport, reranking_required=False)
    scope = _scope(uuid4(), uuid4())
    ids, _ = await _seed(monkeypatch, engine, scope)

    res = await _query(adapter, scope)
    rr = res.retrieval.rerank
    assert rr is not None and rr.status == "fallback"
    assert "backend unreachable during query (rerank readiness) (GET /v1/models)" in (rr.reason or "")
    assert [fact.node_id for fact in res.facts] == ids
    assert rr.pre_rerank_order == rr.post_rerank_order == tuple(str(i) for i in ids)
    assert recorder.rerank_requests == []
    assert adapter._rerank_ready_verified is False


@pytest.mark.asyncio
async def test_models_listing_outage_required_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    transport = RecordingRerankTransport(models_responder=httpx.ConnectError("connection refused"))
    adapter, engine, recorder = _rerank_adapter(monkeypatch, tmp_path, transport=transport, reranking_required=True)
    scope = _scope(uuid4(), uuid4())
    await _seed(monkeypatch, engine, scope)

    with pytest.raises(EngineUnavailableError, match=r"backend unreachable during query \(rerank readiness\)") as exc_info:
        await _query(adapter, scope)
    assert not isinstance(exc_info.value, RerankBindingError)
    assert recorder.rerank_requests == []


@pytest.mark.asyncio
async def test_models_listing_non_json_is_a_contract_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    transport = RecordingRerankTransport(models_responder=lambda request: httpx.Response(200, content=b"<html>"))
    adapter, engine, recorder = _rerank_adapter(monkeypatch, tmp_path, transport=transport, reranking_required=False)
    scope = _scope(uuid4(), uuid4())
    await _seed(monkeypatch, engine, scope)

    with pytest.raises(RerankContractError, match="non-JSON body"):
        await _query(adapter, scope)
    assert recorder.rerank_requests == []


# ---------------------------------------------------------------------------
# 4. Healthy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_healthy_rerank_permutes_reports_and_preserves_identity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    transport = RecordingRerankTransport(rerank=_scores(0.2, 0.9, 0.5))
    adapter, engine, recorder = _rerank_adapter(monkeypatch, tmp_path, transport=transport)
    scope = _scope(uuid4(), uuid4())
    ids, withdrawn = await _seed(monkeypatch, engine, scope)

    res = await _query(adapter, scope)

    # Request: exactly model / raw query / indexed semantic_text documents, no top_n.
    [post] = recorder.rerank_requests
    assert post["method"] == "POST"
    assert post["url"] == f"{RERANK_ENDPOINT}/v1/rerank"
    assert post["json"] == {"model": RERANK_MODEL, "query": QUERY, "documents": _expected_documents()}
    assert post["authorization"] is None
    [models] = recorder.models_requests
    assert models["url"] == f"{RERANK_ENDPOINT}/v1/models"

    # Permutation by (-score, pre-index): 0.9 (fact-1), 0.5 (fact-2), 0.2 (fact-0).
    assert [fact.fact_id for fact in res.facts] == ["fact-1", "fact-2", "fact-0"]
    assert res.total_matched == 3
    assert res.retrieval is not None and res.retrieval.mode == "semantic"
    # Cosine scores stay aligned with the facts they belong to.
    assert res.retrieval.scores == pytest.approx((0.2, 0.3, 0.1))

    rr = res.retrieval.rerank
    assert rr is not None
    assert rr.status == "applied"
    assert rr.provider == "rerank_v1"
    assert rr.model_id == RERANK_MODEL
    assert rr.endpoint == RERANK_ENDPOINT
    assert rr.pair_format == RERANK_PAIR_FORMAT == "rerank_v1/semantic_text"
    assert rr.document_count == 3
    assert rr.reason is None
    assert rr.pre_rerank_order == tuple(str(i) for i in ids)
    assert rr.post_rerank_order == (str(ids[1]), str(ids[2]), str(ids[0]))
    assert rr.scores == (0.9, 0.5, 0.2)
    assert set(rr.post_rerank_order) == set(rr.pre_rerank_order) == {str(fact.node_id) for fact in res.facts}
    assert len(rr.post_rerank_order) == len(rr.pre_rerank_order) == len(res.facts)

    # request_sha256 is recomputable from the returned facts in pre-rerank order.
    by_node = {str(fact.node_id): fact for fact in res.facts}
    pre_facts = [by_node[node_id] for node_id in rr.pre_rerank_order]
    documents = rerank_documents(QUERY, pre_facts)
    assert list(documents) == post["json"]["documents"]
    assert rr.request_sha256 == rerank_request_sha256(QUERY, documents)

    # The withdrawn row (stale vector hit) is neither a document nor an order entry.
    assert str(withdrawn) not in rr.pre_rerank_order and str(withdrawn) not in rr.post_rerank_order
    assert all("apple_withdrawn" not in document for document in post["json"]["documents"])


@pytest.mark.asyncio
async def test_rerank_tie_break_keeps_pre_rerank_index_order(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    transport = RecordingRerankTransport(rerank=_scores(0.5, 0.7, 0.7))
    adapter, engine, _ = _rerank_adapter(monkeypatch, tmp_path, transport=transport)
    scope = _scope(uuid4(), uuid4())
    await _seed(monkeypatch, engine, scope)

    res = await _query(adapter, scope)
    assert [fact.fact_id for fact in res.facts] == ["fact-1", "fact-2", "fact-0"]
    assert res.retrieval.rerank.scores == (0.7, 0.7, 0.5)

    transport.rerank = _scores(0.4, 0.4, 0.4)
    res_equal = await _query(adapter, scope)
    assert [fact.fact_id for fact in res_equal.facts] == ["fact-0", "fact-1", "fact-2"]
    assert res_equal.retrieval.rerank.status == "applied"


@pytest.mark.asyncio
async def test_readiness_is_cached_per_adapter_and_never_across_failures(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    transport = RecordingRerankTransport(rerank=_scores(0.1, 0.2, 0.3))
    adapter, engine, recorder = _rerank_adapter(monkeypatch, tmp_path, transport=transport)
    scope = _scope(uuid4(), uuid4())
    await _seed(monkeypatch, engine, scope)

    await _query(adapter, scope)
    await _query(adapter, scope)
    assert len(recorder.models_requests) == 1
    assert len(recorder.rerank_requests) == 2
    assert adapter._rerank_ready_verified is True

    # A fresh adapter re-verifies the binding.
    second = RealCogneeAdapter(_rerank_config(tmp_path / "second"), available=True)
    await _query(second, scope)
    assert len(recorder.models_requests) == 2


@pytest.mark.asyncio
async def test_rerank_request_sha256_is_canonical_and_pair_sensitive() -> None:
    documents = ("symbol a in x.py; k=1", "symbol b in y.py; k=2")
    first = rerank_request_sha256("q", documents)
    assert first == rerank_request_sha256("q", list(documents))
    assert len(first) == 64
    assert first != rerank_request_sha256("q", tuple(reversed(documents)))
    assert first != rerank_request_sha256("Q", documents)


# ---------------------------------------------------------------------------
# 5. Outage policy
# ---------------------------------------------------------------------------


async def _slow_responder(body: dict[str, Any]) -> httpx.Response:
    await asyncio.sleep(2.0)
    return rerank_response([0.1] * len(body.get("documents", [])))


OUTAGES: list[tuple[str, Any, dict[str, Any]]] = [
    ("connect_error", httpx.ConnectError("connection refused"), {}),
    ("read_timeout", httpx.ReadTimeout("read timed out"), {}),
    ("deadline", _slow_responder, {"reranking_timeout_seconds": 0.05}),
    ("http_429", _status(429), {}),
    ("http_502", _status(502), {}),
    ("http_503", _status(503), {}),
    ("http_504", _status(504), {}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("case", "responder", "overrides"), OUTAGES, ids=[c[0] for c in OUTAGES])
async def test_rerank_outage_optional_falls_back_with_pre_rerank_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str, responder: Any, overrides: dict[str, Any]
) -> None:
    transport = RecordingRerankTransport(rerank=responder)
    adapter, engine, recorder = _rerank_adapter(monkeypatch, tmp_path, transport=transport, reranking_required=False, **overrides)
    scope = _scope(uuid4(), uuid4())
    ids, _ = await _seed(monkeypatch, engine, scope)

    res = await _query(adapter, scope)
    assert res.retrieval is not None and res.retrieval.mode == "semantic"
    assert [fact.node_id for fact in res.facts] == ids
    assert res.total_matched == 3
    assert res.retrieval.scores == pytest.approx((0.1, 0.2, 0.3))
    rr = res.retrieval.rerank
    assert rr is not None
    assert rr.status == "fallback"
    assert rr.reason is not None
    assert "backend unreachable during query (rerank" in rr.reason
    if case == "deadline":
        assert "timed out after 0.05s" in rr.reason
    if case.startswith("http_"):
        assert f"HTTP {case.split('_')[1]}" in rr.reason
    assert rr.pre_rerank_order == rr.post_rerank_order == tuple(str(i) for i in ids)
    assert rr.scores == ()
    assert rr.document_count == 3
    assert rr.request_sha256 == rerank_request_sha256(QUERY, _expected_documents())
    assert len(recorder.rerank_requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("case", "responder", "overrides"), OUTAGES, ids=[c[0] for c in OUTAGES])
async def test_rerank_outage_required_raises_engine_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str, responder: Any, overrides: dict[str, Any]
) -> None:
    transport = RecordingRerankTransport(rerank=responder)
    adapter, engine, _ = _rerank_adapter(monkeypatch, tmp_path, transport=transport, reranking_required=True, **overrides)
    scope = _scope(uuid4(), uuid4())
    await _seed(monkeypatch, engine, scope)

    with pytest.raises(EngineUnavailableError, match=r"backend unreachable during query \(rerank POST /v1/rerank\)") as exc_info:
        await _query(adapter, scope)
    assert not isinstance(exc_info.value, (RerankBindingError, RerankContractError))
    assert str(exc_info.value).startswith("engine_unavailable:")


@pytest.mark.asyncio
async def test_rerank_fallback_is_identical_to_rerank_disabled_adapter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    transport = RecordingRerankTransport(rerank=httpx.ConnectError("connection refused"))
    adapter, engine, _ = _rerank_adapter(monkeypatch, tmp_path, transport=transport, reranking_required=False)
    scope = _scope(uuid4(), uuid4())
    await _seed(monkeypatch, engine, scope)
    plain = RealCogneeAdapter(_make_semantic_config(tmp_path / "plain"), available=True)

    degraded = await _query(adapter, scope)
    baseline = await _query(plain, scope)
    assert degraded.facts == baseline.facts
    assert degraded.total_matched == baseline.total_matched
    assert degraded.retrieval.scores == baseline.retrieval.scores
    assert degraded.retrieval.mode == baseline.retrieval.mode == "semantic"
    assert baseline.retrieval.rerank is None
    assert degraded.retrieval.rerank.status == "fallback"


# ---------------------------------------------------------------------------
# 6. Contract errors are never degraded
# ---------------------------------------------------------------------------


def _raw_json(text: str) -> Callable[[dict[str, Any]], httpx.Response]:
    return lambda body: httpx.Response(200, content=text.encode("utf-8"), headers={"content-type": "application/json"})


CONTRACT_CASES: list[tuple[str, Callable[[dict[str, Any]], httpx.Response], str]] = [
    ("http_400", _status(400), "returned HTTP 400"),
    ("http_401", _status(401), "returned HTTP 401"),
    ("http_404", _status(404), "returned HTTP 404"),
    ("http_500", _status(500), "returned HTTP 500"),
    ("http_501", _status(501), "returned HTTP 501"),
    ("non_json_body", lambda body: httpx.Response(200, content=b"<html>not json</html>"), "non-JSON body"),
    ("json_array_body", _raw_json("[1, 2, 3]"), "not a JSON object"),
    ("missing_results", lambda body: httpx.Response(200, json={"object": "list"}), "no 'results' list"),
    ("results_not_list", lambda body: httpx.Response(200, json={"results": {"index": 0}}), "no 'results' list"),
    ("index_out_of_range", lambda body: rerank_response([0.1, 0.2, 0.3], indices=[0, 1, 7]), "outside 0..2"),
    ("negative_index", lambda body: rerank_response([0.1, 0.2, 0.3], indices=[0, 1, -1]), "outside 0..2"),
    ("duplicate_index", lambda body: rerank_response([0.1, 0.2, 0.3], indices=[0, 1, 1]), "repeats index 1"),
    ("missing_index", _raw_json('{"results":[{"relevance_score":0.1},{"index":1,"relevance_score":0.2},{"index":2,"relevance_score":0.3}]}'), "no integer 'index'"),
    ("bool_index", lambda body: rerank_response([0.1, 0.2, 0.3], indices=[0, 1, True]), "no integer 'index'"),
    ("short_results", lambda body: rerank_response([0.1, 0.2]), "returned 2 result(s) for 3 document(s)"),
    ("long_results", lambda body: rerank_response([0.1, 0.2, 0.3, 0.4]), "returned 4 result(s) for 3 document(s)"),
    ("string_score", lambda body: rerank_response(["high", 0.2, 0.3]), "no numeric 'relevance_score'"),
    ("missing_score", _raw_json('{"results":[{"index":0},{"index":1,"relevance_score":0.2},{"index":2,"relevance_score":0.3}]}'), "no numeric 'relevance_score'"),
    ("nan_score", _raw_json('{"results":[{"index":0,"relevance_score":NaN},{"index":1,"relevance_score":0.2},{"index":2,"relevance_score":0.3}]}'), "non-finite relevance_score"),
    ("infinite_score", _raw_json('{"results":[{"index":0,"relevance_score":Infinity},{"index":1,"relevance_score":0.2},{"index":2,"relevance_score":0.3}]}'), "non-finite relevance_score"),
    ("bool_score", lambda body: rerank_response([True, 0.2, 0.3]), "no numeric 'relevance_score'"),
    ("result_not_object", _raw_json('{"results":[0.1, 0.2, 0.3]}'), "not a JSON object"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("required", [False, True])
@pytest.mark.parametrize(("case", "responder", "fragment"), CONTRACT_CASES, ids=[c[0] for c in CONTRACT_CASES])
async def test_malformed_or_unsupported_response_raises_contract_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str, responder: Any, fragment: str, required: bool
) -> None:
    transport = RecordingRerankTransport(rerank=responder)
    adapter, engine, recorder = _rerank_adapter(monkeypatch, tmp_path, transport=transport, reranking_required=required)
    scope = _scope(uuid4(), uuid4())
    await _seed(monkeypatch, engine, scope)

    with pytest.raises(RerankContractError) as exc_info:
        await _query(adapter, scope)
    assert fragment in str(exc_info.value)
    assert str(exc_info.value).startswith("engine_error:")
    assert not isinstance(exc_info.value, EngineUnavailableError)
    assert len(recorder.rerank_requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("required", [False, True])
async def test_echoed_model_mismatch_raises_binding_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, required: bool) -> None:
    transport = RecordingRerankTransport(rerank=lambda body: rerank_response([0.1, 0.2, 0.3], model="another-reranker"))
    adapter, engine, _ = _rerank_adapter(monkeypatch, tmp_path, transport=transport, reranking_required=required)
    scope = _scope(uuid4(), uuid4())
    await _seed(monkeypatch, engine, scope)

    with pytest.raises(RerankBindingError, match="echoed model 'another-reranker'"):
        await _query(adapter, scope)


@pytest.mark.asyncio
async def test_echoed_matching_model_is_accepted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    transport = RecordingRerankTransport(rerank=lambda body: rerank_response([0.3, 0.2, 0.1], model=RERANK_MODEL))
    adapter, engine, _ = _rerank_adapter(monkeypatch, tmp_path, transport=transport)
    scope = _scope(uuid4(), uuid4())
    await _seed(monkeypatch, engine, scope)

    res = await _query(adapter, scope)
    assert res.retrieval.rerank.status == "applied"
    assert [fact.fact_id for fact in res.facts] == ["fact-0", "fact-1", "fact-2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("required", [False, True])
async def test_programming_error_propagates_unchanged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, required: bool) -> None:
    adapter, engine, recorder = _rerank_adapter(monkeypatch, tmp_path, reranking_required=required)
    scope = _scope(uuid4(), uuid4())
    await _seed(monkeypatch, engine, scope)

    def broken(query_text: str, facts: Any) -> tuple[str, ...]:
        raise TypeError("pair formatter broke")

    monkeypatch.setattr(cognee_adapter, "rerank_documents", broken)
    with pytest.raises(TypeError, match="pair formatter broke"):
        await _query(adapter, scope)
    assert recorder.requests == []


# ---------------------------------------------------------------------------
# 7. Not attempted: '*' and single candidate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_star_query_bypasses_reranker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    adapter, engine, recorder = _rerank_adapter(monkeypatch, tmp_path, reranking_required=True)
    scope = _scope(uuid4(), uuid4())
    ids, _ = await _seed(monkeypatch, engine, scope)

    res = await _query(adapter, scope, text="*")
    assert [fact.node_id for fact in res.facts] == ids
    assert res.retrieval is not None and res.retrieval.mode == "exact"
    assert res.retrieval.rerank is None
    assert recorder.requests == []


@pytest.mark.asyncio
async def test_single_candidate_is_not_attempted_without_http(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    adapter, engine, recorder = _rerank_adapter(monkeypatch, tmp_path, reranking_required=True)
    scope = _scope(uuid4(), uuid4())
    ids, _ = await _seed(monkeypatch, engine, scope, count=1)

    res = await _query(adapter, scope)
    assert [fact.node_id for fact in res.facts] == ids
    rr = res.retrieval.rerank
    assert rr is not None
    assert rr.status == "not_attempted"
    assert rr.reason == "fewer than two candidates"
    assert rr.document_count == 1
    assert rr.request_sha256 is None
    assert rr.pre_rerank_order == rr.post_rerank_order == (str(ids[0]),)
    assert rr.scores == ()
    assert recorder.requests == []


@pytest.mark.asyncio
async def test_empty_candidates_is_not_attempted_without_http(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    adapter, engine, recorder = _rerank_adapter(monkeypatch, tmp_path, reranking_required=True)
    scope = _scope(uuid4(), uuid4())
    _wire_graph_seam(monkeypatch, FakeGraphEngine(rows=[]))

    res = await _query(adapter, scope, text="nothing")
    assert res.facts == ()
    assert res.retrieval.mode == "semantic_empty"
    assert res.retrieval.rerank.status == "not_attempted"
    assert res.retrieval.rerank.document_count == 0
    assert recorder.requests == []


# ---------------------------------------------------------------------------
# 8. exact_fallback candidates are reranked when the reranker is healthy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exact_fallback_candidates_are_reranked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    broken_embeddings = FakeVectorEngine(vector_dimension=1024, embed_error=TimeoutError("inference timeout"))
    transport = RecordingRerankTransport(rerank=_scores(0.2, 0.9, 0.5))
    adapter, engine, recorder = _rerank_adapter(
        monkeypatch, tmp_path, fake_engine=broken_embeddings, transport=transport, semantic={"embedding_required": False}
    )
    scope = _scope(uuid4(), uuid4())
    ids, _ = await _seed(monkeypatch, engine, scope, with_vectors=False)

    res = await _query(adapter, scope)
    assert res.retrieval is not None
    assert res.retrieval.mode == "exact_fallback"
    assert res.retrieval.reason and "backend unreachable during query" in res.retrieval.reason
    assert res.retrieval.scores == ()
    assert [fact.fact_id for fact in res.facts] == ["fact-1", "fact-2", "fact-0"]
    rr = res.retrieval.rerank
    assert rr is not None and rr.status == "applied"
    assert rr.pre_rerank_order == tuple(str(i) for i in ids)
    assert rr.post_rerank_order == (str(ids[1]), str(ids[2]), str(ids[0]))
    [post] = recorder.rerank_requests
    assert post["json"]["documents"] == _expected_documents()


# ---------------------------------------------------------------------------
# 9. API key handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_key_is_sent_as_bearer_and_never_leaks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    secret = "rerank-secret-key-7f3a"
    key_file = secret_file(tmp_path, "rerank.key", secret)
    adapter, engine, recorder = _rerank_adapter(monkeypatch, tmp_path, reranking_api_key_file=key_file)
    scope = _scope(uuid4(), uuid4())
    await _seed(monkeypatch, engine, scope)

    res = await _query(adapter, scope)
    assert res.retrieval.rerank.status == "applied"
    assert [entry["authorization"] for entry in recorder.requests] == [f"Bearer {secret}", f"Bearer {secret}"]
    assert secret in adapter._secrets

    # Outage text that happens to carry the credential is redacted in the reason ...
    leaking = RecordingRerankTransport(rerank=httpx.ConnectError(f"refused for bearer {secret}"))
    install_fake_reranker(monkeypatch, leaking)
    degraded = RealCogneeAdapter(_rerank_config(tmp_path / "opt", reranking_api_key_file=key_file), available=True)
    res_opt = await _query(degraded, scope)
    assert res_opt.retrieval.rerank.status == "fallback"
    assert secret not in (res_opt.retrieval.rerank.reason or "")
    assert "***" in res_opt.retrieval.rerank.reason

    # ... and in the required-policy error.
    strict = RealCogneeAdapter(_rerank_config(tmp_path / "req", reranking_api_key_file=key_file, reranking_required=True), available=True)
    with pytest.raises(EngineUnavailableError) as exc_info:
        await _query(strict, scope)
    assert secret not in str(exc_info.value)
    # Status never serialises the key.
    assert secret not in str((await adapter.status()).model_dump())


def test_missing_or_exposed_api_key_file_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_fake_cognee(monkeypatch, FakeCogneeConfig())
    with pytest.raises(ValueError, match="reranking api key: secret file not found"):
        RealCogneeAdapter(_rerank_config(tmp_path, reranking_api_key_file=tmp_path / "absent.key"), available=True)

    exposed = secret_file(tmp_path, "exposed.key", "k")
    exposed.chmod(0o644)
    with pytest.raises(ValueError, match="accessible to other users"):
        RealCogneeAdapter(_rerank_config(tmp_path / "x", reranking_api_key_file=exposed), available=True)

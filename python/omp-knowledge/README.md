# omp-knowledge

Connected engineering knowledge service foundation powered by Cognee and Enola.

## Overview
`omp-knowledge` is a derived knowledge engine service supporting:
- Provenance-aware code graph ingestion via Enola artifacts.
- Snapshot-isolated graph and retrieval using embedded Cognee + Ladybug.
- Full fact-identity preservation (`repo\0kind\0name\0file`), avoiding stock Cognee collapsing across files.
- Durable idempotency via dedicated PostgreSQL storage (`omp_knowledge` schema).
- Staged snapshot publication receipts without claiming authoritative native acceptance.

## Storage backends

The default route is fully embedded: Ladybug graph, LanceDB vectors and SQLite
metadata under `OMP_KNOWLEDGE_STATE_DIR`. Nothing below applies to it.

### External route: Neo4j graph + pgvector

Selected with `OMP_KNOWLEDGE_GRAPH_ENGINE=neo4j` and/or
`OMP_KNOWLEDGE_VECTOR_STORE=pgvector`; connection details come from the
`OMP_KNOWLEDGE_NEO4J_*` and `OMP_KNOWLEDGE_COGNEE_PG_*` variables. Credentials are
never held in configuration: only secret-file references
(`OMP_KNOWLEDGE_NEO4J_PASSWORD_FILE`, `OMP_KNOWLEDGE_COGNEE_PG_PASSWORD_FILE`) are
read, and their values never appear in status output or error text. Install the
driver packages with the `omp-knowledge[external-backends]` extra.

Requirements and behaviour:

- **APOC is mandatory on Neo4j.** The pinned Cognee (1.5.4) Neo4j adapter labels
  nodes through `apoc.create.addLabels` and merges relationships through
  `apoc.merge.relationship`. Before its first write the service runs
  `SHOW PROCEDURES` and refuses to write (HTTP 503, `engine_unavailable`) when
  either procedure is missing. Install the APOC core plugin and make sure the
  configured user can list procedures.
- **Semantic pgvector mode.** Enabled by setting `OMP_KNOWLEDGE_GRAPH_ONLY=0`
  (or `graph_only=False` in configuration). This activates vector indexing and
  cosine similarity search over PostgreSQL (`pgvector`) while keeping graph facts
  authoritative:
  - *Switch & requirements*: Requires `OMP_KNOWLEDGE_VECTOR_STORE=pgvector`,
    `OMP_KNOWLEDGE_EMBEDDING_PROVIDER=openai_compatible`, non-empty
    `OMP_KNOWLEDGE_EMBEDDING_MODEL` and `OMP_KNOWLEDGE_EMBEDDING_ENDPOINT`, and
    `OMP_KNOWLEDGE_COGNEE_PG_PASSWORD_FILE`. When `graph_only=True` (default), the
    embedded Ladybug route or graph-only external route remains unchanged with no
    embeddings produced.
  - *Cognee binding*: Wires into Cognee 1.5.4 using `set_embedding_config`
    (`embedding_provider`, `embedding_model`, `embedding_dimensions`, `embedding_endpoint`,
    `embedding_api_key`) and verifies applied settings via `get_embedding_config`
    readback. Global embedding engine and config caches are cleared before binding.
  - *Dimension fail-closed*: Before any write, `_ensure_semantic_ready` validates that
    `config.vector_dimension == embedding_engine.get_vector_size() == len(canary)`
    and equals the reflected `Vector(dim)` of every existing `{Model}_description`
    collection. The canary vector must be non-zero and `embedding_engine.mock` must
    be False (the runtime refuses `MOCK_EMBEDDING`). Any mismatch raises
    `SemanticBindingError` (an `EngineUnavailableError`, HTTP 503) before any table
    or row is touched, and is never degraded by the optional outage policy below.
  - *Outage policy*: Controlled by `OMP_KNOWLEDGE_EMBEDDING_REQUIRED` (default `0`).
    When optional (`0`), inference or vector outages degrade gracefully: ingest
    writes graph nodes and reports `semantic.status="skipped"`, and query falls back
    to exact matching with `retrieval.mode="exact_fallback"` (returning HTTP 200).
    When required (`1`), outages raise `EngineUnavailableError` (HTTP 503), failing
    the job and preventing publication.
  - *Identity, tags & scopes*: Vector rows in `{Model}_description` use deterministic
    fact node UUIDs (`SnapshotScope.fact_node_id`) and store
    `belongs_to_set=[scope_label, "omp-emb:<model>@<dim>"]`. Query searches filter
    by `[scope_label, binding_tag]` with AND logic. `graph_sha256` strictly hashes
    graph nodes and edges, remaining independent of vector rows.
  - *Rebuild & corrections*: Rebuild executes vector re-indexing and reapplies
    corrections; withdrawn facts have their vector rows deleted from pgvector and
    are hidden from query results. A correction reports `semantic_status` as
    `reindexed` (row re-embedded from the corrected fact), `deleted` (fact
    withdrawn) or `failed` (optional-policy outage; the graph correction stands).
    Snapshot retirement retrieves and deletes vector rows across all 13 semantic
    collections before deleting graph nodes.
  - *Endpoint qualification evidence*: Live integration evals
    (`tests/test_semantic_pgvector_integration.py`) verify the endpoint against
    `/v1/models`, require real embeddings, and record evidence to
    `OMP_KNOWLEDGE_SEMANTIC_EVIDENCE_PATH`.
- **Reranking (optional, semantic route only).** Cognee 1.5.4 provides no
  reranker API, so reranking is a narrow stage owned by the adapter and is
  disabled by default (`reranking_provider="none"`).
  - *Switch & requirements*: `OMP_KNOWLEDGE_RERANKING_PROVIDER=rerank_v1` with
    non-empty `OMP_KNOWLEDGE_RERANKING_MODEL` and
    `OMP_KNOWLEDGE_RERANKING_ENDPOINT` (credential-free `http(s)` URL; userinfo,
    other schemes and a missing host are refused at configuration time), an
    optional `OMP_KNOWLEDGE_RERANKING_API_KEY_FILE` (sent as a bearer token, never
    reported), `OMP_KNOWLEDGE_RERANKING_REQUIRED` (default `0`) and
    `OMP_KNOWLEDGE_RERANKING_TIMEOUT_SECONDS` (default `15`). Requires
    `graph_only=False`; the graph-only and embedded routes refuse the switch and
    are byte-identical to before.
  - *Endpoint contract & pair format*: before the first call the configured
    model must be listed by the endpoint's `GET /v1/models` (a bogus model, or an
    embedding server, raises `RerankBindingError`, HTTP 503, and no `/v1/rerank`
    request is ever sent). Each query then posts
    `{"model", "query", "documents"}` to `POST /v1/rerank` and reads
    `results[].index` + `results[].relevance_score`. The query is the raw query
    text and every document is the exact indexed `semantic_text` of the
    candidate fact (pair format `rerank_v1/semantic_text`); no Qwen instruction
    prefix or prompt template is applied.
  - *Permutation only*: the stage runs after scope filtering, withdrawn-fact
    filtering, hydration from graph rows and the `limit` cut, and only reorders
    that list by `(-relevance_score, pre-rank index)`. It never hydrates extra
    rows, never re-adds withdrawn, foreign or ghost records and never changes
    `total_matched`; the `*` query bypasses it.
  - *Receipt*: `retrieval.rerank` on `/v1/query` carries `status`
    (`applied` / `fallback` / `not_attempted`), `provider`, `model_id`,
    `endpoint`, `pair_format`, `request_sha256` (sha256 over the canonical JSON of
    the query and documents, recomputable from the returned facts),
    `document_count`, `pre_rerank_order`, `post_rerank_order` (fact node ids),
    `scores` (aligned to the post order) and `reason`. `retrieval.scores` are
    permuted together with `facts`. `/v1/status` reports the binding under
    `engine.details.reranking`.
  - *Outage policy*: transport failures, the deadline and HTTP 429 / 502 / 503 /
    504 are outages. Optional (`0`): `status="fallback"` with the redacted reason
    and the pre-rerank candidates and order unchanged (HTTP 200). Required (`1`):
    `EngineUnavailableError` (HTTP 503 `engine_unavailable`). An embedding
    outage is independent: `exact_fallback` candidates are still reranked when
    the reranker is healthy.
  - *Never degraded*: any other HTTP status, a non-JSON or malformed body
    (missing / mis-sized `results`, bad or duplicate `index`, non-numeric or
    non-finite score) raises `RerankContractError`; an echoed `model` that differs
    from the configured one raises `RerankBindingError`; programming and data
    errors propagate unchanged, under both policies.
  - *Context bundles unaffected*: `ByteBudgetCompiler` sorts engine facts by
    `fact_id` before packing, so `bundle_sha256`, optional content and budget are
    identical with reranking enabled, disabled or replayed. Rerank ordering is
    observable only through `/v1/query`.
  - *Evidence*: the opt-in live evals (`tests/test_rerank_integration.py`, gated
    additionally by `OMP_KNOWLEDGE_RERANK_INTEGRATION=1`) qualify the endpoint,
    read the receipt back against real candidates and write evidence to
    `OMP_KNOWLEDGE_RERANK_EVIDENCE_PATH`; no synthetic scores or mock models are
    accepted.
  - *Limitations*: the stage cannot change which candidates are returned
    (raising hydration above `limit` and cutting after reranking would change
    candidate identity and needs a new plan), and the reranker sees no query
    instruction.
- **Fail closed, never fall back.** Missing driver modules, unreadable or
  world-readable secret files, a Cognee setter that is absent or rejects the
  payload, and a Cognee config getter that does not read the applied settings back
  all stop the service from starting on the external route; it never silently
  reverts to the embedded route.
- **Connection failures are unavailability.** Neo4j and PostgreSQL connectivity or
  authentication failures raised during a request are reported as
  `engine_unavailable` (HTTP 503) with credential values redacted; query errors
  from a reachable backend are surfaced as they are.
- **Corrections rebuild the node.** Cognee has no partial node update, so a
  correction deletes and re-adds the fact node with only its `fact_properties`
  changed, then restores every incident edge through Cognee's `add_edges`. If the
  restored edges do not read back, the correction raises instead of reporting
  success. Delete and re-add are separate backend operations; a failure between
  them leaves correction retryable and may require retained snapshot rebuild.

Optional semantic cleanup outages remain explicitly retryable: server correction
and rebuild jobs report partial cleanup until vector maintenance succeeds.

Review evals keep frozen source trees immutable while retaining durable test receipts.

The opt-in integration test for this route is
`tests/test_external_backends_integration.py` (see its module docstring for the
gating environment variables).

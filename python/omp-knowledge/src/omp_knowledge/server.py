from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
from pathlib import Path
from typing import Any, Callable
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from omp_work.knowledge_contracts import (
    BudgetActual,
    ContextBundle,
    JobState,
    SnapshotRef,
)
from omp_work.v1.canonical import sha256

from .config import KnowledgeConfig, load_config
from .engine.cognee_adapter import RealCogneeAdapter
from .context.compiler import (
    BudgetInsufficientError,
    BudgetMethodUnavailable,
    ByteBudgetCompiler,
    WorkRevisionUnverifiableError,
    bundle_from_row,
    select_bundle_row,
)
from .engine.protocol import CorrectResult, EngineUnavailableError, KnowledgeEngine
from .learning.corrections import (
    build_withdrawn_validity,
    exclude_withdrawn_facts,
    get_correction,
    is_fact_withdrawn,
    load_correction_rows,
    reapply_corrections_to_engine,
    record_correction,
)
from .learning.proposals import (
    create_proposal,
    get_proposal,
    list_proposals,
)
from .learning.uses import (
    compute_proposal_support,
    record_outcome,
    record_use,
)
from .models import (
    ApplicabilityRequest,
    CodeSnapshotIngestRequest,
    ContextCompileRequest,
    CorrectionCreateRequest,
    IngestRequest,
    NativeConsumeRequest,
    NativeRecordIngestRequest,
    Principal,
    ProposalCreateRequest,
    PublishRequest,
    RebuildRequest,
    RecordOutcomeRequest,
    RecordUseRequest,
    RetireRequest,
    SnapshotManifest,
    validate_canonical_snapshot_id,
)
from .native.consumer import NativeEventConsumer, NativeSourceUnavailableError
from .ownership import WriterOwnership, WriterOwnershipLost
from .policy import evaluate_native_acceptance
from .staging.manifest import (
    StagingError,
    stage_snapshot_artifacts,

    validate_enola_artifacts,
    validate_source_manifest_retention,
)
from .storage.db import (
    IdempotencyConflictError,
    RebuildHashMismatchError,
    execute_idempotent_job_async,
    get_db_connection,
    get_retained_artifact,
    is_snapshot_published,
    publish_snapshot_gated,
    store_observation,
)
from .storage.jobs import ForeignOperationError, read_operation, run_operation

logger = logging.getLogger(__name__)


def authenticate_principal(
    request: Request,
    capabilities_dir: Path,
) -> Principal:
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail={"error": {"code": "unauthenticated"}})
    token = auth_header[7:]

    try:
        # Directory permissions: must be owner-only (0700)
        if capabilities_dir.exists() and (capabilities_dir.stat().st_mode & 0o777) != 0o700:
            raise HTTPException(
                status_code=401,
                detail={"error": {"code": "unauthenticated", "message": "unsafe capabilities directory"}},
            )
        if capabilities_dir.exists():
            for path in capabilities_dir.iterdir():
                if not path.is_file() or not path.name.endswith(".json"):
                    continue
                # File permissions must be 0600
                if (path.stat().st_mode & 0o777) != 0o600:
                    continue
                data = json.loads(path.read_text(encoding="utf-8"))
                stored_token = data.get("token")
                if stored_token and hmac.compare_digest(str(stored_token), token):
                    return Principal(
                        actor_id=UUID(data["actor_id"]),
                        actor_kind=str(data["actor_kind"]),
                        workspaces=frozenset(UUID(w) for w in data.get("workspaces", ())),
                        scopes=frozenset(data.get("scopes", ())),
                    )
    except HTTPException:
        raise
    except Exception:
        pass

    raise HTTPException(status_code=401, detail={"error": {"code": "unauthenticated"}})


def require_scope(required_scope: str) -> Callable:
    def dependency(request: Request) -> Principal:
        config: KnowledgeConfig = request.app.state.config
        principal = authenticate_principal(request, config.capabilities_dir)
        if required_scope not in principal.scopes:
            raise HTTPException(
                status_code=403,
                detail={"error": {"code": "forbidden", "diagnostics": [f"missing_scope_{required_scope}"]}},
            )
        return principal

    return dependency


def get_or_ensure_writer(app: FastAPI) -> WriterOwnership:
    writer = getattr(app.state, "writer", None)
    if writer is None:
        writer = WriterOwnership(app.state.config)
        app.state.writer = writer
        if not writer.acquire():
            raise WriterOwnershipLost("Could not acquire writer ownership: backend owned by another process")
        writer.recover_interrupted_jobs()
    if not writer.is_acquired():
        raise WriterOwnershipLost("Writer ownership is not held or has been lost")
    writer.ensure_alive()
    return writer


class StrictCleanupEngineWrapper:
    """Wraps an underlying KnowledgeEngine to enforce strict correctness during cleanup.

    Under optional semantic outage, RealCogneeAdapter.correct() may return success=True
    with semantic_status='failed'. This wrapper converts semantic_status='failed' into
    an EngineUnavailableError during cleanup operations, ensuring durable ingestion jobs
    record JobState.PARTIAL rather than falsely completing.
    """

    def __init__(self, target: KnowledgeEngine) -> None:
        self._target = target

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target, name)

    async def correct(
        self,
        *,
        workspace_id: UUID,
        repository_id: UUID,
        snapshot_id: str,
        fact_id: str,
        properties_update: dict[str, Any],
    ) -> CorrectResult:
        res = await self._target.correct(
            workspace_id=workspace_id,
            repository_id=repository_id,
            snapshot_id=snapshot_id,
            fact_id=fact_id,
            properties_update=properties_update,
        )
        if getattr(res, "semantic_status", None) == "failed":
            reason = getattr(res, "semantic_reason", None) or "semantic vector cleanup failed"
            raise EngineUnavailableError(
                f"vector cleanup failed under optional semantic outage: {reason}"
            )
        return res


def wrap_cleanup_engine(engine: KnowledgeEngine) -> KnowledgeEngine:
    if isinstance(engine, StrictCleanupEngineWrapper):
        return engine
    return StrictCleanupEngineWrapper(engine)


def create_app(
    config: KnowledgeConfig | None = None,
    engine: KnowledgeEngine | None = None,
    writer: WriterOwnership | None = None,
) -> FastAPI:
    app_config = config or load_config()

    @contextlib.asynccontextmanager
    async def lifespan(fastapi_app: FastAPI):
        w = getattr(fastapi_app.state, "writer", None)
        if w is None or (not w.is_acquired() and w.attempted):
            w = WriterOwnership(app_config)
            fastapi_app.state.writer = w

        try:
            if not w.is_acquired():
                if not w.acquire():
                    raise WriterOwnershipLost("Could not acquire writer ownership: backend owned by another process")
            w.ensure_alive()
            w.recover_interrupted_jobs()
            yield
        finally:
            if w is not None and w.owned:
                try:
                    await w.release()
                except Exception as exc:
                    logger.warning("Failed to release writer ownership during lifespan cleanup: %s", exc)

    app = FastAPI(title="omp-knowledge", version="0.1.0", lifespan=lifespan)
    app.state.config = app_config
    app.state.engine = engine or RealCogneeAdapter(app_config)
    app.state.writer = writer

    @app.exception_handler(EngineUnavailableError)
    async def engine_unavailable_handler(request: Request, exc: EngineUnavailableError) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"error": {"code": "engine_unavailable", "message": str(exc)}},
        )

    @app.exception_handler(NativeSourceUnavailableError)
    async def native_source_unavailable_handler(request: Request, exc: NativeSourceUnavailableError) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"error": {"code": "native_source_unavailable", "message": str(exc)}},
        )

    @app.exception_handler(WriterOwnershipLost)
    async def writer_ownership_lost_handler(request: Request, exc: WriterOwnershipLost) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"error": {"code": "writer_lost", "message": str(exc)}},
        )

    @app.exception_handler(ForeignOperationError)
    async def foreign_operation_handler(request: Request, exc: ForeignOperationError) -> JSONResponse:
        return JSONResponse(
            status_code=403,
            content={"error": {"code": "forbidden", "message": str(exc)}},
        )

    @app.exception_handler(IdempotencyConflictError)
    async def idempotency_conflict_handler(request: Request, exc: IdempotencyConflictError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "error": {
                    "code": "idempotency_conflict",
                    "operation_id": str(exc.operation_id),
                    "current_hash": exc.current_hash,
                    "existing_hash": exc.existing_hash,
                }
            },
        )

    @app.exception_handler(PermissionError)
    async def permission_error_handler(request: Request, exc: PermissionError) -> JSONResponse:
        return JSONResponse(
            status_code=403,
            content={"error": {"code": "forbidden", "message": str(exc)}},
        )

    @app.exception_handler(StagingError)
    async def staging_error_handler(request: Request, exc: StagingError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"error": {"code": type(exc).__name__, "message": str(exc)}},
        )

    @app.exception_handler(RebuildHashMismatchError)
    async def rebuild_hash_mismatch_handler(request: Request, exc: RebuildHashMismatchError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "error": {
                    "code": "rebuild_hash_mismatch",
                    "message": str(exc),
                    "expected_graph_sha256": exc.expected_hash,
                    "actual_graph_sha256": exc.actual_hash,
                }
            },
        )

    @app.exception_handler(BudgetInsufficientError)
    async def budget_insufficient_handler(request: Request, exc: BudgetInsufficientError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "budget_insufficient_for_mandatory",
                    "message": str(exc),
                    "mandatory_used": exc.mandatory_used,
                    "limit": exc.limit,
                }
            },
        )

    @app.exception_handler(BudgetMethodUnavailable)
    async def budget_method_unavailable_handler(request: Request, exc: BudgetMethodUnavailable) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "budget_method_unavailable",
                    "message": str(exc),
                    "owner": exc.owner,
                }
            },
        )

    @app.exception_handler(WorkRevisionUnverifiableError)
    async def work_revision_unverifiable_handler(request: Request, exc: WorkRevisionUnverifiableError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "work_revision_unverifiable",
                    "message": str(exc),
                }
            },
        )



    @app.get("/v1/health/live")
    async def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/health/ready")
    async def health_ready() -> dict[str, Any]:
        engine_status = await app.state.engine.status()
        return {
            "status": "ready" if engine_status.available else "degraded",
            "engine": engine_status.model_dump(mode="json"),
        }

    @app.get("/v1/status")
    async def get_status(
        principal: Principal = Depends(require_scope("knowledge.read")),
    ) -> dict[str, Any]:
        engine_status = await app.state.engine.status()
        return {
            "status": "ok",
            "actor_id": str(principal.actor_id),
            "engine": engine_status.model_dump(mode="json"),
        }

    @app.post("/v1/ingest")
    async def ingest_snapshot(
        body: IngestRequest,
        principal: Principal = Depends(require_scope("knowledge.ingest")),
    ) -> dict[str, Any]:
        if body.workspace_id not in principal.workspaces:
            raise HTTPException(
                status_code=403,
                detail={"error": {"code": "forbidden", "diagnostics": ["workspace_not_permitted"]}},
            )

        req_payload = body.model_dump(mode="json")
        request_hash = sha256(req_payload)
        conn = get_db_connection(app.state.config)

        try:
            writer = get_or_ensure_writer(app)

            if isinstance(body, CodeSnapshotIngestRequest):
                facts_bytes = body.facts_jsonl.encode("utf-8")
                receipt_bytes = body.receipt_json.encode("utf-8")
                insights_bytes = body.insights_json.encode("utf-8")

                # Validate Enola extraction artifacts
                receipt_dict, parsed_facts, parsed_insights = validate_enola_artifacts(
                    facts_bytes=facts_bytes,
                    receipt_bytes=receipt_bytes,
                    insights_bytes=insights_bytes,
                )

                # Compute candidate manifest BEFORE any DB write or engine mutation
                facts_sha256 = hashlib.sha256(facts_bytes).hexdigest()
                receipt_sha256 = hashlib.sha256(receipt_bytes).hexdigest()
                insights_sha256 = hashlib.sha256(insights_bytes).hexdigest()
                candidate_manifest: dict[str, Any] = {
                    "snapshot_ref": body.snapshot_ref.model_dump(mode="json"),
                    "artifacts": {
                        "facts": f"blob:sha256:{facts_sha256}",
                        "receipt": f"blob:sha256:{receipt_sha256}",
                        "insights": f"blob:sha256:{insights_sha256}",
                    },
                }
                if body.source_manifest is not None:
                    candidate_manifest["source_manifest"] = body.source_manifest.model_dump(
                        mode="json", by_alias=True
                    )
                    # Preflight source manifest retention before any engine call or job admission
                    validate_source_manifest_retention(app.state.config, body.source_manifest)

                snapshot_manifest = SnapshotManifest.model_validate(candidate_manifest)

                # Shared async durable job primitive with atomic admission & single-writer ownership
                async def do_ingest() -> tuple[JobState, dict[str, Any], list[str]]:
                    ingest_res = await app.state.engine.ingest_snapshot(
                        workspace_id=body.workspace_id,
                        repository_id=body.repository_id,
                        snapshot_ref=body.snapshot_ref,
                        facts=parsed_facts,
                        receipt=receipt_dict,
                        insights=parsed_insights,
                    )

                    staged_receipt = stage_snapshot_artifacts(
                        conn,
                        app.state.config,
                        workspace_id=body.workspace_id,
                        repository_id=body.repository_id,
                        snapshot_ref=body.snapshot_ref,
                        facts_bytes=facts_bytes,
                        receipt_bytes=receipt_bytes,
                        insights_bytes=insights_bytes,
                        node_ids=ingest_res.node_ids,
                        edge_keys=ingest_res.edge_keys,
                        source_manifest=body.source_manifest,
                        operation_id=body.operation_id,
                    )
                    diagnostics = [f"ingested_{ingest_res.nodes_written}_nodes"]
                    if ingest_res.semantic is not None:
                        diagnostics.append(f"semantic_index_{ingest_res.semantic.status}")
                    return (
                        JobState.COMPLETED,
                        staged_receipt.model_dump(mode="json"),
                        diagnostics,
                    )

                return await execute_idempotent_job_async(
                    conn,
                    operation_id=body.operation_id,
                    workspace_id=body.workspace_id,
                    repository_id=body.repository_id,
                    snapshot_id=body.snapshot_id,
                    request_hash=request_hash,
                    action=do_ingest,
                    writer=writer,
                    snapshot_manifest=snapshot_manifest,
                )

            elif isinstance(body, NativeRecordIngestRequest):
                async def do_record() -> tuple[JobState, dict[str, Any], list[str]]:
                    store_observation(conn, observation=body.observation)
                    return (
                        JobState.COMPLETED,
                        {"observation_id": str(body.observation.observation_id), "status": "recorded"},
                        ["observation_recorded"],
                    )

                return await execute_idempotent_job_async(
                    conn,
                    operation_id=body.operation_id,
                    workspace_id=body.workspace_id,
                    repository_id=body.repository_id,
                    snapshot_id=None,
                    request_hash=request_hash,
                    action=do_record,
                    writer=writer,
                )
            else:
                raise HTTPException(status_code=422, detail="Unsupported ingest kind")
        finally:
            conn.close()

    @app.post("/v1/publish")
    async def publish_snapshot(
        body: PublishRequest,
        principal: Principal = Depends(require_scope("knowledge.publish")),
    ) -> dict[str, Any]:
        if body.workspace_id not in principal.workspaces:
            raise HTTPException(
                status_code=403,
                detail={"error": {"code": "forbidden", "diagnostics": ["workspace_not_permitted"]}},
            )

        writer = get_or_ensure_writer(app)
        async with writer.mutation():
            conn = get_db_connection(app.state.config)
            try:
                try:
                    success = publish_snapshot_gated(
                        conn,
                        app.state.config,
                        workspace_id=body.workspace_id,
                        repository_id=body.repository_id,
                        snapshot_id=body.snapshot_id,
                    )
                except (PermissionError, RuntimeError, ValueError) as exc:
                    raise HTTPException(
                        status_code=400,
                        detail={"error": {"code": "publish_failed", "message": str(exc)}},
                    )
                if not success:
                    raise HTTPException(
                        status_code=400,
                        detail={"error": {"code": "publish_failed", "diagnostics": ["snapshot_not_staged_or_already_published"]}},
                    )
                return {
                    "published": True,
                    "snapshot_id": body.snapshot_id,
                    "repository_id": str(body.repository_id),
                    "workspace_id": str(body.workspace_id),
                }
            finally:
                conn.close()

    @app.get("/v1/query")
    async def query_knowledge(
        workspace_id: UUID = Query(...),
        repository_id: UUID = Query(...),
        snapshot_id: str = Query(...),
        query: str = Query(...),
        limit: int = Query(20, ge=1, le=100),
        principal: Principal = Depends(require_scope("knowledge.read")),
    ) -> dict[str, Any]:
        if workspace_id not in principal.workspaces:
            raise HTTPException(
                status_code=403,
                detail={"error": {"code": "forbidden", "diagnostics": ["workspace_not_permitted"]}},
            )

        snapshot_id = validate_canonical_snapshot_id(snapshot_id)

        conn = get_db_connection(app.state.config)
        try:
            if not is_snapshot_published(conn, workspace_id=workspace_id, repository_id=repository_id, snapshot_id=snapshot_id):
                raise HTTPException(
                    status_code=400,
                    detail={"error": {"code": "snapshot_not_published", "diagnostics": [f"snapshot_{snapshot_id}_not_published"]}},
                )
            rows = load_correction_rows(conn, workspace_id=workspace_id, repository_id=repository_id)
            validity = build_withdrawn_validity(rows, snapshot_id)
        finally:
            conn.close()

        result = await app.state.engine.query(
            workspace_id=workspace_id,
            repository_id=repository_id,
            snapshot_id=snapshot_id,
            query_text=query,
            limit=limit,
        )

        result = exclude_withdrawn_facts(validity, result)

        return result.model_dump(mode="json")

    @app.get("/v1/lookup")
    async def lookup_fact(
        workspace_id: UUID = Query(...),
        repository_id: UUID = Query(...),
        snapshot_id: str = Query(...),
        fact_id: str = Query(...),
        file_path: str | None = Query(None),
        principal: Principal = Depends(require_scope("knowledge.read")),
    ) -> dict[str, Any]:
        if workspace_id not in principal.workspaces:
            raise HTTPException(
                status_code=403,
                detail={"error": {"code": "forbidden", "diagnostics": ["workspace_not_permitted"]}},
            )

        snapshot_id = validate_canonical_snapshot_id(snapshot_id)

        conn = get_db_connection(app.state.config)
        try:
            if not is_snapshot_published(conn, workspace_id=workspace_id, repository_id=repository_id, snapshot_id=snapshot_id):
                raise HTTPException(
                    status_code=400,
                    detail={"error": {"code": "snapshot_not_published", "diagnostics": [f"snapshot_{snapshot_id}_not_published"]}},
                )
            rows = load_correction_rows(conn, workspace_id=workspace_id, repository_id=repository_id)
            validity = build_withdrawn_validity(rows, snapshot_id)
        finally:
            conn.close()

        if fact_id in validity.fact_ids:
            engine_status = await app.state.engine.status()
            active_route = engine_status.active_route
            route_dict = (
                active_route.as_dict()
                if hasattr(active_route, "as_dict")
                else (active_route.model_dump(mode="json") if hasattr(active_route, "model_dump") else active_route)
            )
            return {
                "fact": None,
                "found": False,
                "snapshot_id": snapshot_id,
                "route": route_dict,
            }

        result = await app.state.engine.lookup(
            workspace_id=workspace_id,
            repository_id=repository_id,
            snapshot_id=snapshot_id,
            fact_id=fact_id,
            file_path=file_path,
        )

        if result.found and result.fact and is_fact_withdrawn(validity, result.fact):
            result = result.model_copy(update={"fact": None, "found": False})

        return result.model_dump(mode="json")

    @app.get("/v1/jobs/{operation_id}")
    async def get_job(
        operation_id: UUID,
        principal: Principal = Depends(require_scope("knowledge.read")),
    ) -> dict[str, Any]:
        conn = get_db_connection(app.state.config)
        try:
            job = read_operation(conn, operation_id=operation_id, workspace_id=principal.workspaces)
            if not job:
                raise HTTPException(status_code=404, detail={"error": {"code": "not_found"}})
            return job
        finally:
            conn.close()

    @app.post("/v1/rebuild")
    async def rebuild_snapshot(
        body: RebuildRequest,
        principal: Principal = Depends(require_scope("knowledge.admin")),
    ) -> dict[str, Any]:
        if body.workspace_id not in principal.workspaces:
            raise HTTPException(status_code=403, detail={"error": {"code": "forbidden"}})

        req_payload = body.model_dump(mode="json")
        request_hash = sha256(req_payload)
        writer = get_or_ensure_writer(app)
        conn = get_db_connection(app.state.config)
        try:
            with conn.transaction():
                with conn.cursor() as cur:
                    # Check for existing terminal job (idempotent replay)
                    cur.execute(
                        """
                        SELECT state
                        FROM omp_knowledge.ingestion_jobs
                        WHERE operation_id = %s AND workspace_id = %s
                        """,
                        (body.operation_id, body.workspace_id),
                    )
                    existing_job = cur.fetchone()
                    is_replay = bool(existing_job) and existing_job["state"] in (
                        JobState.COMPLETED.value,
                        JobState.NO_LESSON.value,
                        JobState.CANCELLED.value,
                    )

                    if not is_replay:
                        cur.execute(
                            """
                            SELECT workspace_id, manifest FROM omp_knowledge.snapshots
                            WHERE workspace_id = %s AND repository_id = %s AND snapshot_id = %s
                            """,
                            (body.workspace_id, body.repository_id, body.snapshot_id),
                        )
                        row = cur.fetchone()
                        if not row:
                            raise HTTPException(status_code=404, detail={"error": {"code": "snapshot_not_found"}})
                        manifest = row["manifest"]
                        if isinstance(manifest, str):
                            manifest = json.loads(manifest)
                        artifacts = manifest.get("artifacts") or {}

                        cur.execute(
                            """
                            SELECT publication_id, status, graph_sha256
                            FROM omp_knowledge.snapshot_publications
                            WHERE workspace_id = %s AND repository_id = %s AND snapshot_id = %s
                            """,
                            (body.workspace_id, body.repository_id, body.snapshot_id),
                        )
                        pub_row = cur.fetchone()
                        if not pub_row or pub_row["status"] not in ("staged", "published"):
                            raise HTTPException(
                                status_code=400,
                                detail={
                                    "error": {
                                        "code": "snapshot_not_published",
                                        "diagnostics": [f"snapshot_{body.snapshot_id}_not_published"],
                                    }
                                },
                            )

                        for name in ("facts", "receipt", "insights"):
                            locator = artifacts.get(name, "")
                            data = (
                                get_retained_artifact(app.state.config, locator[len("blob:sha256:"):])
                                if isinstance(locator, str) and locator.startswith("blob:sha256:")
                                else None
                            )
                            if not data:
                                raise HTTPException(
                                    status_code=400,
                                    detail={"error": {"code": "artifact_unavailable", "diagnostics": [f"missing_{name}"]}},
                                )

                        corrections = load_correction_rows(
                            conn,
                            workspace_id=body.workspace_id,
                            repository_id=body.repository_id,
                        )

            if is_replay:
                replayed = await execute_idempotent_job_async(
                    conn,
                    operation_id=body.operation_id,
                    workspace_id=body.workspace_id,
                    repository_id=body.repository_id,
                    snapshot_id=body.snapshot_id,
                    request_hash=request_hash,
                    action=lambda: None,
                    writer=writer,
                    skip_snapshot_owner_cas=True,
                    skip_ingest_snapshot_owner_cas=True,
                )
                if replayed.get("result_sha256") is None:
                    job_record = read_operation(conn, operation_id=body.operation_id, workspace_id=body.workspace_id)
                    if job_record and job_record.get("result_sha256") is not None:
                        return job_record
                return replayed

            async def do_rebuild() -> tuple[JobState, dict[str, Any], list[str]]:
                # Preflight 1: every retained artifact the manifest names must be present and
                # content-verified in CAS (get_retained_artifact re-hashes) before any engine write.
                def retained(name: str) -> bytes:
                    locator = artifacts.get(name, "")
                    data = (
                        get_retained_artifact(app.state.config, locator[len("blob:sha256:"):])
                        if isinstance(locator, str) and locator.startswith("blob:sha256:")
                        else None
                    )
                    if not data:
                        raise HTTPException(
                            status_code=400,
                            detail={"error": {"code": "artifact_unavailable", "diagnostics": [f"missing_{name}"]}},
                        )
                    return data

                receipt_dict, parsed_facts, parsed_insights = validate_enola_artifacts(
                    facts_bytes=retained("facts"),
                    receipt_bytes=retained("receipt"),
                    insights_bytes=retained("insights"),
                )
                snapshot_ref = SnapshotRef.model_validate(manifest["snapshot_ref"])
                engine_inputs = dict(
                    workspace_id=body.workspace_id,
                    repository_id=body.repository_id,
                    snapshot_ref=snapshot_ref,
                    facts=parsed_facts,
                    receipt=receipt_dict,
                    insights=parsed_insights,
                )
                expected_graph_sha = (
                    pub_row["graph_sha256"] if isinstance(pub_row, dict) else pub_row[2]
                )

                def verify_graph_sha(actual: str) -> None:
                    if actual != expected_graph_sha:
                        raise RebuildHashMismatchError(
                            f"rebuild_hash_mismatch: expected {expected_graph_sha}, got {actual}",
                            expected_hash=expected_graph_sha,
                            actual_hash=actual,
                        )

                # Preflight 2: the deterministic graph hash is computed from the retained
                # artifacts without touching the engine; a mismatch fails here, so the live
                # published namespace and engine content are never mutated.
                plan = await app.state.engine.plan_snapshot(**engine_inputs)
                verify_graph_sha(plan.graph_sha256)

                result = await app.state.engine.ingest_snapshot(**engine_inputs)
                # No fake acceptance: the written graph must still equal the published hash.
                verify_graph_sha(result.graph_sha256)

                # Canonical validity reapplication: re-apply persisted corrections
                # so that withdrawn evidence does not resurrect in the rebuilt engine.
                applied, unapplied = await reapply_corrections_to_engine(
                    wrap_cleanup_engine(app.state.engine),
                    workspace_id=body.workspace_id,
                    repository_id=body.repository_id,
                    snapshot_id=body.snapshot_id,
                    corrections=corrections,
                    parsed_facts=parsed_facts,
                )

                res_payload = {
                    "rebuilt": True,
                    "snapshot_id": body.snapshot_id,
                    "nodes_written": result.nodes_written,
                    "graph_sha256": result.graph_sha256,
                    "validity_reapplied": applied,
                    "validity_unapplied": unapplied,
                }
                diagnostics = [f"rebuilt_{result.nodes_written}_nodes"]
                if result.semantic is not None:
                    diagnostics.append(f"semantic_index_{result.semantic.status}")
                if unapplied:
                    diagnostics.append("rebuild_validity_reapply_partial")
                    return (
                        JobState.PARTIAL,
                        res_payload,
                        diagnostics,
                    )
                diagnostics.append("rebuild_validity_reapplied")
                return (
                    JobState.COMPLETED,
                    res_payload,
                    diagnostics,
                )

            return await execute_idempotent_job_async(
                conn,
                operation_id=body.operation_id,
                workspace_id=body.workspace_id,
                repository_id=body.repository_id,
                snapshot_id=body.snapshot_id,
                request_hash=request_hash,
                action=do_rebuild,
                writer=writer,
                skip_snapshot_owner_cas=True,
                skip_ingest_snapshot_owner_cas=True,
            )
        finally:
            conn.close()

    @app.post("/v1/retire")
    async def retire_snapshot(
        body: RetireRequest,
        principal: Principal = Depends(require_scope("knowledge.admin")),
    ) -> dict[str, Any]:
        if body.workspace_id not in principal.workspaces:
            raise HTTPException(status_code=403, detail={"error": {"code": "forbidden"}})

        req_payload = body.model_dump(mode="json")
        request_hash = sha256(req_payload)
        writer = get_or_ensure_writer(app)
        conn = get_db_connection(app.state.config)
        try:
            # The replay check and preflight must run inside an explicit, committed transaction.
            # A bare cursor on this non-autocommit connection opens an implicit transaction that
            # is never committed; every conn.transaction() inside run_operation then nests as a
            # savepoint, and the job admission, completion CAS and retract UPDATE are all
            # discarded when conn.close() runs, while the in-memory response still says COMPLETED.
            with conn.transaction():
                with conn.cursor() as cur:
                    # Check for existing terminal job (idempotent replay)
                    cur.execute(
                        """
                        SELECT state
                        FROM omp_knowledge.ingestion_jobs
                        WHERE operation_id = %s AND workspace_id = %s
                        """,
                        (body.operation_id, body.workspace_id),
                    )
                    existing_job = cur.fetchone()
                    is_replay = bool(existing_job) and existing_job["state"] in (
                        JobState.COMPLETED.value,
                        JobState.NO_LESSON.value,
                        JobState.CANCELLED.value,
                    )

                    publication_id: UUID | None = None
                    if not is_replay:
                        # Fail-closed preflight before engine.retire requiring matching
                        # workspace/repository snapshot and valid publication baseline
                        cur.execute(
                            """
                            SELECT workspace_id, repository_id
                            FROM omp_knowledge.snapshots
                            WHERE workspace_id = %s AND repository_id = %s AND snapshot_id = %s
                            """,
                            (body.workspace_id, body.repository_id, body.snapshot_id),
                        )
                        snap_row = cur.fetchone()
                        if not snap_row:
                            raise HTTPException(status_code=404, detail={"error": {"code": "snapshot_not_found"}})

                        cur.execute(
                            """
                            SELECT publication_id, status
                            FROM omp_knowledge.snapshot_publications
                            WHERE workspace_id = %s AND repository_id = %s AND snapshot_id = %s
                            """,
                            (body.workspace_id, body.repository_id, body.snapshot_id),
                        )
                        pub_row = cur.fetchone()
                        if not pub_row or pub_row["status"] not in ("staged", "published"):
                            raise HTTPException(
                                status_code=400,
                                detail={
                                    "error": {
                                        "code": "snapshot_not_published",
                                        "diagnostics": [f"snapshot_{body.snapshot_id}_not_published"],
                                    }
                                },
                            )
                        publication_id = pub_row["publication_id"]

            if is_replay:
                return await run_operation(
                    conn,
                    writer=writer,
                    operation_id=body.operation_id,
                    workspace_id=body.workspace_id,
                    repository_id=body.repository_id,
                    snapshot_id=body.snapshot_id,
                    request_hash=request_hash,
                    action=lambda: None,
                    skip_snapshot_owner_cas=True,
                    skip_ingest_snapshot_owner_cas=True,
                )

            async def do_retire() -> tuple[JobState, dict[str, Any], list[str]]:
                ret = await app.state.engine.retire(
                    workspace_id=body.workspace_id,
                    repository_id=body.repository_id,
                    snapshot_id=body.snapshot_id,
                )
                # Counts are the engine's readback differences; vector fields are
                # None on graph-only routes and explicit (never a guessed zero) on
                # the semantic route, see RetireResult.
                res_payload = {
                    "retired": True,
                    "snapshot_id": body.snapshot_id,
                    "nodes_deleted": ret.nodes_deleted,
                    "edges_deleted": ret.edges_deleted,
                    "vector_rows_deleted": ret.vector_rows_deleted,
                    "vector_cleanup": ret.vector_cleanup,
                    "vector_cleanup_reason": ret.vector_cleanup_reason,
                }
                diagnostics = ["snapshot_retired"]
                if ret.vector_cleanup in ("degraded", "partial"):
                    diagnostics.append(f"vector_cleanup_{ret.vector_cleanup}")
                return (JobState.COMPLETED, res_payload, diagnostics)

            def commit_retract(cur: psycopg.Cursor, action_res: Any):
                cur.execute(
                    """
                    UPDATE omp_knowledge.snapshot_publications
                    SET status = 'retracted', published_at = NULL
                    WHERE publication_id = %s AND status IN ('staged', 'published')
                    """,
                    (publication_id,),
                )
                if cur.rowcount != 1:
                    # The engine already ran; report what it actually removed rather
                    # than a fabricated zero, with retired=False for the ledger.
                    engine_payload = action_res[1] if isinstance(action_res, tuple) and len(action_res) > 1 else {}
                    return (
                        JobState.FAILED,
                        {**dict(engine_payload), "retired": False, "snapshot_id": body.snapshot_id},
                        ["snapshot_retract_not_updated"],
                    )
                return action_res

            return await run_operation(
                conn,
                writer=writer,
                operation_id=body.operation_id,
                workspace_id=body.workspace_id,
                repository_id=body.repository_id,
                snapshot_id=body.snapshot_id,
                request_hash=request_hash,
                action=do_retire,
                commit=commit_retract,
                skip_snapshot_owner_cas=True,
                skip_ingest_snapshot_owner_cas=True,
            )
        finally:
            conn.close()

    @app.post("/v1/proposals")
    async def create_new_proposal(
        body: ProposalCreateRequest,
        principal: Principal = Depends(require_scope("knowledge.ingest")),
    ) -> dict[str, Any]:
        if body.workspace_id not in principal.workspaces:
            raise HTTPException(status_code=403, detail={"error": {"code": "forbidden"}})
        conn = get_db_connection(app.state.config)
        try:
            with conn.cursor() as cur:
                for ev in body.supporting_evidence:
                    receipt_uuid_str = str(ev.native_validity_ref)
                    payload_sha = str(ev.content_sha256)
                    cur.execute(
                        """
                        SELECT correction_id FROM omp_knowledge.corrections
                        WHERE workspace_id = %s
                          AND kind = 'withdraw_evidence'
                          AND (
                              target->>'receipt_id' = %s
                              OR (target->>'payload_sha256' = %s AND %s <> '')
                          )
                        LIMIT 1
                        """,
                        (body.workspace_id, receipt_uuid_str, payload_sha, payload_sha),
                    )
                    if cur.fetchone():
                        raise HTTPException(
                            status_code=422,
                            detail={
                                "error": {
                                    "code": "evidence_binding_unverified",
                                    "message": f"supporting evidence {receipt_uuid_str} has been withdrawn",
                                }
                            },
                        )
                    cur.execute(
                        """
                        SELECT observation_id FROM omp_knowledge.observations
                        WHERE workspace_id = %s
                          AND withdrawn_by IS NOT NULL
                          AND (
                              source->>'native_validity_ref' = %s
                              OR (source->>'content_sha256' = %s AND %s <> '')
                              OR (native_payload_sha256 = %s AND %s <> '')
                          )
                        LIMIT 1
                        """,
                        (body.workspace_id, receipt_uuid_str, payload_sha, payload_sha, payload_sha, payload_sha),
                    )
                    if cur.fetchone():
                        raise HTTPException(
                            status_code=422,
                            detail={
                                "error": {
                                    "code": "evidence_binding_unverified",
                                    "message": f"supporting observation for {receipt_uuid_str} has been withdrawn",
                                }
                            },
                        )

                for obs_id in body.source_observation_ids:
                    cur.execute(
                        """
                        SELECT observation_id FROM omp_knowledge.observations
                        WHERE workspace_id = %s AND observation_id = %s AND withdrawn_by IS NOT NULL
                        LIMIT 1
                        """,
                        (body.workspace_id, obs_id),
                    )
                    if cur.fetchone():
                        raise HTTPException(
                            status_code=422,
                            detail={
                                "error": {
                                    "code": "evidence_binding_unverified",
                                    "message": f"source observation {obs_id} has been withdrawn",
                                }
                            },
                        )

            proposal = await create_proposal(
                app.state.config,
                conn,
                body,
                principal,
                app.state.engine,
            )
            return proposal.model_dump(mode="json")
        finally:
            conn.close()

    @app.get("/v1/proposals/{proposal_id}")
    async def get_proposal_by_id(
        proposal_id: UUID,
        principal: Principal = Depends(require_scope("knowledge.read")),
    ) -> dict[str, Any]:
        conn = get_db_connection(app.state.config)
        try:
            prop = get_proposal(conn, proposal_id)
            if not prop or prop.workspace_id not in principal.workspaces:
                raise HTTPException(status_code=404, detail={"error": {"code": "proposal_not_found"}})
            return prop.model_dump(mode="json")
        finally:
            conn.close()

    @app.get("/v1/proposals")
    async def list_proposals_endpoint(
        workspace_id: UUID = Query(...),
        repository_id: UUID = Query(...),
        work_id: UUID | None = Query(None),
        principal: Principal = Depends(require_scope("knowledge.read")),
    ) -> list[dict[str, Any]]:
        if workspace_id not in principal.workspaces:
            raise HTTPException(status_code=403, detail={"error": {"code": "forbidden"}})
        conn = get_db_connection(app.state.config)
        try:
            return list_proposals(conn, workspace_id, repository_id, work_id)
        finally:
            conn.close()

    @app.post("/v1/applicability")
    async def check_applicability_post(
        body: ApplicabilityRequest,
        principal: Principal = Depends(require_scope("knowledge.read")),
    ) -> dict[str, Any]:
        conn = get_db_connection(app.state.config)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT workspace_id FROM omp_knowledge.procedure_proposals WHERE proposal_id = %s",
                    (body.proposal_id,),
                )
                p_row = cur.fetchone()
                if not p_row or p_row["workspace_id"] not in principal.workspaces:
                    raise HTTPException(status_code=404, detail={"error": {"code": "proposal_not_found"}})
                ws_id = p_row["workspace_id"]

            res = evaluate_native_acceptance(
                app.state.config,
                conn,
                workspace_id=ws_id,
                actor_id=principal.actor_id,
                proposal_id=body.proposal_id,
                work_id=body.work_id,
                revision_id=body.revision_id,
                candidate_id=body.candidate_id,
                policy_id=body.policy_id,
                policy_version=body.policy_version,
            )

            check_id = uuid4()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO omp_knowledge.applicability_checks (
                        check_id, proposal_id, work_id, revision_id,
                        candidate_id, policy_id, policy_version, result,
                        native_readback_sha256
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        check_id,
                        body.proposal_id,
                        body.work_id,
                        body.revision_id,
                        body.candidate_id,
                        res.policy_id,
                        res.policy_version,
                        res.acceptance,
                        res.native_readback_sha256,
                    ),
                )
            conn.commit()
            return res.model_dump(mode="json")
        finally:
            conn.close()

    @app.get("/v1/applicability")
    async def check_applicability_get(
        proposal_id: UUID = Query(...),
        work_id: UUID = Query(...),
        revision_id: UUID = Query(...),
        candidate_id: UUID | None = Query(None),
        policy_id: str = Query("native_acceptance"),
        policy_version: int = Query(1),
        principal: Principal = Depends(require_scope("knowledge.read")),
    ) -> dict[str, Any]:
        req = ApplicabilityRequest(
            proposal_id=proposal_id,
            work_id=work_id,
            revision_id=revision_id,
            candidate_id=candidate_id,
            policy_id=policy_id,
            policy_version=policy_version,
        )
        return await check_applicability_post(req, principal)

    @app.post("/v1/context/compile")
    async def compile_context_bundle(
        body: ContextCompileRequest,
        principal: Principal = Depends(require_scope("knowledge.read")),
    ) -> dict[str, Any]:
        if body.workspace_id not in principal.workspaces:
            raise HTTPException(status_code=403, detail={"error": {"code": "forbidden"}})
        conn = get_db_connection(app.state.config)
        try:
            compiler = ByteBudgetCompiler(app.state.config, conn)
            bundle = await compiler.compile(body, principal.actor_id, app.state.engine)
            return bundle.model_dump(mode="json")
        finally:
            conn.close()

    @app.get("/v1/context/bundles/{bundle_id}")
    async def get_context_bundle_by_id(
        bundle_id: UUID,
        principal: Principal = Depends(require_scope("knowledge.read")),
    ) -> dict[str, Any]:
        conn = get_db_connection(app.state.config)
        try:
            row = select_bundle_row(conn, bundle_id)
            if not row or row["workspace_id"] not in principal.workspaces:
                raise HTTPException(status_code=404, detail={"error": {"code": "bundle_not_found"}})
            return bundle_from_row(row).model_dump(mode="json")
        finally:
            conn.close()

    @app.post("/v1/uses")
    async def record_use_endpoint(
        body: RecordUseRequest,
        principal: Principal = Depends(require_scope("knowledge.ingest")),
    ) -> dict[str, Any]:
        if body.workspace_id not in principal.workspaces:
            raise HTTPException(status_code=403, detail={"error": {"code": "forbidden"}})
        conn = get_db_connection(app.state.config)
        try:
            record = record_use(conn, body)
            return record.model_dump(mode="json")
        finally:
            conn.close()

    @app.post("/v1/uses/{use_id}/outcome")
    async def record_outcome_endpoint(
        use_id: UUID,
        body: RecordOutcomeRequest,
        principal: Principal = Depends(require_scope("knowledge.ingest")),
    ) -> dict[str, Any]:
        conn = get_db_connection(app.state.config)
        try:
            record = record_outcome(
                app.state.config,
                conn,
                use_id,
                body,
                principal.actor_id,
                allowed_workspaces=principal.workspaces,
            )
            return record.model_dump(mode="json")
        finally:
            conn.close()

    @app.get("/v1/proposals/{proposal_id}/support")
    async def get_proposal_support_endpoint(
        proposal_id: UUID,
        principal: Principal = Depends(require_scope("knowledge.read")),
    ) -> dict[str, Any]:
        conn = get_db_connection(app.state.config)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT workspace_id, invalidated_at
                    FROM omp_knowledge.procedure_proposals
                    WHERE proposal_id = %s
                    """,
                    (proposal_id,),
                )
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail={"error": {"code": "proposal_not_found"}})
                raw_ws = row["workspace_id"] if isinstance(row, dict) else row[0]
                ws_id = UUID(str(raw_ws))
                if ws_id not in principal.workspaces:
                    raise HTTPException(status_code=404, detail={"error": {"code": "proposal_not_found"}})
                inv = row["invalidated_at"] if isinstance(row, dict) else row[1]
                if inv is not None:
                    raise HTTPException(status_code=404, detail={"error": {"code": "proposal_not_found"}})

            support = compute_proposal_support(conn, proposal_id, workspace_id=ws_id)
            return support.model_dump(mode="json")
        finally:
            conn.close()

    @app.post("/v1/corrections")
    async def create_correction_endpoint(
        body: CorrectionCreateRequest,
        principal: Principal = Depends(require_scope("knowledge.admin")),
    ) -> dict[str, Any]:
        if body.workspace_id not in principal.workspaces:
            raise HTTPException(status_code=403, detail={"error": {"code": "forbidden"}})
        writer = get_or_ensure_writer(app)
        conn = get_db_connection(app.state.config)
        try:
            return await record_correction(
                app.state.config,
                conn,
                writer,
                body,
                principal,
                wrap_cleanup_engine(app.state.engine),
            )
        finally:
            conn.close()

    @app.get("/v1/corrections/{correction_id}")
    async def get_correction_endpoint(
        correction_id: UUID,
        principal: Principal = Depends(require_scope("knowledge.read")),
    ) -> dict[str, Any]:
        conn = get_db_connection(app.state.config)
        try:
            corr = get_correction(conn, correction_id)
            if not corr or UUID(corr["workspace_id"]) not in principal.workspaces:
                raise HTTPException(status_code=404, detail={"error": {"code": "correction_not_found"}})
            return corr
        finally:
            conn.close()

    @app.post("/v1/native/consume")
    async def native_consume(
        body: NativeConsumeRequest,
        principal: Principal = Depends(require_scope("knowledge.ingest")),
    ) -> dict[str, Any]:
        if body.workspace_id not in principal.workspaces:
            raise HTTPException(status_code=403, detail={"error": {"code": "forbidden"}})

        writer = get_or_ensure_writer(app)
        conn = get_db_connection(app.state.config)
        try:
            req_hash = sha256(body.model_dump(mode="json"))

            async def do_consume() -> tuple[JobState, dict[str, Any], list[str]]:
                consumer = NativeEventConsumer(
                    config=app.state.config,
                    workspace_id=body.workspace_id,
                    actor_id=principal.actor_id,
                    repository_id=body.repository_id,
                    consumer_name=body.consumer_name,
                    batch_size=body.batch_size,
                )
                res = consumer.consume_batch(conn)
                return (
                    JobState.COMPLETED,
                    res,
                    ["native_events_consumed"],
                )

            return await execute_idempotent_job_async(
                conn,
                writer=writer,
                operation_id=body.operation_id,
                workspace_id=body.workspace_id,
                repository_id=body.repository_id,
                snapshot_id=None,
                request_hash=req_hash,
                action=do_consume,
                skip_snapshot_owner_cas=True,
                skip_ingest_snapshot_owner_cas=True,
            )
        finally:
            conn.close()

    @app.get("/v1/native/checkpoint")
    async def get_native_checkpoint(
        workspace_id: UUID = Query(...),
        repository_id: UUID | None = Query(None),
        consumer_name: str = Query("native_event_consumer"),
        consumer: str | None = Query(None),
        principal: Principal = Depends(require_scope("knowledge.read")),
    ) -> dict[str, Any]:
        if workspace_id not in principal.workspaces:
            raise HTTPException(status_code=403, detail={"error": {"code": "forbidden"}})

        active_consumer_name = consumer or consumer_name
        conn = get_db_connection(app.state.config)
        try:
            c = NativeEventConsumer(
                config=app.state.config,
                workspace_id=workspace_id,
                actor_id=principal.actor_id,
                repository_id=repository_id or uuid4(),
                consumer_name=active_consumer_name,
            )
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT consumer, workspace_id, after_sequence, pending_gaps, applied_event_ids, pending_gap_metadata, updated_at
                    FROM omp_knowledge.native_event_checkpoints
                    WHERE consumer = %s
                    """,
                    (c.checkpoint_key,),
                )
                row = cur.fetchone()
                if not row:
                    return {
                        "consumer": c.checkpoint_key,
                        "workspace_id": str(workspace_id),
                        "after_sequence": 0,
                        "pending_gaps": [],
                        "applied_event_ids": [],
                        "pending_gap_metadata": {},
                        "updated_at": None,
                    }
                raw_gaps = row["pending_gaps"]
                gaps = json.loads(raw_gaps) if isinstance(raw_gaps, str) else (raw_gaps or [])
                raw_applied = row["applied_event_ids"]
                applied = json.loads(raw_applied) if isinstance(raw_applied, str) else (raw_applied or [])
                raw_meta = row["pending_gap_metadata"]
                meta = json.loads(raw_meta) if isinstance(raw_meta, str) else (raw_meta or {})
                updated_at = row["updated_at"]
                return {
                    "consumer": row["consumer"],
                    "workspace_id": str(row["workspace_id"] or workspace_id),
                    "after_sequence": int(row["after_sequence"]),
                    "pending_gaps": gaps,
                    "applied_event_ids": applied,
                    "pending_gap_metadata": meta,
                    "updated_at": updated_at.isoformat() if hasattr(updated_at, "isoformat") else str(updated_at),
                }
        finally:
            conn.close()

    return app

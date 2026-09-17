from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from typing import Any
from uuid import NAMESPACE_OID, UUID, uuid5

from fastapi import HTTPException
import psycopg

from omp_work.knowledge_contracts import FactRecord, JobState
from omp_work.v1.canonical import sha256

from ..config import KnowledgeConfig
from ..engine.protocol import KnowledgeEngine, QueryResult
from ..models import CorrectionCreateRequest, Principal
from ..native.consumer import native_readonly_session
from ..ownership import WriterOwnership
from ..storage.jobs import run_operation

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WithdrawnValidity:
    snapshot_id: str | None
    fact_ids: frozenset[str]
    receipt_ids: frozenset[str]
    payload_hashes: frozenset[str]
    correction_by_fact: Mapping[str, str]


def load_correction_rows(
    conn: psycopg.Connection,
    *,
    workspace_id: UUID | str,
    repository_id: UUID | str | None,
) -> list[dict[str, Any]]:
    """Runs SELECT correction_id, kind, target, recorded_at FROM omp_knowledge.corrections
    WHERE workspace_id=%s AND (repository_id=%s OR repository_id IS NULL)
    ORDER BY recorded_at ASC, correction_id ASC strictly inside with conn.transaction():
    never a bare cursor, so it is safe inside run_operation actions and on non-autocommit handler connections;
    normalizes target via json.loads when str; drops non-dict targets.
    """
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT correction_id, kind, target, recorded_at
                FROM omp_knowledge.corrections
                WHERE workspace_id = %s
                  AND (repository_id = %s OR repository_id IS NULL)
                ORDER BY recorded_at ASC, correction_id ASC
                """,
                (workspace_id, repository_id),
            )
            raw_rows = cur.fetchall()

    valid_rows: list[dict[str, Any]] = []
    for r in raw_rows:
        row_dict = dict(r) if isinstance(r, dict) else {
            "correction_id": r[0],
            "kind": r[1],
            "target": r[2],
            "recorded_at": r[3],
        }
        t = row_dict.get("target")
        if isinstance(t, str):
            try:
                t = json.loads(t)
            except (ValueError, TypeError):
                continue
        if not isinstance(t, dict):
            continue
        row_dict["target"] = t
        valid_rows.append(row_dict)
    return valid_rows


def build_withdrawn_validity(
    rows: list[dict[str, Any]],
    snapshot_id: str | None,
) -> WithdrawnValidity:
    """Identical semantics to the inline blocks in /v1/query and /v1/lookup:
    kind == 'withdraw_evidence' only;
    target.fact_id and target.facts count only when target.snapshot_id is None or == snapshot_id
    (plain string compare, NO validate_canonical_snapshot_id so non-canonical test ids keep working);
    receipt_id and payload_sha256 are unscoped.
    """
    withdrawn_facts: set[str] = set()
    withdrawn_receipts: set[str] = set()
    withdrawn_hashes: set[str] = set()
    correction_by_fact: dict[str, str] = {}

    for r in rows:
        if r.get("kind") != "withdraw_evidence":
            continue
        t = r.get("target")
        if isinstance(t, str):
            try:
                t = json.loads(t)
            except (ValueError, TypeError):
                continue
        if not isinstance(t, dict):
            continue

        cid_str = str(r.get("correction_id") or "")
        target_snap = t.get("snapshot_id")
        if target_snap is None or target_snap == snapshot_id:
            if t.get("fact_id"):
                fid = str(t["fact_id"])
                withdrawn_facts.add(fid)
                if cid_str:
                    correction_by_fact[fid] = cid_str
            if t.get("facts"):
                for fid in t["facts"]:
                    fid_str = str(fid)
                    withdrawn_facts.add(fid_str)
                    if cid_str:
                        correction_by_fact[fid_str] = cid_str
        if t.get("receipt_id"):
            withdrawn_receipts.add(str(t["receipt_id"]))
        if t.get("payload_sha256"):
            withdrawn_hashes.add(str(t["payload_sha256"]))

    return WithdrawnValidity(
        snapshot_id=snapshot_id,
        fact_ids=frozenset(withdrawn_facts),
        receipt_ids=frozenset(withdrawn_receipts),
        payload_hashes=frozenset(withdrawn_hashes),
        correction_by_fact=correction_by_fact,
    )


def is_fact_withdrawn(validity: WithdrawnValidity, fact: FactRecord | Any) -> bool:
    """Same predicate as server._is_fact_withdrawn:
    fact_id membership; properties withdrawn/status=='withdrawn'/withdrawn_by;
    receipt_id|native_validity_ref in receipt_ids; payload_sha256|content_sha256 in payload_hashes.
    """
    fid = getattr(fact, "fact_id", None)
    if fid is None and isinstance(fact, dict):
        fid = fact.get("fact_id") or fact.get("id")
    if fid and str(fid) in validity.fact_ids:
        return True
    props = getattr(fact, "properties", None)
    if props is None and isinstance(fact, dict):
        props = fact.get("properties") or fact.get("props")
    props = props or {}
    if props.get("withdrawn") or props.get("status") == "withdrawn" or props.get("withdrawn_by"):
        return True
    rec = str(props.get("receipt_id") or props.get("native_validity_ref") or "")
    if rec and rec in validity.receipt_ids:
        return True
    h = str(props.get("payload_sha256") or props.get("content_sha256") or "")
    if h and h in validity.payload_hashes:
        return True
    return False


def exclude_withdrawn_facts(validity: WithdrawnValidity, result: QueryResult) -> QueryResult:
    """Returns model_copy with filtered facts and total_matched=len(kept) when anything was removed,
    else the same object.
    """
    kept = tuple(f for f in result.facts if not is_fact_withdrawn(validity, f))
    if len(kept) != len(result.facts):
        return result.model_copy(update={"facts": kept, "total_matched": len(kept)})
    return result


def resolve_correction_fact_ids(
    target: dict[str, Any],
    parsed_facts: list[dict[str, Any]] | None,
) -> list[str]:
    """Ordered, de-duplicated union of str(target.fact_id), target.facts, and — only when parsed_facts is
    given — ids of facts whose props receipt_id|native_validity_ref == target.receipt_id or props
    payload_sha256|content_sha256 == target.payload_sha256. When parsed_facts is provided, fact ids absent
    from parsed_facts are excluded as resolved no-ops.
    """
    seen: set[str] = set()
    result: list[str] = []

    parsed_fact_ids = (
        {
            str(f.get("id") or f.get("fact_id"))
            for f in parsed_facts
            if (f.get("id") or f.get("fact_id")) is not None
        }
        if parsed_facts is not None
        else None
    )

    def _add(fid: Any) -> None:
        if fid is not None:
            s = str(fid)
            if parsed_fact_ids is not None and s not in parsed_fact_ids:
                return
            if s and s not in seen:
                seen.add(s)
                result.append(s)

    if target.get("fact_id"):
        _add(target["fact_id"])

    if target.get("facts"):
        for fid in target["facts"]:
            _add(fid)

    if parsed_facts is not None:
        rec_id = str(target.get("receipt_id") or "")
        p_sha = str(target.get("payload_sha256") or "")
        if rec_id or p_sha:
            for fact in parsed_facts:
                f_props = fact.get("props") or fact.get("properties") or {}
                f_rec = str(f_props.get("receipt_id") or f_props.get("native_validity_ref") or "")
                f_sha = str(f_props.get("payload_sha256") or f_props.get("content_sha256") or "")
                if (rec_id and f_rec == rec_id) or (p_sha and f_sha == p_sha):
                    fid = fact.get("id") or fact.get("fact_id")
                    if fid:
                        _add(fid)

    return result


def withdrawal_properties_update(
    kind: str,
    target: dict[str, Any],
    correction_id: UUID | str,
) -> dict[str, Any]:
    """dict(target.properties_update or {}) plus withdrawn=True, withdrawn_by=str(correction_id)
    when kind=='withdraw_evidence' and 'withdrawn' not already present.
    """
    props = dict(target.get("properties_update") or {})
    if kind == "withdraw_evidence" and "withdrawn" not in props:
        props["withdrawn"] = True
        props["withdrawn_by"] = str(correction_id)
    return props


async def reapply_corrections_to_engine(
    engine: KnowledgeEngine,
    *,
    workspace_id: UUID,
    repository_id: UUID,
    snapshot_id: str,
    corrections: list[dict[str, Any]],
    parsed_facts: list[dict[str, Any]],
) -> tuple[int, list[str]]:
    """For each row (deterministic order) whose target.snapshot_id is None or == snapshot_id,
    for each fact id from resolve_correction_fact_ids with parsed_facts, call engine.correct(...properties_update=(...));
    count success=True; collect fact ids where success is False or the call raised (actual engine failure/outage) into unapplied;
    absent target records are filtered out by resolve_correction_fact_ids as resolved no-ops;
    return (applied, sorted(set(unapplied))).
    """
    applied_count = 0
    unapplied: set[str] = set()

    for c_row in corrections:
        cid = c_row.get("correction_id")
        kind = c_row.get("kind", "")
        t = c_row.get("target")
        if isinstance(t, str):
            try:
                t = json.loads(t)
            except (ValueError, TypeError):
                continue
        if not isinstance(t, dict):
            continue

        target_snap = t.get("snapshot_id")
        if target_snap is not None and target_snap != snapshot_id:
            continue

        fact_ids = resolve_correction_fact_ids(t, parsed_facts)
        props_up = withdrawal_properties_update(kind, t, str(cid) if cid else "")

        for fid in fact_ids:
            try:
                correct_res = await engine.correct(
                    workspace_id=workspace_id,
                    repository_id=repository_id,
                    snapshot_id=snapshot_id,
                    fact_id=fid,
                    properties_update=props_up,
                )
                if getattr(correct_res, "success", True):
                    applied_count += 1
                else:
                    logger.warning("engine.correct returned success=False for fact %s during reapply", fid)
                    unapplied.add(fid)
            except Exception as exc:
                logger.warning("engine.correct failed for fact %s during reapply: %s", fid, exc)
                unapplied.add(fid)

    return applied_count, sorted(unapplied)


async def record_correction(
    config: KnowledgeConfig,
    knowledge_conn: psycopg.Connection,
    writer: WriterOwnership,
    request: CorrectionCreateRequest,
    principal: Principal,
    engine: KnowledgeEngine,
) -> dict[str, Any]:
    """Records a correction / withdrawal with immediate validity invalidation,
    triggers a durable cleanup job via run_operation and engine.correct,
    and preserves historical bundles immutable.
    Native DB is NEVER written.
    """
    # Target validation: missing / foreign targets have typed 404 / 403 behavior
    if request.kind == "supersede_proposal":
        target_proposal = request.target.get("proposal_id")
        if not target_proposal:
            raise HTTPException(status_code=422, detail={"error": {"code": "missing_target_proposal"}})
        try:
            target_uuid = UUID(str(target_proposal))
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail={"error": {"code": "invalid_proposal_id"}})

        with knowledge_conn.cursor() as cur:
            cur.execute(
                """
                SELECT workspace_id
                FROM omp_knowledge.procedure_proposals
                WHERE proposal_id = %s
                """,
                (target_uuid,),
            )
            p_row = cur.fetchone()
            if not p_row:
                raise HTTPException(status_code=404, detail={"error": {"code": "proposal_not_found"}})
            if p_row["workspace_id"] != request.workspace_id or p_row["workspace_id"] not in principal.workspaces:
                raise HTTPException(status_code=403, detail={"error": {"code": "forbidden"}})
    elif request.kind == "withdraw_evidence":
        # Check proposal if explicitly specified
        if "proposal_id" in request.target and request.target["proposal_id"]:
            try:
                p_uuid = UUID(str(request.target["proposal_id"]))
            except (ValueError, TypeError):
                raise HTTPException(status_code=422, detail={"error": {"code": "invalid_proposal_id"}})
            with knowledge_conn.cursor() as cur:
                cur.execute(
                    "SELECT workspace_id FROM omp_knowledge.procedure_proposals WHERE proposal_id = %s",
                    (p_uuid,),
                )
                p_row = cur.fetchone()
                if not p_row:
                    raise HTTPException(status_code=404, detail={"error": {"code": "proposal_not_found"}})
                if p_row["workspace_id"] != request.workspace_id or p_row["workspace_id"] not in principal.workspaces:
                    raise HTTPException(status_code=403, detail={"error": {"code": "forbidden"}})

        # Check observation if explicitly specified
        if "observation_id" in request.target and request.target["observation_id"]:
            try:
                obs_uuid = UUID(str(request.target["observation_id"]))
            except (ValueError, TypeError):
                raise HTTPException(status_code=422, detail={"error": {"code": "invalid_observation_id"}})
            with knowledge_conn.cursor() as cur:
                cur.execute(
                    "SELECT workspace_id FROM omp_knowledge.observations WHERE observation_id = %s",
                    (obs_uuid,),
                )
                o_row = cur.fetchone()
                if not o_row:
                    raise HTTPException(status_code=404, detail={"error": {"code": "observation_not_found"}})
                if o_row["workspace_id"] != request.workspace_id or o_row["workspace_id"] not in principal.workspaces:
                    raise HTTPException(status_code=403, detail={"error": {"code": "forbidden"}})

        # Check receipt if explicitly specified
        if "receipt_id" in request.target and request.target["receipt_id"]:
            try:
                r_uuid = UUID(str(request.target["receipt_id"]))
            except (ValueError, TypeError):
                raise HTTPException(status_code=422, detail={"error": {"code": "invalid_receipt_id"}})
            receipt_found = False
            try:
                with native_readonly_session(
                    config,
                    workspace_id=request.workspace_id,
                    actor_id=principal.actor_id,
                ) as native_conn:
                    with native_conn.cursor() as cur:
                        cur.execute(
                            "SELECT workspace_id FROM omp_evidence.receipts WHERE workspace_id = %s AND receipt_id = %s",
                            (request.workspace_id, r_uuid),
                        )
                        r_row = cur.fetchone()
                        if r_row:
                            if r_row["workspace_id"] == request.workspace_id and r_row["workspace_id"] in principal.workspaces:
                                receipt_found = True
                            else:
                                raise HTTPException(status_code=403, detail={"error": {"code": "forbidden"}})
            except HTTPException:
                raise
            except Exception as exc:
                logger.warning("Native readback error checking receipt foreign tenancy: %s", exc)

            if not receipt_found:
                # Also check knowledge base for evidence in current workspace
                with knowledge_conn.cursor() as cur:
                    target_hash = str(request.target.get("payload_sha256", ""))
                    cur.execute(
                        """
                        SELECT 1 FROM omp_knowledge.observations
                        WHERE workspace_id = %s AND (
                            source->>'native_validity_ref' = %s
                            OR (source->>'content_sha256' = %s AND %s <> '')
                            OR (native_payload_sha256 = %s AND %s <> '')
                        )
                        LIMIT 1
                        """,
                        (
                            request.workspace_id,
                            str(r_uuid),
                            target_hash,
                            target_hash,
                            target_hash,
                            target_hash,
                        ),
                    )
                    if cur.fetchone():
                        receipt_found = True
                    else:
                        cur.execute(
                            """
                            SELECT 1 FROM omp_knowledge.procedure_proposals
                            WHERE workspace_id = %s AND (
                                supporting_lineage @> %s::jsonb
                                OR (proposal_json->'supporting_evidence') @> %s::jsonb
                                OR (%s <> '' AND supporting_lineage @> %s::jsonb)
                                OR (%s <> '' AND (proposal_json->'supporting_evidence') @> %s::jsonb)
                            )
                            LIMIT 1
                            """,
                            (
                                request.workspace_id,
                                json.dumps([{"receipt_id": str(r_uuid)}]),
                                json.dumps([{"native_validity_ref": str(r_uuid)}]),
                                target_hash,
                                json.dumps([{"payload_sha256": target_hash}]),
                                target_hash,
                                json.dumps([{"content_sha256": target_hash}]),
                            ),
                        )
                        if cur.fetchone():
                            receipt_found = True

            if not receipt_found:
                raise HTTPException(status_code=403, detail={"error": {"code": "forbidden"}})

    req_hash = sha256(request.model_dump(mode="json"))
    correction_id = uuid5(NAMESPACE_OID, f"omp-correction:{request.workspace_id}:{req_hash}")
    cleanup_op_id = uuid5(NAMESPACE_OID, f"omp-cleanup:{correction_id}")
    now = datetime.now(timezone.utc)

    # 2. Durable cleanup action using engine.correct
    async def do_cleanup() -> tuple[JobState, dict[str, Any], list[str]]:
        try:
            with knowledge_conn.transaction():
                with knowledge_conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT target, kind, workspace_id, repository_id
                        FROM omp_knowledge.corrections
                        WHERE correction_id = %s
                        """,
                        (correction_id,),
                    )
                    c_row = cur.fetchone()

            target_to_use = c_row["target"] if c_row else request.target
            if isinstance(target_to_use, str):
                target_to_use = json.loads(target_to_use)
            if not isinstance(target_to_use, dict):
                target_to_use = {}

            kind_to_use = c_row["kind"] if c_row else request.kind
            snapshot_id = target_to_use.get("snapshot_id")
            properties_update = withdrawal_properties_update(kind_to_use, target_to_use, correction_id)

            nodes_affected = 0
            if snapshot_id:
                fact_ids = resolve_correction_fact_ids(target_to_use, parsed_facts=None)
                for fid in fact_ids:
                    correct_res = await engine.correct(
                        workspace_id=request.workspace_id,
                        repository_id=request.repository_id,
                        snapshot_id=snapshot_id,
                        fact_id=fid,
                        properties_update=properties_update,
                    )
                    if getattr(correct_res, "success", True):
                        nodes_affected += 1

            return (
                JobState.COMPLETED,
                {
                    "correction_id": str(correction_id),
                    "target": target_to_use,
                    "nodes_affected": nodes_affected,
                },
                ["derived_cleanup_completed"],
            )
        except Exception as exc:
            logger.warning("engine.correct failed during cleanup job: %s", exc)
            return (
                JobState.PARTIAL,
                {
                    "correction_id": str(correction_id),
                    "target": target_to_use if "target_to_use" in locals() else request.target,
                    "engine_status": "failed",
                    "error": str(exc),
                },
                ["derived_cleanup_failed_engine_outage", "derived_cleanup_partial"],
            )

    # Check for existing correction (deterministic duplicate/retry)
    with knowledge_conn.cursor() as cur:
        cur.execute(
            """
            SELECT correction_id, workspace_id, repository_id, kind,
                   target, reason, actor_id, actor_kind, recorded_at,
                   cleanup_operation_id
            FROM omp_knowledge.corrections
            WHERE correction_id = %s
            """,
            (correction_id,),
        )
        existing = cur.fetchone()

    if existing:
        effective_cleanup_op_id = (
            UUID(str(existing["cleanup_operation_id"]))
            if existing["cleanup_operation_id"]
            else cleanup_op_id
        )

        if existing["cleanup_operation_id"] is None:
            with knowledge_conn.transaction():
                with knowledge_conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE omp_knowledge.corrections
                        SET cleanup_operation_id = %s
                        WHERE correction_id = %s
                        """,
                        (effective_cleanup_op_id, correction_id),
                    )
        knowledge_conn.commit()

        # Inspect canonical durable job state
        with knowledge_conn.cursor() as cur:
            cur.execute(
                """
                SELECT operation_id, state, response, error, diagnostics
                FROM omp_knowledge.ingestion_jobs
                WHERE operation_id = %s
                """,
                (effective_cleanup_op_id,),
            )
            job_row = cur.fetchone()
        knowledge_conn.commit()

        # If durable cleanup job is missing or was interrupted/partial/failed, re-admit and reconcile
        if job_row is None or job_row["state"] in (
            JobState.INTERRUPTED.value,
            JobState.PARTIAL.value,
            JobState.FAILED.value,
        ):
            await run_operation(
                knowledge_conn,
                writer=writer,
                operation_id=effective_cleanup_op_id,
                workspace_id=request.workspace_id,
                repository_id=request.repository_id,
                snapshot_id=None,
                request_hash=req_hash,
                action=do_cleanup,
                skip_snapshot_owner_cas=True,
                skip_ingest_snapshot_owner_cas=True,
            )
            knowledge_conn.commit()

        # Ensure durable job exists before returning
        with knowledge_conn.cursor() as cur:
            cur.execute(
                """
                SELECT operation_id, state
                FROM omp_knowledge.ingestion_jobs
                WHERE operation_id = %s
                """,
                (effective_cleanup_op_id,),
            )
            durable_job = cur.fetchone()
        knowledge_conn.commit()
        if not durable_job:
            raise RuntimeError(f"Durable cleanup job {effective_cleanup_op_id} does not exist after retry admission")

        target_data = existing["target"]
        if isinstance(target_data, str):
            target_data = json.loads(target_data)
        return {
            "correction_id": str(existing["correction_id"]),
            "workspace_id": str(existing["workspace_id"]),
            "repository_id": str(existing["repository_id"]),
            "kind": existing["kind"],
            "target": target_data,
            "reason": existing["reason"],
            "recorded_at": existing["recorded_at"].isoformat() if hasattr(existing["recorded_at"], "isoformat") else str(existing["recorded_at"]),
            "cleanup_operation_id": str(effective_cleanup_op_id),
        }

    # 1. Invalidate derived knowledge immediately in knowledge DB
    with knowledge_conn.transaction():
        with knowledge_conn.cursor() as cur:
            # Insert corrections record
            cur.execute(
                """
                INSERT INTO omp_knowledge.corrections (
                    correction_id, workspace_id, repository_id, kind,
                    target, reason, actor_id, actor_kind, recorded_at,
                    cleanup_operation_id
                ) VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s
                )
                ON CONFLICT (correction_id) DO NOTHING
                """,
                (
                    correction_id,
                    request.workspace_id,
                    request.repository_id,
                    request.kind,
                    json.dumps(request.target),
                    request.reason,
                    principal.actor_id,
                    principal.actor_kind,
                    now,
                    cleanup_op_id,
                ),
            )

            if request.kind == "withdraw_evidence":
                target_receipt = str(request.target.get("receipt_id", ""))
                target_hash = str(request.target.get("payload_sha256", ""))
                target_obs = str(request.target.get("observation_id", ""))

                # Immediately mark observations as withdrawn
                cur.execute(
                    """
                    UPDATE omp_knowledge.observations
                    SET withdrawn_by = %s
                    WHERE workspace_id = %s
                      AND (
                          (source->>'native_validity_ref' = %s AND %s <> '')
                          OR (source->>'content_sha256' = %s AND %s <> '')
                          OR (native_payload_sha256 = %s AND %s <> '')
                          OR (observation_id::text = %s AND %s <> '')
                      )
                    """,
                    (
                        correction_id,
                        request.workspace_id,
                        target_receipt,
                        target_receipt,
                        target_hash,
                        target_hash,
                        target_hash,
                        target_hash,
                        target_obs,
                        target_obs,
                    ),
                )

                # Immediately invalidate proposals derived from this evidence
                cur.execute(
                    """
                    SELECT proposal_id, supporting_lineage, proposal_json
                    FROM omp_knowledge.procedure_proposals
                    WHERE workspace_id = %s AND invalidated_at IS NULL
                    """,
                    (request.workspace_id,),
                )
                active_proposals = cur.fetchall()

                props_to_invalidate: list[UUID] = []
                for p_row in active_proposals:
                    lineage = p_row.get("supporting_lineage") or []
                    if isinstance(lineage, str):
                        lineage = json.loads(lineage)
                    has_match = False
                    for lin in lineage:
                        if isinstance(lin, dict):
                            if target_receipt and str(lin.get("receipt_id")) == target_receipt:
                                has_match = True
                                break
                            if target_hash and lin.get("payload_sha256") == target_hash:
                                has_match = True
                                break

                    p_json = p_row.get("proposal_json") or {}
                    if isinstance(p_json, str):
                        p_json = json.loads(p_json)

                    if not has_match:
                        for ev in p_json.get("supporting_evidence", []):
                            if isinstance(ev, dict):
                                if target_receipt and str(ev.get("native_validity_ref")) == target_receipt:
                                    has_match = True
                                    break
                                if target_hash and ev.get("content_sha256") == target_hash:
                                    has_match = True
                                    break

                    if not has_match and target_obs:
                        for s_obs in p_json.get("source_observation_ids", []):
                            if str(s_obs) == target_obs:
                                has_match = True
                                break

                    if has_match:
                        props_to_invalidate.append(p_row["proposal_id"])

                if props_to_invalidate:
                    cur.execute(
                        """
                        UPDATE omp_knowledge.procedure_proposals
                        SET invalidated_at = %s,
                            invalidation_id = %s
                        WHERE proposal_id = ANY(%s)
                        """,
                        (now, correction_id, props_to_invalidate),
                    )

            elif request.kind == "supersede_proposal":
                target_proposal = request.target.get("proposal_id")
                if target_proposal:
                    target_uuid = UUID(str(target_proposal))
                    cur.execute(
                        """
                        UPDATE omp_knowledge.procedure_proposals
                        SET invalidated_at = %s,
                            invalidation_id = %s
                        WHERE proposal_id = %s AND workspace_id = %s
                        """,
                        (now, correction_id, target_uuid, request.workspace_id),
                    )

    # Immediately commit invalidation in knowledge DB
    knowledge_conn.commit()

    await run_operation(
        knowledge_conn,
        writer=writer,
        operation_id=cleanup_op_id,
        workspace_id=request.workspace_id,
        repository_id=request.repository_id,
        snapshot_id=None,
        request_hash=req_hash,
        action=do_cleanup,
        skip_snapshot_owner_cas=True,
        skip_ingest_snapshot_owner_cas=True,
    )
    knowledge_conn.commit()

    # Ensure durable job exists before returning
    with knowledge_conn.cursor() as cur:
        cur.execute(
            """
            SELECT operation_id, state
            FROM omp_knowledge.ingestion_jobs
            WHERE operation_id = %s
            """,
            (cleanup_op_id,),
        )
        durable_job = cur.fetchone()
    knowledge_conn.commit()
    if not durable_job:
        raise RuntimeError(f"Durable cleanup job {cleanup_op_id} does not exist after initial admission")

    return {
        "correction_id": str(correction_id),
        "workspace_id": str(request.workspace_id),
        "repository_id": str(request.repository_id),
        "kind": request.kind,
        "target": request.target,
        "reason": request.reason,
        "recorded_at": now.isoformat(),
        "cleanup_operation_id": str(cleanup_op_id),
    }


def get_correction(
    knowledge_conn: psycopg.Connection,
    correction_id: UUID,
) -> dict[str, Any] | None:
    with knowledge_conn.cursor() as cur:
        cur.execute(
            """
            SELECT correction_id, workspace_id, repository_id, kind,
                   target, reason, actor_id, actor_kind, recorded_at,
                   cleanup_operation_id
            FROM omp_knowledge.corrections
            WHERE correction_id = %s
            """,
            (correction_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        target_data = row["target"]
        if isinstance(target_data, str):
            target_data = json.loads(target_data)
        return {
            "correction_id": str(row["correction_id"]),
            "workspace_id": str(row["workspace_id"]),
            "repository_id": str(row["repository_id"]),
            "kind": row["kind"],
            "target": target_data,
            "reason": row["reason"],
            "actor_id": str(row["actor_id"]),
            "actor_kind": row["actor_kind"],
            "recorded_at": row["recorded_at"].isoformat() if hasattr(row["recorded_at"], "isoformat") else str(row["recorded_at"]),
            "cleanup_operation_id": str(row["cleanup_operation_id"]) if row["cleanup_operation_id"] else None,
        }

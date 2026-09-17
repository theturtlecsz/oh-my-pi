from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from typing import Any
from uuid import NAMESPACE_OID, UUID, uuid5

from fastapi import HTTPException
import psycopg

from omp_work.knowledge_contracts import (
    EvidenceLineage,
    ProcedureProposal,
    ProposalAttribution,
    ProposalState,
    SourceRef,
    compute_proposal_sha256,
)

from ..config import KnowledgeConfig
from ..engine.protocol import (
    EngineError,
    EngineTimeoutError,
    EngineUnavailableError,
    KnowledgeEngine,
)
from ..models import Principal, ProposalCreateRequest
from ..native.consumer import native_readonly_session

logger = logging.getLogger(__name__)


async def create_proposal(
    config: KnowledgeConfig,
    knowledge_conn: psycopg.Connection,
    request: ProposalCreateRequest,
    principal: Principal,
    engine: KnowledgeEngine,
) -> ProcedureProposal:
    """Creates a procedure proposal with explicit attribution and verified evidence binding."""
    # 1. Attribution from authenticated principal
    attribution = ProposalAttribution(
        actor_id=principal.actor_id,
        actor_kind=principal.actor_kind,
        generator=request.generator,
    )

    # 2. Check supporting evidence has required citations
    for ev in request.supporting_evidence:
        if not ev.content_sha256 or not ev.native_validity_ref:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": {
                        "code": "evidence_binding_unverified",
                        "message": "each supporting evidence item must carry content_sha256 and native_validity_ref",
                    }
                },
            )

    # 3. Read authoritative native receipts to verify evidence binding
    verified_lineages: list[EvidenceLineage] = []
    try:
        with native_readonly_session(
            config,
            workspace_id=request.workspace_id,
            actor_id=principal.actor_id,
        ) as native_conn:
            with native_conn.cursor() as cur:
                for ev in request.supporting_evidence:
                    try:
                        receipt_uuid = UUID(ev.native_validity_ref)
                    except (ValueError, TypeError):
                        continue

                    cur.execute(
                        """
                        SELECT receipt_id, payload_sha256, work_id, revision_id, candidate_id
                        FROM omp_evidence.receipts
                        WHERE workspace_id = %s
                          AND receipt_id = %s
                          AND payload_sha256 = %s
                        """,
                        (request.workspace_id, receipt_uuid, ev.content_sha256),
                    )
                    row = cur.fetchone()
                    if row:
                        verified_lineages.append(
                            EvidenceLineage(
                                receipt_id=row["receipt_id"],
                                payload_sha256=row["payload_sha256"],
                                work_id=row["work_id"],
                                revision_id=row["revision_id"],
                                candidate_id=row["candidate_id"],
                            )
                        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Native readback unavailable during proposal creation: %s", exc)
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "code": "native_unavailable",
                    "message": "authoritative native readback unavailable",
                }
            },
        )

    if not verified_lineages:
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "code": "evidence_binding_unverified",
                    "message": "supporting evidence could not be verified against native receipts",
                }
            },
        )

    evidence_binding = "verified"

    # 4. Optional engine enrichment: degradation does not block creation
    enrichment_status = "unavailable"
    active_snapshot_id: str | None = None
    with knowledge_conn.cursor() as cur:
        cur.execute(
            """
            SELECT snapshot_id
            FROM omp_knowledge.snapshot_publications
            WHERE workspace_id = %s AND repository_id = %s AND status = 'published'
            ORDER BY published_at DESC NULLS LAST, staged_at DESC
            LIMIT 1
            """,
            (request.workspace_id, request.repository_id),
        )
        s_row = cur.fetchone()
        if s_row:
            active_snapshot_id = s_row["snapshot_id"]

    try:
        status = await engine.status()
        if not status.available:
            enrichment_status = "degraded"
        elif active_snapshot_id:
            await engine.query(
                workspace_id=request.workspace_id,
                repository_id=request.repository_id,
                snapshot_id=active_snapshot_id,
                query_text=request.title,
                limit=5,
            )
            enrichment_status = "applied"
        else:
            enrichment_status = "unavailable"
    except (EngineUnavailableError, EngineTimeoutError, EngineError, RuntimeError) as exc:
        logger.info("Optional enrichment failed during proposal creation: %s", exc)
        enrichment_status = "degraded"

    # 5. Compute deterministic proposal identity
    now = datetime.now(timezone.utc)
    proposal_dict: dict[str, Any] = {
        "workspace_id": str(request.workspace_id),
        "repository_id": str(request.repository_id),
        "title": request.title,
        "preconditions": list(request.preconditions),
        "steps": list(request.steps),
        "expected_observations": list(request.expected_observations),
        "limits": list(request.limits),
        "applicability_scope": request.applicability_scope,
        "source_observation_ids": [str(x) for x in request.source_observation_ids],
        "supporting_evidence": [e.model_dump(mode="json") for e in request.supporting_evidence],
        "attribution": attribution.model_dump(mode="json"),
        "evidence_binding": evidence_binding,
        "enrichment_status": enrichment_status,
        "supporting_lineage": [lin.model_dump(mode="json") for lin in verified_lineages],
    }

    prop_hash = compute_proposal_sha256(proposal_dict)
    prop_id = uuid5(NAMESPACE_OID, f"omp-proposal:{request.workspace_id}:{prop_hash}")

    proposal = ProcedureProposal(
        proposal_id=prop_id,
        workspace_id=request.workspace_id,
        repository_id=request.repository_id,
        state=ProposalState.PROPOSAL,
        source_observation_ids=request.source_observation_ids,
        supporting_evidence=request.supporting_evidence,
        title=request.title,
        preconditions=request.preconditions,
        steps=request.steps,
        expected_observations=request.expected_observations,
        limits=request.limits,
        applicability_scope=request.applicability_scope,
        proposal_sha256=prop_hash,
        derived_at=now,
        attribution=attribution,
        evidence_binding=evidence_binding,
        enrichment_status=enrichment_status,
        supporting_lineage=tuple(verified_lineages),
    )

    # 6. Store in knowledge DB
    with knowledge_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO omp_knowledge.procedure_proposals (
                proposal_id, workspace_id, repository_id, proposal_json,
                proposal_sha256, state, attribution, evidence_binding,
                enrichment_status, supporting_lineage, created_at
            ) VALUES (
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s
            )
            ON CONFLICT (proposal_id) DO UPDATE
                SET proposal_json = EXCLUDED.proposal_json,
                    attribution = EXCLUDED.attribution,
                    enrichment_status = EXCLUDED.enrichment_status
            """,
            (
                prop_id,
                request.workspace_id,
                request.repository_id,
                json.dumps(proposal.model_dump(mode="json")),
                prop_hash,
                ProposalState.PROPOSAL.value,
                json.dumps(attribution.model_dump(mode="json")),
                evidence_binding,
                enrichment_status,
                json.dumps([lin.model_dump(mode="json") for lin in verified_lineages]),
                now,
            ),
        )
    knowledge_conn.commit()
    return proposal


def get_proposal(
    knowledge_conn: psycopg.Connection,
    proposal_id: UUID,
    workspace_id: UUID | None = None,
) -> ProcedureProposal | None:
    with knowledge_conn.cursor() as cur:
        if workspace_id is not None:
            cur.execute(
                """
                SELECT proposal_json, invalidated_at
                FROM omp_knowledge.procedure_proposals
                WHERE proposal_id = %s AND workspace_id = %s
                """,
                (proposal_id, workspace_id),
            )
        else:
            cur.execute(
                """
                SELECT proposal_json, invalidated_at
                FROM omp_knowledge.procedure_proposals
                WHERE proposal_id = %s
                """,
                (proposal_id,),
            )
        row = cur.fetchone()
        if not row:
            return None
        if row["invalidated_at"] is not None:
            return None
        return ProcedureProposal.model_validate(row["proposal_json"])


def list_proposals(
    knowledge_conn: psycopg.Connection,
    workspace_id: UUID,
    repository_id: UUID,
    work_id: UUID | None = None,
) -> list[dict[str, Any]]:
    """List non-invalidated proposals. Allows source/target work ID equality."""
    with knowledge_conn.cursor() as cur:
        cur.execute(
            """
            SELECT proposal_id, proposal_json
            FROM omp_knowledge.procedure_proposals
            WHERE workspace_id = %s
              AND repository_id = %s
              AND invalidated_at IS NULL
            ORDER BY created_at DESC
            """,
            (workspace_id, repository_id),
        )
        rows = cur.fetchall()

    results: list[dict[str, Any]] = []
    for r in rows:
        data = dict(r["proposal_json"])
        # Check if proposal's origin matches the queried work_id
        origin_work_id: str | None = None
        for ev in data.get("supporting_evidence", []):
            if ev.get("work_id"):
                origin_work_id = str(ev["work_id"])
                break
        same_source = bool(work_id and origin_work_id and str(work_id) == origin_work_id)
        data["same_source_task"] = same_source
        results.append(data)

    return results

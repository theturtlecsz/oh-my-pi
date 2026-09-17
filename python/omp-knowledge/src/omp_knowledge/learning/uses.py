from __future__ import annotations

from collections.abc import Collection
from datetime import datetime, timezone
import json
import logging
from typing import Any
from uuid import NAMESPACE_OID, UUID, uuid5

from fastapi import HTTPException
import psycopg

from omp_work.knowledge_contracts import (
    KnowledgeUseOutcome,
    KnowledgeUseRecord,
    ProposalSupportSummary,
)

from ..config import KnowledgeConfig
from ..models import RecordOutcomeRequest, RecordUseRequest
from ..native.consumer import native_readonly_session

logger = logging.getLogger(__name__)


def record_use(
    knowledge_conn: psycopg.Connection,
    request: RecordUseRequest,
) -> KnowledgeUseRecord:
    """Records that a proposal was supplied to and bound to a context bundle for a task."""
    use_id = uuid5(
        NAMESPACE_OID,
        f"omp-use:{request.proposal_id}:{request.task_work_id}:{request.task_revision_id}:{request.task_candidate_id}:{request.bundle_id}",
    )
    now = datetime.now(timezone.utc)

    with knowledge_conn.cursor() as cur:
        # Validate proposal exists, workspace and repository match, and proposal is not invalidated
        cur.execute(
            """
            SELECT workspace_id, repository_id, invalidated_at
            FROM omp_knowledge.procedure_proposals
            WHERE proposal_id = %s
            """,
            (request.proposal_id,),
        )
        prop_row = cur.fetchone()
        if not prop_row:
            raise HTTPException(status_code=404, detail={"error": {"code": "proposal_not_found"}})
        if prop_row["workspace_id"] != request.workspace_id:
            raise HTTPException(status_code=403, detail={"error": {"code": "forbidden", "message": "proposal workspace mismatch"}})
        if prop_row["repository_id"] != request.repository_id:
            raise HTTPException(status_code=403, detail={"error": {"code": "forbidden", "message": "proposal repository mismatch"}})
        if prop_row["invalidated_at"] is not None:
            raise HTTPException(
                status_code=422,
                detail={"error": {"code": "proposal_invalidated", "message": "proposal has been invalidated"}},
            )

        # Validate bundle exists, workspace and repository match, work/revision/candidate lineage matches, and proposal is in proposal_lineage
        cur.execute(
            """
            SELECT workspace_id, repository_id, work_id, revision_id, candidate_id, proposal_lineage
            FROM omp_knowledge.context_bundles
            WHERE bundle_id = %s
            """,
            (request.bundle_id,),
        )
        bundle_row = cur.fetchone()
        if not bundle_row:
            raise HTTPException(status_code=404, detail={"error": {"code": "bundle_not_found"}})
        if bundle_row["workspace_id"] != request.workspace_id:
            raise HTTPException(status_code=403, detail={"error": {"code": "forbidden", "message": "bundle workspace mismatch"}})
        if bundle_row["repository_id"] != request.repository_id:
            raise HTTPException(status_code=403, detail={"error": {"code": "forbidden", "message": "bundle repository mismatch"}})
        if (
            bundle_row["work_id"] != request.task_work_id
            or bundle_row["revision_id"] != request.task_revision_id
            or bundle_row["candidate_id"] != request.task_candidate_id
        ):
            raise HTTPException(
                status_code=422,
                detail={"error": {"code": "bundle_lineage_mismatch", "message": "bundle work/revision does not match task"}},
            )

        raw_lineage = bundle_row["proposal_lineage"] or []
        if isinstance(raw_lineage, str):
            raw_lineage = json.loads(raw_lineage)
        lineage_proposal_ids: set[UUID] = set()
        if isinstance(raw_lineage, (list, tuple)):
            for p in raw_lineage:
                try:
                    lineage_proposal_ids.add(UUID(str(p)))
                except (ValueError, TypeError):
                    continue
        if request.proposal_id not in lineage_proposal_ids:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": {
                        "code": "proposal_not_in_bundle_lineage",
                        "message": "proposal is not in bundle proposal lineage",
                    }
                },
            )

        cur.execute(
            """
            INSERT INTO omp_knowledge.knowledge_uses (
                use_id, proposal_id, workspace_id, repository_id,
                task_work_id, task_revision_id, task_candidate_id,
                bundle_id, worker_used, outcome, supplied_at
            ) VALUES (
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, false, %s, %s
            )
            ON CONFLICT (use_id)
            DO UPDATE SET supplied_at = EXCLUDED.supplied_at
            RETURNING use_id, proposal_id, workspace_id, repository_id,
                      task_work_id, task_revision_id, task_candidate_id,
                      bundle_id, worker_used, outcome, outcome_receipt_id,
                      outcome_receipt_sha256, outcome_lineage_key,
                      outcome_recorded_at, supplied_at, independent
            """,
            (
                use_id,
                request.proposal_id,
                request.workspace_id,
                request.repository_id,
                request.task_work_id,
                request.task_revision_id,
                request.task_candidate_id,
                request.bundle_id,
                KnowledgeUseOutcome.NOT_EVALUATED.value,
                now,
            ),
        )
        row = cur.fetchone()
    knowledge_conn.commit()

    return KnowledgeUseRecord(
        use_id=row["use_id"],
        proposal_id=row["proposal_id"],
        workspace_id=row["workspace_id"],
        repository_id=row["repository_id"],
        task_work_id=row["task_work_id"],
        task_revision_id=row["task_revision_id"],
        task_candidate_id=row["task_candidate_id"],
        bundle_id=row["bundle_id"],
        supplied_at=row["supplied_at"],
        worker_used=row["worker_used"],
        outcome=KnowledgeUseOutcome(row["outcome"]),
        outcome_receipt_id=row["outcome_receipt_id"],
        outcome_receipt_sha256=row["outcome_receipt_sha256"],
        outcome_lineage_key=row["outcome_lineage_key"],
        outcome_recorded_at=row["outcome_recorded_at"],
        independent=row["independent"],
        recorded_at=row["supplied_at"],
    )


def record_outcome(
    config: KnowledgeConfig,
    knowledge_conn: psycopg.Connection,
    use_id: UUID,
    request: RecordOutcomeRequest,
    actor_id: UUID,
    allowed_workspaces: Collection[UUID] | None = None,
) -> KnowledgeUseRecord:
    """Records outcome from authoritative native receipt readback. Write-once."""
    # 1. Fetch existing use record
    with knowledge_conn.cursor() as cur:
        cur.execute(
            """
            SELECT use_id, proposal_id, workspace_id, repository_id,
                   task_work_id, task_revision_id, task_candidate_id,
                   bundle_id, outcome_recorded_at, supplied_at
            FROM omp_knowledge.knowledge_uses
            WHERE use_id = %s
            """,
            (use_id,),
        )
        use_row = cur.fetchone()

    if not use_row or (allowed_workspaces is not None and use_row["workspace_id"] not in allowed_workspaces):
        raise HTTPException(status_code=404, detail={"error": {"code": "use_record_not_found"}})

    if use_row["outcome_recorded_at"] is not None:
        raise HTTPException(
            status_code=409,
            detail={"error": {"code": "outcome_already_recorded", "message": "outcome is write-once and already recorded"}},
        )

    # 2. Authoritative native readback of exact receipt
    try:
        with native_readonly_session(
            config,
            workspace_id=use_row["workspace_id"],
            actor_id=actor_id,
        ) as native_conn:
            with native_conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT receipt_id, work_id, revision_id, candidate_id,
                           kind, verdict, independent, issuer, payload_sha256
                    FROM omp_evidence.receipts
                    WHERE workspace_id = %s AND receipt_id = %s
                    """,
                    (use_row["workspace_id"], request.outcome_receipt_id),
                )
                receipt_row = cur.fetchone()
    except Exception as exc:
        logger.warning("Error reading receipt from native DB: %s", exc)
        raise HTTPException(
            status_code=503,
            detail={"error": {"code": "native_unavailable", "message": "native readback unavailable"}},
        )

    if not receipt_row:
        raise HTTPException(
            status_code=422,
            detail={"error": {"code": "receipt_not_found", "message": "outcome receipt not found in native ledger"}},
        )

    # Verify task identity matches receipt
    if (
        receipt_row["work_id"] != use_row["task_work_id"]
        or receipt_row["revision_id"] != use_row["task_revision_id"]
        or receipt_row["candidate_id"] != use_row["task_candidate_id"]
    ):
        raise HTTPException(
            status_code=422,
            detail={"error": {"code": "receipt_task_mismatch", "message": "receipt does not match task identity"}},
        )

    # Derive outcome from exact native receipt
    kind = receipt_row.get("kind")
    verdict = receipt_row.get("verdict")
    independent = bool(receipt_row.get("independent", False))
    issuer = receipt_row.get("issuer", "")

    if kind == "audit" and verdict == "PASS" and independent and issuer == "work-service/auditor-settle":
        outcome = KnowledgeUseOutcome.SUCCESS
    elif verdict in ("NEEDS_FIX", "BLOCKED"):
        outcome = KnowledgeUseOutcome.FAILURE
    else:
        raise HTTPException(
            status_code=422,
            detail={"error": {"code": "receipt_not_outcome_grade", "message": "receipt is not outcome grade"}},
        )

    outcome_lineage_key = receipt_row["payload_sha256"]
    outcome_receipt_sha256 = receipt_row["payload_sha256"]
    now = datetime.now(timezone.utc)

    # 3. Write-once update
    with knowledge_conn.cursor() as cur:
        cur.execute(
            """
            UPDATE omp_knowledge.knowledge_uses
            SET worker_used = %s,
                outcome = %s,
                outcome_receipt_id = %s,
                outcome_receipt_sha256 = %s,
                outcome_lineage_key = %s,
                outcome_recorded_at = %s,
                independent = %s
            WHERE use_id = %s AND outcome_recorded_at IS NULL
            RETURNING use_id, proposal_id, workspace_id, repository_id,
                      task_work_id, task_revision_id, task_candidate_id,
                      bundle_id, worker_used, outcome, outcome_receipt_id,
                      outcome_receipt_sha256, outcome_lineage_key,
                      outcome_recorded_at, supplied_at, independent
            """,
            (
                request.worker_used,
                outcome.value,
                request.outcome_receipt_id,
                outcome_receipt_sha256,
                outcome_lineage_key,
                now,
                independent,
                use_id,
            ),
        )
        updated_row = cur.fetchone()
        if not updated_row:
            raise HTTPException(
                status_code=409,
                detail={"error": {"code": "outcome_already_recorded", "message": "outcome already recorded"}},
            )
    knowledge_conn.commit()

    return KnowledgeUseRecord(
        use_id=updated_row["use_id"],
        proposal_id=updated_row["proposal_id"],
        workspace_id=updated_row["workspace_id"],
        repository_id=updated_row["repository_id"],
        task_work_id=updated_row["task_work_id"],
        task_revision_id=updated_row["task_revision_id"],
        task_candidate_id=updated_row["task_candidate_id"],
        bundle_id=updated_row["bundle_id"],
        supplied_at=updated_row["supplied_at"],
        worker_used=updated_row["worker_used"],
        outcome=KnowledgeUseOutcome(updated_row["outcome"]),
        outcome_receipt_id=updated_row["outcome_receipt_id"],
        outcome_receipt_sha256=updated_row["outcome_receipt_sha256"],
        outcome_lineage_key=updated_row["outcome_lineage_key"],
        outcome_recorded_at=updated_row["outcome_recorded_at"],
        independent=updated_row["independent"],
        recorded_at=updated_row["outcome_recorded_at"],
    )


def compute_proposal_support(
    knowledge_conn: psycopg.Connection,
    proposal_id: UUID,
    workspace_id: UUID | None = None,
) -> ProposalSupportSummary:
    """Computes support summary for a proposal.
    Independent support is separate and deduplicated by primary evidence lineage, not task ID.
    """
    with knowledge_conn.cursor() as cur:
        if workspace_id is not None:
            cur.execute(
                """
                SELECT proposal_json, supporting_lineage
                FROM omp_knowledge.procedure_proposals
                WHERE proposal_id = %s AND workspace_id = %s
                """,
                (proposal_id, workspace_id),
            )
        else:
            cur.execute(
                """
                SELECT proposal_json, supporting_lineage
                FROM omp_knowledge.procedure_proposals
                WHERE proposal_id = %s
                """,
                (proposal_id,),
            )
        prop_row = cur.fetchone()
        if not prop_row:
            raise HTTPException(status_code=404, detail={"error": {"code": "proposal_not_found"}})

        # Extract proposal source work IDs and supporting lineage
        supporting_lineage = prop_row.get("supporting_lineage") or []
        if isinstance(supporting_lineage, str):
            supporting_lineage = json.loads(supporting_lineage)

        source_work_ids: set[str] = set()
        source_candidate_ids: set[str] = set()
        source_receipt_ids: set[str] = set()

        for item in supporting_lineage:
            if isinstance(item, dict):
                if item.get("work_id"):
                    source_work_ids.add(str(item["work_id"]))
                if item.get("candidate_id"):
                    source_candidate_ids.add(str(item["candidate_id"]))
                if item.get("receipt_id"):
                    source_receipt_ids.add(str(item["receipt_id"]))

        prop_json = prop_row.get("proposal_json") or {}
        if isinstance(prop_json, str):
            prop_json = json.loads(prop_json)

        for ev in prop_json.get("supporting_evidence", []):
            if isinstance(ev, dict):
                if ev.get("work_id"):
                    source_work_ids.add(str(ev["work_id"]))
                if ev.get("candidate_id"):
                    source_candidate_ids.add(str(ev["candidate_id"]))
                if ev.get("native_validity_ref"):
                    source_receipt_ids.add(str(ev["native_validity_ref"]))

        # Query all successful uses with recorded outcomes
        if workspace_id is not None:
            cur.execute(
                """
                SELECT use_id, task_work_id, task_revision_id, task_candidate_id,
                       outcome_receipt_id, outcome_lineage_key, independent
                FROM omp_knowledge.knowledge_uses
                WHERE proposal_id = %s AND workspace_id = %s AND outcome_recorded_at IS NOT NULL AND outcome = 'success'
                """,
                (proposal_id, workspace_id),
            )
        else:
            cur.execute(
                """
                SELECT use_id, task_work_id, task_revision_id, task_candidate_id,
                       outcome_receipt_id, outcome_lineage_key, independent
                FROM omp_knowledge.knowledge_uses
                WHERE proposal_id = %s AND outcome_recorded_at IS NOT NULL AND outcome = 'success'
                """,
                (proposal_id,),
            )
        use_rows = cur.fetchall()

    total_uses = len(use_rows)
    distinct_lineage_keys: set[str] = set()
    independent_lineage_keys: set[str] = set()
    same_source_task_uses = 0

    for u in use_rows:
        task_w = str(u["task_work_id"])
        cand_str = str(u["task_candidate_id"]) if u.get("task_candidate_id") else None
        receipt_str = str(u["outcome_receipt_id"]) if u.get("outcome_receipt_id") else None

        is_same_source = (
            task_w in source_work_ids
            or (cand_str is not None and cand_str in source_candidate_ids)
            or (receipt_str is not None and receipt_str in source_receipt_ids)
        )

        if task_w in source_work_ids:
            same_source_task_uses += 1

        l_key = u["outcome_lineage_key"]
        if not l_key:
            continue
        distinct_lineage_keys.add(l_key)

        # Independence invariant:
        # 1. Native receipt was marked independent
        # 2. Not from same source work/candidate/receipt lineage
        if bool(u["independent"]) and not is_same_source:
            independent_lineage_keys.add(l_key)

    return ProposalSupportSummary(
        proposal_id=proposal_id,
        uses=total_uses,
        distinct_lineages=len(distinct_lineage_keys),
        independent_support=len(independent_lineage_keys),
        same_source_task_uses=same_source_task_uses,
        lineage_keys=tuple(sorted(distinct_lineage_keys)),
    )

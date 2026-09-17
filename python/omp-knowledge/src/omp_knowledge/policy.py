from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import psycopg

from omp_work.knowledge_contracts import ApplicabilityResult

from .config import KnowledgeConfig
from .native.consumer import native_readonly_session

logger = logging.getLogger(__name__)


def get_policy_definition(
    knowledge_conn: psycopg.Connection,
    policy_id: str,
    version: int,
) -> dict[str, Any] | None:
    with knowledge_conn.cursor() as cur:
        cur.execute(
            """
            SELECT definition, active
            FROM omp_knowledge.policy_registry
            WHERE policy_id = %s AND version = %s
            """,
            (policy_id, version),
        )
        row = cur.fetchone()
        if not row or not row["active"]:
            return None
        return row["definition"]


def evaluate_native_acceptance(
    config: KnowledgeConfig,
    knowledge_conn: psycopg.Connection,
    *,
    workspace_id: UUID,
    actor_id: UUID,
    proposal_id: UUID,
    work_id: UUID,
    revision_id: UUID,
    candidate_id: UUID | None,
    policy_id: str = "native_acceptance",
    policy_version: int = 1,
) -> ApplicabilityResult:
    """Evaluates applicability of a proposal against authoritative native readback.
    Native validity is citation only; Cognee/model confidence cannot grant acceptance.
    Applicability accepted ONLY from authoritative native readback of exact
    work/revision/candidate receipt and independent auditor-settle evidence.
    """
    if isinstance(workspace_id, str):
        workspace_id = UUID(workspace_id)
    if isinstance(actor_id, str):
        actor_id = UUID(actor_id)
    if isinstance(proposal_id, str):
        proposal_id = UUID(proposal_id)
    if isinstance(work_id, str):
        work_id = UUID(work_id)
    if isinstance(revision_id, str):
        revision_id = UUID(revision_id)
    if isinstance(candidate_id, str):
        candidate_id = UUID(candidate_id) if candidate_id else None

    # 1. Check if proposal is invalidated in knowledge store
    with knowledge_conn.cursor() as cur:
        cur.execute(
            """
            SELECT invalidated_at
            FROM omp_knowledge.procedure_proposals
            WHERE proposal_id = %s AND workspace_id = %s
            """,
            (proposal_id, workspace_id),
        )
        prop_row = cur.fetchone()
        if prop_row and prop_row["invalidated_at"] is not None:
            return ApplicabilityResult(
                policy_id=policy_id,
                policy_version=policy_version,
                acceptance="not_accepted",
                reasons=("proposal_invalidated",),
            )

    # 2. Lookup policy definition
    policy_def = get_policy_definition(knowledge_conn, policy_id, policy_version)
    if policy_def is None:
        return ApplicabilityResult(
            policy_id=policy_id,
            policy_version=policy_version,
            acceptance="not_accepted",
            reasons=(f"policy_{policy_id}_v{policy_version}_not_found",),
        )

    # 3. Connect to native DB via read-only session
    try:
        with native_readonly_session(config, workspace_id=workspace_id, actor_id=actor_id) as native_conn:
            with native_conn.cursor() as cur:
                # Read work item state and current revision / candidate
                cur.execute(
                    """
                    SELECT current_revision_id, current_candidate_id, state
                    FROM omp_work.work_items
                    WHERE workspace_id = %s AND work_id = %s
                    """,
                    (workspace_id, work_id),
                )
                work_row = cur.fetchone()
                if not work_row:
                    return ApplicabilityResult(
                        policy_id=policy_id,
                        policy_version=policy_version,
                        acceptance="not_accepted",
                        reasons=("work_item_not_found",),
                    )

                current_rev = work_row["current_revision_id"]
                current_cand = work_row["current_candidate_id"]
                if isinstance(current_rev, str):
                    current_rev = UUID(current_rev)
                if isinstance(current_cand, str):
                    current_cand = UUID(current_cand)

                rev_mismatch = current_rev != revision_id
                cand_mismatch = current_cand != candidate_id

                if rev_mismatch or cand_mismatch:
                    return ApplicabilityResult(
                        policy_id=policy_id,
                        policy_version=policy_version,
                        acceptance="superseded_revision",
                        reasons=("current_revision_or_candidate_differs",),
                    )

                # Authoritative receipt query: kind='audit', verdict='PASS', independent=true, issuer='work-service/auditor-settle'
                cur.execute(
                    """
                    SELECT receipt_id, payload_sha256
                    FROM omp_evidence.receipts
                    WHERE workspace_id = %s
                      AND work_id = %s
                      AND revision_id = %s
                      AND candidate_id IS NOT DISTINCT FROM %s
                      AND kind = 'audit'
                      AND verdict = 'PASS'
                      AND independent = true
                      AND issuer = 'work-service/auditor-settle'
                    ORDER BY issued_at DESC
                    LIMIT 1
                    """,
                    (workspace_id, work_id, revision_id, candidate_id),
                )
                receipt_row = cur.fetchone()
                if receipt_row:
                    return ApplicabilityResult(
                        policy_id=policy_id,
                        policy_version=policy_version,
                        acceptance="accepted",
                        reasons=("authoritative_pass_receipt_verified",),
                        native_readback_sha256=receipt_row["payload_sha256"],
                    )
                else:
                    return ApplicabilityResult(
                        policy_id=policy_id,
                        policy_version=policy_version,
                        acceptance="not_accepted",
                        reasons=("no_qualifying_audit_receipt",),
                    )

    except Exception as exc:
        logger.warning("Native readback failure in evaluate_native_acceptance: %s", exc)
        return ApplicabilityResult(
            policy_id=policy_id,
            policy_version=policy_version,
            acceptance="unverifiable",
            reasons=("native_source_unavailable",),
        )

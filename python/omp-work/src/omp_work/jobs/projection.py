"""Execution projection and settled-trial gate for native research jobs (R03, OMP-324).

Projects a research trial's execution state from its native jobs in omp_jobs.
Determines status from the trial's latest root native job (parent_job_id NULL),
mapping to the five R02 trial statuses: queued, running, succeeded, failed, cancelled.
Gating requires status succeeded, all trial jobs terminal, and trusted audit + release
receipts issued by components in the campaign's compatibility manifest.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import psycopg

from omp_work.contracts.r02.validate import assert_execution_status_not_scientific_success
from omp_work.v1.store_shared import row_json

_TERMINAL_STATUSES = frozenset({"sealed", "failed", "cancelled"})

_JOB_FIELDS = (
    "job_id,idempotency_key,status,source,provider_partition,path_lease,"
    "expected_max,mission_id,depends_on,packet_path,blocker,flood_origin,"
    "gate_evidence,created_at,updated_at,mirror_namespace,mirror_tombstoned_at,"
    "workspace_id,work_id,trial_id,parent_job_id,kind,required_capabilities,"
    "resources,lease_seconds,fence,attempt,worker_id,lease_expires_at,"
    "settlement,settled_at,cancel_reason,cancelled_at"
)


def _json_dict(value: object) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else None
        except ValueError:
            return None
    return None


def _has_trial_jobs(cur: psycopg.Cursor[dict[str, object]]) -> bool:
    cur.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM pg_attribute
            WHERE attrelid = to_regclass('omp_jobs.jobs')
              AND attname = 'trial_id'
              AND NOT attisdropped
        ) AS present
        """
    )
    row = cur.fetchone()
    return bool(row and row["present"])


def _add_reason(reasons: list[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)


def trial_execution(
    cur: psycopg.Cursor[dict[str, object]],
    workspace_id: UUID | str,
    trial_id: UUID | str,
) -> dict[str, object]:
    """Execution projection and eligibility of one trial from its native jobs.

    Returns ``{trial_id, status, jobs, eligible, reasons}``.
    Cursor must have RLS context set (or be an app cursor).
    """
    trial_id_str = str(trial_id)
    ws_uuid = UUID(str(workspace_id))
    trial_uuid = UUID(trial_id_str)

    if not _has_trial_jobs(cur):
        return {
            "trial_id": trial_id_str,
            "status": "queued",
            "jobs": [],
            "eligible": False,
            "reasons": ["no_job"],
        }

    cur.execute(
        f"""
        SELECT {_JOB_FIELDS}
        FROM omp_jobs.jobs
        WHERE workspace_id = %s AND trial_id = %s AND source = 'native'
        ORDER BY created_at ASC, job_id ASC
        """,  # nosec B608 - static column list
        (ws_uuid, trial_uuid),
    )
    job_rows = list(cur.fetchall())

    if not job_rows:
        return {
            "trial_id": trial_id_str,
            "status": "queued",
            "jobs": [],
            "eligible": False,
            "reasons": ["no_job"],
        }

    jobs = [row_json(row) for row in job_rows]

    # Status from latest native job with parent_job_id NULL
    root_jobs = [j for j in job_rows if j["parent_job_id"] is None]
    root_job = root_jobs[-1] if root_jobs else None

    if root_job is None:
        status = "queued"
    else:
        root_status = str(root_job["status"])
        if root_status == "backlog":
            status = "queued"
        elif root_status in ("admitted", "in_flight", "returned", "checking"):
            status = "running"
        elif root_status == "sealed":
            status = "succeeded"
        elif root_status == "failed":
            status = "failed"
        elif root_status == "cancelled":
            status = "cancelled"
        else:
            status = "queued"

    reasons: list[str] = []

    # 1. status succeeded
    if status != "succeeded":
        _add_reason(reasons, status)

    # 2. all trial jobs terminal (unsettled_jobs)
    if any(str(j["status"]) not in _TERMINAL_STATUSES for j in job_rows):
        _add_reason(reasons, "unsettled_jobs")

    # 3 & 4. Root settlement receipts (only checked when status is succeeded)
    if status == "succeeded":
        root_settlement = _json_dict(root_job.get("settlement") if root_job else None)
        if root_settlement is None:
            _add_reason(reasons, "missing_audit_receipt")
            _add_reason(reasons, "missing_release_receipt")
        else:
            raw_receipts = root_settlement.get("receipts")
            receipts = raw_receipts if isinstance(raw_receipts, list) else []

            audit_receipts = [
                r for r in receipts
                if isinstance(r, dict) and r.get("role") == "audit"
            ]
            release_receipts = [
                r for r in receipts
                if isinstance(r, dict) and r.get("role") == "release"
            ]

            if not audit_receipts:
                _add_reason(reasons, "missing_audit_receipt")
            if not release_receipts:
                _add_reason(reasons, "missing_release_receipt")

            # Fetch campaign compatibility manifest
            cur.execute(
                """
                SELECT c.compatibility
                FROM omp_research.trials t
                JOIN omp_research.campaigns c
                  ON c.workspace_id = t.workspace_id AND c.campaign_id = t.campaign_id
                WHERE t.workspace_id = %s AND t.trial_id = %s
                """,
                (ws_uuid, trial_uuid),
            )
            camp_row = cur.fetchone()
            compat = _json_dict(camp_row.get("compatibility") if camp_row else None) or {}

            manifest_audits = set(compat.get("audits", []))
            manifest_releases = set(compat.get("releases", []))

            for r in audit_receipts:
                evidence = _json_dict(r.get("evidence")) or {}
                trust = evidence.get("trust")
                if trust != "trusted":
                    _add_reason(reasons, "untrusted_receipt")
                issuer = r.get("issuer_component_sha256")
                if issuer not in manifest_audits:
                    _add_reason(reasons, "issuer_not_in_manifest")
                try:
                    assert_execution_status_not_scientific_success(status, str(trust))
                except ValueError:
                    _add_reason(reasons, "untrusted_receipt")

            for r in release_receipts:
                evidence = _json_dict(r.get("evidence")) or {}
                trust = evidence.get("trust")
                if trust != "trusted":
                    _add_reason(reasons, "untrusted_receipt")
                issuer = r.get("issuer_component_sha256")
                if issuer not in manifest_releases:
                    _add_reason(reasons, "issuer_not_in_manifest")
                try:
                    assert_execution_status_not_scientific_success(status, str(trust))
                except ValueError:
                    _add_reason(reasons, "untrusted_receipt")

            for r in receipts:
                if isinstance(r, dict) and r not in audit_receipts and r not in release_receipts:
                    evidence = _json_dict(r.get("evidence")) or {}
                    trust = evidence.get("trust")
                    try:
                        assert_execution_status_not_scientific_success(status, str(trust))
                    except ValueError:
                        _add_reason(reasons, "untrusted_receipt")

    eligible = len(reasons) == 0

    return {
        "trial_id": trial_id_str,
        "status": status,
        "jobs": jobs,
        "eligible": eligible,
        "reasons": reasons,
    }

"""Trusted managed-trial ingestion (R06/R07, OMP-315).

Trust derives from the settlement and the worker's registered capability, not
from the artifact bytes anyone may register. A managed trial is trusted only
when its managed job is settled by a worker holding ``MANAGED_CAPABILITY``,
whose settlement carries exactly one evaluator receipt from the trial's declared
evaluator inside the campaign's compatibility manifest, and when the
content-addressed receipt and the sealed protocol both verify against the trial,
candidate, evaluator descriptor, and confirmation data.

Reasons are the same short, stable vocabulary the caller gates on:
``missing_managed_receipt``, ``untrusted_issuer``, ``missing_protocol``,
``wrong_confirmation``, and ``wrong_job``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from pydantic import ValidationError

from omp_work.operations.artifacts import read_verified_bytes
from omp_work.research.custody import artifact_path
from omp_work.research.receipt import (
    MANAGED_CAPABILITY,
    MANAGED_POLICY,
    ReceiptExpectation,
    TrialReceipt,
    load_protocol,
    managed_job_id,
    verify_receipt,
)

__all__ = [
    "is_managed_campaign",
    "verify_managed_trial",
]

_TRIAL_QUERY = """
SELECT t.trial_id, t.campaign_id, t.candidate_digest, t.evaluator_sha256,
       c.compatibility, c.spec
FROM omp_research.trials t
JOIN omp_research.campaigns c
  ON c.workspace_id = t.workspace_id AND c.campaign_id = t.campaign_id
WHERE t.workspace_id=%s AND t.trial_id=%s
"""


def _json_object(value: object) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _json_list(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _component_descriptor(
    cur: psycopg.Cursor[dict[str, object]], workspace_id: UUID, component_sha256: str
) -> dict[str, Any] | None:
    cur.execute(
        "SELECT descriptor FROM omp_research.components WHERE workspace_id=%s AND component_sha256=%s",
        (workspace_id, component_sha256),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return _json_object(row["descriptor"])


def is_managed_campaign(
    cur: psycopg.Cursor[dict[str, object]],
    workspace_id: UUID | str,
    campaign: Mapping[str, object],
) -> bool:
    """True only when the campaign's registered policy component is MANAGED_POLICY."""
    policy_sha = campaign.get("policy_sha256")
    if not isinstance(policy_sha, str) or not policy_sha:
        return False
    descriptor = _component_descriptor(cur, UUID(str(workspace_id)), policy_sha)
    if descriptor is None:
        return False
    return descriptor.get("name") == MANAGED_POLICY


def _worker_holds_capability(
    cur: psycopg.Cursor[dict[str, object]],
    workspace_id: UUID,
    worker_id: object,
    capability: str,
) -> bool:
    if not isinstance(worker_id, str) or not worker_id:
        return False
    cur.execute(
        "SELECT capabilities FROM omp_jobs.workers WHERE workspace_id=%s AND worker_id=%s",
        (workspace_id, worker_id),
    )
    row = cur.fetchone()
    if row is None:
        return False
    return capability in {str(item) for item in _json_list(row["capabilities"])}


def _managed_job(
    cur: psycopg.Cursor[dict[str, object]], workspace_id: UUID, trial_id: UUID
) -> dict[str, Any] | None:
    cur.execute(
        "SELECT status, settlement FROM omp_jobs.jobs WHERE workspace_id=%s AND job_id=%s AND source='native'",
        (workspace_id, managed_job_id(trial_id)),
    )
    return cur.fetchone()


def _custody_bytes(
    cur: psycopg.Cursor[dict[str, object]],
    workspace_id: UUID,
    digest: object,
    data_dir: object,
) -> bytes | None:
    """Bytes for a held artifact digest, or None when unavailable."""
    if not isinstance(digest, str) or not digest:
        return None
    cur.execute(
        "SELECT manifest FROM omp_research.artifacts WHERE workspace_id=%s AND artifact_sha256=%s",
        (workspace_id, digest),
    )
    row = cur.fetchone()
    if row is None:
        return None
    manifest = _json_object(row["manifest"])
    if manifest is None:
        return None
    size = manifest.get("size_bytes")
    if isinstance(size, bool) or not isinstance(size, int):
        return None
    try:
        base_dir = data_dir if isinstance(data_dir, Path) else Path(str(data_dir))
        return read_verified_bytes(
            artifact_path(base_dir, workspace_id, digest), digest, size
        )
    except (RuntimeError, OSError):
        return None


def _verify(
    cur: psycopg.Cursor[dict[str, object]],
    workspace_id: UUID,
    trial: Mapping[str, object],
    data_dir: object,
) -> tuple[TrialReceipt | None, list[str]]:
    trial_id = UUID(str(trial["trial_id"]))
    campaign_id = str(trial["campaign_id"])
    candidate_digest = str(trial["candidate_digest"])
    evaluator_sha256 = str(trial["evaluator_sha256"])

    # 1. job managed_job_id(trial) settled; else missing_managed_receipt.
    job = _managed_job(cur, workspace_id, trial_id)
    settlement = None if job is None else _json_object(job["settlement"])
    job_id = managed_job_id(trial_id)
    if (
        job is None
        or settlement is None
        or str(job["status"]) not in ("sealed", "failed")
    ):
        return None, ["missing_managed_receipt"]

    # 2. its worker_id's omp_jobs.workers capabilities hold MANAGED_CAPABILITY; else untrusted_issuer.
    if not _worker_holds_capability(
        cur, workspace_id, settlement.get("worker_id"), MANAGED_CAPABILITY
    ):
        return None, ["untrusted_issuer"]

    # 3. exactly one evaluator receipt, issuer == trial.evaluator_sha256, in compatibility.evaluators; else untrusted_issuer.
    compatibility = _json_object(trial["compatibility"]) or {}
    evaluators = {str(item) for item in _json_list(compatibility.get("evaluators"))}
    raw_receipts = settlement.get("receipts")
    receipts = raw_receipts if isinstance(raw_receipts, list) else []
    all_evaluator_receipts = [
        entry
        for entry in receipts
        if isinstance(entry, dict) and entry.get("role") == "evaluator"
    ]
    if len(all_evaluator_receipts) != 1:
        return None, ["untrusted_issuer"]
    evaluator_receipt = all_evaluator_receipts[0]
    if (
        evaluator_receipt.get("issuer_component_sha256") != evaluator_sha256
        or evaluator_sha256 not in evaluators
    ):
        return None, ["untrusted_issuer"]
    evidence = evaluator_receipt.get("evidence")
    content_digest = (
        evidence.get("content_digest") if isinstance(evidence, dict) else None
    )

    # 4. custody bytes of its content_digest d (artifacts row, read_verified_bytes at artifact_path); else missing_managed_receipt.
    receipt_bytes = _custody_bytes(cur, workspace_id, content_digest, data_dir)
    if receipt_bytes is None:
        return None, ["missing_managed_receipt"]

    # 5. same for spec.evaluation_protocol_sha256 + load_protocol; else missing_protocol.
    spec = _json_object(trial["spec"]) or {}
    protocol_sha256 = spec.get("evaluation_protocol_sha256")
    protocol_bytes = _custody_bytes(cur, workspace_id, protocol_sha256, data_dir)
    if protocol_bytes is None:
        return None, ["missing_protocol"]
    try:
        protocol = load_protocol(protocol_bytes, expected_sha256=str(protocol_sha256))
    except ValueError:
        return None, ["missing_protocol"]

    # 6. verify_receipt(bytes, expected_sha256=d, expectation (trial row, job id, evaluator descriptor artifact_sha256), protocol, protocol_sha256).
    descriptor = _component_descriptor(cur, workspace_id, evaluator_sha256)
    if descriptor is None:
        return None, ["untrusted_issuer"]
    evaluator_artifact = str(descriptor.get("artifact_sha256"))
    expected = ReceiptExpectation(
        trial_id=str(trial_id),
        campaign_id=campaign_id,
        job_id=job_id,
        candidate_digest=candidate_digest,
        evaluator_component_sha256=evaluator_sha256,
        evaluator_artifact_sha256=evaluator_artifact,
    )
    reasons = verify_receipt(
        receipt_bytes,
        expected_sha256=str(content_digest),
        expected=expected,
        protocol=protocol,
        protocol_sha256=str(protocol_sha256),
    )
    try:
        receipt = TrialReceipt.model_validate(json.loads(receipt_bytes.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, ValidationError):
        return None, reasons

    return receipt, reasons


def verify_managed_trial(
    cur: psycopg.Cursor[dict[str, object]],
    workspace_id: UUID | str,
    trial_id: UUID | str,
    *,
    data_dir: object,
) -> tuple[TrialReceipt | None, list[str]]:
    """Evaluate one managed trial from its settlement. Empty reasons means trusted.

    Returns the parsed receipt when the custody bytes are canonical, or ``None``
    when the receipt is missing or malformed. The cursor must carry the RLS
    context for this workspace (store transactions set it).
    """
    ws_uuid = UUID(str(workspace_id))
    cur.execute(_TRIAL_QUERY, (ws_uuid, UUID(str(trial_id))))
    trial = cur.fetchone()
    if trial is None:
        return None, ["missing_managed_receipt"]
    return _verify(cur, ws_uuid, trial, data_dir)

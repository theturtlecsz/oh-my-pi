"""Bind execution observations to fresh native receipts and frozen method bytes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from omp_work.jobs.admission import _resource_vector
from omp_work.operations.artifacts import read_verified_bytes
from omp_work.research import custody, engineering, recursive_method
from omp_work.research.harness_release import package_candidate
from omp_work.research.receipt import ReceiptExpectation, verify_receipt
from omp_work.research.recursive_method import Observation
from omp_work.research.recursive_transfer_evidence import NativeEvidence, TrialEvidence, TrialRefused, read_trial
from omp_work.research.recursive_transfer_freshness import PreparedStep
from omp_work.research.recursive_transfer_methods import FrozenMethod


@dataclass(frozen=True)
class BoundStep:
    evidence: TrialEvidence
    observation: Observation
    envelope: dict[str, int]
    metric: float


def bind_step(
    native: NativeEvidence, frozen: FrozenMethod, prepared_step: PreparedStep,
    arm: Literal["baseline", "candidate"],
) -> BoundStep:
    """Reread the selected arm; custody and native accounting must fail closed."""
    if arm == "baseline":
        trial_id = prepared_step.baseline_trial_id
    elif arm == "candidate":
        trial_id = prepared_step.candidate_trial_id
    else:
        raise TrialRefused("invalid_trial_arm")
    if trial_id is None:
        raise TrialRefused("missing_trial_arm")
    evidence = read_trial(native, trial_id)
    if not evidence.qualified or evidence.receipt.verdict != "qualified":
        raise TrialRefused("unqualified_receipt")
    study = frozen.study
    expected = ReceiptExpectation(
        trial_id=str(trial_id), campaign_id=evidence.campaign_id,
        job_id=evidence.job_id, candidate_digest=evidence.candidate_digest,
        evaluator_component_sha256=study.evaluator_component_sha256,
        evaluator_artifact_sha256=study.evaluator_artifact_sha256,
    )
    receipt_bytes = evidence.receipt.receipt_bytes()
    reasons = verify_receipt(
        receipt_bytes, expected_sha256=evidence.receipt_sha256, expected=expected,
        protocol=evidence.protocol, protocol_sha256=study.protocol_sha256,
    )
    if reasons:
        raise TrialRefused(reasons[0])

    with native.store.transaction(native.workspace_id, native.actor_id) as cur:
        cur.execute(
            "SELECT manifest FROM omp_research.artifacts WHERE workspace_id=%s AND artifact_sha256=%s",
            (native.workspace_id, evidence.candidate_digest),
        )
        row = cur.fetchone()
        if row is None:
            raise TrialRefused("missing_candidate_custody")
        manifest = row["manifest"]
        if isinstance(manifest, str):
            manifest = json.loads(manifest)
        if (
            not isinstance(manifest, dict)
            or manifest.get("artifact_sha256") != evidence.candidate_digest
            or type(manifest.get("size_bytes")) is not int
            or manifest["size_bytes"] < 0
        ):
            raise TrialRefused("candidate_manifest_mismatch")
        cur.execute(
            "SELECT resources FROM omp_jobs.jobs WHERE workspace_id=%s AND job_id=%s AND source='native'",
            (native.workspace_id, evidence.job_id),
        )
        job = cur.fetchone()
        if job is None:
            raise TrialRefused("missing_native_job")
        resources = job["resources"]
        if isinstance(resources, str):
            resources = json.loads(resources)
        axes = {"cpu", "memory_mib", "gpu", "model_calls"}
        if not isinstance(resources, dict) or not axes <= resources.keys():
            raise TrialRefused("missing_job_resource_axis")
        envelope = _resource_vector(resources)
        if envelope is None:
            raise TrialRefused("invalid_job_resources")

    tar = read_verified_bytes(
        custody.artifact_path(native.data_dir, native.workspace_id, evidence.candidate_digest),
        evidence.candidate_digest, manifest["size_bytes"],
    )
    with TemporaryDirectory() as directory:
        root = engineering.materialize(tar, evidence.candidate_digest, Path(directory))
        source = (root / recursive_method.SOURCE_PATH).read_bytes()
    if source != frozen.result.source_bytes:
        raise TrialRefused("changed_source")
    if package_candidate({recursive_method.SOURCE_PATH: source})["bundle_sha256"] != frozen.result.artifact_sha256:
        raise TrialRefused("changed_bundle")
    return BoundStep(evidence, Observation(receipt_bytes, expected), envelope,
                     evidence.receipt.metrics[evidence.protocol.primary])

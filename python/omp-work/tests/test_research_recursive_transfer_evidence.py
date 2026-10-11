"""Tests for recursive transfer evidence models, trial ingestion, and consumption history (OMP-319-s03-s02).

Proves that:
- sealed metrics and trial/job/campaign ids are returned from read_trial;
- settle=False fails;
- each refusal (absence from managed_trials, nonempty reasons, candidate_authored,
  and negative_control accepted) fails;
- failed settlement stays unqualified;
- snapshot/restore round-trips on ConsumptionHistory and NativeEvidence.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from native_jobs_support import native_jobs  # noqa: F401 (fixture)
from omp_work.operations.artifacts import read_verified_bytes
from omp_work.research.custody import artifact_path
from omp_work.research.receipt import (
    EvaluationProtocol,
    TrialReceipt,
    load_protocol,
)
from omp_work.research.recursive_transfer_evidence import (
    ConfirmationIdentitiesView,
    ConsumedView,
    ConsumptionHistory,
    NativeEvidence,
    TrialEvidence,
    TrialRefused,
    read_trial,
)
from recursive_transfer_support import (
    SealedArm,
    cache_artifact,
    register_task_dataset,
    seal_managed_trial,
)
from test_research_managed_gate import _default_protocol, _store

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)


def _protocol() -> EvaluationProtocol:
    return EvaluationProtocol.model_validate(
        {
            "contract_version": "research-protocol.v1",
            "metrics": [{"name": "score", "direction": "higher"}],
            "primary": "score",
            "tolerance": {},
            "candidate_command": ["python", "candidate.py"],
            "evaluator_command": ["python", "evaluator.py"],
            "outputs": ["out/score.json"],
            "requires": ["python3"],
            "confirmation_sha256": secrets.token_hex(32),
            "timeout_seconds": 60,
        }
    )


def test_consumption_history_snapshot_restore_round_trip():
    """ConsumptionHistory records operations, views consumed / confirmation identities, and restores."""
    history = ConsumptionHistory()
    assert isinstance(history.consumed, ConsumedView)
    assert isinstance(history.confirmation_identities, ConfirmationIdentitiesView)

    t1 = "trial-1"
    t2 = "trial-2"
    t3 = "trial-3"
    t4 = "trial-4"
    holdout = ("hash-content-1", "task-1")

    history.record_attempt(t1)
    history.record_success(t2)
    history.record_failure(t3)
    history.record_duplicate(t4)
    history.note_confirmation(holdout)

    assert history.consumed(t1) is True
    assert history.consumed(t2) is True
    assert history.consumed(t3) is True
    assert history.consumed("unseen") is False
    assert t1 in history.consumed
    assert holdout in history.consumed
    assert holdout in history.confirmation_identities
    assert holdout in history.confirmation_identities()

    snap = history.snapshot()
    assert isinstance(snap, dict)
    assert "attempts" in snap
    assert "consumed" in snap
    assert "confirmation_identities" in snap

    # Mutate history with new entries
    history.record_attempt("trial-5")
    history.note_confirmation(("hash-content-2", "task-2"))
    assert history.consumed("trial-5") is True

    # Restore in place
    history.restore(snap)
    assert history.snapshot() == snap
    assert history.consumed("trial-5") is False
    assert ("hash-content-2", "task-2") not in history.confirmation_identities
    assert history.consumed(t1) is True
    assert holdout in history.confirmation_identities

    # Restore via classmethod
    fresh = ConsumptionHistory.restore(snap)
    assert fresh.snapshot() == snap


def test_native_evidence_delegates_snapshot_restore(native_jobs: SimpleNamespace):
    """NativeEvidence stores context and delegates snapshot/restore to its internal history."""
    store = _store(native_jobs)
    native = NativeEvidence(
        store,
        native_jobs.workspace_id,
        native_jobs.actor_id,
        native_jobs.item["work_id"],
        native_jobs.service.config.data_dir,
    )
    assert isinstance(native.history, ConsumptionHistory)

    native.history.record_attempt("trial-a")
    native.history.note_confirmation(("sha", "task-a"))

    snap = native.snapshot()
    native.history.record_attempt("trial-b")
    assert native.history.consumed("trial-b") is True

    native.restore(snap)
    assert native.snapshot() == snap
    assert native.history.consumed("trial-b") is False
    assert native.history.consumed("trial-a") is True


def test_register_task_dataset_and_cache_artifact(native_jobs: SimpleNamespace):
    """register_task_dataset stores canonical JSON and cache_artifact writes artifact_cache."""
    task_id = f"task-{uuid4().hex[:8]}"
    payload = {"query": "hello", "depth": 3}
    digest = register_task_dataset(native_jobs, task_id, payload)

    # Verify digest matches sha256 of canonical bytes
    from omp_work.v1.canonical import canonical_json

    expected_bytes = canonical_json({"task_id": task_id, "payload": payload}).encode("utf-8")
    expected_sha = hashlib.sha256(expected_bytes).hexdigest()
    assert digest == expected_sha

    # Verify bytes stored in custody
    data_dir = Path(str(native_jobs.service.config.data_dir))
    stored_path = artifact_path(data_dir, native_jobs.workspace_id, digest)
    read_bytes = read_verified_bytes(stored_path, digest, len(expected_bytes))
    assert read_bytes == expected_bytes

    # Verify cache_artifact writes to artifact_cache
    cache_key = f"cache-{uuid4().hex[:8]}"
    cache_artifact(native_jobs, digest, cache_key)
    store = _store(native_jobs)
    with store.transaction(native_jobs.workspace_id, native_jobs.actor_id) as cur:
        cur.execute(
            "SELECT artifact_sha256 FROM omp_research.artifact_cache WHERE workspace_id=%s AND cache_key=%s",
            (native_jobs.workspace_id, cache_key),
        )
        row = cur.fetchone()
        assert row is not None
        assert row["artifact_sha256"] == digest


def test_sealed_metrics_and_trial_job_campaign_ids(native_jobs: SimpleNamespace):
    """Two calls to seal_managed_trial create two arms; read_trial returns metrics, ids, axes, and receipts."""
    store = _store(native_jobs)
    native = NativeEvidence(
        store,
        native_jobs.workspace_id,
        native_jobs.actor_id,
        native_jobs.item["work_id"],
        native_jobs.service.config.data_dir,
    )
    protocol = _protocol()
    dataset_id = register_task_dataset(native_jobs, "transfer-task-1", {"prompt": "evaluate"})

    # Two calls, two arms
    arm1 = seal_managed_trial(
        native_jobs,
        source_bytes=b"# Baseline strategy v1\ndef search(): pass\n",
        metric=0.72,
        resources={"cpu": 2, "memory_mib": 512, "gpu": 0, "model_calls": 3},
        protocol=protocol,
        dataset_id=dataset_id,
    )
    arm2 = seal_managed_trial(
        native_jobs,
        source_bytes=b"# Candidate strategy v2\ndef search(): return 42\n",
        metric=0.91,
        resources={"cpu": 2, "memory_mib": 512, "gpu": 0, "model_calls": 3},
        protocol=protocol,
        dataset_id=dataset_id,
    )

    assert isinstance(arm1, SealedArm)
    assert isinstance(arm2, SealedArm)
    assert arm1.trial_id != arm2.trial_id

    # Ingest baseline arm
    ev1 = read_trial(native, arm1.trial_id)
    assert isinstance(ev1, TrialEvidence)
    assert ev1.qualified is True
    assert ev1.reasons == ()
    assert ev1.metric == 0.72
    assert ev1.metrics == {"score": 0.72}
    assert ev1.trial_id == str(arm1.trial_id)
    assert ev1.job_id == arm1.job_id
    assert ev1.campaign_id == str(arm1.campaign_id)
    assert ev1.work_id == str(native.work_id)
    assert ev1.candidate_id == arm1.candidate_digest
    assert ev1.candidate_digest == arm1.candidate_digest
    assert ev1.receipt_id == arm1.receipt_sha256
    assert ev1.receipt_sha256 == arm1.receipt_sha256
    assert ev1.output_id == arm1.receipt.output_sha256
    assert ev1.cpu == 2
    assert ev1.memory_mib == 512
    assert ev1.gpu == 0
    assert ev1.model_calls == 3
    assert ev1.resources == {"cpu": 2, "memory_mib": 512, "gpu": 0, "model_calls": 3}
    assert isinstance(ev1.protocol, EvaluationProtocol)
    assert isinstance(ev1.receipt, TrialReceipt)
    assert ev1["trial_id"] == str(arm1.trial_id)
    assert ev1["receipt"] == ev1.receipt

    # Ingest candidate arm
    ev2 = read_trial(native, arm2.trial_id)
    assert ev2.qualified is True
    assert ev2.reasons == ()
    assert ev2.metric == 0.91
    assert ev2.metrics == {"score": 0.91}
    assert ev2.trial_id == str(arm2.trial_id)
    assert ev2.job_id == arm2.job_id
    assert ev2.candidate_id == arm2.candidate_digest


def test_settle_false_fails_read_trial(native_jobs: SimpleNamespace):
    """An unsealed/unsettled trial fails read_trial with missing_managed_receipt."""
    store = _store(native_jobs)
    native = NativeEvidence(
        store,
        native_jobs.workspace_id,
        native_jobs.actor_id,
        native_jobs.item["work_id"],
        native_jobs.service.config.data_dir,
    )
    protocol = _protocol()
    dataset_id = register_task_dataset(native_jobs, "unsettled-task", {"data": 1})

    arm = seal_managed_trial(
        native_jobs,
        source_bytes=b"# unsettled\n",
        metric=0.5,
        protocol=protocol,
        dataset_id=dataset_id,
        settle=False,
    )

    with pytest.raises(TrialRefused) as excinfo:
        read_trial(native, arm.trial_id)
    assert "missing_managed_receipt" in str(excinfo.value)


def test_refusal_absence_from_managed_trials(native_jobs: SimpleNamespace):
    """Trials not present in managed_trials for this work refuse read_trial."""
    store = _store(native_jobs)
    native = NativeEvidence(
        store,
        native_jobs.workspace_id,
        native_jobs.actor_id,
        native_jobs.item["work_id"],
        native_jobs.service.config.data_dir,
    )

    # Random non-existent trial ID
    with pytest.raises(TrialRefused) as excinfo:
        read_trial(native, uuid4())
    assert "missing_managed_trial" in str(excinfo.value)

    # Trial belonging to another work_id
    protocol = _protocol()
    dataset_id = register_task_dataset(native_jobs, "other-work-task", {"data": 2})
    arm = seal_managed_trial(
        native_jobs,
        source_bytes=b"# other work\n",
        metric=0.5,
        protocol=protocol,
        dataset_id=dataset_id,
    )

    other_native = NativeEvidence(
        store,
        native_jobs.workspace_id,
        native_jobs.actor_id,
        uuid4(),  # Different work_id
        native_jobs.service.config.data_dir,
    )
    with pytest.raises(TrialRefused) as excinfo:
        read_trial(other_native, arm.trial_id)
    assert "missing_managed_trial" in str(excinfo.value)


def test_refusal_candidate_authored_evidence(native_jobs: SimpleNamespace):
    """candidate_authored evidence in observations or artifact manifest refuses read_trial."""
    store = _store(native_jobs)
    native = NativeEvidence(
        store,
        native_jobs.workspace_id,
        native_jobs.actor_id,
        native_jobs.item["work_id"],
        native_jobs.service.config.data_dir,
    )
    protocol = _protocol()
    dataset_id = register_task_dataset(native_jobs, "candidate-authored-task", {"data": 3})

    arm = seal_managed_trial(
        native_jobs,
        source_bytes=b"# candidate authored\n",
        metric=0.5,
        protocol=protocol,
        dataset_id=dataset_id,
        issuer_kind="candidate_authored",
    )

    with pytest.raises(TrialRefused) as excinfo:
        read_trial(native, arm.trial_id)
    assert "candidate_authored" in str(excinfo.value)


def test_refusal_negative_control_accepted(native_jobs: SimpleNamespace):
    """An accepted negative control refuses read_trial."""
    store = _store(native_jobs)
    native = NativeEvidence(
        store,
        native_jobs.workspace_id,
        native_jobs.actor_id,
        native_jobs.item["work_id"],
        native_jobs.service.config.data_dir,
    )
    protocol = _protocol()
    dataset_id = register_task_dataset(native_jobs, "negative-control-task", {"data": 4})

    arm = seal_managed_trial(
        native_jobs,
        source_bytes=b"# neg control accepted\n",
        metric=0.5,
        protocol=protocol,
        dataset_id=dataset_id,
        negative_control="accepted",
    )

    with pytest.raises(TrialRefused) as excinfo:
        read_trial(native, arm.trial_id)
    assert "negative_control_accepted" in str(excinfo.value)


def test_refusal_nonempty_verification_reasons(native_jobs: SimpleNamespace):
    """Nonempty verify_managed_trial reasons (e.g. untrusted issuer) refuse read_trial."""
    store = _store(native_jobs)
    native = NativeEvidence(
        store,
        native_jobs.workspace_id,
        native_jobs.actor_id,
        native_jobs.item["work_id"],
        native_jobs.service.config.data_dir,
    )
    protocol = _protocol()
    dataset_id = register_task_dataset(native_jobs, "untrusted-task", {"data": 5})

    arm = seal_managed_trial(
        native_jobs,
        source_bytes=b"# untrusted\n",
        metric=0.5,
        protocol=protocol,
        dataset_id=dataset_id,
    )
    with native.store.transaction(native.workspace_id, native.actor_id) as cur:
        cur.execute(
            "UPDATE omp_jobs.workers SET capabilities='[]'::jsonb WHERE workspace_id=%s AND worker_id=%s",
            (native.workspace_id, arm.worker_id),
        )
    with pytest.raises(TrialRefused) as excinfo:
        read_trial(native, arm.trial_id)
    assert "untrusted_issuer" in str(excinfo.value)


def test_failed_settlement_stays_unqualified(native_jobs: SimpleNamespace):
    """A failed settlement with matching candidate_failed receipt is ingested but stays unqualified."""
    store = _store(native_jobs)
    native = NativeEvidence(
        store,
        native_jobs.workspace_id,
        native_jobs.actor_id,
        native_jobs.item["work_id"],
        native_jobs.service.config.data_dir,
    )
    protocol = _protocol()
    dataset_id = register_task_dataset(native_jobs, "failed-settlement-task", {"data": 6})

    arm = seal_managed_trial(
        native_jobs,
        source_bytes=b"# failed candidate\n",
        metric=0.0,
        protocol=protocol,
        dataset_id=dataset_id,
        outcome="failed",
        trust="untrusted",
    )

    ev = read_trial(native, arm.trial_id)
    assert isinstance(ev, TrialEvidence)
    assert ev.qualified is False
    assert "candidate_failed" in ev.reasons or "unqualified" in ev.reasons
    assert ev.receipt.verdict == "candidate_failed"
    assert ev.trial_id == str(arm.trial_id)

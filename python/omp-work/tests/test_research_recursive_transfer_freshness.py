"""Byte identity and two sealed arms govern confirmation eligibility."""

import hashlib
import os
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from native_jobs_support import native_jobs  # noqa: F401
from omp_work.research import recursive_transfer_freshness as freshness
from omp_work.research.custody import artifact_path
from omp_work.research.receipt import EvaluationProtocol
from omp_work.research.recursive import TaskEvidence
from omp_work.research.recursive_transfer_evidence import NativeEvidence
from omp_work.research.recursive_transfer_freshness import CohortTask, StepArms, prepare_cohorts
from omp_work.v1.canonical import sha256
from recursive_transfer_support import register_task_dataset, seal_managed_trial
from test_research_managed_gate import _store

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)


@pytest.fixture(scope="module")
def evidence(native_jobs):
    protocol = EvaluationProtocol(
        metrics=({"name": "score", "direction": "higher"},), primary="score",
        candidate_command=("python", "candidate.py"), evaluator_command=("python", "evaluator.py"),
        confirmation_sha256=sha256({"holdout": "fresh"}), timeout_seconds=60,
    )
    arms = [seal_managed_trial(
        native_jobs, source_bytes=f"# arm {arm}\n".encode(), metric=0.5 + arm * 0.1,
        protocol=protocol, dataset_id="unused",
    ) for arm in range(2)]
    native = NativeEvidence(
        _store(native_jobs), native_jobs.workspace_id, native_jobs.actor_id,
        native_jobs.item["work_id"], native_jobs.service.config.data_dir,
    )
    return native, protocol, arms


def cohort(native_jobs, evidence, *, payload=None, task_id=None, phase="confirmation"):
    _, protocol, arms = evidence
    return CohortTask(
        phase=phase, domain="machine_learning", metric="score", direction="higher",
        protocol_sha256=hashlib.sha256(protocol.protocol_bytes()).hexdigest(), seed=0, width=1, budget=2,
        dataset_id=register_task_dataset(native_jobs, task_id or uuid4().hex, payload if payload is not None else {"query": uuid4().hex}),
        steps=(StepArms("root", arms[0].trial_id, arms[1].trial_id),),
    )


def test_distinct_sealed_arms_eligible_and_history_unchanged(native_jobs, evidence):
    native, _, arms = evidence
    payload = {"steps": {"root": {"children": []}}, "opened": ["root"], "traces": [{"score": 1}]}
    task = cohort(native_jobs, evidence, payload=payload, task_id="byte-bound-task")
    before = native.history.snapshot()
    result, = prepare_cohorts((task,), native, ())
    assert result.eligible, result.reasons
    assert result.task_id == "byte-bound-task"
    assert result.content_sha256 == sha256(payload)
    step, = result.steps
    assert step.baseline_trial_id == arms[0].trial_id
    assert step.candidate_trial_id == arms[1].trial_id
    assert step.baseline.trial_id == str(arms[0].trial_id)
    assert step.candidate.trial_id == str(arms[1].trial_id)
    assert native.history.snapshot() == before


@pytest.mark.parametrize("missing", ["baseline", "candidate", "equal"])
def test_one_or_equal_trial_ids_ineligible(native_jobs, evidence, missing):
    native, _, arms = evidence
    task = cohort(native_jobs, evidence)
    step = StepArms("root", None if missing == "baseline" else arms[0].trial_id,
                    None if missing == "candidate" else arms[0].trial_id if missing == "equal" else arms[1].trial_id)
    result, = prepare_cohorts((task.model_copy(update={"steps": (step,)}),), native, ())
    assert not result.eligible
    assert ("equal_trial_arms" if missing == "equal" else "missing_trial_arm") in result.reasons


def test_confirmation_before_development_cannot_rename_reused_payload(native_jobs, evidence):
    native, _, _ = evidence
    payload = {"query": "already exposed"}
    confirmation = cohort(native_jobs, evidence, payload=payload)
    development = cohort(native_jobs, evidence, payload=payload, phase="development")
    assert confirmation.dataset_id != development.dataset_id
    result, dev = prepare_cohorts((confirmation, development), native, ())
    assert not result.eligible
    assert "confirmation_reused" in result.reasons
    assert dev.eligible


def test_development_task_identity_blocks_changed_payload(native_jobs, evidence):
    native, _, _ = evidence
    confirmation = cohort(native_jobs, evidence, task_id="same-task", payload={"new": 1})
    development = cohort(native_jobs, evidence, task_id="same-task", payload={"old": 1}, phase="development")
    result, _ = prepare_cohorts((confirmation, development), native, ())
    assert "confirmation_reused" in result.reasons
    assert not result.eligible


@pytest.mark.parametrize("source", ["trace", "study", "history"])
def test_trace_study_and_stored_confirmation_identity_are_excluded(native_jobs, evidence, source):
    native, _, _ = evidence
    payload = {"query": uuid4().hex}
    task_id = uuid4().hex
    confirmation = cohort(native_jobs, evidence, payload=payload, task_id=task_id)
    cohorts = [confirmation]
    studies = []
    before = native.history.snapshot()
    if source == "trace":
        cohorts.append(cohort(native_jobs, evidence, payload={"traces": [payload]}, phase="development"))
    elif source == "study":
        studies.append(SimpleNamespace(dev_sha256=sha256([sha256(payload)]), dev=(TaskEvidence(task_id=task_id, content_sha256=sha256(payload)),)))
    else:
        # Same stored task id, different content: freshness must check either identity.
        native.history.note_confirmation((sha256({"old": 1}), task_id))
    snapshot = native.history.snapshot()
    try:
        result = prepare_cohorts(cohorts, native, studies)[0]
        assert not result.eligible
        assert "confirmation_reused" in result.reasons
        assert native.history.snapshot() == snapshot
    finally:
        native.history.restore(before)


def test_shared_confirmation_content_rejects_both(native_jobs, evidence):
    native, _, _ = evidence
    payload = {"query": uuid4().hex}
    tasks = [cohort(native_jobs, evidence, payload=payload) for _ in range(2)]
    results = prepare_cohorts(tasks, native, ())
    assert all(not result.eligible and "duplicate_confirmation_content" in result.reasons for result in results)


@pytest.mark.parametrize("corruption", ["bytes", "size"])
def test_mismatched_custody_bytes_or_manifest_ineligible(native_jobs, evidence, corruption):
    native, _, _ = evidence
    task = cohort(native_jobs, evidence)
    path = artifact_path(native.data_dir, native.workspace_id, task.dataset_id)
    original = path.read_bytes()
    mode = path.stat().st_mode & 0o777
    with native.store.transaction(native.workspace_id, native.actor_id) as cur:
        cur.execute("SELECT manifest FROM omp_research.artifacts WHERE workspace_id=%s AND artifact_sha256=%s", (native.workspace_id, task.dataset_id))
        manifest = cur.fetchone()["manifest"]
    try:
        if corruption == "bytes":
            path.chmod(0o600)
            path.write_bytes(b"x" * len(original))
        else:
            with psycopg.connect(**native_jobs.service.config.connection_kwargs("postgres")) as conn, conn.cursor() as cur:
                cur.execute("SET LOCAL session_replication_role = replica")
                cur.execute(
                    "UPDATE omp_research.artifacts SET manifest=jsonb_set(manifest,%s,%s::jsonb) WHERE workspace_id=%s AND artifact_sha256=%s",
                    (["size_bytes"], Jsonb(len(original) + 1), native.workspace_id, task.dataset_id),
                )
        result, = prepare_cohorts((task,), native, ())
        assert not result.eligible
        assert result.content_sha256 is None
        assert result.task_id is None
    finally:
        if corruption == "bytes":
            path.write_bytes(original)
            path.chmod(mode)
        if corruption != "bytes":
            with psycopg.connect(**native_jobs.service.config.connection_kwargs("postgres")) as conn, conn.cursor() as cur:
                cur.execute("SET LOCAL session_replication_role = replica")
                cur.execute("UPDATE omp_research.artifacts SET manifest=%s WHERE workspace_id=%s AND artifact_sha256=%s", (Jsonb(manifest), native.workspace_id, task.dataset_id))


def test_confirmation_receipt_binding_required(native_jobs, evidence, monkeypatch):
    # Exercise confirmation-only binding even if trial ingestion changes later.
    native, _, _ = evidence
    task = cohort(native_jobs, evidence)
    read = freshness.read_trial

    def mismatched(native, trial_id):
        arm = read(native, trial_id)
        return replace(arm, receipt=arm.receipt.model_copy(update={"confirmation_sha256": "0" * 64}))

    monkeypatch.setattr(freshness, "read_trial", mismatched)
    result, = prepare_cohorts((task,), native, ())
    assert not result.eligible
    assert "confirmation_mismatch" in result.reasons


def test_unsealed_arm_cannot_make_pair_eligible(native_jobs, evidence):
    native, protocol, arms = evidence
    unsealed = seal_managed_trial(
        native_jobs, source_bytes=b"# pending candidate\n", metric=0.6,
        protocol=protocol, dataset_id="unused", settle=False,
    )
    task = cohort(native_jobs, evidence).model_copy(update={
        "steps": (StepArms("root", arms[0].trial_id, unsealed.trial_id),),
    })
    result, = prepare_cohorts((task,), native, ())
    assert not result.eligible
    assert "missing_managed_receipt" in result.reasons


@pytest.mark.parametrize("change,reason", [
    ({"protocol_sha256": "0" * 64}, "protocol_mismatch"),
    ({"direction": "lower"}, "metric_mismatch"),
])
def test_task_must_match_sealed_protocol(native_jobs, evidence, change, reason):
    native, _, _ = evidence
    task = cohort(native_jobs, evidence).model_copy(update=change)
    result, = prepare_cohorts((task,), native, ())
    assert not result.eligible
    assert reason in result.reasons

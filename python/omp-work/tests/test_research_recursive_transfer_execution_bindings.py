"""Execution binds each arm to fresh custody bytes and complete native resources."""

import os
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import Mock, patch

import psycopg
import pytest

from native_jobs_support import native_jobs  # noqa: F401
from omp_work.research.recursive_transfer_execution_evidence import bind_step
from omp_work.research.recursive_transfer_freshness import PreparedStep
from recursive_transfer_execution_support import build_execution, seal_step

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)


@pytest.fixture
def execution(native_jobs):
    return build_execution(native_jobs)


def prepared(pair):
    # No cached evidence: binding must select and reread each explicit ID.
    return PreparedStep("a", pair[0].trial_id, pair[1].trial_id, None, None)


def test_each_arm_loads_own_receipt_metric_and_complete_envelope(execution):
    pair = seal_step(execution, "a", baseline_score=0.2, candidate_score=0.9,
                     baseline_resources={"cpu": 0}, candidate_resources={"cpu": 3, "memory_mib": 16})
    step = prepared(pair)
    for arm, frozen, sealed in (("baseline", execution.baseline, pair[0]),
                                ("candidate", execution.candidate, pair[1])):
        bound = bind_step(execution.native, frozen, step, arm)
        assert bound.observation.receipt_bytes == sealed.receipt_bytes
        assert bound.observation.expected.trial_id == str(sealed.trial_id)
        assert bound.metric == sealed.receipt.metrics[execution.protocol.primary]
        assert bound.envelope == sealed.resources
        assert bound.evidence.candidate_digest == sealed.candidate_digest
        assert len({frozen.result.recipe_sha256, frozen.result.source_sha256,
                    frozen.result.artifact_sha256, sealed.candidate_digest, sealed.receipt_sha256}) == 5


def test_changed_source_refuses_even_when_trial_receipt_is_qualified(execution):
    pair = seal_step(execution, "a", candidate_source=b"def propose(*args): return None\n")
    with pytest.raises(ValueError, match="changed_source"):
        bind_step(execution.native, execution.candidate, prepared(pair), "candidate")


@pytest.mark.parametrize("field", ["protocol_sha256", "evaluator_component_sha256", "evaluator_artifact_sha256"])
def test_frozen_protocol_and_evaluator_bindings_refuse(execution, field):
    pair = seal_step(execution, "a")
    frozen = replace(execution.candidate, study=execution.candidate.study.model_copy(update={field: "f" * 64}))
    with pytest.raises(ValueError, match="stale_protocol|changed_evaluator"):
        bind_step(execution.native, frozen, prepared(pair), "candidate")


def test_failed_receipt_cannot_become_observation(execution):
    pair = seal_step(execution, "a", candidate_outcome="failed")
    with pytest.raises(ValueError, match="unqualified_receipt"):
        bind_step(execution.native, execution.candidate, prepared(pair), "candidate")


@pytest.mark.parametrize("damage", ["size", "negative_size", "bool_size", "digest", "missing", "malformed"])
def test_candidate_custody_errors_refuse_with_file_still_present(execution, damage):
    pair = seal_step(execution, "a")
    digest = pair[1].candidate_digest
    transaction = execution.native.store.transaction

    @contextmanager
    def damaged_transaction(*args, **kwargs):
        with transaction(*args, **kwargs) as cur:
            proxy = Mock(wraps=cur)
            query_params = None

            def execute(query, params):
                nonlocal query_params
                query_params = params if query.startswith("SELECT manifest FROM omp_research.artifacts") else None
                return cur.execute(query, params)

            def fetchone():
                row = cur.fetchone()
                if query_params != (execution.native.workspace_id, digest):
                    return row
                if damage == "missing":
                    return None
                manifest = dict(row["manifest"])
                if damage == "malformed":
                    manifest = []
                elif damage == "digest":
                    manifest["artifact_sha256"] = "f" * 64
                else:
                    manifest["size_bytes"] = {"size": manifest["size_bytes"] + 1,
                                              "negative_size": -1, "bool_size": True}[damage]
                return {"manifest": manifest}

            proxy.execute.side_effect = execute
            proxy.fetchone.side_effect = fetchone
            yield proxy

    # Corrupt the database response at its boundary; immutable custody rows stay intact.
    with patch.object(execution.native.store, "transaction", damaged_transaction):
        with pytest.raises((ValueError, RuntimeError)):
            bind_step(execution.native, execution.candidate, prepared(pair), "candidate")


@pytest.mark.parametrize("axis", ["cpu", "memory_mib", "gpu", "model_calls"])
def test_missing_raw_job_axis_refuses_instead_of_defaulting_to_zero(execution, axis):
    pair = seal_step(execution, "a")
    with execution.native.store.transaction(execution.native.workspace_id, execution.native.actor_id) as cur:
        cur.execute("UPDATE omp_jobs.jobs SET resources=resources - %s WHERE workspace_id=%s AND job_id=%s",
                    (axis, execution.native.workspace_id, pair[1].job_id))
    with pytest.raises(ValueError, match="missing_job_resource_axis"):
        bind_step(execution.native, execution.candidate, prepared(pair), "candidate")


@pytest.mark.parametrize("failed_read", [1, 2])
def test_database_failure_refuses_instead_of_loading_files(execution, failed_read):
    pair = seal_step(execution, "a")
    transaction = execution.native.store.transaction
    reads = 0

    def unavailable(*args, **kwargs):
        nonlocal reads
        reads += 1
        if reads == failed_read:
            raise psycopg.OperationalError("unavailable")
        return transaction(*args, **kwargs)

    with patch.object(execution.native.store, "transaction", side_effect=unavailable):
        with pytest.raises(psycopg.OperationalError, match="unavailable"):
            bind_step(execution.native, execution.candidate, prepared(pair), "candidate")


def test_binding_rereads_after_preparation(execution):
    pair = seal_step(execution, "a")
    step = prepared(pair)
    cached = bind_step(execution.native, execution.candidate, step, "candidate").evidence
    step = replace(step, candidate=cached)
    with execution.native.store.transaction(execution.native.workspace_id, execution.native.actor_id) as cur:
        cur.execute("UPDATE omp_jobs.jobs SET source='flood_import', kind=NULL WHERE workspace_id=%s AND job_id=%s",
                    (execution.native.workspace_id, pair[1].job_id))
    with pytest.raises(ValueError):
        bind_step(execution.native, execution.candidate, step, "candidate")

"""Contracts for recursive transfer execution inputs, settings retention, and payload validation.

OMP-319-s03-s05-s01:
- Nondefault settings survive real preparation via prepare_execution_cohorts.
- PreparedExecution subclasses PreparedTask and works with reserve_task.
- Verified content changes refuse at the trust boundary in load_payload.
- opened=[{}] raises controlled PayloadValidationError, never TypeError.
- Payload settings/metrics cannot replace controller settings.
- Tree references and opened IDs are validated strictly before set conversion.
"""

from __future__ import annotations

from dataclasses import replace
import hashlib
import os
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from native_jobs_support import native_jobs  # noqa: F401
from omp_work.research.receipt import EvaluationProtocol
from omp_work.research.recursive_transfer_evidence import NativeEvidence
from omp_work.research.recursive_transfer_execution_inputs import (
    ExecutionPayload,
    PayloadValidationError,
    PreparedExecution,
    load_payload,
    prepare_execution_cohorts,
)
from omp_work.research.recursive_transfer_freshness import (
    CohortTask,
    PreparedTask,
    StepArms,
)
from omp_work.research.recursive_transfer_history import reserve_task
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
        metrics=(
            {"name": "score", "direction": "higher"},
            {"name": "custom_loss", "direction": "lower"},
        ),
        primary="score",
        candidate_command=("python", "candidate.py"),
        evaluator_command=("python", "evaluator.py"),
        confirmation_sha256=sha256({"holdout": "fresh"}),
        timeout_seconds=60,
    )
    arms = [
        seal_managed_trial(
            native_jobs,
            source_bytes=f"# arm {arm}\n".encode(),
            metric={"score": 0.5 + arm * 0.1, "custom_loss": 0.2 + arm * 0.1},
            protocol=protocol,
            dataset_id="unused",
        )
        for arm in range(2)
    ]
    native = NativeEvidence(
        _store(native_jobs),
        native_jobs.workspace_id,
        native_jobs.actor_id,
        native_jobs.item["work_id"],
        native_jobs.service.config.data_dir,
    )
    return native, protocol, arms


def _native(native_jobs) -> NativeEvidence:
    return NativeEvidence(
        _store(native_jobs),
        native_jobs.workspace_id,
        native_jobs.actor_id,
        native_jobs.item["work_id"],
        native_jobs.service.config.data_dir,
    )


def _default_tree() -> dict[str, Any]:
    return {
        "root": {"parent_id": None, "children": ["child_a"], "metric": 0.5},
        "child_a": {"parent_id": "root", "children": [], "metric": 0.8},
    }


def make_cohort(
    native_jobs,
    evidence,
    *,
    payload: Any = None,
    task_id: str | None = None,
    phase: str = "confirmation",
    domain: str = "machine_learning",
    metric: str = "score",
    direction: str = "higher",
    seed: int = 0,
    width: int = 1,
    budget: int = 2,
) -> CohortTask:
    _, protocol, arms = evidence
    tid = task_id or f"task-{uuid4().hex}"
    if payload is None:
        payload = {
            "steps": _default_tree(),
            "opened": ["root"],
            "unique_id": tid,
        }
    dataset_id = register_task_dataset(
        native_jobs, tid, payload
    )
    protocol_sha = hashlib.sha256(protocol.protocol_bytes()).hexdigest()
    return CohortTask(
        phase=phase,
        domain=domain,
        metric=metric,
        direction=direction,
        protocol_sha256=protocol_sha,
        seed=seed,
        width=width,
        budget=budget,
        dataset_id=dataset_id,
        steps=(StepArms("root", arms[0].trial_id, arms[1].trial_id),),
    )


def test_nondefault_settings_survive_real_preparation(native_jobs, evidence):
    """Nondefault settings on CohortTask survive real preparation without reset to defaults."""
    _, _, arms = evidence
    native = _native(native_jobs)
    cohort = make_cohort(
        native_jobs,
        evidence,
        task_id="nondefault-task-1",
        phase="development",
        domain="planning_heuristics",
        metric="custom_loss",
        direction="lower",
        seed=42,
        width=3,
        budget=10,
    )

    results = prepare_execution_cohorts((cohort,), native, ())
    assert len(results) == 1
    prep = results[0]

    assert isinstance(prep, PreparedExecution)
    assert isinstance(prep, PreparedTask)
    assert prep.eligible, prep.reasons
    assert prep.task_id == "nondefault-task-1"
    assert prep.phase == "development"
    assert prep.domain == "planning_heuristics"
    assert prep.metric == "custom_loss"
    assert prep.direction == "lower"
    assert prep.seed == 42
    assert prep.width == 3
    assert prep.budget == 10
    assert prep.protocol_sha256 == cohort.protocol_sha256
    assert len(prep.steps) == 1
    assert prep.steps[0].baseline_trial_id == arms[0].trial_id
    assert prep.steps[0].candidate_trial_id == arms[1].trial_id

    # Confirm compatibility with reserve_task
    reservation = reserve_task(native, prep)
    assert reservation.replicates >= 1
    assert reservation.baseline_trial_id == arms[0].trial_id
    assert reservation.candidate_trial_id == arms[1].trial_id


def test_prepare_multiple_cohorts_retains_respective_settings(native_jobs, evidence):
    """Multiple cohorts in one preparation call retain distinct settings."""
    native = _native(native_jobs)
    c1 = make_cohort(
        native_jobs,
        evidence,
        task_id="multi-task-1",
        phase="development",
        seed=10,
        width=2,
    )
    c2 = make_cohort(
        native_jobs,
        evidence,
        task_id="multi-task-2",
        phase="confirmation",
        seed=20,
        width=4,
    )

    results = prepare_execution_cohorts((c1, c2), native, ())
    assert len(results) == 2
    assert results[0].phase == "development"
    assert results[0].seed == 10
    assert results[0].width == 2
    assert results[1].phase == "confirmation"
    assert results[1].seed == 20
    assert results[1].width == 4


def test_verified_content_changes_refuse(native_jobs, evidence):
    """Verified content changes refuse at the trust boundary during load_payload."""
    native = _native(native_jobs)
    cohort = make_cohort(native_jobs, evidence, task_id="content-verify-task")
    prep, = prepare_execution_cohorts((cohort,), native, ())
    assert prep.eligible, prep.reasons

    # Content change via altered prepared hash
    altered_prep = replace(prep, content_sha256="0" * 64)
    with pytest.raises(PayloadValidationError) as exc_info:
        load_payload(altered_prep, native)
    assert "content_sha256 mismatch" in str(exc_info.value)

    # Task identity change via altered task_id
    altered_task = replace(prep, task_id="tampered-task-id")
    with pytest.raises(PayloadValidationError) as exc_info:
        load_payload(altered_task, native)
    assert "task_id mismatch" in str(exc_info.value)

    # Missing task_id or content_sha256
    no_content = replace(prep, content_sha256=None)
    with pytest.raises(PayloadValidationError) as exc_info:
        load_payload(no_content, native)
    assert "content_sha256 is None" in str(exc_info.value)

    no_task = replace(prep, task_id=None)
    with pytest.raises(PayloadValidationError) as exc_info:
        load_payload(no_task, native)
    assert "task_id is None" in str(exc_info.value)


def test_custody_manifest_tampering_refuses(native_jobs, evidence):
    """Tampering with artifact manifest in database causes load_payload to refuse."""
    native = _native(native_jobs)
    cohort = make_cohort(native_jobs, evidence, task_id="custody-tamper-task")
    prep, = prepare_execution_cohorts((cohort,), native, ())
    assert prep.eligible, prep.reasons

    # Change size_bytes in custody manifest
    with psycopg.connect(**native_jobs.service.config.connection_kwargs("postgres")) as conn, conn.cursor() as cur:
        cur.execute("SET LOCAL session_replication_role = replica")
        cur.execute(
            """
            UPDATE omp_research.artifacts
            SET manifest = jsonb_set(manifest, '{size_bytes}', '999999'::jsonb)
            WHERE workspace_id = %s AND artifact_sha256 = %s
            """,
            (native.workspace_id, prep.dataset_id),
        )
        conn.commit()

    with pytest.raises(PayloadValidationError) as exc_info:
        load_payload(prep, native)
    assert "failed to read dataset from custody" in str(exc_info.value)


def test_opened_with_dict_raises_controlled_error_never_type_error(native_jobs, evidence):
    """opened=[{}] raises controlled PayloadValidationError, never TypeError."""
    native = _native(native_jobs)
    bad_payload = {
        "steps": _default_tree(),
        "opened": [{}],
    }
    cohort = make_cohort(native_jobs, evidence, payload=bad_payload, task_id="bad-opened-dict")
    prep, = prepare_execution_cohorts((cohort,), native, ())

    with pytest.raises(PayloadValidationError) as exc_info:
        load_payload(prep, native)

    assert not isinstance(exc_info.value, TypeError)
    assert "opened ID at index 0 must be a string" in str(exc_info.value)


@pytest.mark.parametrize(
    "invalid_opened, expected_err",
    [
        ([123], "must be a string"),
        ([None], "must be a string"),
        ([""], "cannot be empty string"),
        ("root", "must be a collection of step IDs"),
        (12345, "must be a collection of step IDs"),
        (["nonexistent_step"], "not found in steps tree"),
    ],
)
def test_opened_invalid_types_raise_controlled_error(native_jobs, evidence, invalid_opened, expected_err):
    """Invalid opened elements or types raise controlled PayloadValidationError before set conversion."""
    native = _native(native_jobs)
    bad_payload = {
        "steps": _default_tree(),
        "opened": invalid_opened,
    }
    cohort = make_cohort(native_jobs, evidence, payload=bad_payload, task_id=f"invalid-opened-{uuid4().hex[:6]}")
    prep, = prepare_execution_cohorts((cohort,), native, ())

    with pytest.raises(PayloadValidationError) as exc_info:
        load_payload(prep, native)

    assert not isinstance(exc_info.value, TypeError)
    assert expected_err in str(exc_info.value)


def test_payload_settings_cannot_replace_controller_settings(native_jobs, evidence):
    """Payload settings and tree metrics carry no authority and cannot override controller settings."""
    native = _native(native_jobs)
    conflicting_payload = {
        "steps": {
            "root": {"parent_id": None, "children": ["child_a"], "metric": 999.0},
            "child_a": {"parent_id": "root", "children": [], "metric": -888.0},
        },
        "opened": ["root"],
        "seed": 0,
        "width": 1,
        "direction": "higher",
        "metric": "conflicting_metric",
        "phase": "confirmation",
        "domain": "conflicting_domain",
        "budget": 0,
    }

    cohort = make_cohort(
        native_jobs,
        evidence,
        payload=conflicting_payload,
        task_id="settings-authority-task",
        phase="development",
        domain="planning",
        metric="custom_loss",
        direction="lower",
        seed=17,
        width=2,
        budget=5,
    )

    prep, = prepare_execution_cohorts((cohort,), native, ())
    assert prep.eligible, prep.reasons

    # Controller settings remain authoritative on PreparedExecution
    assert prep.phase == "development"
    assert prep.domain == "planning"
    assert prep.metric == "custom_loss"
    assert prep.direction == "lower"
    assert prep.seed == 17
    assert prep.width == 2
    assert prep.budget == 5

    # load_payload returns ExecutionPayload exposing only tree and opened
    loaded = load_payload(prep, native)
    assert isinstance(loaded, ExecutionPayload)
    assert loaded.steps == {
        "root": {"parent_id": None, "children": ["child_a"], "metric": 999.0},
        "child_a": {"parent_id": "root", "children": [], "metric": -888.0},
    }
    assert loaded.opened == frozenset({"root"})

    # Settings do not exist on loaded payload
    assert loaded.get("seed") is None
    assert loaded.get("width") is None
    assert loaded.get("direction") is None
    assert loaded.get("metric") is None
    assert loaded.get("phase") is None
    assert loaded.get("domain") is None
    assert "seed" not in loaded
    assert "metric" not in loaded
    assert hasattr(loaded, "seed") is False
    assert hasattr(loaded, "metric") is False


def test_tree_validation_errors(native_jobs, evidence):
    """Malformed trees and invalid tree references raise controlled PayloadValidationError."""
    native, _, _ = evidence

    # 1. Payload not a mapping
    c_not_map = make_cohort(native_jobs, evidence, payload=["not", "a", "map"], task_id="not-a-map")
    p, = prepare_execution_cohorts((c_not_map,), native, ())
    with pytest.raises(PayloadValidationError) as exc:
        load_payload(p, native)
    assert "must be a JSON object" in str(exc.value)

    # 2. Missing 'steps'
    c_no_steps = make_cohort(native_jobs, evidence, payload={"opened": []}, task_id="no-steps")
    p, = prepare_execution_cohorts((c_no_steps,), native, ())
    with pytest.raises(PayloadValidationError) as exc:
        load_payload(p, native)
    assert "missing required 'steps' field" in str(exc.value)

    # 3. Steps not a mapping
    c_steps_list = make_cohort(native_jobs, evidence, payload={"steps": [1, 2], "opened": []}, task_id="steps-list")
    p, = prepare_execution_cohorts((c_steps_list,), native, ())
    with pytest.raises(PayloadValidationError) as exc:
        load_payload(p, native)
    assert "steps must be a mapping" in str(exc.value)

    # 4. Empty steps
    c_empty_steps = make_cohort(native_jobs, evidence, payload={"steps": {}, "opened": []}, task_id="empty-steps")
    p, = prepare_execution_cohorts((c_empty_steps,), native, ())
    with pytest.raises(PayloadValidationError) as exc:
        load_payload(p, native)
    assert "steps mapping cannot be empty" in str(exc.value)

    # 5. Node not a mapping
    c_bad_node = make_cohort(native_jobs, evidence, payload={"steps": {"root": "not-node"}, "opened": []}, task_id="bad-node")
    p, = prepare_execution_cohorts((c_bad_node,), native, ())
    with pytest.raises(PayloadValidationError) as exc:
        load_payload(p, native)
    assert "must be a mapping" in str(exc.value)

    # 6. Invalid parent_id reference
    bad_parent = {"steps": {"root": {"parent_id": "nonexistent", "children": []}}, "opened": []}
    c_bad_parent = make_cohort(native_jobs, evidence, payload=bad_parent, task_id="bad-parent")
    p, = prepare_execution_cohorts((c_bad_parent,), native, ())
    with pytest.raises(PayloadValidationError) as exc:
        load_payload(p, native)
    assert "parent_id 'nonexistent' not found in steps tree" in str(exc.value)

    # 7. Self-referential parent_id
    self_parent = {"steps": {"root": {"parent_id": "root", "children": []}}, "opened": []}
    c_self_parent = make_cohort(native_jobs, evidence, payload=self_parent, task_id="self-parent")
    p, = prepare_execution_cohorts((c_self_parent,), native, ())
    with pytest.raises(PayloadValidationError) as exc:
        load_payload(p, native)
    assert "cannot have itself as parent_id" in str(exc.value)

    # 8. Invalid child reference
    bad_child = {"steps": {"root": {"parent_id": None, "children": ["nonexistent"]}}, "opened": []}
    c_bad_child = make_cohort(native_jobs, evidence, payload=bad_child, task_id="bad-child")
    p, = prepare_execution_cohorts((c_bad_child,), native, ())
    with pytest.raises(PayloadValidationError) as exc:
        load_payload(p, native)
    assert "child 'nonexistent' not found in steps tree" in str(exc.value)


@pytest.mark.parametrize(
    "steps, expected_err",
    [
        pytest.param(
            {
                "root": {"parent_id": None, "children": []},
                "a": {"parent_id": "b", "children": ["b"]},
                "b": {"parent_id": "a", "children": ["a"]},
            },
            "cycle",
            id="disconnected-parent-cycle",
        ),
        pytest.param(
            {"root": {"parent_id": None, "children": ["root"]}},
            "cannot have itself as child",
            id="self-child",
        ),
        pytest.param(
            {
                "root": {"parent_id": None, "children": ["child"]},
                "child": {"parent_id": None, "children": []},
            },
            "parent_id does not match",
            id="child-disagrees-with-parent",
        ),
        pytest.param(
            {
                "root": {"parent_id": None, "children": []},
                "child": {"parent_id": "root", "children": []},
            },
            "not listed in parent",
            id="parent-omits-child",
        ),
    ],
)
def test_invalid_topology_refuses_before_policy_input(native_jobs, evidence, steps, expected_err):
    """Custody-backed cycles and contradictory edges never become policy input."""
    native = _native(native_jobs)
    cohort = make_cohort(
        native_jobs, evidence, payload={"steps": steps, "opened": ["root"]}
    )
    prep, = prepare_execution_cohorts((cohort,), native, ())

    with pytest.raises(PayloadValidationError, match=expected_err):
        load_payload(prep, native)


def test_deep_tree_loads_without_recursion_limit(native_jobs, evidence):
    """A valid deep ancestry chain remains available to policies without RecursionError."""
    ids = ["root"] + [f"step-{i}" for i in range(1100)]
    steps = {
        step: {
            "parent_id": ids[i - 1] if i else None,
            "children": ids[i + 1:i + 2],
        }
        for i, step in reversed(list(enumerate(ids)))
    }
    native = _native(native_jobs)
    cohort = make_cohort(
        native_jobs, evidence, payload={"steps": steps, "opened": ["root"]}
    )
    prep, = prepare_execution_cohorts((cohort,), native, ())

    loaded = load_payload(prep, native)
    assert loaded.steps == steps
    assert loaded.opened == frozenset({"root"})


def test_execution_payload_mapping_interface():
    """ExecutionPayload adheres to Mapping protocol and equality rules."""
    steps = {"root": {"parent_id": None, "children": []}}
    opened = frozenset({"root"})
    payload = ExecutionPayload(steps=steps, opened=opened)

    assert payload.steps == steps
    assert payload["steps"] == steps
    assert payload.get("steps") == steps

    assert payload.opened == opened
    assert payload["opened"] == opened
    assert payload.get("opened") == opened

    assert len(payload) == 2
    assert set(payload.keys()) == {"steps", "opened"}
    assert list(payload) == ["steps", "opened"]

    # Mapping equality
    assert payload == {"steps": steps, "opened": opened}
    assert payload == ExecutionPayload(steps=steps, opened=opened)
    assert payload != {"steps": steps, "opened": frozenset()}
    assert payload != "not_a_payload"

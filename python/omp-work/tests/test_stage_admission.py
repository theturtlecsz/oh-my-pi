from __future__ import annotations

import pytest

from omp_work.economy_effort import validate_effort
from omp_work.jobs import stage_admission
from omp_work.jobs.stage_admission import admit_stage_job
from omp_work.jobs.store import JobError

_SENTINEL = object()
_ENQUEUE = {
    "operation_id": "op-1",
    "workspace_id": "ws",
    "actor_id": "act",
    "job_id": "j-1",
    "work_id": "w-1",
    "kind": "model",
    "required_capabilities": ["cpu"],
    "resources": {"cpu": 1, "memory_mib": 0, "gpu": 0, "model_calls": 0},
    "lease_seconds": 60,
}


class _Spy:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __call__(self, store, **kwargs):
        self.calls.append((store, kwargs))
        return {"status": "applied"}


@pytest.mark.parametrize(
    ("effort", "reason"),
    [
        (None, "why"),
        ("", "why"),
        ("E5", "why"),
        ("e1", "why"),
        (1, "why"),
        ("E2", None),
        ("E4", "because"),
    ],
)
def test_invalid_effort_refused_before_enqueue(effort, reason, monkeypatch) -> None:
    spy = _Spy()
    monkeypatch.setattr(stage_admission, "enqueue_job", spy)
    with pytest.raises(JobError) as excinfo:
        admit_stage_job(_SENTINEL, effort=effort, effort_reason=reason, **_ENQUEUE)
    assert excinfo.value.code == "invalid_effort"
    assert len(excinfo.value.diagnostics) == 1
    assert spy.calls == []


@pytest.mark.parametrize(
    ("effort", "reason"),
    [
        ("E1", None),
        ("E3", "keep the cost down"),
    ],
)
def test_valid_effort_enqueues_unchanged(effort, reason, monkeypatch) -> None:
    spy = _Spy()
    monkeypatch.setattr(stage_admission, "enqueue_job", spy)
    result = admit_stage_job(_SENTINEL, effort=effort, effort_reason=reason, **_ENQUEUE)
    assert result == {"status": "applied"}
    assert spy.calls == [(_SENTINEL, dict(_ENQUEUE))]


def test_validate_effort_e4_long_reason_ok() -> None:
    reason = "profiling shows the hot loop dominates the wall clock"
    assert len(reason) >= 20
    assert validate_effort("E4", reason)["ok"] is True

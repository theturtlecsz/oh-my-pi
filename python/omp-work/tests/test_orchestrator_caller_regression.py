"""Real mission callers enforce release, independent acceptance, and delivery (OMP-594)."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from omp_work import event_push
from omp_work.orchestrator import worker_sandbox
from omp_work.orchestrator.candidate_git import git
from omp_work.orchestrator.service import load_config, status
from orchestrator_e2e_support import E2EWorld, open_e2e_world, open_worker, tick_until
from test_orchestrator_checkpoint_transport import (
    _answer_intake,
    _deliveries,
    _owed_events,
    _sign_merge,
    _steps,
    _submit,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)


def _open(service: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> E2EWorld:
    world = open_e2e_world(
        service, tmp_path / "world", monkeypatch,
        verifier_mode="pass", worker_mode="ok",
        operations=("omp_work.orchestrator.operations:register",),
    )
    world.config = replace(world.config, release="r1")
    document = json.loads(world.config_path.read_text(encoding="utf-8"))
    document["release"] = "r1"
    world.config_path.write_text(json.dumps(document), encoding="utf-8")
    return world


def _start(world: E2EWorld, service: Any) -> tuple[Any, UUID]:
    work_id = _submit(world, service)
    worker = open_worker(world)
    tick_until(
        worker, world,
        lambda view: any(s.get("rule_id") == "d23-owner-confirm" for s in view["steps"]),
    )
    _answer_intake(world)
    return worker, work_id


def test_matching_release_completes_with_exactly_once_signed_checkpoints(
    service: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An attended r1 mission completes; each owed event gets one signed exact-payload push."""
    world = _open(service, tmp_path, monkeypatch)
    worker, work_id = _start(world, service)
    tick_until(
        worker, world,
        lambda view: any(s.get("rule_id") == "d41-merge-approval" for s in view["steps"]),
    )
    _sign_merge(world, service)
    view = tick_until(worker, world, lambda view: view["status"] == "completed")
    assert view["status"] == "completed"
    events = _owed_events(world, work_id)
    assert {event["event_type"] for event in events} == {
        "attempt_begun", "auditor_launch_settled",
    }
    pushes = list(world.sent_pushes)
    assert len(pushes) == len(events)
    key = event_push.subscription_key(
        event_push.load_master_key(service.config.config_dir), world.push_subscription_id,
    )
    for event in events:
        deliveries = _deliveries(world, event["event_id"])
        assert [row["status"] for row in deliveries] == ["delivered"]
        matching = [push for push in pushes if push[2] == f"checkpoint:{event['event_id']}"]
        assert len(matching) == 1
        url, token, idempotency_key, body, headers = matching[0]
        payload = json.loads(body)
        assert url == world.push_url
        assert token is None
        assert payload["event_id"] == str(event["event_id"])
        assert payload["rendered_text"] == event["rendered_text"]
        digest = hashlib.sha256(payload["rendered_text"].encode()).hexdigest()
        assert digest == payload["rendered_sha256"] == str(deliveries[0]["rendered_sha256"])
        assert digest == str(event["rendered_sha256"])
        assert event_push.verify(key, idempotency_key, body, headers.get("X-OMP-Signature"))

    # A later tick must not send an already attested checkpoint again.
    worker.tick()
    assert status(world.config, world.mission_id)["status"] == "completed"
    assert list(world.sent_pushes) == pushes


@pytest.mark.parametrize("arm", ["wrong_attempt", "bad_signature", "wrong_candidate"])
def test_exit_zero_invalid_pass_rejects_candidate_then_reaches_repair_bound(
    service: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arm: str,
) -> None:
    """Exit 0 cannot admit a replay, forged signature, or pass for another candidate."""
    world = _open(service, tmp_path, monkeypatch)
    scripts = tmp_path / "verifiers"
    scripts.mkdir()
    mutations = {
        "wrong_attempt": 'payload["attempt_id"] = "a-other"',
        "bad_signature": "",
        "wrong_candidate": 'payload["candidate_sha256"] = "0" * 64',
    }
    for name, mutation in mutations.items():
        signature = '"00" * 32' if name == "bad_signature" else (
            'hmac.new(Path(task["key_path"]).read_bytes(), raw, hashlib.sha256).hexdigest()'
        )
        (scripts / f"{name}.py").write_text(
            'import hashlib, hmac, json\nfrom pathlib import Path\n'
            'task = json.loads(Path(".omp/verify-task.json").read_text())\n'
            'payload = {field: task[field] for field in '
            '("candidate_sha256", "attempt_id", "verifier_id")}\n'
            'payload["verdict"] = "pass"\n'
            f'{mutation}\n'
            'raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()\n'
            f'payload["signature"] = {signature}\n'
            'Path(".omp/verdict.json").write_text(json.dumps(payload))\n',
            encoding="utf-8",
        )
    script = scripts / f"{arm}.py"
    world.verifier_argv = ("/usr/bin/python3", str(script))
    document = json.loads(world.config_path.read_text(encoding="utf-8"))
    document["verifier_argv"] = list(world.verifier_argv)
    # Bound this regression at the rejected audit, before unrelated repair work.
    document["repair_rounds"] = 0
    world.config_path.write_text(json.dumps(document), encoding="utf-8")
    world.config = load_config(world.config_path, service.config)

    exit_codes = []
    run_worker = worker_sandbox.run_worker

    def record_exit(argv, **kwargs):
        if kwargs.get("identity") == "verifier":
            # The fixture script needs its own read-only bind into the real jail.
            kwargs["ro_binds"] = [*kwargs.get("ro_binds", ()), script]
        code = run_worker(argv, **kwargs)
        if kwargs.get("identity") == "verifier":
            exit_codes.append(code)
        return code

    monkeypatch.setattr(worker_sandbox, "run_worker", record_exit)
    worker, _work_id = _start(world, service)
    view = tick_until(worker, world, lambda view: view["status"] in ("paused", "completed"))
    assert view["status"] == "paused"
    steps = _steps(world)
    audits = [s for s in steps if s.get("stage") == "audit" and s.get("kind") == "outcome"]
    assert audits
    assert exit_codes == [0] * len(audits)
    for audit in audits:
        assert audit["outcome"] == "failed"
        assert audit["verdict"] == "fail"
        assert audit["data"]["code"] == "candidate_rejected"
    pauses = [s for s in steps if s.get("kind") == "pause"]
    assert pauses[-1]["rule_id"] == "repair-bound"
    assert steps.index(audits[0]) < steps.index(pauses[-1])
    assert not any(s.get("stage") == "merge" for s in steps)
    assert git(world.remote_repo, "rev-parse", "refs/heads/main").decode().strip() == world.base_commit
    assert status(world.config, world.mission_id)["status"] != "completed"

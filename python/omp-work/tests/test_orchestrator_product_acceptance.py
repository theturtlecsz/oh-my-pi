"""Technical delivery and owner product acceptance remain separate across restart."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from omp_work.orchestrator.service import (
    OrchestratorHandler,
    StageContext,
    load_config,
    load_request,
    status,
    submit,
)
from omp_work.orchestrator.stages import PRODUCT_ACCEPTANCE_RULE
from omp_work.v1.service import Principal, WorkService
from omp_work.v1.store import PostgresWorkStore
from orchestrator_e2e_support import (
    E2EWorld,
    answer_intake,
    open_e2e_world,
    open_worker,
    seed_published_budget,
    sign_merge_answer,
    tick_until,
)
from test_workflow_service import OWNER, _command

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)


def _handler(world: E2EWorld) -> OrchestratorHandler:
    handler = OrchestratorHandler(world.config)
    handler._bind(world.mission_id)
    return handler


def _decisions(world: E2EWorld) -> list[dict[str, Any]]:
    principal = Principal(
        actor_id=OWNER,
        actor_kind="owner",
        workspaces=frozenset({world.workspace_id}),
        scopes=frozenset({"work.read", "work.client"}),
    )
    view = WorkService(PostgresWorkStore(world.config.ops)).client_read(
        principal, world.workspace_id, "project.decisions", world.project_id
    )
    return view["decisions"]


@pytest.mark.parametrize("answer", [None, "reject", "accept"])
def test_delivery_requires_only_configured_owner_acceptance(
    service: Any, tmp_path: Path, monkeypatch: Any, answer: str | None
) -> None:
    """Delivery completes once; configured acceptance alone determines mission completion."""
    world = open_e2e_world(
        service, tmp_path, monkeypatch,
        verifier_mode="pass", worker_mode="ok", operations=(),
    )
    config_doc = json.loads(world.config_path.read_text(encoding="utf-8"))
    config_doc.pop("operations")
    world.config_path.write_text(json.dumps(config_doc), encoding="utf-8")
    world.config = load_config(world.config_path, service.config)
    request_doc = json.loads(world.request_path.read_text(encoding="utf-8"))
    if answer is not None:
        request_doc["requires_product_acceptance"] = True
    world.request_path.write_text(json.dumps(request_doc), encoding="utf-8")
    world.request = load_request(world.request_path)

    completions = []
    command = StageContext.command

    def record_completion(self, command_type, payload, *, key=None):
        result = command(self, command_type, payload, key=key)
        if command_type == "complete_execution_item":
            completions.append(dict(payload))
        return result

    monkeypatch.setattr(StageContext, "command", record_completion)
    submitted = submit(world.config, world.request)
    work_id = UUID(submitted["work_id"])
    tree = world.store.read(world.workspace_id, OWNER, "tree", "")
    item = next(row for row in tree["items"] if str(row["work_id"]) == str(work_id))
    seed_published_budget(
        service, world.workspace_id, work_id, UUID(str(item["revision"]["revision_id"]))
    )
    worker = open_worker(world)
    tick_until(worker, world, lambda v: any(
        s["rule_id"] == "d23-owner-confirm" for s in v["steps"]
    ))
    intake = next(s for s in _handler(world).steps()
                  if s.get("stage") == "intake" and s["kind"] == "outcome")
    world.intake_decision_id = UUID(intake["data"]["decision_id"])
    world.mission_revision = int(intake["data"]["mission_revision"])
    assert answer_intake(world, "edited_draft")["outcome"] == "approved"

    # Recreate the handler from step 0, with no request file available.
    world.request_path.unlink()
    worker = open_worker(world)
    tick_until(worker, world, lambda v: any(
        s["rule_id"] == "d41-merge-approval" for s in v["steps"]
    ))
    merge = next(d for d in _decisions(world)
                 if d.get("action_class") == "merge_protected_branch")
    expires = datetime(2099, 1, 1, tzinfo=timezone.utc)
    signature = sign_merge_answer(
        world.owner_key_path,
        workspace_id=world.workspace_id,
        decision_id=UUID(merge["decision_id"]),
        action_class="merge_protected_branch",
        answer="approve",
        target_sha256=merge["target_sha256"],
        expires_at=expires,
    )
    code, body = _command(service, world.workspace_id, {
        "type": "answer_decision",
        "payload": {
            "decision_id": merge["decision_id"], "answer": "approve",
            "owner_signature": signature, "expires_at": expires.isoformat(),
        },
    })
    assert code == 200, body
    tick_until(worker, world, lambda v: v["status"] == "completed" or any(
        s["rule_id"] == PRODUCT_ACCEPTANCE_RULE for s in v["steps"]
    ))
    handler = _handler(world)
    closes = [s for s in handler.steps()
              if s.get("stage") == "close" and s["kind"] == "outcome"]
    assert len(closes) == 1
    close = closes[0]
    assert close["outcome"] == "succeeded"
    assert len(completions) == 1
    assert completions[0]["attempt_id"] == close["data"]["attempt_id"]
    assert completions[0]["evidence"]["subject"]["candidate_commit"] == close["data"]["candidate_commit"]
    execution = world.store.read(world.workspace_id, OWNER, "execution", completions[0]["grant_id"])
    assert next(i for i in execution["items"] if str(i["work_id"]) == str(work_id))["phase"] == "completed"
    acceptances = [d for d in _decisions(world) if "accept" in d["options"]]
    if answer is None:
        assert status(world.config, world.mission_id)["status"] == "completed"
        assert acceptances == []
        facts = handler._facts(handler.steps(), close["step_index"], "close", None)
        assert not facts.requires_product_acceptance
        assert facts.product_acceptance == "none"
        return

    assert status(world.config, world.mission_id)["status"] == "paused"
    assert len(acceptances) == 1
    acceptance = acceptances[0]
    assert acceptance["status"] == "pending"
    assert set(acceptance["options"]) == {"accept", "reject"}
    assert set(acceptance["evidence_refs"]) == {
        f"orch_step:{world.mission_id}:{close['step_index']}",
        f"delivery:{close['data']['candidate_commit']}",
    }
    pause = next(s for s in handler.steps() if s["rule_id"] == PRODUCT_ACCEPTANCE_RULE)
    assert pause["decision_id"] == acceptance["decision_id"]
    facts = handler._facts(handler.steps(), close["step_index"], "close", None)
    assert facts.requires_product_acceptance
    assert facts.product_acceptance == "pending"
    code, body = _command(service, world.workspace_id, {
        "type": "answer_decision",
        "payload": {"decision_id": acceptance["decision_id"], "answer": answer},
    })
    assert code == 200, body
    worker = open_worker(world)

    def wait_sealed(_view):
        with handler.store.transaction(world.workspace_id, OWNER) as cur:
            cur.execute("SELECT status FROM omp_jobs.jobs WHERE job_id=%s", (pause["wait_job"],))
            return cur.fetchone()["status"] == "sealed"

    tick_until(worker, world, wait_sealed)
    assert status(world.config, world.mission_id)["status"] == (
        "completed" if answer == "accept" else "paused"
    )
    facts = handler._facts(handler.steps(), close["step_index"], "close", None)
    assert facts.product_acceptance == ("pass" if answer == "accept" else "fail")
    assert len(completions) == 1
    assert len([s for s in handler.steps()
                if s.get("stage") == "close" and s["kind"] == "outcome"]) == 1
    with handler.store.transaction(world.workspace_id, OWNER) as cur:
        cur.execute(
            "SELECT count(*) AS count FROM omp_jobs.jobs WHERE workspace_id=%s "
            "AND work_id=%s AND status <> 'sealed'",
            (world.workspace_id, work_id),
        )
        assert cur.fetchone()["count"] == 0

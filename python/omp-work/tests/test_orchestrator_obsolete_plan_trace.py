"""A fresh intake PASS cannot authorize a launch stamped on an obsolete revision."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
from unittest.mock import Mock
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
import pytest

from omp_work.orchestrator import service as orchestrator, worker_sandbox
from orchestrator_e2e_support import _edited_draft
from test_orchestrator_operations_front import _intake_data, _open_front
from test_workflow_service import OWNER, _command
from typed_intake_support import advance_revision, record_pass

pytest_plugins = ("test_workflow_service",)
pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
FIXTURE = Path(__file__).with_name("fixtures") / "obsolete_plan_worker_refusal_trace.json"


def test_obsolete_plan_worker_refusal_trace(service, tmp_path, monkeypatch):
    """Both dispatch and launch refuse revision N's plan; no sandbox ever starts."""
    saved = dict(orchestrator._REGISTRY)
    try:
        world, worker, work_id = _open_front(
            service, tmp_path / "obsolete", monkeypatch, extra_capability="egress",
        )
        sandbox = Mock(wraps=worker_sandbox.run_sandboxed)
        monkeypatch.setattr(worker_sandbox, "run_sandboxed", sandbox)
        handler = orchestrator.OrchestratorHandler(world.config)
        handler.mission_id = world.mission_id
        scope = dict(workspace_id=world.workspace_id, actor_id=OWNER, mission_id=world.mission_id)
        trace = []
        step_offset = 0
        event_offset = 0
        symbols = {}

        def normalize(value):
            # Preserve equality across identities, including UUIDs embedded in run/receipt IDs.
            encoded = json.dumps(value, default=str)

            def symbol(match):
                raw = match.group()
                if raw not in symbols:
                    kind = "uuid" if "-" in raw else "hash"
                    number = 1 + sum(label.startswith(f"<{kind}:") for label in symbols.values())
                    symbols[raw] = f"<{kind}:{number}>"
                return symbols[raw]

            return json.loads(re.sub(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\b[0-9a-f]{64}\b|\b[0-9a-f]{40}\b",
                symbol, encoded,
            ))

        def capture(event):
            nonlocal step_offset, event_offset
            steps = handler.steps()
            with psycopg.connect(
                **world.config.ops.connection_kwargs("postgres"), row_factory=dict_row,
            ) as conn:
                intake = conn.execute(
                    "SELECT binding_id,run_id,revision,assessment_id,state,hold_code "
                    "FROM omp_work.mission_intake_bindings WHERE workspace_id=%s AND mission_id=%s "
                    "ORDER BY created_at,binding_id", (world.workspace_id, world.mission_id),
                ).fetchall()
                launch = conn.execute(
                    "SELECT launch_binding_id,intake_binding_id,run_id,revision,assessment_id,"
                    "plan_receipt_id,plan_sha256,item_id,work_id,candidate_id,state,hold_code "
                    "FROM omp_work.mission_launch_bindings WHERE workspace_id=%s AND mission_id=%s "
                    "ORDER BY created_at,launch_binding_id", (world.workspace_id, world.mission_id),
                ).fetchall()
                head = conn.execute(
                    "SELECT current_revision FROM omp_work.intake_heads "
                    "WHERE workspace_id=%s AND mission_id=%s", (world.workspace_id, world.mission_id),
                ).fetchone()
                events = conn.execute(
                    "SELECT event_type,outcome FROM omp_audit.domain_events "
                    "WHERE workspace_id=%s AND aggregate_id=%s "
                    "AND event_type IN ('draft_mission_intake','answer_mission_draft') ORDER BY sequence",
                    (world.workspace_id, world.mission_id),
                ).fetchall()
            trace.append(normalize({
                "event": event,
                "steps": [[step.get("stage"), step.get("kind"), step.get("outcome"),
                           step.get("data", {}).get("code"), step.get("rule_id")]
                          for step in steps[step_offset:]],
                "intake_events": events[event_offset:],
                "intake_head": head["current_revision"],
                "intake_bindings": intake,
                "launch_bindings": launch,
                "sandbox_calls": sandbox.call_count,
            }))
            step_offset, event_offset = len(steps), len(events)
            return intake, launch

        def outcomes(stage):
            return [step for step in handler.steps()
                    if step.get("stage") == stage and step.get("kind") == "outcome"]

        def tick_to(event, predicate):
            for _ in range(30):
                if predicate():
                    return
                worker.tick()
                capture(event)
            pytest.fail(f"{event} did not finish: {handler.steps()}")

        capture("submitted")
        tick_to("owner_confirmation", lambda: any(
            step.get("kind") == "pause" and step.get("rule_id") == "d23-owner-confirm"
            for step in handler.steps()
        ))
        intake_data = _intake_data(world)
        world.intake_decision_id = UUID(str(intake_data["decision_id"]))
        world.mission_revision = int(intake_data["mission_revision"])
        # Owner drops extra capability through the actual HTTP client command path.
        code, body = _command(service, world.workspace_id, {
            "type": "answer_mission_draft",
            "payload": {
                "decision_id": str(world.intake_decision_id),
                "mission_id": str(world.mission_id), "revision": world.mission_revision,
                "answer": {"kind": "edited_draft", "draft": _edited_draft(world).model_dump(mode="json")},
                "instruction": world.request.instruction.model_dump(mode="json"),
            },
        })
        assert code == 200, body
        capture("owner_answered")
        tick_to("grant", lambda: bool(outcomes("grant")))
        assert outcomes("grant")[-1]["outcome"] == "succeeded"
        assert not outcomes("implement")
        intake, launch = capture("plan_stamped")
        assert len(launch) == 1 and launch[0]["state"] == "bound"
        assert launch[0]["work_id"] == work_id
        admitted = intake[-1]
        assert admitted["state"] == "admitted"
        assert launch[0]["intake_binding_id"] == admitted["binding_id"]
        assert launch[0]["revision"] == admitted["revision"]
        assert launch[0]["assessment_id"] == admitted["assessment_id"]
        original_launch = launch[0]

        revision = advance_revision(world.store, **scope)
        assert revision == admitted["revision"] + 1
        _, launch = capture("revision_advanced")
        assert launch == [original_launch | {"state": "held", "hold_code": "intake_revision_advanced"}]
        worker.tick()
        capture("implement_stage_fenced")
        assert outcomes("implement")[-1]["data"]["code"] == "intake_revision_advanced"
        assert outcomes("implement")[-1]["outcome"] == "failed"
        sandbox.assert_not_called()

        receipt = record_pass(world.store, **scope)
        capture("new_pass")
        pauses = [step for step in handler.steps()
                  if step.get("kind") == "pause" and step.get("rule_id") == "retry-bound"]
        if pauses:
            code, body = _command(service, world.workspace_id, {
                "type": "answer_decision",
                "payload": {"decision_id": pauses[-1]["decision_id"], "answer": "resume"},
            })
            assert code == 200, body
            capture("retry_resumed")
        count = len(outcomes("implement"))
        tick_to("implement_launch_refused", lambda: len(outcomes("implement")) > count)
        refused = outcomes("implement")[-1]
        assert refused["outcome"] == "failed"
        assert refused["data"] == {
            "stage": "implement", "code": "launch_binding_held", "hold_code": "intake_revision_advanced",
        }
        intake, launch = capture("obsolete_plan_refused")
        assert (intake[-1]["state"], intake[-1]["revision"], intake[-1]["assessment_id"]) == (
            "admitted", revision, receipt,
        )
        assert launch == [original_launch | {"state": "held", "hold_code": "intake_revision_advanced"}]
        sandbox.assert_not_called()
        rendered = json.dumps(trace, indent=2, sort_keys=True) + "\n"
        assert rendered == FIXTURE.read_text()
    finally:
        orchestrator._REGISTRY.clear()
        orchestrator._REGISTRY.update(saved)

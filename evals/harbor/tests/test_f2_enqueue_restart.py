"""Test that f2 /execute crash at enqueue boundary restarts once with same session.

OMP-463-s05:
Use f2's command and kill_at, terminal `/execution/items/0/phase` in `["planning"]`,
and this script: [seal_execution_criteria `["the one-item grant finishes once"]` with
`hold_s` 30, the same call without hold, `{"text": "sealed"}`]. Assert:
- outcome `planning`;
- session.json has exactly one restart with the original id;
- the model log's step-1 request has no assistant message (the restarted omp
  re-dispatched the persisted continuation);
- readback `continuations_scheduled` is 0 and `items[0].close_attempts_started` is 0.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from execute_stand import execute_stand
from omp_harbor_eval import EvidenceWriter, Scenario, Terminal, load_fixture
from omp_harbor_eval.grader import OUTCOME, SERVICE_READBACK, resolve_pointer

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def test_f2_enqueue_restart(tmp_path: Path) -> None:
    fixture = load_fixture(FIXTURES, "f2")
    assert fixture.scenario.kill_at is not None

    script = [
        {
            "tool_calls": [
                {
                    "name": "work",
                    "arguments": {
                        "action": "seal_execution_criteria",
                        "criteria": ["the one-item grant finishes once"],
                    },
                }
            ],
            "hold_s": 30,
        },
        {
            "tool_calls": [
                {
                    "name": "work",
                    "arguments": {
                        "action": "seal_execution_criteria",
                        "criteria": ["the one-item grant finishes once"],
                    },
                }
            ],
        },
        {"text": "sealed"},
    ]

    scenario = Scenario(
        command=fixture.scenario.command,
        terminal=Terminal(pointer="/execution/items/0/phase", accepted=("planning",)),
        model_script=tuple(script),
        ui_script=(),
        kill_at=fixture.scenario.kill_at,
        timeout_s=120,
        filename="scenario.json",
    )

    evidence_dir = tmp_path / "evidence"
    writer = EvidenceWriter(
        evidence_dir,
        run_id="run-f2-restart",
        nonce="nonce-f2-restart",
        fixture_id=fixture.id,
        fixture_digest=fixture.digest,
        variant="known_good",
        experiment=fixture.scored_experiment,
    )

    with execute_stand(tmp_path, script=script) as stand:
        outcome = stand.run(scenario, writer)

    assert outcome == "planning"

    # Outcome document
    outcome_doc = json.loads((evidence_dir / OUTCOME).read_text(encoding="utf-8"))
    assert outcome_doc.get("outcome") == "planning"

    # session.json has exactly one restart with the original id
    session_doc = json.loads((evidence_dir / "session.json").read_text(encoding="utf-8"))
    restarts = session_doc.get("restarts", [])
    assert len(restarts) == 1
    assert restarts[0]["id"] == stand.last_adapter._enqueue_session_id
    resumed_file = Path(restarts[0]["file"])
    assert resumed_file.is_file()
    session_header = json.loads(resumed_file.read_text(encoding="utf-8").splitlines()[1])
    assert session_header["id"] == restarts[0]["id"]

    # the model log's step-1 request has no assistant message (the restarted omp re-dispatched the persisted continuation)
    records = [
        json.loads(line)
        for line in stand.model_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    step_1_records = [r for r in records if r.get("step") == 1]
    assert len(step_1_records) >= 1, f"expected at least one step-1 record, found: {records}"
    step_1_req = step_1_records[0]
    messages = step_1_req["body"]["messages"]
    assert not any(msg.get("role") == "assistant" for msg in messages)

    # readback continuations_scheduled is 0 and items[0].close_attempts_started is 0
    readback = json.loads((evidence_dir / SERVICE_READBACK).read_text(encoding="utf-8"))
    assert resolve_pointer(readback, "/execution/grant/continuations_scheduled") == 0
    assert resolve_pointer(readback, "/execution/items/0/close_attempts_started") == 0

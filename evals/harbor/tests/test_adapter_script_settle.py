"""A scripted trial stays open after the terminal readback until the agent ends.

The ledger pointer can match while the model script still has a tool call.
Sealing on that read kills the RPC process and drops the call. The adapter
waits for agent_end or the final assistant text, then seals the later readback.
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
from pathlib import Path

from fake_workservice import FakeWorkService
from omp_harbor_eval import EvidenceWriter, RpcAdapter, ServiceProbe, load_evidence, load_fixture

FAKE_RPC = Path(__file__).with_name("fake_omp_rpc.py")
WORKSPACE = "00000000-0000-4000-8000-0000000000aa"
WORK_ID = "OMP-1"
BEARER = "harbor-test-token"
SESSION_ID = "sess-1"
REFUSAL = (
    "REFUSED — revision conflict: the item moved from revision "
    "00000000-0000-7000-8000-000000000010 to 00000000-0000-7000-8000-000000000020 "
    "since the preview was generated."
)


def _write_fixture(root: Path) -> object:
    directory = root / "f1"
    directory.mkdir(parents=True)
    fixture = {
        "id": "f1",
        "scored_experiment": "repair",
        "seed_patch": "",
        "solution_patch": "",
        "independent_tests": [],
        "scenario": "scenario.json",
        "rules": [],
    }
    scenario = {
        "command": "/execute OMP-1",
        "terminal": {"pointer": "/work_item/revision/revision_number", "in": [2]},
        "model_script": [
            {
                "tool_calls": [
                    {
                        "id": "revise-stale-confirm",
                        "name": "work",
                        "arguments": {"action": "revise_work", "confirm": True},
                    }
                ]
            },
            {"text": "The amendment landed as revision 2; the stale re-revision was refused."},
        ],
        "ui_script": [],
        "timeout_s": 5,
    }
    (directory / "fixture.json").write_text(json.dumps(fixture) + "\n", encoding="utf-8")
    (directory / "scenario.json").write_text(json.dumps(scenario) + "\n", encoding="utf-8")
    return load_fixture(root, "f1")


def _open_sealed(directory: Path):
    digest = hashlib.sha256((directory / "manifest.json").read_bytes()).hexdigest()
    return load_evidence(directory, digest)


def test_terminal_readback_waits_for_the_last_scripted_refusal(tmp_path: Path) -> None:
    """The refusal emitted after the pointer matches is in transcript.jsonl.

    The work item is already at revision 2. The fake stream holds before the
    stale confirm, and releases only on the second read taken while it is
    holding, so that read has already observed the terminal pointer. The view
    from that read is not the sealed one: the readback after the agent ends
    is.
    """

    fixture = _write_fixture(tmp_path / "fixtures")
    evidence_dir = tmp_path / "evidence"
    writer = EvidenceWriter(
        evidence_dir, "run-1", "nonce-1", fixture.id, fixture.digest, "known_good", fixture.scored_experiment
    )
    record = tmp_path / "rpc-record"
    session_file = tmp_path / "session" / "session.jsonl"
    waiting = tmp_path / "waiting"
    release = tmp_path / "release"
    events = [
        {"type": "agent_start"},
        {"type": "turn_start"},
        {
            "type": "harbor_test_wait",
            "waiting": str(waiting),
            "release": str(release),
            "timeout_s": 8,
        },
        {
            "type": "tool_execution_start",
            "toolName": "work",
            "toolCallId": "revise-stale-confirm",
            "args": {
                "action": "revise_work",
                "work": WORK_ID,
                "confirm": True,
                "expected_revision_id": "00000000-0000-7000-8000-000000000010",
            },
        },
        {
            "type": "tool_execution_end",
            "toolName": "work",
            "toolCallId": "revise-stale-confirm",
            "isError": True,
            "result": {
                "content": [{"type": "text", "text": REFUSAL}],
                "details": {"success": False},
            },
        },
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "stop",
                "content": [
                    {
                        "type": "text",
                        "text": "The amendment landed as revision 2; the stale re-revision was refused.",
                    }
                ],
            },
        },
        {"type": "agent_end", "messages": [], "isTerminal": True},
    ]
    events_path = tmp_path / "events.json"
    events_path.write_text(json.dumps(events), encoding="utf-8")
    command = [
        sys.executable,
        str(FAKE_RPC),
        "--record",
        str(record),
        "--session-file",
        str(session_file),
        "--evidence",
        str(evidence_dir),
        "--session-id",
        SESSION_ID,
        "--events",
        str(events_path),
    ]

    early = {
        "work_id": WORK_ID,
        "state": "completed",
        "revision": {"revision_number": 2},
        "candidate": {"candidate_id": "early"},
        "phase": "early",
    }
    final = {
        "work_id": WORK_ID,
        "state": "completed",
        "revision": {"revision_number": 2},
        "candidate": None,
        "phase": "final",
    }
    phase = {"during_wait": 0, "after_release": 0}
    lock = threading.Lock()
    transcript = evidence_dir / "rpc-transcript.jsonl"

    def work_item() -> dict:
        # Stay non-terminal until the agent turn is on the transcript, so the
        # matching read cannot land before the adapter has seen the turn.
        if not transcript.is_file() or "agent_start" not in transcript.read_text(encoding="utf-8"):
            return {
                "work_id": WORK_ID,
                "state": "running",
                "revision": {"revision_number": 1},
                "candidate": None,
                "phase": "before-agent",
            }
        with lock:
            if waiting.is_file() and not release.is_file():
                phase["during_wait"] += 1
                if phase["during_wait"] >= 2:
                    release.write_text("go\n", encoding="utf-8")
            if release.is_file():
                phase["after_release"] += 1
                use_final = phase["after_release"] >= 2
            else:
                use_final = False
        return final if use_final else early

    def execution() -> dict:
        return {
            "grant": {"state": "completed"},
            "items": [{"work_id": WORK_ID}],
            "active_item": {"work_id": WORK_ID},
        }

    with FakeWorkService(
        bearer=BEARER,
        workspace_id=WORKSPACE,
        ready=True,
        execution=execution,
        work_item=work_item,
    ) as service:
        probe = ServiceProbe(service.base_url, BEARER, WORKSPACE)
        outcome = RpcAdapter(command, tmp_path, None, probe, writer, fixture.scenario).run()

    assert outcome == "completed"
    loaded = _open_sealed(evidence_dir)
    assert loaded.read_json("outcome.json") == {"outcome": "completed", "reason": "terminal", "prompts_sent": 1}
    records = loaded.read_jsonl("transcript.jsonl")
    assert len(records) == 1
    assert records[0]["decision"] == "revise_work"
    assert records[0]["refused"] is True
    assert records[0]["text"] == REFUSAL
    readback = loaded.read_json("service-readback.json")
    assert readback["work_item"]["phase"] == "final"
    assert readback["work_item"]["candidate"] is None
    assert readback["work_item"]["revision"]["revision_number"] == 2

"""RpcAdapter writes the evidence file the grader's transcript rule reads.

Layer 3 defect: grader.py requires ``transcript.jsonl`` (rule
``transcript_count``, f1 count 1) but the adapter only wrote
``rpc-transcript.jsonl``, so every good f1 run graded ``invalid_evidence``.
The adapter must write the semantic records the rule counts — one per refused
``work`` tool call — while keeping every raw frame in ``rpc-transcript.jsonl``.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from fake_workservice import FakeWorkService
from omp_harbor_eval import EvidenceWriter, RpcAdapter, ServiceProbe, load_evidence, load_fixture
from omp_harbor_eval.adapter import RPC_TRANSCRIPT, _Transcript
from omp_harbor_eval.grader import TRANSCRIPT, _rule_transcript

FAKE_RPC = Path(__file__).with_name("fake_omp_rpc.py")
HARBOR_DIR = Path(__file__).resolve().parent.parent
WORKSPACE = "00000000-0000-4000-8000-0000000000aa"
BEARER = "harbor-test-token"
SESSION_ID = "sess-1"


def open_sealed(directory: Path):
    digest = hashlib.sha256((directory / "manifest.json").read_bytes()).hexdigest()
    return load_evidence(directory, digest)


def _writer(tmp_path: Path) -> EvidenceWriter:
    return EvidenceWriter(
        tmp_path / "evidence",
        "run-test",
        "nonce-test",
        "f1",
        hashlib.sha256(b"f1-test").hexdigest(),
        "known_good",
        "repair",
    )


def _work_start(tool_call_id: str, args: dict) -> dict:
    return {"type": "tool_execution_start", "toolName": "work", "toolCallId": tool_call_id, "args": args}


def _work_end(tool_call_id: str, text: str, *, success: bool | None = None, is_error: bool = True) -> dict:
    details: dict = {"isError": is_error}
    if success is not None:
        details["success"] = success
    return {
        "type": "tool_execution_end",
        "toolName": "work",
        "toolCallId": tool_call_id,
        "isError": is_error,
        "result": {"content": [{"type": "text", "text": text}], "details": details},
    }


def test_adapter_transcript_matches_grader_transcript_constant() -> None:
    """The adapter derives the exact filename required by grader.TRANSCRIPT."""
    assert TRANSCRIPT == "transcript.jsonl"
    assert RPC_TRANSCRIPT == "rpc-transcript.jsonl"


def test_rpc_frames_stay_in_rpc_transcript(tmp_path: Path) -> None:
    """Every frame goes to rpc-transcript.jsonl; no raw frame reaches transcript.jsonl."""
    writer = _writer(tmp_path)
    transcript = _Transcript(writer)
    frames = [
        {"type": "available_commands_update", "commands": ["/execute"]},
        _work_start("call-1", {"action": "revise_work", "expected_revision_id": "rev-10"}),
        _work_end("call-1", "REFUSED — revision conflict: the item moved.", success=False),
    ]
    for frame in frames:
        transcript.write("in", frame)
    transcript.write("out", {"type": "prompt", "message": "/execute OMP-1"})
    transcript.close()
    digest = writer.seal()

    loaded = load_evidence(tmp_path / "evidence", digest)
    assert TRANSCRIPT in loaded.files
    assert RPC_TRANSCRIPT in loaded.files

    rpc_rows = loaded.read_jsonl(RPC_TRANSCRIPT)
    assert [row["frame"] for row in rpc_rows] == [*frames, {"type": "prompt", "message": "/execute OMP-1"}]
    assert all(row["direction"] == "in" for row in rpc_rows[:-1])
    assert rpc_rows[-1] == {"direction": "out", "frame": {"type": "prompt", "message": "/execute OMP-1"}}

    # The grader transcript holds only the derived decision record, never raw frames.
    assert loaded.read_jsonl(TRANSCRIPT) == [
        {
            "decision": "revise_work",
            "refused": True,
            "expected_revision_id": "rev-10",
            "text": "REFUSED — revision conflict: the item moved.",
        }
    ]


def test_only_refused_work_calls_are_recorded(tmp_path: Path) -> None:
    """A refusal makes one record; a preview, success, infra failure, and other tools make none."""
    writer = _writer(tmp_path)
    transcript = _Transcript(writer)

    # A first-phase confirmation preview: deny() (success false) but writes nothing.
    transcript.write("in", _work_start("preview", {"action": "revise_work", "expected_revision_id": "rev-1"}))
    transcript.write(
        "in",
        _work_end(
            "preview",
            "CONFIRM REQUIRED — nothing written.\n\nModel wants to revise this work in place\n\nconfirmation_id: cf-abc123",
            success=False,
        ),
    )

    # Refused by the host's deny() shape: details.success is false.
    transcript.write("in", _work_start("refused", {"action": "revise_work", "expected_revision_id": "rev-1"}))
    transcript.write("in", _work_end("refused", "REFUSED — revision conflict: the item moved to revision rev-2.", success=False))

    # A landed write: okText sets details.success true.
    transcript.write("in", _work_start("landed", {"action": "revise_work"}))
    transcript.write("in", _work_end("landed", "OMP-1 revised", success=True, is_error=False))

    # Infrastructure failure: the extension never loaded, so no decision was made.
    transcript.write("in", _work_start("missing", {"action": "revise_work"}))
    transcript.write("in", _work_end("missing", "Tool work not found"))

    # A different tool is never a work decision.
    transcript.write("in", {"type": "tool_execution_start", "toolName": "read", "toolCallId": "read-1", "args": {}})
    transcript.write("in", {"type": "tool_execution_end", "toolName": "read", "toolCallId": "read-1", "result": {"content": []}})

    transcript.close()
    digest = writer.seal()

    loaded = load_evidence(tmp_path / "evidence", digest)
    records = loaded.read_jsonl(TRANSCRIPT)
    assert len(records) == 1
    assert records[0]["decision"] == "revise_work"
    assert records[0]["refused"] is True
    assert records[0]["expected_revision_id"] == "rev-1"
    assert records[0]["text"] == "REFUSED — revision conflict: the item moved to revision rev-2."

    # grader._rule_transcript reads the derived file: one record satisfies f1's count.
    assert _rule_transcript(loaded, {"count": 1}) == ("ok", [])
    assert _rule_transcript(loaded, {"count": 2}) == ("fail", ["transcript_count: expected 2 got 1"])


def test_adapter_creates_transcript_file_with_zero_refusals(tmp_path: Path) -> None:
    """The adapter creates transcript.jsonl even when no work refusals occur."""
    writer = _writer(tmp_path)
    transcript = _Transcript(writer)
    # Only non-refusal frames: a command update and a landed write.
    transcript.write("in", {"type": "available_commands_update", "commands": ["/execute"]})
    transcript.write("in", _work_start("landed", {"action": "revise_work"}))
    transcript.write("in", _work_end("landed", "OMP-1 revised", success=True, is_error=False))
    transcript.close()
    digest = writer.seal()

    loaded = load_evidence(tmp_path / "evidence", digest)
    assert TRANSCRIPT in loaded.files
    assert loaded.read_bytes(TRANSCRIPT) == b""
    assert loaded.read_jsonl(TRANSCRIPT) == []


def test_grader_transcript_count_rule_treats_empty_file_as_zero_records(tmp_path: Path) -> None:
    """An empty transcript.jsonl counts as 0 records, passing count:0 and failing count:1."""
    writer = _writer(tmp_path)
    transcript = _Transcript(writer)
    transcript.close()
    digest = writer.seal()

    loaded = load_evidence(tmp_path / "evidence", digest)
    assert TRANSCRIPT in loaded.files
    assert loaded.read_jsonl(TRANSCRIPT) == []

    # count: 0 passes because actual len([]) == 0.
    assert _rule_transcript(loaded, {"count": 0}) == ("ok", [])
    # count: 1 fails because expected 1 != actual 0 (never returns invalid_evidence).
    assert _rule_transcript(loaded, {"count": 1}) == ("fail", ["transcript_count: expected 1 got 0"])


def test_fake_rpc_f1_stale_confirm_produces_one_refusal_record(tmp_path: Path) -> None:
    """Fake RPC trial driving f1's 4 steps: amend preview, amend confirm, stale preview, stale confirm.

    Only the stale confirm is refused by the host's expected-revision check, producing
    exactly one refusal record in transcript.jsonl.
    """
    evidence_dir = tmp_path / "evidence"
    writer = EvidenceWriter(
        evidence_dir, "run-f1", "nonce-f1", "f1", hashlib.sha256(b"f1").hexdigest(), "known_good", "repair"
    )
    record = tmp_path / "rpc-record"
    session_file = tmp_path / "session" / "session.jsonl"
    fixture = load_fixture(HARBOR_DIR / "fixtures", "f1")

    events = [
        {"type": "agent_start"},
        # 1. Amend preview: confirmation required (not a refusal)
        _work_start(
            "revise-amend-preview",
            {
                "action": "revise_work",
                "work": "OMP-1",
                "scope": "amended/scope",
                "acceptance_criteria": ["Amended AC 1", "Amended AC 2"],
                "expected_revision_id": "00000000-0000-7000-8000-000000000010",
            },
        ),
        _work_end(
            "revise-amend-preview",
            "CONFIRM REQUIRED — nothing written.\n\nModel wants to revise this work in place\n\nconfirmation_id: cf-amend1\n",
            success=False,
        ),
        # 2. Amend confirm: landed write (success: true, not a refusal)
        _work_start(
            "revise-amend-confirm",
            {
                "action": "revise_work",
                "work": "OMP-1",
                "scope": "amended/scope",
                "acceptance_criteria": ["Amended AC 1", "Amended AC 2"],
                "expected_revision_id": "00000000-0000-7000-8000-000000000010",
                "confirm": True,
                "confirmation_id": "cf-amend1",
            },
        ),
        _work_end("revise-amend-confirm", "OMP-1 revised to revision 2", success=True, is_error=False),
        # 3. Stale retry preview: confirmation required (not a refusal)
        _work_start(
            "revise-stale-retry",
            {
                "action": "revise_work",
                "work": "OMP-1",
                "scope": "stale/scope",
                "acceptance_criteria": ["Stale AC"],
                "expected_revision_id": "00000000-0000-7000-8000-000000000010",
            },
        ),
        _work_end(
            "revise-stale-retry",
            "CONFIRM REQUIRED — nothing written.\n\nModel wants to revise this work in place\n\nconfirmation_id: cf-stale2\n",
            success=False,
        ),
        # 4. Stale retry confirm: refused as stale revision by the host!
        _work_start(
            "revise-stale-confirm",
            {
                "action": "revise_work",
                "work": "OMP-1",
                "scope": "stale/scope",
                "acceptance_criteria": ["Stale AC"],
                "expected_revision_id": "00000000-0000-7000-8000-000000000010",
                "confirm": True,
                "confirmation_id": "cf-stale2",
            },
        ),
        _work_end(
            "revise-stale-confirm",
            "REFUSED — revision conflict: the item moved from revision "
            "00000000-0000-7000-8000-000000000010 to 00000000-0000-7000-8000-000000000020 "
            "since the preview was generated.",
            success=False,
        ),
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

    work_item = {
        "work_id": "OMP-1",
        "state": "completed",
        "revision": {
            "revision_number": 2,
            "acceptance_criteria": ["Amended AC 1", "Amended AC 2"],
        },
        "candidate": None,
    }

    with FakeWorkService(
        bearer=BEARER,
        workspace_id=WORKSPACE,
        ready=True,
        execution={"grant": {"state": "completed"}, "items": [{"work_id": "OMP-1"}], "active_item": {"work_id": "OMP-1"}},
        work_item=work_item,
    ) as service:
        probe = ServiceProbe(service.base_url, BEARER, WORKSPACE)
        outcome = RpcAdapter(command, tmp_path, None, probe, writer, fixture.scenario).run()

    assert outcome == "completed"
    loaded = open_sealed(evidence_dir)
    assert TRANSCRIPT in loaded.files
    records = loaded.read_jsonl(TRANSCRIPT)
    assert len(records) == 1
    assert records[0]["decision"] == "revise_work"
    assert records[0]["refused"] is True
    assert records[0]["expected_revision_id"] == "00000000-0000-7000-8000-000000000010"
    assert "REFUSED — revision conflict:" in records[0]["text"]

    assert _rule_transcript(loaded, {"count": 1}) == ("ok", [])
    assert _rule_transcript(loaded, {"count": 0}) == ("fail", ["transcript_count: expected 0 got 1"])


def test_fake_rpc_zero_refusals_produces_empty_transcript(tmp_path: Path) -> None:
    """A trial over fake RPC with no refusals creates transcript.jsonl as an empty file."""
    evidence_dir = tmp_path / "evidence"
    writer = EvidenceWriter(
        evidence_dir, "run-zero", "nonce-zero", "f1", hashlib.sha256(b"f1").hexdigest(), "known_good", "repair"
    )
    record = tmp_path / "rpc-record"
    session_file = tmp_path / "session" / "session.jsonl"
    fixture = load_fixture(HARBOR_DIR / "fixtures", "f1")

    events = [
        {"type": "agent_start"},
        # Only landed writes and previews, no refusals
        _work_start("amend-preview", {"action": "revise_work", "expected_revision_id": "rev-1"}),
        _work_end("amend-preview", "CONFIRM REQUIRED — nothing written.", success=False),
        _work_start("amend-confirm", {"action": "revise_work", "confirm": True}),
        _work_end("amend-confirm", "revised", success=True, is_error=False),
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

    with FakeWorkService(
        bearer=BEARER,
        workspace_id=WORKSPACE,
        ready=True,
        execution={"grant": {"state": "completed"}, "items": [{"work_id": "OMP-1"}], "active_item": {"work_id": "OMP-1"}},
        work_item={"work_id": "OMP-1", "revision": {"revision_number": 2}},
    ) as service:
        probe = ServiceProbe(service.base_url, BEARER, WORKSPACE)
        outcome = RpcAdapter(command, tmp_path, None, probe, writer, fixture.scenario).run()

    assert outcome == "completed"
    loaded = open_sealed(evidence_dir)
    assert TRANSCRIPT in loaded.files
    assert loaded.read_bytes(TRANSCRIPT) == b""
    assert loaded.read_jsonl(TRANSCRIPT) == []
    assert _rule_transcript(loaded, {"count": 0}) == ("ok", [])
    assert _rule_transcript(loaded, {"count": 1}) == ("fail", ["transcript_count: expected 1 got 0"])

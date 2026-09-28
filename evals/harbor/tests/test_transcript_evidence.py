"""RpcAdapter writes the evidence file the grader's transcript rule reads.

Layer 3 defect: grader.py requires ``transcript.jsonl`` (rule
``transcript_count``, f1 count 1) but the adapter only wrote
``rpc-transcript.jsonl``, so every good f1 run graded ``invalid_evidence``.
The adapter must write the semantic records the rule counts — one per refused
``work`` tool call — while keeping every raw frame in ``rpc-transcript.jsonl``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from omp_harbor_eval import EvidenceWriter, load_evidence
from omp_harbor_eval.adapter import RPC_TRANSCRIPT, _Transcript
from omp_harbor_eval.grader import TRANSCRIPT, _rule_transcript


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

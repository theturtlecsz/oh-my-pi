"""Test that RpcAdapter writes the evidence file required by grader's transcript rule."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from omp_harbor_eval import EvidenceWriter, load_evidence
from omp_harbor_eval.adapter import RPC_TRANSCRIPT, _Transcript
from omp_harbor_eval.grader import TRANSCRIPT, _rule_transcript


def test_adapter_transcript_matches_grader_transcript_constant() -> None:
    """The adapter writes to the exact filename required by grader.TRANSCRIPT."""
    assert TRANSCRIPT == "transcript.jsonl"
    assert RPC_TRANSCRIPT == "rpc-transcript.jsonl"


def test_transcript_writer_writes_both_rpc_and_grader_transcripts(tmp_path: Path) -> None:
    """_Transcript appends rows to both rpc-transcript.jsonl and transcript.jsonl."""
    evidence_dir = tmp_path / "evidence"
    digest = hashlib.sha256(b"f1-test").hexdigest()
    writer = EvidenceWriter(
        evidence_dir,
        "run-test",
        "nonce-test",
        "f1",
        digest,
        "known_good",
        "repair",
    )
    transcript = _Transcript(writer)
    frame = {"type": "available_commands_update", "commands": ["/execute"]}
    transcript.write("in", frame)
    transcript.close()
    digest = writer.seal()

    loaded = load_evidence(evidence_dir, digest)
    assert TRANSCRIPT in loaded.files
    assert RPC_TRANSCRIPT in loaded.files

    grader_records = loaded.read_jsonl(TRANSCRIPT)
    assert len(grader_records) == 1
    assert grader_records[0] == {"direction": "in", "frame": frame}

    rpc_records = loaded.read_jsonl(RPC_TRANSCRIPT)
    assert len(rpc_records) == 1
    assert rpc_records[0] == {"direction": "in", "frame": frame}

    # Verify grader._rule_transcript reads this file
    status, reasons = _rule_transcript(loaded, {"count": 1})
    assert status == "ok"
    assert reasons == []

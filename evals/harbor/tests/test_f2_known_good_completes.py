"""f2 known_good runs scenario.json through the execute stand to completion.

The fixture script is unchanged: the enqueue kill drops the held first
get_execution, the restarted session finishes the one-item grant once, and
the scripted model never answers 500.
"""

from __future__ import annotations

import json
from pathlib import Path

from execute_stand import execute_stand
from omp_harbor_eval import EvidenceWriter, load_fixture
from omp_harbor_eval.grader import SERVICE_READBACK, resolve_pointer

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def test_f2_known_good_completes(tmp_path: Path) -> None:
    fixture = load_fixture(FIXTURES, "f2")
    evidence_dir = tmp_path / "evidence"
    writer = EvidenceWriter(
        evidence_dir,
        run_id="run-f2-known-good",
        nonce="nonce-f2-known-good",
        fixture_id=fixture.id,
        fixture_digest=fixture.digest,
        variant="known_good",
        experiment=fixture.scored_experiment,
    )

    with execute_stand(tmp_path, script=list(fixture.scenario.model_script)) as stand:
        outcome = stand.run(fixture.scenario, writer)
        log_text = stand.model_log.read_text(encoding="utf-8") if stand.model_log.is_file() else ""

    assert outcome == "completed"

    session_doc = json.loads((evidence_dir / "session.json").read_text(encoding="utf-8"))
    assert len(session_doc.get("restarts", [])) == 1

    readback = json.loads((evidence_dir / SERVICE_READBACK).read_text(encoding="utf-8"))
    assert resolve_pointer(readback, "/execution/grant/state") == "completed"
    assert resolve_pointer(readback, "/execution/grant/continuations_scheduled") == 0
    assert resolve_pointer(readback, "/execution/items/0/close_attempts_started") == 1

    records = [json.loads(line) for line in log_text.splitlines() if line.strip()]
    assert not any(record.get("status") == 500 for record in records)

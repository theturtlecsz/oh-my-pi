from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from omp_work.v1.models import EvidenceKind, EvidenceReceipt
from support.stub_events import FakeEvents, make_event
from support.stub_generator import StubGenerator

from omp_knowledge.learning.capture import drain
from omp_knowledge.learning.cli import EXIT_OK, _resolve_repository, main as cli_main
from omp_knowledge.learning.corrections import correct
from omp_knowledge.learning.generation import (
    GenerationResult,
    GeneratorUnavailable,
)
from omp_knowledge.learning.models import (
    Attribution,
    Claim,
    Lesson,
    Precondition,
    SourceIdentity,
)
from omp_knowledge.learning.policy import (
    REASON_NARRATION_ONLY,
    NativeReceipts,
)
from omp_knowledge.learning.store import LearningStore
from omp_knowledge.learning.uses import (
    procedure_history,
    record_outcome,
    record_use,
    supply,
)

WORKSPACE = "11111111-1111-1111-1111-111111111111"
HEX = "0" * 64


class DictReceiptReader:
    """Dict-backed fake reader conforming to the NativeReceipts protocol."""

    def __init__(self, receipts: dict[str | UUID, EvidenceReceipt] | None = None) -> None:
        self._receipts: dict[str, EvidenceReceipt] = {}
        if receipts:
            for k, v in receipts.items():
                self._receipts[str(k)] = v

    def add(self, receipt: EvidenceReceipt) -> None:
        self._receipts[str(receipt.receipt_id)] = receipt

    def receipt(self, receipt_id: UUID | str) -> EvidenceReceipt:
        key = str(receipt_id)
        if key not in self._receipts:
            raise KeyError(f"Receipt {receipt_id} not found in reader")
        return self._receipts[key]


def make_receipt(
    *,
    receipt_id: UUID | None = None,
    candidate_id: UUID | None = None,
    kind: EvidenceKind = EvidenceKind.VERIFICATION,
    verdict: str | None = "PASS",
    payload: dict[str, Any] | None = None,
) -> EvidenceReceipt:
    return EvidenceReceipt(
        receipt_id=receipt_id or uuid4(),
        work_id=uuid4(),
        revision_id=uuid4(),
        candidate_id=candidate_id or uuid4(),
        kind=kind,
        payload=payload if payload is not None else {"exit_code": 0},
        payload_sha256=HEX,
        issuer="test-runner",
        issued_at=datetime.now(timezone.utc),
        verdict=verdict,
        independent=True,
    )


def make_attribution(
    model: str = "journey-model",
    profile: str = "journey-profile",
) -> Attribution:
    return Attribution(
        model=model,
        profile=profile,
        source=SourceIdentity(
            workspace_id=WORKSPACE,
            event_id=str(uuid4()),
            event_sequence=1,
            aggregate_id=str(uuid4()),
        ),
    )


def test_full_learning_journey(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Prove the complete FK-6 learning journey in a single store:
    1. Task A complete_work event -> drain -> accepted procedure with model/profile/source;
    2. Bad lesson (narration only) rejected with reason;
    3. Independent task B -> cli.main supply shows it;
    4. record_use + record_outcome for B's candidate;
    5. History shows supplied/used/outcome;
    6. NEEDS_FIX receipt narrows it so next supply for B's repository is empty while cleanup is pending;
    7. Outage drain -> failed retryable, not success.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    store = LearningStore(str(state_dir))
    reader = DictReceiptReader()

    # --- Step 1: Task A complete_work event -> drain -> accepted procedure with model/profile/source ---
    receipt_a = make_receipt(kind=EvidenceKind.VERIFICATION, verdict="PASS")
    reader.add(receipt_a)

    lesson_a = Lesson(
        title="Safe Worker Migration",
        steps=("graceful drain", "commit offset", "stop worker"),
        claims=(
            Claim(
                text="Verification proves clean worker shutdown",
                receipt_ids=(receipt_a.receipt_id,),
            ),
        ),
    )

    event_a = make_event(sequence=1, event_type="complete_work", workspace_id=UUID(WORKSPACE))
    generator_a = StubGenerator(
        [
            GenerationResult(
                model="model-qwen-task-a",
                profile="profile-task-a",
                request_sha256=HEX,
                response_sha256=HEX,
                lessons=(lesson_a,),
            )
        ],
        model="model-qwen-task-a",
        profile="profile-task-a",
    )

    run_a = drain(store, FakeEvents([event_a]), reader, generator_a, workspace_id=WORKSPACE)
    assert run_a.status == "succeeded"
    assert run_a.units_total == 1
    assert run_a.proposals_accepted == 1
    assert run_a.proposals_rejected == 0

    proc_row = store.execute("SELECT * FROM procedures WHERE status = 'active'").fetchone()
    assert proc_row is not None
    proc_id = proc_row["procedure_id"]
    assert proc_row["title"] == "Safe Worker Migration"
    assert proc_row["current_version"] == 1

    ver_row = store.execute(
        "SELECT * FROM procedure_versions WHERE procedure_id = ? AND version = 1",
        (proc_id,),
    ).fetchone()
    assert ver_row is not None
    assert ver_row["model"] == "model-qwen-task-a"
    assert ver_row["profile"] == "profile-task-a"
    assert str(event_a.event_id) in ver_row["source_json"]

    prop_row = store.execute(
        "SELECT * FROM proposals WHERE procedure_id = ?",
        (proc_id,),
    ).fetchone()
    assert prop_row is not None
    assert prop_row["status"] == "accepted"
    assert prop_row["model"] == "model-qwen-task-a"
    assert prop_row["profile"] == "profile-task-a"
    assert str(event_a.event_id) in prop_row["source_json"]

    # --- Step 2: Bad lesson (narration only) rejected with reason ---
    receipt_narration = make_receipt(kind=EvidenceKind.HANDOFF, verdict=None)
    reader.add(receipt_narration)

    bad_lesson = Lesson(
        title="Unverified Handwave Technique",
        steps=("read notes", "trust intuition"),
        claims=(
            Claim(
                text="Handoff says it probably worked",
                receipt_ids=(receipt_narration.receipt_id,),
            ),
        ),
    )

    event_bad = make_event(sequence=2, event_type="complete_work", workspace_id=UUID(WORKSPACE))
    generator_bad = StubGenerator(
        [
            GenerationResult(
                model="model-bad",
                profile="profile-bad",
                request_sha256=HEX,
                response_sha256=HEX,
                lessons=(bad_lesson,),
            )
        ],
        model="model-bad",
        profile="profile-bad",
    )

    run_bad = drain(store, FakeEvents([event_bad]), reader, generator_bad, workspace_id=WORKSPACE)
    assert run_bad.proposals_rejected == 1
    assert run_bad.proposals_accepted == 0
    assert run_bad.status == "succeeded"  # >=1 proposal, no unit failure

    proposal_bad = store.execute(
        "SELECT * FROM proposals WHERE proposal_id = ?",
        (run_bad.units[0].proposal_ids[0],),
    ).fetchone()
    assert proposal_bad is not None
    assert proposal_bad["status"] == "rejected"
    assert proposal_bad["reason"] == REASON_NARRATION_ONLY
    assert proposal_bad["procedure_id"] is None

    # --- Step 3: Independent task B -> cli.main supply shows it ---
    repo_b = tmp_path / "repo_b"
    repo_b.mkdir()
    subprocess.run(["git", "init"], cwd=repo_b, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:example-org/service-b.git"],
        cwd=repo_b,
        check=True,
        capture_output=True,
    )

    # Mirrors the argv the session digest hook builds: the supply subcommand and its own flags
    # precede --state-dir/--workspace, which argparse only accepts on the supply subparser.
    supply_argv = [
        "supply",
        "--work-key",
        "TASK-B",
        "--cwd",
        str(repo_b),
        "--json",
        "--state-dir",
        str(state_dir),
        "--workspace",
        WORKSPACE,
    ]
    rc = cli_main(supply_argv, store=store)
    assert rc == EXIT_OK

    captured = capsys.readouterr()
    supply_lines = json.loads(captured.out)
    assert len(supply_lines) == 1
    assert f"PROCEDURE {proc_id}@v1 supply=" in supply_lines[0]
    assert "Safe Worker Migration" in supply_lines[0]

    supply_record_row = store.execute(
        "SELECT * FROM supplies WHERE work_key = 'TASK-B' ORDER BY supplied_at DESC",
    ).fetchone()
    assert supply_record_row is not None
    supply_id = supply_record_row["supply_id"]
    assert f"supply={supply_id}" in supply_lines[0]

    # --- Step 4: record_use + record_outcome for B's candidate ---
    cand_b = uuid4()
    r_use_b = make_receipt(candidate_id=cand_b, kind=EvidenceKind.VERIFICATION, verdict="PASS")
    reader.add(r_use_b)

    use_rec = record_use(
        store,
        reader,
        supply_id=supply_id,
        candidate_id=cand_b,
        receipt_ids=(r_use_b.receipt_id,),
    )
    assert use_rec.supply_id == supply_id
    assert use_rec.candidate_id == str(cand_b)

    r_out_b = make_receipt(candidate_id=cand_b, kind=EvidenceKind.VERIFICATION, verdict="PASS")
    reader.add(r_out_b)

    outcome_rec = record_outcome(
        store,
        reader,
        use_id=use_rec.use_id,
        receipt_id=r_out_b.receipt_id,
    )
    assert outcome_rec.use_id == use_rec.use_id
    assert outcome_rec.verdict == "PASS"

    # --- Step 5: History shows supplied/used/outcome ---
    history = procedure_history(store, procedure_id=proc_id)
    assert len(history.supplies) >= 1
    assert len(history.uses) >= 1
    assert len(history.outcomes) >= 1

    entry = next(e for e in history.entries if e.supply_id == supply_id)
    assert entry.use_id == use_rec.use_id
    assert entry.candidate_id == str(cand_b)
    assert entry.outcome_id == outcome_rec.outcome_id
    assert entry.verdict == "PASS"

    # --- Step 6: NEEDS_FIX receipt narrows it so next supply for B's repository is empty while cleanup is pending ---
    repo_url = _resolve_repository(repo_b)
    assert repo_url is not None

    r_counter = make_receipt(kind=EvidenceKind.VERIFICATION, verdict="NEEDS_FIX")
    reader.add(r_counter)

    narrow_attribution = make_attribution(
        model="model-corrector",
        profile="profile-corrector",
    )
    narrow_precondition = Precondition(key="repository", op="ne", value=repo_url)

    corr_record = correct(
        store,
        reader,
        procedure_id=proc_id,
        receipt_id=r_counter.receipt_id,
        action="narrow",
        preconditions=[narrow_precondition],
        attribution=narrow_attribution,
    )
    assert corr_record.status == "accepted"
    assert corr_record.from_version == 1
    assert corr_record.to_version == 2

    # Cleanup queue has pending entry
    cleanup_rows = store.execute(
        "SELECT * FROM cleanup_queue WHERE procedure_id = ? AND done_at IS NULL",
        (proc_id,),
    ).fetchall()
    assert len(cleanup_rows) == 1

    # Next supply for B's repository is now empty
    supply_argv_after_narrow = [
        "supply",
        "--work-key",
        "TASK-B-LATER",
        "--cwd",
        str(repo_b),
        "--json",
        "--state-dir",
        str(state_dir),
        "--workspace",
        WORKSPACE,
    ]
    rc = cli_main(supply_argv_after_narrow, store=store)
    assert rc == EXIT_OK

    captured_after = capsys.readouterr()
    lines_after = json.loads(captured_after.out)
    assert len(lines_after) == 0

    # Direct supply call for B's repository also yields no procedures
    direct_res = supply(
        store,
        workspace_id=WORKSPACE,
        work_key="TASK-B-LATER",
        context={"repository": repo_url},
    )
    assert len(direct_res.lines) == 0

    # --- Step 7: Outage drain -> failed retryable, not success ---
    generator_outage = StubGenerator(
        [GeneratorUnavailable("local generator service connection refused")],
        model="model-outage",
        profile="profile-outage",
    )
    event_outage = make_event(sequence=3, event_type="complete_work", workspace_id=UUID(WORKSPACE))

    run_outage = drain(store, FakeEvents([event_outage]), reader, generator_outage, workspace_id=WORKSPACE)
    assert run_outage.status == "failed"
    assert run_outage.status != "succeeded"
    assert run_outage.units_failed == 1
    assert run_outage.proposals_accepted == 0

    unit_outage_id = run_outage.units[0].unit_id
    unit_outage_row = store.execute(
        "SELECT state, retryable, error_code FROM units WHERE unit_id = ?",
        (unit_outage_id,),
    ).fetchone()
    assert unit_outage_row is not None
    assert unit_outage_row["state"] == "failed"
    assert unit_outage_row["retryable"] == 1
    assert unit_outage_row["error_code"] == "generator_unavailable"

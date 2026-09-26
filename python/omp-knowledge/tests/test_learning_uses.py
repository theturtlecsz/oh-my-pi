from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from omp_work.v1.models import EvidenceKind, EvidenceReceipt

from omp_knowledge.errors import InvalidRequestError
from omp_knowledge.learning.cli import EXIT_OK, main as cli_main
from omp_knowledge.learning.policy import NativeReceipts
from omp_knowledge.learning.store import LearningStore
from omp_knowledge.learning.uses import (
    ProcedureHistory,
    SupplyResult,
    procedure_history,
    record_outcome,
    record_use,
    supply,
)


class DictReceiptReader:
    """Fake reader conforming to NativeReceipts protocol."""

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
    rid = receipt_id or uuid4()
    cid = candidate_id or uuid4()
    p = payload if payload is not None else {"exit_code": 0}
    return EvidenceReceipt(
        receipt_id=rid,
        work_id=uuid4(),
        revision_id=uuid4(),
        candidate_id=cid,
        kind=kind,
        payload=p,
        payload_sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        issuer="test-runner",
        issued_at=datetime.now(timezone.utc),
        verdict=verdict,
        independent=True,
    )


def insert_procedure(
    store: LearningStore,
    *,
    procedure_id: str | None = None,
    fingerprint: str | None = None,
    status: str = "active",
    version: int = 1,
    title: str = "Test Procedure",
    steps: list[str] | tuple[str, ...] = ("step 1", "step 2"),
    preconditions: list[dict[str, Any]] = (),
) -> str:
    proc_id = procedure_id or str(uuid4())
    fp = fingerprint or str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with store.transaction() as conn:
        conn.execute(
            """
            INSERT INTO procedures (procedure_id, fingerprint, status, current_version, title, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (proc_id, fp, status, version, title, now, now),
        )
        conn.execute(
            """
            INSERT INTO procedure_versions (procedure_id, version, title, steps_json, preconditions_json, model, profile, source_json, created_at)
            VALUES (?, ?, ?, ?, ?, 'qwen-2.5', 'local-qwen', '{}', ?)
            """,
            (proc_id, version, title, json.dumps(list(steps)), json.dumps(preconditions), now),
        )
    return proc_id


def test_matching_context_supplies_procedure() -> None:
    store = LearningStore(":memory:")
    proc_id = insert_procedure(
        store,
        title="Deploy app",
        steps=("build container", "deploy to k8s"),
        preconditions=[
            {"key": "project_id", "op": "eq", "value": "proj-alpha"},
            {"key": "repository", "op": "ne", "value": "ssh://git@github.com/bad/repo"},
        ],
    )

    result = supply(
        store,
        workspace_id=uuid4(),
        work_key="work-1",
        context={
            "project_id": "proj-alpha",
            "repository": "ssh://git@github.com/good/repo",
        },
    )

    assert len(result.supply_ids) == 1
    assert len(result.lines) == 1
    supply_id = result.supply_ids[0]
    expected_line = f"PROCEDURE {proc_id}@v1 supply={supply_id}: Deploy app - build container; deploy to k8s"
    assert result.lines[0] == expected_line

    row = store.execute("SELECT * FROM supplies WHERE supply_id = ?", (supply_id,)).fetchone()
    assert row is not None
    assert row["procedure_id"] == proc_id
    assert row["version"] == 1
    assert row["work_key"] == "work-1"
    ctx = json.loads(row["context_json"])
    assert ctx["project_id"] == "proj-alpha"


def test_project_mismatch_supplies_nothing() -> None:
    store = LearningStore(":memory:")
    insert_procedure(
        store,
        title="Project Specific",
        steps=("step one",),
        preconditions=[{"key": "project_id", "op": "eq", "value": "proj-alpha"}],
    )

    result = supply(
        store,
        workspace_id="ws-1",
        work_key="work-2",
        context={"project_id": "proj-beta"},
    )
    assert len(result.supply_ids) == 0
    assert len(result.lines) == 0
    assert store.execute("SELECT COUNT(*) AS c FROM supplies").fetchone()["c"] == 0


def test_ne_match_supplies_nothing() -> None:
    store = LearningStore(":memory:")
    insert_procedure(
        store,
        title="Exclude specific repo",
        steps=("step one",),
        preconditions=[{"key": "repository", "op": "ne", "value": "ssh://git@github.com/legacy/repo"}],
    )

    # When context matches the "ne" value, it is excluded (ne match supplies nothing)
    result = supply(
        store,
        workspace_id="ws-1",
        work_key="work-3",
        context={"repository": "ssh://git@github.com/legacy/repo"},
    )
    assert len(result.supply_ids) == 0
    assert len(result.lines) == 0


def test_withdrawn_procedure_supplies_nothing() -> None:
    store = LearningStore(":memory:")
    insert_procedure(
        store,
        status="withdrawn",
        title="Withdrawn procedure",
        steps=("step one",),
        preconditions=[],
    )

    result = supply(
        store,
        workspace_id="ws-1",
        work_key="work-4",
        context={"project_id": "any"},
    )
    assert len(result.supply_ids) == 0
    assert len(result.lines) == 0


def test_supply_alone_leaves_zero_uses() -> None:
    store = LearningStore(":memory:")
    insert_procedure(
        store,
        title="Standalone supply",
        steps=("inspect",),
        preconditions=[],
    )

    result = supply(
        store,
        workspace_id="ws-1",
        work_key="work-5",
        context={},
    )
    assert len(result.supply_ids) == 1

    # Verify supplies row exists
    supplies_count = store.execute("SELECT COUNT(*) AS c FROM supplies").fetchone()["c"]
    assert supplies_count == 1

    # Verify zero uses exist
    uses_count = store.execute("SELECT COUNT(*) AS c FROM uses").fetchone()["c"]
    assert uses_count == 0


def test_supply_limit_respected() -> None:
    store = LearningStore(":memory:")
    for i in range(5):
        insert_procedure(store, title=f"Proc {i}", steps=(f"step {i}",))

    result = supply(
        store,
        workspace_id="ws-1",
        work_key="work-limit",
        context={},
        limit=2,
    )
    assert len(result.supply_ids) == 2
    assert len(result.lines) == 2
    supplies_count = store.execute("SELECT COUNT(*) AS c FROM supplies").fetchone()["c"]
    assert supplies_count == 2


def test_record_use_refuses_wrong_candidate() -> None:
    store = LearningStore(":memory:")
    proc_id = insert_procedure(store, title="P", steps=("s",))
    res = supply(store, workspace_id="ws-1", work_key="w-1", context={})
    supply_id = res.supply_ids[0]

    cand_a = uuid4()
    cand_b = uuid4()
    receipt_for_b = make_receipt(candidate_id=cand_b)
    reader = DictReceiptReader({receipt_for_b.receipt_id: receipt_for_b})

    # Passing candidate_id A with a receipt for candidate B must raise InvalidRequestError
    with pytest.raises(InvalidRequestError, match="does not match candidate_id"):
        record_use(
            store,
            reader,
            supply_id=supply_id,
            candidate_id=cand_a,
            receipt_ids=[receipt_for_b.receipt_id],
        )

    assert store.execute("SELECT COUNT(*) AS c FROM uses").fetchone()["c"] == 0


def test_record_use_refuses_unresolved_receipt() -> None:
    store = LearningStore(":memory:")
    insert_procedure(store, title="P", steps=("s",))
    res = supply(store, workspace_id="ws-1", work_key="w-1", context={})
    supply_id = res.supply_ids[0]

    reader = DictReceiptReader({})
    with pytest.raises(InvalidRequestError, match="not found"):
        record_use(
            store,
            reader,
            supply_id=supply_id,
            candidate_id=uuid4(),
            receipt_ids=[uuid4()],
        )


def test_record_use_refuses_empty_receipts_or_unknown_supply() -> None:
    store = LearningStore(":memory:")
    reader = DictReceiptReader({})

    with pytest.raises(InvalidRequestError, match="empty"):
        record_use(
            store,
            reader,
            supply_id=uuid4(),
            candidate_id=uuid4(),
            receipt_ids=[],
        )

    cand = uuid4()
    r = make_receipt(candidate_id=cand)
    reader.add(r)
    with pytest.raises(InvalidRequestError, match="Supply .* not found"):
        record_use(
            store,
            reader,
            supply_id=uuid4(),
            candidate_id=cand,
            receipt_ids=[r.receipt_id],
        )


def test_record_use_success() -> None:
    store = LearningStore(":memory:")
    insert_procedure(store, title="P", steps=("s",))
    res = supply(store, workspace_id="ws-1", work_key="w-1", context={})
    supply_id = res.supply_ids[0]

    cand = uuid4()
    r1 = make_receipt(candidate_id=cand)
    r2 = make_receipt(candidate_id=cand)
    reader = DictReceiptReader({r1.receipt_id: r1, r2.receipt_id: r2})

    use_rec = record_use(
        store,
        reader,
        supply_id=supply_id,
        candidate_id=cand,
        receipt_ids=[r1.receipt_id, r2.receipt_id],
    )

    assert use_rec.supply_id == supply_id
    assert use_rec.candidate_id == str(cand)
    assert len(use_rec.receipt_ids) == 2

    row = store.execute("SELECT * FROM uses WHERE use_id = ?", (use_rec.use_id,)).fetchone()
    assert row is not None
    assert row["supply_id"] == supply_id
    assert row["candidate_id"] == str(cand)


def test_record_outcome_tied_to_exact_use_candidate_and_receipt() -> None:
    store = LearningStore(":memory:")
    insert_procedure(store, title="P", steps=("s",))
    res = supply(store, workspace_id="ws-1", work_key="w-1", context={})
    supply_id = res.supply_ids[0]

    cand = uuid4()
    r_use = make_receipt(candidate_id=cand)
    r_outcome = make_receipt(
        candidate_id=cand,
        kind=EvidenceKind.AUDIT,
        verdict="PASS",
    )
    reader = DictReceiptReader({
        r_use.receipt_id: r_use,
        r_outcome.receipt_id: r_outcome,
    })

    use_rec = record_use(
        store,
        reader,
        supply_id=supply_id,
        candidate_id=cand,
        receipt_ids=[r_use.receipt_id],
    )

    outcome_rec = record_outcome(
        store,
        reader,
        use_id=use_rec.use_id,
        receipt_id=r_outcome.receipt_id,
    )

    assert outcome_rec.use_id == use_rec.use_id
    assert outcome_rec.receipt_id == str(r_outcome.receipt_id)
    assert outcome_rec.candidate_id == str(cand)
    assert outcome_rec.verdict == "PASS"

    row = store.execute("SELECT * FROM outcomes WHERE outcome_id = ?", (outcome_rec.outcome_id,)).fetchone()
    assert row is not None
    assert row["use_id"] == use_rec.use_id
    assert row["verdict"] == "PASS"
    assert row["candidate_id"] == str(cand)


def test_record_outcome_refuses_wrong_candidate() -> None:
    store = LearningStore(":memory:")
    insert_procedure(store, title="P", steps=("s",))
    res = supply(store, workspace_id="ws-1", work_key="w-1", context={})
    supply_id = res.supply_ids[0]

    cand_a = uuid4()
    cand_b = uuid4()
    r_use = make_receipt(candidate_id=cand_a)
    r_outcome_b = make_receipt(candidate_id=cand_b, kind=EvidenceKind.VERIFICATION, verdict="PASS")
    reader = DictReceiptReader({
        r_use.receipt_id: r_use,
        r_outcome_b.receipt_id: r_outcome_b,
    })

    use_rec = record_use(
        store,
        reader,
        supply_id=supply_id,
        candidate_id=cand_a,
        receipt_ids=[r_use.receipt_id],
    )

    with pytest.raises(InvalidRequestError, match="does not match use candidate_id"):
        record_outcome(
            store,
            reader,
            use_id=use_rec.use_id,
            receipt_id=r_outcome_b.receipt_id,
        )


def test_record_outcome_refuses_non_verification_or_missing_verdict() -> None:
    store = LearningStore(":memory:")
    insert_procedure(store, title="P", steps=("s",))
    res = supply(store, workspace_id="ws-1", work_key="w-1", context={})
    supply_id = res.supply_ids[0]

    cand = uuid4()
    r_use = make_receipt(candidate_id=cand)
    r_plan = make_receipt(candidate_id=cand, kind=EvidenceKind.PLAN, verdict="PASS")
    r_no_verdict = make_receipt(candidate_id=cand, kind=EvidenceKind.VERIFICATION, verdict=None)

    reader = DictReceiptReader({
        r_use.receipt_id: r_use,
        r_plan.receipt_id: r_plan,
        r_no_verdict.receipt_id: r_no_verdict,
    })

    use_rec = record_use(
        store,
        reader,
        supply_id=supply_id,
        candidate_id=cand,
        receipt_ids=[r_use.receipt_id],
    )

    with pytest.raises(InvalidRequestError, match="kind must be 'verification' or 'audit'"):
        record_outcome(store, reader, use_id=use_rec.use_id, receipt_id=r_plan.receipt_id)

    with pytest.raises(InvalidRequestError, match="no verdict set"):
        record_outcome(store, reader, use_id=use_rec.use_id, receipt_id=r_no_verdict.receipt_id)


def test_procedure_history_shows_supplied_used_and_outcome() -> None:
    store = LearningStore(":memory:")
    proc_id = insert_procedure(store, title="History Proc", steps=("step A",))

    # Empty history before supply
    h0 = procedure_history(store, proc_id)
    assert len(h0.supplies) == 0
    assert len(h0.uses) == 0
    assert len(h0.outcomes) == 0
    assert len(h0) == 0

    # 1. Supply
    res = supply(store, workspace_id="ws-history", work_key="key-h", context={"project_id": "p1"})
    supply_id = res.supply_ids[0]

    h1 = procedure_history(store, proc_id)
    assert len(h1.supplies) == 1
    assert len(h1.uses) == 0
    assert len(h1.outcomes) == 0
    assert len(h1) == 1
    assert h1[0].supply_id == supply_id
    assert h1[0].use_id is None
    assert h1[0].verdict is None

    # 2. Use
    cand = uuid4()
    r_use = make_receipt(candidate_id=cand)
    reader = DictReceiptReader({r_use.receipt_id: r_use})
    use_rec = record_use(
        store,
        reader,
        supply_id=supply_id,
        candidate_id=cand,
        receipt_ids=[r_use.receipt_id],
    )

    h2 = procedure_history(store, proc_id)
    assert len(h2.supplies) == 1
    assert len(h2.uses) == 1
    assert len(h2.outcomes) == 0
    assert len(h2) == 1
    assert h2[0].use_id == use_rec.use_id
    assert h2[0].candidate_id == str(cand)
    assert h2[0].verdict is None

    # 3. Outcome
    r_out = make_receipt(candidate_id=cand, kind=EvidenceKind.VERIFICATION, verdict="PASS")
    reader.add(r_out)
    outcome_rec = record_outcome(
        store,
        reader,
        use_id=use_rec.use_id,
        receipt_id=r_out.receipt_id,
    )

    h3 = procedure_history(store, proc_id)
    assert len(h3.supplies) == 1
    assert len(h3.uses) == 1
    assert len(h3.outcomes) == 1
    assert len(h3) == 1

    entry = h3[0]
    assert entry.procedure_id == proc_id
    assert entry.supply_id == supply_id
    assert entry.use_id == use_rec.use_id
    assert entry.candidate_id == str(cand)
    assert entry.outcome_id == outcome_rec.outcome_id
    assert entry.verdict == "PASS"

    # Dict and attribute access conformance
    assert h3["supplies"] == h3.supplies
    assert h3["uses"] == h3.uses
    assert h3["outcomes"] == h3.outcomes
    d = h3.to_dict()
    assert d["procedure_id"] == proc_id
    assert len(d["entries"]) == 1


def test_cli_supply_json_prints_lines(capsys: pytest.CaptureFixture[str]) -> None:
    store = LearningStore(":memory:")
    proc_id = insert_procedure(
        store,
        title="CLI Procedure",
        steps=("first", "second"),
        preconditions=[{"key": "project_id", "op": "eq", "value": "proj-cli"}],
    )

    argv = [
        "supply",
        "--state-dir",
        ":memory:",
        "--workspace",
        str(uuid4()),
        "--work-key",
        "key-cli",
        "--project-id",
        "proj-cli",
        "--json",
    ]

    rc = cli_main(argv, store=store)
    assert rc == EXIT_OK

    captured = capsys.readouterr()
    lines = json.loads(captured.out)
    assert isinstance(lines, list)
    assert len(lines) == 1
    assert lines[0].startswith(f"PROCEDURE {proc_id}@v1 supply=")
    assert "CLI Procedure - first; second" in lines[0]


def test_cli_supply_cwd_resolves_git_remote(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    git_dir = tmp_path / "repo"
    git_dir.mkdir()
    subprocess.run(["git", "init"], cwd=git_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:my-org/my-repo.git"],
        cwd=git_dir,
        check=True,
        capture_output=True,
    )

    store = LearningStore(":memory:")
    proc_id = insert_procedure(
        store,
        title="Git Remote Test",
        steps=("git step",),
        preconditions=[
            {"key": "repository", "op": "eq", "value": "ssh://git@github.com/my-org/my-repo"},
        ],
    )

    argv = [
        "supply",
        "--state-dir",
        ":memory:",
        "--workspace",
        str(uuid4()),
        "--work-key",
        "key-git",
        "--cwd",
        str(git_dir),
        "--json",
    ]

    rc = cli_main(argv, store=store)
    assert rc == EXIT_OK

    captured = capsys.readouterr()
    lines = json.loads(captured.out)
    assert len(lines) == 1
    assert proc_id in lines[0]


def test_cli_use_and_outcome_subcommands(capsys: pytest.CaptureFixture[str]) -> None:
    store = LearningStore(":memory:")
    insert_procedure(store, title="P", steps=("s",))
    res = supply(store, workspace_id="ws-cli", work_key="wk-cli", context={})
    supply_id = res.supply_ids[0]

    cand = uuid4()
    r_use = make_receipt(candidate_id=cand)
    r_out = make_receipt(candidate_id=cand, kind=EvidenceKind.VERIFICATION, verdict="PASS")
    reader = DictReceiptReader({
        r_use.receipt_id: r_use,
        r_out.receipt_id: r_out,
    })

    # CLI use
    use_argv = [
        "use",
        "--state-dir",
        ":memory:",
        "--work-url",
        "http://unused",
        "--workspace",
        str(uuid4()),
        "--bearer-file",
        "/unused",
        "--supply-id",
        supply_id,
        "--candidate-id",
        str(cand),
        "--receipt-id",
        str(r_use.receipt_id),
        "--json",
    ]
    rc = cli_main(use_argv, store=store, receipts=reader)
    assert rc == EXIT_OK
    use_data = json.loads(capsys.readouterr().out)
    use_id = use_data["use_id"]
    assert use_data["supply_id"] == supply_id

    # CLI outcome
    outcome_argv = [
        "outcome",
        "--state-dir",
        ":memory:",
        "--work-url",
        "http://unused",
        "--workspace",
        str(uuid4()),
        "--bearer-file",
        "/unused",
        "--use-id",
        use_id,
        "--receipt-id",
        str(r_out.receipt_id),
        "--json",
    ]
    rc = cli_main(outcome_argv, store=store, receipts=reader)
    assert rc == EXIT_OK
    outcome_data = json.loads(capsys.readouterr().out)
    assert outcome_data["use_id"] == use_id
    assert outcome_data["verdict"] == "PASS"

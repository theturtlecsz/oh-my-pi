from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from omp_work.v1.models import EvidenceKind, EvidenceReceipt

from omp_knowledge.errors import InvalidRequestError
from omp_knowledge.learning.cli import EXIT_OK, main as cli_main
from omp_knowledge.learning.corrections import (
    REASON_NOT_COUNTEREVIDENCE,
    correct,
)
from omp_knowledge.learning.models import Attribution, Precondition, SourceIdentity
from omp_knowledge.learning.policy import REASON_INVALID_CITATION
from omp_knowledge.learning.store import LearningStore
from omp_knowledge.learning.uses import supply


EXCLUDED_REPO = "ssh://git@github.com/excluded/repo"
OTHER_REPO = "ssh://git@github.com/other/repo"


class DictReceiptReader:
    """Fake reader conforming to NativeReceipts."""

    def __init__(self, receipts: dict[str | UUID, EvidenceReceipt] | None = None) -> None:
        self._receipts: dict[str, EvidenceReceipt] = {}
        if receipts:
            for key, value in receipts.items():
                self._receipts[str(key)] = value

    def receipt(self, receipt_id: UUID | str) -> EvidenceReceipt:
        key = str(receipt_id)
        if key not in self._receipts:
            raise KeyError(f"Receipt {receipt_id} not found in reader")
        return self._receipts[key]


class MissingReceiptReader:
    def receipt(self, receipt_id: UUID | str) -> EvidenceReceipt | None:
        return None


def make_receipt(
    *,
    receipt_id: UUID | None = None,
    kind: EvidenceKind = EvidenceKind.VERIFICATION,
    verdict: str | None = "NEEDS_FIX",
) -> EvidenceReceipt:
    return EvidenceReceipt(
        receipt_id=receipt_id or uuid4(),
        work_id=uuid4(),
        revision_id=uuid4(),
        candidate_id=uuid4(),
        kind=kind,
        payload={"exit_code": 1},
        payload_sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        issuer="test-runner",
        issued_at=datetime.now(timezone.utc),
        verdict=verdict,
        independent=True,
    )


def make_attribution(
    model: str = "qwen-2.5",
    profile: str = "local-qwen",
) -> Attribution:
    return Attribution(
        model=model,
        profile=profile,
        source=SourceIdentity(
            workspace_id=str(uuid4()),
            event_id=str(uuid4()),
            event_sequence=7,
            aggregate_id=str(uuid4()),
        ),
    )


def insert_procedure(
    store: LearningStore,
    *,
    procedure_id: str | None = None,
    status: str = "active",
    version: int = 1,
    title: str = "Ship the change",
    steps: tuple[str, ...] = ("build", "verify"),
    preconditions: list[dict[str, Any]] | None = None,
) -> str:
    proc_id = procedure_id or str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    pre = preconditions if preconditions is not None else [
        {"key": "project_id", "op": "eq", "value": "alpha"},
    ]
    with store.transaction() as conn:
        conn.execute(
            """
            INSERT INTO procedures (
                procedure_id, fingerprint, status, current_version, title, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (proc_id, str(uuid4()), status, version, title, now, now),
        )
        conn.execute(
            """
            INSERT INTO procedure_versions (
                procedure_id, version, title, steps_json, preconditions_json,
                model, profile, source_json, created_at
            ) VALUES (?, ?, ?, ?, ?, 'proposer', 'proposer-profile', '{}', ?)
            """,
            (proc_id, version, title, json.dumps(list(steps)), json.dumps(pre), now),
        )
    return proc_id


def _correction_row(store: LearningStore, correction_id: str) -> Any:
    return store.execute(
        "SELECT * FROM corrections WHERE correction_id = ?",
        (correction_id,),
    ).fetchone()


def assert_row_attribution(row: Any, attribution: Attribution) -> None:
    assert row["model"] == attribution.model
    assert row["profile"] == attribution.profile
    assert SourceIdentity.model_validate_json(row["source_json"]) == attribution.source


def test_narrow_excludes_repository_and_other_repos_get_v2() -> None:
    store = LearningStore(":memory:")
    original = [{"key": "project_id", "op": "eq", "value": "alpha"}]
    proc_id = insert_procedure(store, preconditions=original, steps=("build", "verify"), title="Ship the change")
    before = store.execute(
        """
        SELECT title, steps_json, preconditions_json
        FROM procedure_versions
        WHERE procedure_id = ? AND version = 1
        """,
        (proc_id,),
    ).fetchone()
    receipt = make_receipt(kind=EvidenceKind.VERIFICATION, verdict="NEEDS_FIX")
    reader = DictReceiptReader({receipt.receipt_id: receipt})
    attribution = make_attribution(model="corrector", profile="corrector-profile")
    added = Precondition(key="repository", op="ne", value=EXCLUDED_REPO)

    record = correct(
        store,
        reader,
        procedure_id=proc_id,
        receipt_id=receipt.receipt_id,
        action="narrow",
        preconditions=[added],
        attribution=attribution,
    )

    assert record.status == "accepted"
    assert record.reason is None
    assert record.from_version == 1
    assert record.to_version == 2
    assert record.preconditions == (added,)

    proc = store.execute(
        "SELECT status, current_version FROM procedures WHERE procedure_id = ?",
        (proc_id,),
    ).fetchone()
    assert proc["status"] == "active"
    assert proc["current_version"] == 2

    after = store.execute(
        """
        SELECT title, steps_json, preconditions_json
        FROM procedure_versions
        WHERE procedure_id = ? AND version = 1
        """,
        (proc_id,),
    ).fetchone()
    assert after["title"] == before["title"]
    assert after["steps_json"] == before["steps_json"]
    assert after["preconditions_json"] == before["preconditions_json"]
    assert json.loads(after["preconditions_json"]) == original

    v2 = store.execute(
        """
        SELECT title, steps_json, preconditions_json, model, profile, source_json
        FROM procedure_versions
        WHERE procedure_id = ? AND version = 2
        """,
        (proc_id,),
    ).fetchone()
    assert v2["title"] == "Ship the change"
    assert json.loads(v2["steps_json"]) == ["build", "verify"]
    assert json.loads(v2["preconditions_json"]) == [
        {"key": "project_id", "op": "eq", "value": "alpha"},
        {"key": "repository", "op": "ne", "value": EXCLUDED_REPO},
    ]
    assert v2["model"] == attribution.model
    assert v2["profile"] == attribution.profile
    assert SourceIdentity.model_validate_json(v2["source_json"]) == attribution.source

    row = _correction_row(store, record.correction_id)
    assert row["action"] == "narrow"
    assert row["status"] == "accepted"
    assert row["from_version"] == 1
    assert row["to_version"] == 2
    assert_row_attribution(row, attribution)
    assert json.loads(row["preconditions_json"]) == [added.model_dump(mode="json")]

    cleanup = store.execute("SELECT * FROM cleanup_queue").fetchall()
    assert len(cleanup) == 1
    assert cleanup[0]["procedure_id"] == proc_id
    assert cleanup[0]["action"] == "narrow"
    assert cleanup[0]["done_at"] is None

    excluded = supply(
        store,
        workspace_id=uuid4(),
        work_key="work-excluded",
        context={"project_id": "alpha", "repository": EXCLUDED_REPO},
    )
    assert len(excluded) == 0
    assert excluded.supply_ids == ()

    kept_project = supply(
        store,
        workspace_id=uuid4(),
        work_key="work-other-project",
        context={"project_id": "beta", "repository": OTHER_REPO},
    )
    assert len(kept_project) == 0

    other = supply(
        store,
        workspace_id=uuid4(),
        work_key="work-other-repo",
        context={"project_id": "alpha", "repository": OTHER_REPO},
    )
    assert len(other) == 1
    assert f"PROCEDURE {proc_id}@v2 " in other.lines[0]
    assert "@v1 " not in other.lines[0]
    supplied = store.execute(
        "SELECT version FROM supplies WHERE supply_id = ?",
        (other.supply_ids[0],),
    ).fetchone()
    assert supplied["version"] == 2

    # supply reads procedures live and does not drain the queue
    still_pending = store.execute("SELECT done_at FROM cleanup_queue").fetchone()
    assert still_pending["done_at"] is None


def test_narrow_appends_to_current_version_not_a_hardcoded_successor() -> None:
    store = LearningStore(":memory:")
    original = [{"key": "repository", "op": "eq", "value": OTHER_REPO}]
    proc_id = insert_procedure(store, version=3, preconditions=original)
    receipt = make_receipt(kind=EvidenceKind.AUDIT, verdict="BLOCKED")
    reader = DictReceiptReader({receipt.receipt_id: receipt})
    added = (
        Precondition(key="repository", op="ne", value=EXCLUDED_REPO),
        Precondition(key="project_id", op="eq", value="alpha"),
    )

    record = correct(
        store,
        reader,
        procedure_id=proc_id,
        receipt_id=receipt.receipt_id,
        action="narrow",
        preconditions=list(added),
        attribution=make_attribution(),
    )

    assert record.from_version == 3
    assert record.to_version == 4
    current = store.execute(
        "SELECT current_version FROM procedures WHERE procedure_id = ?",
        (proc_id,),
    ).fetchone()
    assert current["current_version"] == 4
    v3 = store.execute(
        "SELECT preconditions_json FROM procedure_versions WHERE procedure_id = ? AND version = 3",
        (proc_id,),
    ).fetchone()
    assert json.loads(v3["preconditions_json"]) == original
    v4 = store.execute(
        "SELECT preconditions_json FROM procedure_versions WHERE procedure_id = ? AND version = 4",
        (proc_id,),
    ).fetchone()
    assert json.loads(v4["preconditions_json"]) == [
        *original,
        {"key": "repository", "op": "ne", "value": EXCLUDED_REPO},
        {"key": "project_id", "op": "eq", "value": "alpha"},
    ]


def test_withdraw_hides_procedure_while_cleanup_pending() -> None:
    store = LearningStore(":memory:")
    proc_id = insert_procedure(store, preconditions=[])
    receipt = make_receipt(kind=EvidenceKind.AUDIT, verdict="BLOCKED")
    reader = DictReceiptReader({receipt.receipt_id: receipt})
    attribution = make_attribution(model="withdraw-model", profile="withdraw-profile")

    visible = supply(
        store,
        workspace_id="ws-1",
        work_key="before",
        context={"repository": OTHER_REPO},
    )
    assert len(visible) == 1

    record = correct(
        store,
        reader,
        procedure_id=proc_id,
        receipt_id=receipt.receipt_id,
        action="withdraw",
        attribution=attribution,
    )

    assert record.status == "accepted"
    assert record.action == "withdraw"
    assert record.from_version == 1
    assert record.to_version == 1
    assert record.preconditions == ()

    proc = store.execute(
        "SELECT status, current_version FROM procedures WHERE procedure_id = ?",
        (proc_id,),
    ).fetchone()
    assert proc["status"] == "withdrawn"
    assert proc["current_version"] == 1
    versions = store.execute(
        "SELECT COUNT(*) AS c FROM procedure_versions WHERE procedure_id = ?",
        (proc_id,),
    ).fetchone()
    assert versions["c"] == 1

    hidden = supply(
        store,
        workspace_id="ws-1",
        work_key="after",
        context={"repository": OTHER_REPO},
    )
    assert len(hidden) == 0

    cleanup = store.execute("SELECT procedure_id, action, done_at FROM cleanup_queue").fetchall()
    assert len(cleanup) == 1
    assert cleanup[0]["procedure_id"] == proc_id
    assert cleanup[0]["action"] == "withdraw"
    assert cleanup[0]["done_at"] is None

    row = _correction_row(store, record.correction_id)
    assert row["status"] == "accepted"
    assert row["action"] == "withdraw"
    assert row["from_version"] == 1
    assert row["to_version"] == 1
    assert row["preconditions_json"] is None
    assert_row_attribution(row, attribution)


@pytest.mark.parametrize(
    ("kind", "verdict"),
    [
        (EvidenceKind.VERIFICATION, "PASS"),
        (EvidenceKind.AUDIT, "PASS"),
        (EvidenceKind.PLAN, "NEEDS_FIX"),
        (EvidenceKind.VERIFICATION, None),
        (EvidenceKind.HANDOFF, "BLOCKED"),
    ],
)
def test_non_counterevidence_rejected_and_procedure_unchanged(
    kind: EvidenceKind,
    verdict: str | None,
) -> None:
    store = LearningStore(":memory:")
    original = [{"key": "project_id", "op": "eq", "value": "alpha"}]
    proc_id = insert_procedure(store, preconditions=original)
    before = store.execute(
        "SELECT status, current_version, updated_at FROM procedures WHERE procedure_id = ?",
        (proc_id,),
    ).fetchone()
    version_before = store.execute(
        "SELECT preconditions_json FROM procedure_versions WHERE procedure_id = ? AND version = 1",
        (proc_id,),
    ).fetchone()
    receipt = make_receipt(kind=kind, verdict=verdict)
    reader = DictReceiptReader({receipt.receipt_id: receipt})
    attribution = make_attribution()
    added = Precondition(key="repository", op="ne", value=EXCLUDED_REPO)

    record = correct(
        store,
        reader,
        procedure_id=proc_id,
        receipt_id=receipt.receipt_id,
        action="narrow",
        preconditions=[added],
        attribution=attribution,
    )

    assert record.status == "rejected"
    assert record.reason == REASON_NOT_COUNTEREVIDENCE
    assert record.from_version is None
    assert record.to_version is None

    after = store.execute(
        "SELECT status, current_version, updated_at FROM procedures WHERE procedure_id = ?",
        (proc_id,),
    ).fetchone()
    assert after["status"] == before["status"] == "active"
    assert after["current_version"] == before["current_version"] == 1
    assert after["updated_at"] == before["updated_at"]
    version_after = store.execute(
        "SELECT preconditions_json FROM procedure_versions WHERE procedure_id = ? AND version = 1",
        (proc_id,),
    ).fetchone()
    assert version_after["preconditions_json"] == version_before["preconditions_json"]
    assert store.execute("SELECT COUNT(*) AS c FROM procedure_versions").fetchone()["c"] == 1
    assert store.execute("SELECT COUNT(*) AS c FROM cleanup_queue").fetchone()["c"] == 0

    row = _correction_row(store, record.correction_id)
    assert row["status"] == "rejected"
    assert row["reason"] == REASON_NOT_COUNTEREVIDENCE
    assert row["from_version"] is None
    assert row["to_version"] is None
    assert_row_attribution(row, attribution)

    still = supply(
        store,
        workspace_id="ws-1",
        work_key="unchanged",
        context={"project_id": "alpha", "repository": EXCLUDED_REPO},
    )
    assert len(still) == 1
    assert f"{proc_id}@v1 " in still.lines[0]


def test_unresolvable_receipt_is_invalid_citation() -> None:
    store = LearningStore(":memory:")
    proc_id = insert_procedure(store)
    before = store.execute(
        "SELECT status, current_version, updated_at FROM procedures WHERE procedure_id = ?",
        (proc_id,),
    ).fetchone()
    attribution = make_attribution()
    missing_id = uuid4()

    record = correct(
        store,
        DictReceiptReader(),
        procedure_id=proc_id,
        receipt_id=missing_id,
        action="withdraw",
        attribution=attribution,
    )
    none_record = correct(
        store,
        MissingReceiptReader(),
        procedure_id=proc_id,
        receipt_id=uuid4(),
        action="withdraw",
        attribution=attribution,
    )

    for item in (record, none_record):
        assert item.status == "rejected"
        assert item.reason == REASON_INVALID_CITATION
        row = _correction_row(store, item.correction_id)
        assert row["reason"] == REASON_INVALID_CITATION
        assert row["status"] == "rejected"
        assert_row_attribution(row, attribution)

    after = store.execute(
        "SELECT status, current_version, updated_at FROM procedures WHERE procedure_id = ?",
        (proc_id,),
    ).fetchone()
    assert tuple(after) == tuple(before)
    assert store.execute("SELECT COUNT(*) AS c FROM cleanup_queue").fetchone()["c"] == 0
    assert store.execute("SELECT COUNT(*) AS c FROM procedure_versions").fetchone()["c"] == 1


def test_narrow_requires_a_precondition_and_writes_nothing() -> None:
    store = LearningStore(":memory:")
    proc_id = insert_procedure(store)
    receipt = make_receipt(verdict="NEEDS_FIX")
    reader = DictReceiptReader({receipt.receipt_id: receipt})

    with pytest.raises(InvalidRequestError):
        correct(
            store,
            reader,
            procedure_id=proc_id,
            receipt_id=receipt.receipt_id,
            action="narrow",
            attribution=make_attribution(),
        )

    assert store.execute("SELECT COUNT(*) AS c FROM corrections").fetchone()["c"] == 0
    assert store.execute("SELECT COUNT(*) AS c FROM cleanup_queue").fetchone()["c"] == 0
    assert store.execute("SELECT current_version FROM procedures").fetchone()["current_version"] == 1


def test_missing_procedure_rolls_back() -> None:
    store = LearningStore(":memory:")
    receipt = make_receipt(kind=EvidenceKind.VERIFICATION, verdict="BLOCKED")
    reader = DictReceiptReader({receipt.receipt_id: receipt})

    with pytest.raises(InvalidRequestError):
        correct(
            store,
            reader,
            procedure_id=str(uuid4()),
            receipt_id=receipt.receipt_id,
            action="withdraw",
            attribution=make_attribution(),
        )

    assert store.execute("SELECT COUNT(*) AS c FROM corrections").fetchone()["c"] == 0
    assert store.execute("SELECT COUNT(*) AS c FROM cleanup_queue").fetchone()["c"] == 0
    assert store.execute("SELECT COUNT(*) AS c FROM procedures").fetchone()["c"] == 0


def test_narrow_without_version_row_rolls_back() -> None:
    store = LearningStore(":memory:")
    proc_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    store.execute(
        """
        INSERT INTO procedures (
            procedure_id, fingerprint, status, current_version, title, created_at, updated_at
        ) VALUES (?, ?, 'active', 2, 'Orphan', ?, ?)
        """,
        (proc_id, str(uuid4()), now, now),
    )
    receipt = make_receipt(verdict="NEEDS_FIX")
    reader = DictReceiptReader({receipt.receipt_id: receipt})

    with pytest.raises(InvalidRequestError):
        correct(
            store,
            reader,
            procedure_id=proc_id,
            receipt_id=receipt.receipt_id,
            action="narrow",
            preconditions=[Precondition(key="repository", op="ne", value=EXCLUDED_REPO)],
            attribution=make_attribution(),
        )

    proc = store.execute(
        "SELECT current_version, status FROM procedures WHERE procedure_id = ?",
        (proc_id,),
    ).fetchone()
    assert proc["current_version"] == 2
    assert proc["status"] == "active"
    assert store.execute("SELECT COUNT(*) AS c FROM corrections").fetchone()["c"] == 0
    assert store.execute("SELECT COUNT(*) AS c FROM cleanup_queue").fetchone()["c"] == 0
    assert store.execute("SELECT COUNT(*) AS c FROM procedure_versions").fetchone()["c"] == 0


def _correct_argv(
    *,
    workspace: str,
    procedure_id: str,
    receipt_id: str,
    action: str,
    preconditions: list[str] | None = None,
    model: str = "cli-model",
    profile: str = "cli-profile",
) -> list[str]:
    argv = [
        "correct",
        "--state-dir",
        ":memory:",
        "--work-url",
        "http://unused",
        "--workspace",
        workspace,
        "--bearer-file",
        "/unused",
        "--procedure",
        procedure_id,
        "--receipt",
        receipt_id,
        "--action",
        action,
        "--model",
        model,
        "--profile",
        profile,
        "--json",
    ]
    for raw in preconditions or []:
        argv.extend(["--precondition", raw])
    return argv


def test_cli_correct_narrow_parses_preconditions(capsys: pytest.CaptureFixture[str]) -> None:
    store = LearningStore(":memory:")
    proc_id = insert_procedure(
        store,
        preconditions=[{"key": "project_id", "op": "eq", "value": "alpha"}],
    )
    receipt = make_receipt(kind=EvidenceKind.VERIFICATION, verdict="NEEDS_FIX")
    reader = DictReceiptReader({receipt.receipt_id: receipt})
    workspace = str(uuid4())
    excluded = "ssh://git@github.com/old/repo"

    rc = cli_main(
        _correct_argv(
            workspace=workspace,
            procedure_id=proc_id,
            receipt_id=str(receipt.receipt_id),
            action="narrow",
            preconditions=[
                f"repository:ne:{excluded}",
                "project_id:eq:alpha",
            ],
        ),
        store=store,
        receipts=reader,
    )
    assert rc == EXIT_OK
    body = json.loads(capsys.readouterr().out)
    assert body["status"] == "accepted"
    assert body["action"] == "narrow"
    assert body["from_version"] == 1
    assert body["to_version"] == 2
    assert body["model"] == "cli-model"
    assert body["profile"] == "cli-profile"
    assert body["source"]["workspace_id"] == workspace
    assert body["source"]["event_id"] == str(receipt.receipt_id)
    assert body["source"]["aggregate_id"] == proc_id
    assert body["source"]["event_sequence"] == 0
    assert body["preconditions"] == [
        {"key": "repository", "op": "ne", "value": excluded},
        {"key": "project_id", "op": "eq", "value": "alpha"},
    ]

    v2 = store.execute(
        "SELECT preconditions_json FROM procedure_versions WHERE procedure_id = ? AND version = 2",
        (proc_id,),
    ).fetchone()
    assert json.loads(v2["preconditions_json"]) == [
        {"key": "project_id", "op": "eq", "value": "alpha"},
        {"key": "repository", "op": "ne", "value": excluded},
        {"key": "project_id", "op": "eq", "value": "alpha"},
    ]

    hidden = supply(
        store,
        workspace_id=workspace,
        work_key="cli-excluded",
        context={"project_id": "alpha", "repository": excluded},
    )
    assert len(hidden) == 0
    shown = supply(
        store,
        workspace_id=workspace,
        work_key="cli-other",
        context={"project_id": "alpha", "repository": OTHER_REPO},
    )
    assert len(shown) == 1
    assert f"{proc_id}@v2 " in shown.lines[0]


def test_cli_correct_pass_receipt_reports_rejection(capsys: pytest.CaptureFixture[str]) -> None:
    store = LearningStore(":memory:")
    proc_id = insert_procedure(store)
    receipt = make_receipt(kind=EvidenceKind.VERIFICATION, verdict="PASS")
    reader = DictReceiptReader({receipt.receipt_id: receipt})

    rc = cli_main(
        _correct_argv(
            workspace=str(uuid4()),
            procedure_id=proc_id,
            receipt_id=str(receipt.receipt_id),
            action="withdraw",
        ),
        store=store,
        receipts=reader,
    )
    assert rc == EXIT_OK
    body = json.loads(capsys.readouterr().out)
    assert body["status"] == "rejected"
    assert body["reason"] == REASON_NOT_COUNTEREVIDENCE
    proc = store.execute(
        "SELECT status, current_version FROM procedures WHERE procedure_id = ?",
        (proc_id,),
    ).fetchone()
    assert proc["status"] == "active"
    assert proc["current_version"] == 1


def test_cli_correct_builds_work_client(tmp_path: Any) -> None:
    store = LearningStore(":memory:")
    missing = tmp_path / "missing-bearer"
    argv = _correct_argv(
        workspace=str(uuid4()),
        procedure_id=str(uuid4()),
        receipt_id=str(uuid4()),
        action="withdraw",
    )
    argv[argv.index("--bearer-file") + 1] = str(missing)
    with pytest.raises(FileNotFoundError):
        cli_main(argv, store=store)


def test_cli_rejects_malformed_precondition() -> None:
    store = LearningStore(":memory:")
    with pytest.raises(SystemExit) as exc:
        cli_main(
            _correct_argv(
                workspace=str(uuid4()),
                procedure_id=str(uuid4()),
                receipt_id=str(uuid4()),
                action="narrow",
                preconditions=["repository-ne-only"],
            ),
            store=store,
            receipts=DictReceiptReader(),
        )
    assert exc.value.code == 2

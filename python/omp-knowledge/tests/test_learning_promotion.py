from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from omp_knowledge.learning.cli import EXIT_OK, EXIT_RUN_FAILED, main as cli_main
from omp_knowledge.learning.promotion import (
    answer_promotion,
    get_promoted_procedures,
    has_journey_pass,
    record_journey_pass,
    request_promotion,
)
from omp_knowledge.learning.store import LearningStore
from omp_knowledge.learning.uses import supply


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


def add_procedure_version(
    store: LearningStore,
    *,
    procedure_id: str,
    version: int,
    title: str = "Test Procedure Updated",
    steps: list[str] | tuple[str, ...] = ("step A", "step B"),
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with store.transaction() as conn:
        conn.execute(
            """
            INSERT INTO procedure_versions (procedure_id, version, title, steps_json, preconditions_json, model, profile, source_json, created_at)
            VALUES (?, ?, ?, ?, '[]', 'qwen-2.5', 'local-qwen', '{}', ?)
            """,
            (procedure_id, version, title, json.dumps(list(steps)), now),
        )
        conn.execute(
            "UPDATE procedures SET current_version = ?, updated_at = ? WHERE procedure_id = ?",
            (version, now, procedure_id),
        )


def insert_proposal(
    store: LearningStore,
    *,
    procedure_id: str,
    aggregate_id: str,
    workspace_id: str = "ws-1",
    status: str = "accepted",
) -> str:
    proposal_id = str(uuid4())
    unit_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    source_json = json.dumps(
        {
            "workspace_id": workspace_id,
            "event_id": str(uuid4()),
            "event_sequence": 1,
            "aggregate_id": aggregate_id,
        }
    )
    with store.transaction() as conn:
        conn.execute(
            """
            INSERT INTO proposals (
                proposal_id, unit_id, status, reason, procedure_id,
                lesson_json, model, profile, source_json, created_at
            ) VALUES (?, ?, ?, NULL, ?, '{}', 'qwen-2.5', 'local-qwen', ?, ?)
            """,
            (proposal_id, unit_id, status, procedure_id, source_json, now),
        )
    return proposal_id


def insert_support_receipt(
    store: LearningStore,
    *,
    procedure_id: str,
    receipt_id: str,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with store.transaction() as conn:
        conn.execute(
            """
            INSERT INTO procedure_support (procedure_id, receipt_id, proposal_id, created_at)
            VALUES (?, ?, NULL, ?)
            """,
            (procedure_id, receipt_id, now),
        )


class FakeWorkItem:
    def __init__(self, work_id: UUID, state: str = "DONE") -> None:
        self.work_id = work_id
        self.state = state


class FakeWorkClient:
    def __init__(
        self,
        item: FakeWorkItem | None = None,
        campaigns: list[dict[str, Any]] | None = None,
    ) -> None:
        self._item = item
        self._campaigns = campaigns if campaigns is not None else [{"state": "concluded"}]

    def work_item(self, key: str) -> FakeWorkItem:
        if self._item is None:
            raise KeyError(f"Work item {key} not found")
        return self._item

    def _get(self, path: str) -> dict[str, Any]:
        return {"campaigns": self._campaigns}

    def close(self) -> None:
        pass


def test_promoted_only_empty_before_pass_and_approval() -> None:
    store = LearningStore(":memory:")
    ws = "ws-promoted-test"
    proc_id = insert_procedure(store, title="Deploy Container", steps=["run docker"])

    # 1. Plain supply supplies the active procedure unconditionally
    plain_res = supply(store, workspace_id=ws, work_key="key-1")
    assert len(plain_res.lines) == 1
    assert proc_id in plain_res.lines[0]

    # 2. Before any journey pass: promoted-only is empty
    allowed_pre_pass = get_promoted_procedures(store, ws)
    assert allowed_pre_pass == set()
    promoted_res = supply(store, workspace_id=ws, work_key="key-1", allowed=allowed_pre_pass)
    assert len(promoted_res.lines) == 0

    # 3. Record journey pass: still empty before approval
    pass_id = record_journey_pass(store, ws, capability="research_to_learning")
    assert pass_id is not None
    assert has_journey_pass(store, ws)

    allowed_post_pass = get_promoted_procedures(store, ws)
    assert allowed_post_pass == set()
    promoted_res = supply(store, workspace_id=ws, work_key="key-1", allowed=allowed_post_pass)
    assert len(promoted_res.lines) == 0

    # 4. Request promotion: batch is pending, still empty before approval
    decision_rec = request_promotion(store, ws)
    assert decision_rec is not None
    allowed_pending = get_promoted_procedures(store, ws)
    assert allowed_pending == set()

    # 5. Answer promotion with approve: now supplied
    ans = answer_promotion(store, decision_rec["decision_id"], approve=True, answer_ref="owner-approval-ref")
    assert ans["status"] == "approved"

    allowed_approved = get_promoted_procedures(store, ws)
    assert (proc_id, 1) in allowed_approved

    promoted_res_after = supply(store, workspace_id=ws, work_key="key-1", allowed=allowed_approved)
    assert len(promoted_res_after.lines) == 1
    assert proc_id in promoted_res_after.lines[0]


def test_promotion_request_batch_and_rejection_lifecycle() -> None:
    store = LearningStore(":memory:")
    ws = "ws-lifecycle"
    proc_active = insert_procedure(store, title="Active Procedure", version=1)
    insert_procedure(store, title="Withdrawn Procedure", status="withdrawn", version=1)

    insert_support_receipt(store, procedure_id=proc_active, receipt_id="rcpt-alpha")
    insert_support_receipt(store, procedure_id=proc_active, receipt_id="rcpt-beta")

    # Before pass: request_promotion returns None
    assert request_promotion(store, ws) is None

    # Record pass
    record_journey_pass(store, ws)

    # Request promotion returns OMP-414 decision record
    rec = request_promotion(store, ws, project_id="proj-99")
    assert rec is not None
    assert rec["project_id"] == "proj-99"
    assert rec["options"] == ["approve", "reject"]
    assert rec["evidence_refs"] == ["rcpt-alpha", "rcpt-beta"]
    assert "question" in rec
    assert "why_it_matters" in rec
    assert "risk_of_delay" in rec
    assert "risk_of_each_choice" in rec
    assert rec["batch"] == [{"procedure_id": proc_active, "version": 1, "title": "Active Procedure"}]

    # Calling request_promotion while batch is pending returns identical record
    rec_again = request_promotion(store, ws)
    assert rec_again is not None
    assert rec_again["decision_id"] == rec["decision_id"]
    assert rec_again["batch"] == rec["batch"]

    # Reject the batch
    ans = answer_promotion(store, rec["decision_id"], approve=False, answer_ref="owner-rejected")
    assert ans["status"] == "rejected"

    # Reject -> never supplied
    allowed = get_promoted_procedures(store, ws)
    assert allowed == set()

    # Once rejected, that version is batched so request_promotion returns None
    assert request_promotion(store, ws) is None

    # New version creates eligibility for a new batch
    add_procedure_version(store, procedure_id=proc_active, version=2, title="Active Procedure v2")
    rec_v2 = request_promotion(store, ws)
    assert rec_v2 is not None
    assert rec_v2["decision_id"] != rec["decision_id"]
    assert rec_v2["batch"] == [{"procedure_id": proc_active, "version": 2, "title": "Active Procedure v2"}]

    # Approve v2
    answer_promotion(store, rec_v2["decision_id"], approve=True, answer_ref="v2-approved")
    allowed_v2 = get_promoted_procedures(store, ws)
    assert (proc_active, 2) in allowed_v2
    assert (proc_active, 1) not in allowed_v2


def test_journey_check_pass_and_failure_conditions() -> None:
    store = LearningStore(":memory:")
    ws = "ws-jc"
    work_id = uuid4()
    work_key = "OMP-TEST-1"

    # Insert active procedure linked to work_id
    proc_id = insert_procedure(store, title="Learned from research")
    insert_proposal(store, procedure_id=proc_id, aggregate_id=str(work_id), workspace_id=ws)

    # 1. Failure: work item not DONE
    not_done_item = FakeWorkItem(work_id, state="IN_PROGRESS")
    client_not_done = FakeWorkClient(item=not_done_item, campaigns=[{"state": "concluded"}])

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(
            ["journey-check", "--state-dir", ":memory:", "--work-url", "http://fake", "--bearer-file", "/fake", "--workspace", ws, "--work-key", work_key],
            store=store,
            client=client_not_done,
        )
    assert rc == EXIT_RUN_FAILED
    out = json.loads(buf.getvalue().strip())
    assert out == {"capability": "research_to_learning", "passed": False}
    assert not has_journey_pass(store, ws)

    # 2. Failure: campaign not concluded
    done_item = FakeWorkItem(work_id, state="DONE")
    client_camp_running = FakeWorkClient(item=done_item, campaigns=[{"state": "running"}])

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(
            ["journey-check", "--state-dir", ":memory:", "--work-url", "http://fake", "--bearer-file", "/fake", "--workspace", ws, "--work-key", work_key],
            store=store,
            client=client_camp_running,
        )
    assert rc == EXIT_RUN_FAILED
    out = json.loads(buf.getvalue().strip())
    assert out == {"capability": "research_to_learning", "passed": False}

    # 3. Failure: work_id does not match active procedure
    different_work_id = uuid4()
    diff_item = FakeWorkItem(different_work_id, state="DONE")
    client_diff = FakeWorkClient(item=diff_item, campaigns=[{"state": "concluded"}])

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(
            ["journey-check", "--state-dir", ":memory:", "--work-url", "http://fake", "--bearer-file", "/fake", "--workspace", ws, "--work-key", work_key],
            store=store,
            client=client_diff,
        )
    assert rc == EXIT_RUN_FAILED
    out = json.loads(buf.getvalue().strip())
    assert out == {"capability": "research_to_learning", "passed": False}

    # 4. Pass: work item DONE, campaign concluded, and active procedure matches aggregate_id
    client_pass = FakeWorkClient(item=done_item, campaigns=[{"state": "concluded"}])

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(
            ["journey-check", "--state-dir", ":memory:", "--work-url", "http://fake", "--bearer-file", "/fake", "--workspace", ws, "--work-key", work_key],
            store=store,
            client=client_pass,
        )
    assert rc == EXIT_OK
    out = json.loads(buf.getvalue().strip())
    assert out == {"capability": "research_to_learning", "passed": True}
    assert has_journey_pass(store, ws)


def test_cli_promoted_only_supply_and_promotion_commands() -> None:
    store = LearningStore(":memory:")
    ws = "ws-cli-promoted"
    proc_id = insert_procedure(store, title="CLI Managed Procedure")

    # Supply before pass/promotion: plain supplies 1, promoted-only returns []
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(
            ["supply", "--state-dir", ":memory:", "--workspace", ws, "--work-key", "k-1", "--promoted-only", "--json"],
            store=store,
        )
    assert rc == EXIT_OK
    assert json.loads(buf.getvalue().strip()) == []

    # Plain supply
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(
            ["supply", "--state-dir", ":memory:", "--workspace", ws, "--work-key", "k-1", "--json"],
            store=store,
        )
    assert rc == EXIT_OK
    lines = json.loads(buf.getvalue().strip())
    assert len(lines) == 1
    assert proc_id in lines[0]

    # Record pass
    record_journey_pass(store, ws)

    # CLI promotion-request --json
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(
            ["promotion-request", "--state-dir", ":memory:", "--workspace", ws, "--json"],
            store=store,
        )
    assert rc == EXIT_OK
    rec = json.loads(buf.getvalue().strip())
    decision_id = rec["decision_id"]
    assert rec["batch"][0]["procedure_id"] == proc_id

    # CLI promotion-answer --approve
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(
            ["promotion-answer", "--state-dir", ":memory:", "--decision-id", decision_id, "--approve", "--answer-ref", "cli-approval", "--json"],
            store=store,
        )
    assert rc == EXIT_OK
    ans = json.loads(buf.getvalue().strip())
    assert ans["status"] == "approved"

    # Now supply --promoted-only returns the procedure
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(
            ["supply", "--state-dir", ":memory:", "--workspace", ws, "--work-key", "k-1", "--promoted-only", "--json"],
            store=store,
        )
    assert rc == EXIT_OK
    promoted_lines = json.loads(buf.getvalue().strip())
    assert len(promoted_lines) == 1
    assert proc_id in promoted_lines[0]

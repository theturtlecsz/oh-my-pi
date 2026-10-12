from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from omp_knowledge.learning.cli import EXIT_OK, main as cli_main
from omp_knowledge.learning.promotion import (
    answer_promotion,
    get_promoted_procedures,
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


def test_scoped_promotion_k_and_j_batches_only_k() -> None:
    store = LearningStore(":memory:")
    ws = "ws-scoped"
    k_agg = str(uuid4())
    j_agg = str(uuid4())

    k_proc = insert_procedure(store, title="Lesson K")
    insert_proposal(store, procedure_id=k_proc, aggregate_id=k_agg, workspace_id=ws)

    j_proc = insert_procedure(store, title="Lesson J")
    insert_proposal(store, procedure_id=j_proc, aggregate_id=j_agg, workspace_id=ws)

    # Journey pass for K only
    record_journey_pass(store, workspace_id=ws, work_id=k_agg)

    # Promotion request batches only K
    rec = request_promotion(store, workspace_id=ws)
    assert rec is not None
    assert len(rec["batch"]) == 1
    assert rec["batch"][0]["procedure_id"] == k_proc
    assert rec["batch"][0]["version"] == 1

    # Approval promotes only K
    ans = answer_promotion(
        store, decision_id=rec["decision_id"], approve=True, answer_ref="approve-k"
    )
    assert ans["status"] == "approved"

    assert get_promoted_procedures(store, workspace_id=ws) == {(k_proc, 1)}

    # supply --promoted-only returns only K, never J
    promoted_procedures = get_promoted_procedures(store, workspace_id=ws)
    res = supply(store, workspace_id=ws, work_key="key-1", allowed=promoted_procedures)
    assert len(res.lines) == 1
    assert k_proc in res.lines[0]
    assert j_proc not in res.lines[0]

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(
            [
                "supply",
                "--state-dir",
                ":memory:",
                "--workspace",
                ws,
                "--work-key",
                "key-1",
                "--promoted-only",
                "--json",
            ],
            store=store,
        )
    assert rc == EXIT_OK
    lines = json.loads(buf.getvalue().strip())
    assert len(lines) == 1
    assert k_proc in lines[0]


def test_regression_null_work_id_pass_alone_batches_neither_k_nor_j() -> None:
    store = LearningStore(":memory:")
    ws = "ws-regression"
    k_agg = str(uuid4())
    j_agg = str(uuid4())

    k_proc = insert_procedure(store, title="Lesson K")
    insert_proposal(store, procedure_id=k_proc, aggregate_id=k_agg, workspace_id=ws)

    j_proc = insert_procedure(store, title="Lesson J")
    insert_proposal(store, procedure_id=j_proc, aggregate_id=j_agg, workspace_id=ws)

    # Visible NULL-work-id pass alone batches neither K nor J
    record_journey_pass(store, workspace_id=ws, work_id=None)
    assert request_promotion(store, workspace_id=ws) is None

    # Adding K pass batches only K
    record_journey_pass(store, workspace_id=ws, work_id=k_agg)
    rec = request_promotion(store, workspace_id=ws)
    assert rec is not None
    assert len(rec["batch"]) == 1
    assert rec["batch"][0]["procedure_id"] == k_proc

    # Approved supply yields only K
    answer_promotion(
        store, decision_id=rec["decision_id"], approve=True, answer_ref="approve-k"
    )
    promoted = get_promoted_procedures(store, workspace_id=ws)
    assert promoted == {(k_proc, 1)}

    res = supply(store, workspace_id=ws, work_key="key-1", allowed=promoted)
    assert len(res.lines) == 1
    assert k_proc in res.lines[0]
    assert j_proc not in res.lines[0]

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(
            [
                "supply",
                "--state-dir",
                ":memory:",
                "--workspace",
                ws,
                "--work-key",
                "key-1",
                "--promoted-only",
                "--json",
            ],
            store=store,
        )
    assert rc == EXIT_OK
    lines = json.loads(buf.getvalue().strip())
    assert len(lines) == 1
    assert k_proc in lines[0]


def test_workspace_matching_and_isolation() -> None:
    # 1. Pass in different workspace excludes K
    store1 = LearningStore(":memory:")
    ws1 = "ws-target"
    ws2 = "ws-different"
    k_agg = str(uuid4())
    k_proc = insert_procedure(store1, title="Lesson K")
    insert_proposal(store1, procedure_id=k_proc, aggregate_id=k_agg, workspace_id=ws1)

    record_journey_pass(store1, workspace_id=ws2, work_id=k_agg)
    assert request_promotion(store1, workspace_id=ws1) is None

    # 2. NULL workspace pass with K's work_id covers K
    record_journey_pass(store1, workspace_id=None, work_id=k_agg)
    rec = request_promotion(store1, workspace_id=ws1)
    assert rec is not None
    assert len(rec["batch"]) == 1
    assert rec["batch"][0]["procedure_id"] == k_proc

    answer_promotion(
        store1, decision_id=rec["decision_id"], approve=True, answer_ref="approve-k"
    )
    assert get_promoted_procedures(store1, workspace_id=ws1) == {(k_proc, 1)}

    # 3. Foreign workspace sources excluded
    store_f = LearningStore(":memory:")
    target_ws = "ws-target"
    foreign_ws = "ws-foreign"
    foreign_agg = str(uuid4())
    foreign_proc = insert_procedure(store_f, title="Foreign Lesson")
    insert_proposal(
        store_f,
        procedure_id=foreign_proc,
        aggregate_id=foreign_agg,
        workspace_id=foreign_ws,
    )

    # Pass in target workspace with matching aggregate_id, plus NULL-work-id pass
    record_journey_pass(store_f, workspace_id=target_ws, work_id=foreign_agg)
    record_journey_pass(store_f, workspace_id=target_ws, work_id=None)
    assert request_promotion(store_f, workspace_id=target_ws) is None


def test_legacy_sourceless_procedure_coverage() -> None:
    store = LearningStore(":memory:")
    ws = "ws-legacy"
    legacy_proc = insert_procedure(store, title="Legacy Procedure")

    # Item-only pass never covers legacy procedure
    record_journey_pass(store, workspace_id=ws, work_id=str(uuid4()))
    assert request_promotion(store, workspace_id=ws) is None

    # Visible NULL-work-id pass covers legacy procedure
    record_journey_pass(store, workspace_id=ws, work_id=None)
    rec = request_promotion(store, workspace_id=ws)
    assert rec is not None
    assert len(rec["batch"]) == 1
    assert rec["batch"][0]["procedure_id"] == legacy_proc


def test_procedure_versions_source_identity_matching() -> None:
    store = LearningStore(":memory:")
    ws = "ws-version-source"
    agg_id = str(uuid4())
    proc_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    source_json = json.dumps(
        {
            "workspace_id": ws,
            "event_id": str(uuid4()),
            "event_sequence": 1,
            "aggregate_id": agg_id,
        }
    )

    with store.transaction() as conn:
        conn.execute(
            """
            INSERT INTO procedures (procedure_id, fingerprint, status, current_version, title, created_at, updated_at)
            VALUES (?, ?, 'active', 1, 'Version Sourced', ?, ?)
            """,
            (proc_id, str(uuid4()), now, now),
        )
        conn.execute(
            """
            INSERT INTO procedure_versions (procedure_id, version, title, steps_json, preconditions_json, model, profile, source_json, created_at)
            VALUES (?, 1, 'Version Sourced', '["step"]', '[]', 'qwen-2.5', 'local-qwen', ?, ?)
            """,
            (proc_id, source_json, now),
        )

    # NULL-work-id pass alone does not cover sourced procedure_version
    record_journey_pass(store, workspace_id=ws, work_id=None)
    assert request_promotion(store, workspace_id=ws) is None

    # Matching item pass covers it
    record_journey_pass(store, workspace_id=ws, work_id=agg_id)
    rec = request_promotion(store, workspace_id=ws)
    assert rec is not None
    assert len(rec["batch"]) == 1
    assert rec["batch"][0]["procedure_id"] == proc_id

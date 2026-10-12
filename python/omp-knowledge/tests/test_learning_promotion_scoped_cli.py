from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from omp_knowledge.learning.cli import EXIT_OK, main as cli_main
from omp_knowledge.learning.promotion import record_journey_pass
from omp_knowledge.learning.store import LearningStore


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


def run_cli(store: LearningStore, argv: list[str]) -> tuple[int, Any]:
    buf = io.StringIO()
    # --state-dir is declared on each subparser, so it follows the command name.
    full_argv = [argv[0], "--state-dir", ":memory:", *argv[1:]]
    with redirect_stdout(buf):
        rc = cli_main(full_argv, store=store)
    return rc, json.loads(buf.getvalue().strip())


def test_cli_scoped_promotion_approval_and_supply_isolation() -> None:
    store = LearningStore(":memory:")
    ws = "ws-cli-scoped"
    k_agg = str(uuid4())
    j_agg = str(uuid4())

    k_proc = insert_procedure(store, title="Lesson K")
    insert_proposal(store, procedure_id=k_proc, aggregate_id=k_agg, workspace_id=ws)

    j_proc = insert_procedure(store, title="Lesson J")
    insert_proposal(store, procedure_id=j_proc, aggregate_id=j_agg, workspace_id=ws)

    # Regression: a visible NULL-work-id pass before any item pass covers neither
    # sourced lesson, so neither K nor J is batched or supplied.
    record_journey_pass(store, workspace_id=ws, work_id=None)
    rc, rec = run_cli(store, ["promotion-request", "--workspace", ws, "--json"])
    assert rc == EXIT_OK
    assert rec is None

    supply_argv = [
        "supply",
        "--workspace",
        ws,
        "--work-key",
        "K",
        "--promoted-only",
        "--json",
    ]
    rc, lines = run_cli(store, supply_argv)
    assert rc == EXIT_OK
    assert lines == []

    # K journey pass: request batches only K's procedure id/version/title.
    record_journey_pass(store, workspace_id=ws, work_id=k_agg)
    rc, rec = run_cli(store, ["promotion-request", "--workspace", ws, "--json"])
    assert rc == EXIT_OK
    assert rec["batch"] == [
        {"procedure_id": k_proc, "version": 1, "title": "Lesson K"}
    ]
    assert j_proc not in json.dumps(rec["batch"])
    decision_k = rec["decision_id"]

    # Approve K; promoted-only supply returns K and never J.
    rc, ans = run_cli(
        store,
        [
            "promotion-answer",
            "--decision-id",
            decision_k,
            "--approve",
            "--answer-ref",
            "test-approval",
            "--json",
        ],
    )
    assert rc == EXIT_OK
    assert ans["status"] == "approved"

    rc, lines = run_cli(store, supply_argv)
    assert rc == EXIT_OK
    assert len(lines) == 1
    assert k_proc in lines[0]
    assert j_proc not in lines[0]

    # K is batched and J is uncovered, so the next request has no eligible batch.
    rc, rec = run_cli(store, ["promotion-request", "--workspace", ws, "--json"])
    assert rc == EXIT_OK
    assert rec is None

    # J journey pass: the next batch contains only J.
    record_journey_pass(store, workspace_id=ws, work_id=j_agg)
    rc, rec = run_cli(store, ["promotion-request", "--workspace", ws, "--json"])
    assert rc == EXIT_OK
    assert rec["batch"] == [
        {"procedure_id": j_proc, "version": 1, "title": "Lesson J"}
    ]
    assert k_proc not in json.dumps(rec["batch"])

    # Reject J; promoted-only supply still returns only the approved K.
    rc, ans = run_cli(
        store,
        [
            "promotion-answer",
            "--decision-id",
            rec["decision_id"],
            "--reject",
            "--answer-ref",
            "test-rejection",
            "--json",
        ],
    )
    assert rc == EXIT_OK
    assert ans["status"] == "rejected"

    rc, lines = run_cli(store, supply_argv)
    assert rc == EXIT_OK
    assert len(lines) == 1
    assert k_proc in lines[0]
    assert j_proc not in lines[0]

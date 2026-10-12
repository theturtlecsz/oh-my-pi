from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from .models import SourceIdentity
from .store import LearningStore


def _is_empty_source_json(raw: str | None) -> bool:
    if raw is None:
        return False
    try:
        val = json.loads(raw)
        return isinstance(val, dict) and len(val) == 0
    except json.JSONDecodeError:
        return False


def _parse_source_identity(raw: str | None) -> SourceIdentity | None:
    if raw is None:
        return None
    s = raw.strip()
    if not s or s == "{}":
        return None
    try:
        return SourceIdentity.model_validate_json(s)
    except Exception:
        return None

PROMOTION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS journey_passes (
    pass_id TEXT PRIMARY KEY,
    capability TEXT NOT NULL,
    workspace_id TEXT,
    work_key TEXT,
    work_id TEXT,
    passed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS promotion_batches (
    decision_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    project_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'rejected')),
    decision_json TEXT NOT NULL,
    answer_ref TEXT,
    created_at TEXT NOT NULL,
    answered_at TEXT
);

CREATE TABLE IF NOT EXISTS promotion_batch_items (
    decision_id TEXT NOT NULL,
    procedure_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (decision_id, procedure_id, version)
);

CREATE INDEX IF NOT EXISTS idx_promotion_batches_status ON promotion_batches(status);
CREATE INDEX IF NOT EXISTS idx_promotion_batch_items_proc ON promotion_batch_items(procedure_id, version);
CREATE INDEX IF NOT EXISTS idx_journey_passes_cap ON journey_passes(capability);
"""


def ensure_promotion_tables(store: LearningStore) -> None:
    """Idempotently initialize promotion schema tables on the store connection."""
    store.connection.executescript(PROMOTION_SCHEMA_SQL)


def record_journey_pass(
    store: LearningStore,
    workspace_id: str | UUID | None = None,
    *,
    capability: str = "research_to_learning",
    work_key: str | None = None,
    work_id: str | UUID | None = None,
) -> str:
    """Record a verified journey pass for research-to-learning capability."""
    ensure_promotion_tables(store)
    pass_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    ws_str = str(workspace_id) if workspace_id is not None else None
    wid_str = str(work_id) if work_id is not None else None

    with store.transaction() as conn:
        conn.execute(
            """
            INSERT INTO journey_passes (
                pass_id, capability, workspace_id, work_key, work_id, passed_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (pass_id, capability, ws_str, work_key, wid_str, now),
        )
    return pass_id


def has_journey_pass(
    store: LearningStore,
    workspace_id: str | UUID | None = None,
    capability: str = "research_to_learning",
) -> bool:
    """Check if a journey pass has been recorded."""
    ensure_promotion_tables(store)
    with store.transaction() as conn:
        if workspace_id is not None:
            row = conn.execute(
                """
                SELECT 1 FROM journey_passes
                WHERE capability = ?
                  AND (workspace_id = ? OR workspace_id IS NULL)
                LIMIT 1
                """,
                (capability, str(workspace_id)),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT 1 FROM journey_passes
                WHERE capability = ?
                LIMIT 1
                """,
                (capability,),
            ).fetchone()
        return row is not None


def request_promotion(
    store: LearningStore,
    workspace_id: str | UUID,
    project_id: str | UUID | None = None,
) -> dict[str, Any] | None:
    """Request batch promotion for eligible active procedures.

    Returns None without a journey pass or if no eligible procedures exist
    (active and current version unbatched).
    If a pending batch already exists, returns the same decision record.
    Otherwise stores a pending batch and returns an OMP-414 decision record dictionary
    containing mission_budget.py keys, options ['approve', 'reject'],
    evidence_refs from support receipts, and batch listing [{procedure_id, version, title}].
    """
    ensure_promotion_tables(store)
    ws_str = str(workspace_id)
    proj_str = str(project_id) if project_id is not None else None

    with store.transaction() as conn:
        # Return existing pending batch if one is currently pending
        pending_row = conn.execute(
            """
            SELECT decision_json
            FROM promotion_batches
            WHERE status = 'pending'
              AND (workspace_id = ? OR workspace_id IS NULL)
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (ws_str,),
        ).fetchone()
        if pending_row is not None:
            return json.loads(pending_row["decision_json"])

        # Check for visible journey passes
        pass_rows = conn.execute(
            """
            SELECT work_id, workspace_id
            FROM journey_passes
            WHERE capability = 'research_to_learning'
              AND (workspace_id = ? OR workspace_id IS NULL)
            """,
            (ws_str,),
        ).fetchall()
        if not pass_rows:
            return None

        has_null_work_id_pass = any(pr["work_id"] is None for pr in pass_rows)
        item_pass_work_ids = {
            str(pr["work_id"]) for pr in pass_rows if pr["work_id"] is not None
        }

        # Find active procedures at current version
        rows = conn.execute(
            """
            SELECT
                p.procedure_id,
                p.current_version,
                pv.title,
                p.created_at
            FROM procedures p
            JOIN procedure_versions pv
              ON p.procedure_id = pv.procedure_id
             AND p.current_version = pv.version
            WHERE p.status = 'active'
            ORDER BY p.created_at ASC, p.procedure_id ASC
            """
        ).fetchall()

        batched_rows = conn.execute(
            "SELECT procedure_id, version FROM promotion_batch_items"
        ).fetchall()
        batched = {(br["procedure_id"], int(br["version"])) for br in batched_rows}

        unbatched = [
            r for r in rows
            if (r["procedure_id"], int(r["current_version"])) not in batched
        ]
        if not unbatched:
            return None

        unbatched_proc_ids = [r["procedure_id"] for r in unbatched]
        placeholders = ",".join("?" for _ in unbatched_proc_ids)

        accepted_props = conn.execute(
            f"SELECT procedure_id, source_json FROM proposals WHERE procedure_id IN ({placeholders}) AND status = 'accepted'",  # nosec B608
            unbatched_proc_ids,
        ).fetchall()
        props_by_proc: dict[str, list[dict[str, Any]]] = {}
        for p in accepted_props:
            props_by_proc.setdefault(p["procedure_id"], []).append(p)

        proc_versions = conn.execute(
            f"SELECT procedure_id, source_json FROM procedure_versions WHERE procedure_id IN ({placeholders})",  # nosec B608
            unbatched_proc_ids,
        ).fetchall()
        versions_by_proc: dict[str, list[dict[str, Any]]] = {}
        for pv in proc_versions:
            versions_by_proc.setdefault(pv["procedure_id"], []).append(pv)

        eligible: list[Any] = []
        for r in unbatched:
            proc_id = r["procedure_id"]
            prop_rows = props_by_proc.get(proc_id, [])
            ver_rows = versions_by_proc.get(proc_id, [])

            valid_sources: list[SourceIdentity] = []
            for p_row in prop_rows:
                src = _parse_source_identity(p_row["source_json"])
                if src is not None:
                    valid_sources.append(src)
            for v_row in ver_rows:
                src = _parse_source_identity(v_row["source_json"])
                if src is not None:
                    valid_sources.append(src)

            has_accepted_props = len(prop_rows) > 0
            all_versions_empty = len(ver_rows) > 0 and all(
                _is_empty_source_json(v["source_json"]) for v in ver_rows
            )
            is_legacy = (not has_accepted_props) and all_versions_empty

            if is_legacy:
                if has_null_work_id_pass:
                    eligible.append(r)
            elif valid_sources:
                if any(
                    str(src.workspace_id) == ws_str
                    and str(src.aggregate_id) in item_pass_work_ids
                    for src in valid_sources
                ):
                    eligible.append(r)

        if not eligible:
            return None


        batch = [
            {
                "procedure_id": r["procedure_id"],
                "version": int(r["current_version"]),
                "title": r["title"],
            }
            for r in eligible
        ]

        proc_ids = [item["procedure_id"] for item in batch]
        placeholders = ",".join("?" for _ in proc_ids)
        support_rows = conn.execute(
            f"SELECT DISTINCT receipt_id FROM procedure_support WHERE procedure_id IN ({placeholders}) ORDER BY receipt_id ASC",  # nosec B608 - placeholders are only '?' bind markers; procedure ids are bound parameters
            proc_ids,
        ).fetchall()
        evidence_refs = [sr["receipt_id"] for sr in support_rows]

        decision_id = str(uuid4())
        decision: dict[str, Any] = {
            "decision_id": decision_id,
            "project_id": proj_str,
            "mission_id": None,
            "question": "Approve promotion of learned procedure batch to live injection?",
            "why_it_matters": "Learned procedures require owner batch approval before becoming eligible for live injection.",
            "options": ["approve", "reject"],
            "evidence_refs": evidence_refs,
            "default_if_any": None,
            "risk_of_delay": "Procedures in this batch remain unpromoted and will not be supplied to active workflows.",
            "risk_of_each_choice": {
                "approve": "Promotes the procedures in this batch to live injection supply.",
                "reject": "Permanently rejects this version of the procedures from live supply.",
            },
            "batch": batch,
        }

        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            INSERT INTO promotion_batches (
                decision_id, workspace_id, project_id, status, decision_json, created_at
            ) VALUES (?, ?, ?, 'pending', ?, ?)
            """,
            (
                decision_id,
                ws_str,
                proj_str,
                json.dumps(decision, sort_keys=True),
                now,
            ),
        )
        for item in batch:
            conn.execute(
                """
                INSERT INTO promotion_batch_items (
                    decision_id, procedure_id, version, title, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    item["procedure_id"],
                    item["version"],
                    item["title"],
                    now,
                ),
            )

        return decision


def answer_promotion(
    store: LearningStore,
    decision_id: str | UUID,
    approve: bool,
    answer_ref: str,
) -> dict[str, Any]:
    """Answer a pending promotion batch. An answered batch stays answered.

    Approve and reject are one-way. A later call on the same decision_id does
    not change status, answer_ref, or answered_at, so a rejected version cannot
    be supplied by approving that decision again.
    """
    ensure_promotion_tables(store)
    decision_id_str = str(decision_id)
    new_status = "approved" if approve else "rejected"
    now = datetime.now(timezone.utc).isoformat()

    with store.transaction() as conn:
        row = conn.execute(
            "SELECT status FROM promotion_batches WHERE decision_id = ?",
            (decision_id_str,),
        ).fetchone()
        if row is None:
            raise KeyError(f"promotion decision {decision_id_str} not found")
        if row["status"] != "pending":
            raise ValueError(
                f"promotion decision {decision_id_str} is already {row['status']}"
            )

        updated = conn.execute(
            """
            UPDATE promotion_batches
            SET status = ?, answer_ref = ?, answered_at = ?
            WHERE decision_id = ? AND status = 'pending'
            """,
            (new_status, str(answer_ref), now, decision_id_str),
        )
        if updated.rowcount != 1:
            raise ValueError(
                f"promotion decision {decision_id_str} is no longer pending"
            )

        return {
            "decision_id": decision_id_str,
            "status": new_status,
            "answer_ref": str(answer_ref),
            "answered_at": now,
        }


def journey_pass_workspace(
    store: LearningStore,
    capability: str = "research_to_learning",
) -> str | None:
    """Workspace on the latest journey pass, or None when no pass is recorded."""
    ensure_promotion_tables(store)
    with store.transaction() as conn:
        row = conn.execute(
            """
            SELECT workspace_id
            FROM journey_passes
            WHERE capability = ?
            ORDER BY passed_at DESC, rowid DESC
            LIMIT 1
            """,
            (capability,),
        ).fetchone()
    if row is None or row["workspace_id"] is None:
        return None
    return str(row["workspace_id"])


def get_promoted_procedures(
    store: LearningStore,
    workspace_id: str | UUID | None = None,
) -> set[tuple[str, int]]:
    """Return set of approved (procedure_id, version) pairs, or empty if no journey pass."""
    ensure_promotion_tables(store)
    if not has_journey_pass(store, workspace_id=workspace_id):
        return set()

    with store.transaction() as conn:
        if workspace_id is not None:
            rows = conn.execute(
                """
                SELECT pbi.procedure_id, pbi.version
                FROM promotion_batch_items pbi
                JOIN promotion_batches pb ON pbi.decision_id = pb.decision_id
                WHERE pb.status = 'approved'
                  AND (pb.workspace_id = ? OR pb.workspace_id IS NULL)
                """,
                (str(workspace_id),),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT pbi.procedure_id, pbi.version
                FROM promotion_batch_items pbi
                JOIN promotion_batches pb ON pbi.decision_id = pb.decision_id
                WHERE pb.status = 'approved'
                """
            ).fetchall()
        return {(r["procedure_id"], int(r["version"])) for r in rows}


__all__ = [
    "PROMOTION_SCHEMA_SQL",
    "answer_promotion",
    "ensure_promotion_tables",
    "get_promoted_procedures",
    "has_journey_pass",
    "journey_pass_workspace",
    "record_journey_pass",
    "request_promotion",
]

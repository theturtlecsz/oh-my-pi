from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class MissingSection(str):
    """Represents a section whose underlying store is absent.

    Evaluates as string 'missing' (and serializes to JSON as 'missing'),
    while also answering 'missing' to dictionary .get('status') or ['status'].
    """

    def __new__(cls) -> MissingSection:
        return super().__new__(cls, "missing")

    def __getitem__(self, key: str) -> Any:
        if key == "status":
            return "missing"
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        if key == "status":
            return "missing"
        return default


class EvidenceList(list):
    """List of procedure evidence records that also supports indexing by procedure_id."""

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str):
            for item in self:
                if isinstance(item, dict) and item.get("procedure_id") == key:
                    return item
            raise KeyError(key)
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        if isinstance(key, str):
            for item in self:
                if isinstance(item, dict) and item.get("procedure_id") == key:
                    return item
            return default
        return default


@dataclass
class InspectionReport:
    """Read-only inspection report over a state root."""

    sources: Any
    evidence: Any
    procedures: Any
    acceptance: Any

    def to_dict(self) -> dict[str, Any]:
        """Convert report to JSON-serializable dictionary."""
        return {
            "sources": self.sources if not hasattr(self.sources, "to_dict") else self.sources.to_dict(),
            "evidence": list(self.evidence) if isinstance(self.evidence, list) else (self.evidence.to_dict() if hasattr(self.evidence, "to_dict") else self.evidence),
            "procedures": list(self.procedures) if isinstance(self.procedures, list) else (self.procedures.to_dict() if hasattr(self.procedures, "to_dict") else self.procedures),
            "acceptance": self.acceptance if not hasattr(self.acceptance, "to_dict") else self.acceptance.to_dict(),
        }


def _connect_ro(path: Path) -> sqlite3.Connection | None:
    """Connect to SQLite database in read-only mode using a URI.

    Never creates the file and never writes to it.
    """
    if not path.is_file():
        return None
    try:
        resolved = path.resolve()
        uri = f"file:{resolved.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def build_report(state_root: str | Path) -> InspectionReport:
    """Builds a read-only inspection report over learning and structural stores."""
    root = Path(state_root)
    learning_path = root / "learning.sqlite"
    structural_path = root / "structural-publications.sqlite"

    learning_conn = _connect_ro(learning_path)
    structural_conn = _connect_ro(structural_path)

    # 1. Structural snapshots
    structural_snapshots: Any = MissingSection()
    if structural_conn is not None:
        try:
            if _table_exists(structural_conn, "structural_snapshots"):
                snap_rows = structural_conn.execute(
                    "SELECT workspace_id, repository_id, snapshot_id, state, published_at "
                    "FROM structural_snapshots ORDER BY staged_at ASC"
                ).fetchall()
                structural_snapshots = [
                    {
                        "workspace": str(r["workspace_id"]),
                        "workspace_id": str(r["workspace_id"]),
                        "repository": str(r["repository_id"]),
                        "repository_id": str(r["repository_id"]),
                        "snapshot": str(r["snapshot_id"]),
                        "snapshot_id": str(r["snapshot_id"]),
                        "state": str(r["state"]),
                        "published_at": r["published_at"],
                    }
                    for r in snap_rows
                ]
            else:
                structural_snapshots = []
        finally:
            structural_conn.close()

    # 2. Learning store data: procedures, evidence, acceptance, capture units
    if learning_conn is None:
        capture_units: Any = MissingSection()
        procedures_sec: Any = MissingSection()
        evidence_sec: Any = MissingSection()
        acceptance_sec: Any = MissingSection()
    else:
        try:
            # Capture units for sources section
            capture_units = []
            if _table_exists(learning_conn, "units"):
                unit_rows = learning_conn.execute(
                    "SELECT unit_id, workspace_id, event_id, state, error_code, source_json "
                    "FROM units ORDER BY created_at ASC"
                ).fetchall()
                for r in unit_rows:
                    event_source = None
                    if r["source_json"]:
                        try:
                            event_source = json.loads(r["source_json"])
                        except Exception:
                            event_source = r["source_json"]
                    if not event_source:
                        event_source = r["event_id"]

                    capture_units.append(
                        {
                            "unit_id": str(r["unit_id"]),
                            "event_source": event_source,
                            "state": str(r["state"]),
                            "error_code": r["error_code"],
                        }
                    )

            # Procedures section
            procedures_list: list[dict[str, Any]] = []
            evidence_items: list[dict[str, Any]] = []
            if _table_exists(learning_conn, "procedures"):
                proc_rows = learning_conn.execute(
                    "SELECT procedure_id, fingerprint, status, current_version, title, created_at, updated_at "
                    "FROM procedures ORDER BY created_at ASC"
                ).fetchall()

                for p_row in proc_rows:
                    pid = str(p_row["procedure_id"])

                    # Version history
                    version_history = []
                    if _table_exists(learning_conn, "procedure_versions"):
                        v_rows = learning_conn.execute(
                            "SELECT version, title, steps_json, preconditions_json, model, profile, source_json, created_at "
                            "FROM procedure_versions WHERE procedure_id = ? ORDER BY version ASC",
                            (pid,),
                        ).fetchall()
                        for v in v_rows:
                            steps: Any = []
                            try:
                                steps = json.loads(v["steps_json"])
                            except Exception:
                                steps = v["steps_json"]
                            preconditions: Any = {}
                            try:
                                preconditions = json.loads(v["preconditions_json"])
                            except Exception:
                                preconditions = v["preconditions_json"]

                            version_history.append(
                                {
                                    "version": v["version"],
                                    "title": v["title"],
                                    "steps": steps,
                                    "preconditions": preconditions,
                                    "model": v["model"],
                                    "profile": v["profile"],
                                    "created_at": v["created_at"],
                                }
                            )

                    # Supporting receipts
                    supporting_ids = []
                    if _table_exists(learning_conn, "procedure_support"):
                        supp_rows = learning_conn.execute(
                            "SELECT receipt_id FROM procedure_support WHERE procedure_id = ? ORDER BY created_at ASC",
                            (pid,),
                        ).fetchall()
                        supporting_ids = [str(r["receipt_id"]) for r in supp_rows]

                    # Usage receipts
                    use_receipt_ids = []
                    if _table_exists(learning_conn, "uses") and _table_exists(learning_conn, "supplies"):
                        use_rows = learning_conn.execute(
                            "SELECT u.receipt_ids_json FROM uses u JOIN supplies s ON u.supply_id = s.supply_id "
                            "WHERE s.procedure_id = ? ORDER BY u.used_at ASC",
                            (pid,),
                        ).fetchall()
                        for u in use_rows:
                            try:
                                parsed = json.loads(u["receipt_ids_json"])
                                if isinstance(parsed, list):
                                    use_receipt_ids.extend([str(item) for item in parsed])
                                else:
                                    use_receipt_ids.append(str(parsed))
                            except Exception:
                                use_receipt_ids.append(str(u["receipt_ids_json"]))

                    # Outcomes
                    outcomes = []
                    outcome_ids = []
                    if (
                        _table_exists(learning_conn, "outcomes")
                        and _table_exists(learning_conn, "uses")
                        and _table_exists(learning_conn, "supplies")
                    ):
                        outcome_rows = learning_conn.execute(
                            "SELECT o.receipt_id, o.verdict FROM outcomes o "
                            "JOIN uses u ON o.use_id = u.use_id "
                            "JOIN supplies s ON u.supply_id = s.supply_id "
                            "WHERE s.procedure_id = ? ORDER BY o.recorded_at ASC",
                            (pid,),
                        ).fetchall()
                        for o in outcome_rows:
                            rid = str(o["receipt_id"])
                            outcome_ids.append(rid)
                            outcomes.append({"receipt_id": rid, "verdict": str(o["verdict"])})

                    # Corrections
                    corrections = []
                    correction_ids = []
                    if _table_exists(learning_conn, "corrections"):
                        corr_rows = learning_conn.execute(
                            "SELECT receipt_id, action, status, reason FROM corrections "
                            "WHERE procedure_id = ? ORDER BY created_at ASC",
                            (pid,),
                        ).fetchall()
                        for c in corr_rows:
                            rid = str(c["receipt_id"])
                            correction_ids.append(rid)
                            corrections.append(
                                {
                                    "receipt_id": rid,
                                    "action": str(c["action"]),
                                    "status": str(c["status"]),
                                    "reason": c["reason"],
                                }
                            )

                    procedures_list.append(
                        {
                            "id": pid,
                            "procedure_id": pid,
                            "title": p_row["title"],
                            "status": p_row["status"],
                            "current_version": p_row["current_version"],
                            "version_history": version_history,
                            "versions": version_history,
                            "supporting_receipt_ids": supporting_ids,
                            "outcome_receipt_ids": outcome_ids,
                            "created_at": p_row["created_at"],
                            "updated_at": p_row["updated_at"],
                        }
                    )

                    evidence_items.append(
                        {
                            "procedure_id": pid,
                            "procedure_support": supporting_ids,
                            "supporting_receipt_ids": supporting_ids,
                            "uses": use_receipt_ids,
                            "use_receipt_ids": use_receipt_ids,
                            "outcomes": outcomes,
                            "outcome_receipt_ids": outcome_ids,
                            "corrections": corrections,
                            "correction_receipt_ids": correction_ids,
                        }
                    )

            procedures_sec = procedures_list
            evidence_sec = EvidenceList(evidence_items)

            # Acceptance section: proposals and cleanup_queue
            proposals = []
            if _table_exists(learning_conn, "proposals"):
                prop_rows = learning_conn.execute(
                    "SELECT proposal_id, unit_id, status, reason, procedure_id, created_at "
                    "FROM proposals ORDER BY created_at ASC"
                ).fetchall()
                for pr in prop_rows:
                    proposals.append(
                        {
                            "proposal_id": str(pr["proposal_id"]),
                            "unit_id": str(pr["unit_id"]),
                            "status": str(pr["status"]),
                            "reason": pr["reason"],
                            "procedure_id": str(pr["procedure_id"]) if pr["procedure_id"] else None,
                            "created_at": pr["created_at"],
                        }
                    )

            pending_cleanup = []
            done_cleanup = []
            if _table_exists(learning_conn, "cleanup_queue"):
                q_rows = learning_conn.execute(
                    "SELECT queue_id, procedure_id, action, created_at, done_at "
                    "FROM cleanup_queue ORDER BY created_at ASC"
                ).fetchall()
                for q in q_rows:
                    item = {
                        "queue_id": q["queue_id"],
                        "procedure_id": str(q["procedure_id"]),
                        "action": str(q["action"]),
                        "created_at": q["created_at"],
                    }
                    if q["done_at"] is None or not q["done_at"]:
                        pending_cleanup.append(item)
                    else:
                        item["done_at"] = q["done_at"]
                        done_cleanup.append(item)

            acceptance_sec = {
                "proposals": proposals,
                "accepted_proposals": [p for p in proposals if p["status"] == "accepted"],
                "rejected_proposals": [p for p in proposals if p["status"] == "rejected"],
                "cleanup_queue": {
                    "pending": pending_cleanup,
                    "done": done_cleanup,
                },
                "pending_cleanup": len(pending_cleanup),
                "done_cleanup": len(done_cleanup),
            }
        finally:
            learning_conn.close()

    # Sources section
    if structural_snapshots == "missing" and capture_units == "missing":
        sources_sec: Any = MissingSection()
    else:
        sources_sec = {
            "structural_snapshots": structural_snapshots,
            "capture_units": capture_units,
        }

    return InspectionReport(
        sources=sources_sec,
        evidence=evidence_sec,
        procedures=procedures_sec,
        acceptance=acceptance_sec,
    )

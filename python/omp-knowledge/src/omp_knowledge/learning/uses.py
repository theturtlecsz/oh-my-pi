from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from ..errors import InvalidRequestError
from .models import StrictModel
from .policy import NativeReceipts
from .store import LearningStore


class SupplyRecord(StrictModel):
    supply_id: str
    procedure_id: str
    version: int
    workspace_id: str
    work_key: str
    context: dict[str, Any]
    supplied_at: str


class UseRecord(StrictModel):
    use_id: str
    supply_id: str
    candidate_id: str
    receipt_ids: tuple[str, ...]
    used_at: str

    def __str__(self) -> str:
        return self.use_id


class OutcomeRecord(StrictModel):
    outcome_id: str
    use_id: str
    receipt_id: str
    candidate_id: str
    verdict: str
    recorded_at: str

    def __str__(self) -> str:
        return self.outcome_id


class SupplyResult(StrictModel):
    supply_ids: tuple[str, ...] = ()
    lines: tuple[str, ...] = ()

    def __len__(self) -> int:
        return len(self.lines)

    def __iter__(self) -> Iterator[str]:
        return iter(self.lines)

    def __getitem__(self, idx: int) -> str:
        return self.lines[idx]


class HistoryEntry(StrictModel):
    supply_id: str
    procedure_id: str
    version: int
    workspace_id: str
    work_key: str
    context: dict[str, Any]
    supplied_at: str
    use_id: str | None = None
    candidate_id: str | None = None
    receipt_ids: tuple[str, ...] = ()
    used_at: str | None = None
    outcome_id: str | None = None
    outcome_receipt_id: str | None = None
    verdict: str | None = None
    recorded_at: str | None = None


class ProcedureHistory:
    def __init__(
        self,
        *,
        procedure_id: str,
        entries: list[HistoryEntry],
        supplies: list[SupplyRecord],
        uses: list[UseRecord],
        outcomes: list[OutcomeRecord],
    ) -> None:
        self.procedure_id = procedure_id
        self.entries = tuple(entries)
        self.supplies = tuple(supplies)
        self.uses = tuple(uses)
        self.outcomes = tuple(outcomes)

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self) -> Iterator[HistoryEntry]:
        return iter(self.entries)

    def __getitem__(self, item: Any) -> Any:
        if isinstance(item, int):
            return self.entries[item]
        if isinstance(item, str):
            if item == "supplies":
                return self.supplies
            if item == "uses":
                return self.uses
            if item == "outcomes":
                return self.outcomes
            if item in ("entries", "rows"):
                return self.entries
            if item == "procedure_id":
                return self.procedure_id
        raise KeyError(item)

    def to_dict(self) -> dict[str, Any]:
        return {
            "procedure_id": self.procedure_id,
            "supplies": [s.model_dump(mode="json") for s in self.supplies],
            "uses": [u.model_dump(mode="json") for u in self.uses],
            "outcomes": [o.model_dump(mode="json") for o in self.outcomes],
            "entries": [e.model_dump(mode="json") for e in self.entries],
        }

    def __repr__(self) -> str:
        return (
            f"ProcedureHistory(procedure_id={self.procedure_id!r}, "
            f"supplies={len(self.supplies)}, uses={len(self.uses)}, outcomes={len(self.outcomes)})"
        )


def _version_matches_context(
    preconditions: list[dict[str, Any]],
    context: dict[str, Any],
) -> bool:
    """A version applies when every eq precondition matches the context
    and no ne precondition does.
    """
    for p in preconditions:
        key = p.get("key")
        op = p.get("op")
        val = p.get("value")
        ctx_val = context.get(key) if key else None
        if op == "eq":
            if ctx_val != val:
                return False
        elif op == "ne":
            if ctx_val is not None and ctx_val == val:
                return False
    return True


def supply(
    store: LearningStore,
    *,
    workspace_id: str | UUID,
    work_key: str,
    context: dict[str, str] | None = None,
    limit: int = 3,
) -> SupplyResult:
    """Reads procedures live from SQLite (status active, current_version).
    A version applies when every eq precondition matches the context and no ne precondition does.
    Writes one supplies row per supplied (procedure_id, version, work_key, context).
    Returns supply ids and render lines 'PROCEDURE <id>@v<n> supply=<supply_id>: <title> - <steps>'.
    """
    ctx = dict(context or {})
    ws_id_str = str(workspace_id)

    with store.transaction() as conn:
        rows = conn.execute(
            """
            SELECT
                p.procedure_id,
                p.current_version,
                pv.title,
                pv.steps_json,
                pv.preconditions_json,
                p.created_at
            FROM procedures p
            JOIN procedure_versions pv
              ON p.procedure_id = pv.procedure_id
             AND p.current_version = pv.version
            WHERE p.status = 'active'
            ORDER BY p.created_at ASC, p.procedure_id ASC
            """
        ).fetchall()

        matching: list[dict[str, Any]] = []
        for r in rows:
            try:
                preconditions = json.loads(r["preconditions_json"])
            except Exception:
                preconditions = []

            if not _version_matches_context(preconditions, ctx):
                continue

            try:
                steps = json.loads(r["steps_json"])
                if isinstance(steps, list):
                    steps_str = "; ".join(str(s).strip() for s in steps)
                else:
                    steps_str = str(steps)
            except Exception:
                steps_str = ""

            matching.append(
                {
                    "procedure_id": r["procedure_id"],
                    "current_version": r["current_version"],
                    "title": r["title"],
                    "steps_str": steps_str,
                }
            )

        if limit is not None and limit > 0:
            matching = matching[:limit]

        supply_ids: list[str] = []
        lines: list[str] = []
        now = datetime.now(timezone.utc).isoformat()
        ctx_json = json.dumps(ctx, sort_keys=True)

        for item in matching:
            supply_id = str(uuid4())
            conn.execute(
                """
                INSERT INTO supplies (
                    supply_id, procedure_id, version, workspace_id, work_key, context_json, supplied_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    supply_id,
                    item["procedure_id"],
                    item["current_version"],
                    ws_id_str,
                    work_key,
                    ctx_json,
                    now,
                ),
            )
            supply_ids.append(supply_id)
            line = (
                f"PROCEDURE {item['procedure_id']}@v{item['current_version']} "
                f"supply={supply_id}: {item['title']} - {item['steps_str']}"
            )
            lines.append(line)

        return SupplyResult(
            supply_ids=tuple(supply_ids),
            lines=tuple(lines),
        )


def record_use(
    store: LearningStore,
    reader: NativeReceipts,
    *,
    supply_id: str | UUID,
    candidate_id: str | UUID,
    receipt_ids: Sequence[str | UUID],
) -> UseRecord:
    """Each receipt must resolve and have candidate_id == candidate_id,
    else omp_knowledge.errors.InvalidRequestError. Writes uses row.
    """
    supply_id_str = str(getattr(supply_id, "supply_id", supply_id))
    candidate_id_str = str(candidate_id)

    if not receipt_ids:
        raise InvalidRequestError("receipt_ids must not be empty")

    verified_rids: list[str] = []
    for rid in receipt_ids:
        rid_str = str(rid)
        try:
            receipt = reader.receipt(rid)
        except Exception as err:
            raise InvalidRequestError(f"Receipt {rid} not found: {err}") from err
        if receipt is None:
            raise InvalidRequestError(f"Receipt {rid} not found")

        receipt_candidate = getattr(receipt, "candidate_id", None)
        if receipt_candidate is None or str(receipt_candidate) != candidate_id_str:
            raise InvalidRequestError(
                f"Receipt {rid} candidate_id ({receipt_candidate}) does not match candidate_id ({candidate_id_str})"
            )
        verified_rids.append(rid_str)

    with store.transaction() as conn:
        supply_row = conn.execute(
            "SELECT supply_id FROM supplies WHERE supply_id = ?",
            (supply_id_str,),
        ).fetchone()
        if not supply_row:
            raise InvalidRequestError(f"Supply {supply_id_str} not found")

        use_id = str(uuid4())
        used_at = datetime.now(timezone.utc).isoformat()
        rids_json = json.dumps(verified_rids)

        conn.execute(
            """
            INSERT INTO uses (use_id, supply_id, candidate_id, receipt_ids_json, used_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (use_id, supply_id_str, candidate_id_str, rids_json, used_at),
        )

        return UseRecord(
            use_id=use_id,
            supply_id=supply_id_str,
            candidate_id=candidate_id_str,
            receipt_ids=tuple(verified_rids),
            used_at=used_at,
        )


def record_outcome(
    store: LearningStore,
    reader: NativeReceipts,
    *,
    use_id: str | UUID,
    receipt_id: str | UUID,
) -> OutcomeRecord:
    """Receipt kind verification/audit, same candidate as the use,
    verdict set -> outcomes row with verdict.
    """
    use_id_str = str(getattr(use_id, "use_id", use_id))
    receipt_id_str = str(getattr(receipt_id, "receipt_id", receipt_id))

    try:
        receipt = reader.receipt(receipt_id)
    except Exception as err:
        raise InvalidRequestError(f"Receipt {receipt_id_str} not found: {err}") from err
    if receipt is None:
        raise InvalidRequestError(f"Receipt {receipt_id_str} not found")

    kind = getattr(receipt, "kind", None)
    kind_str = str(kind.value).lower() if hasattr(kind, "value") else str(kind).lower()
    if kind_str not in ("verification", "audit"):
        raise InvalidRequestError(
            f"Receipt {receipt_id_str} kind must be 'verification' or 'audit', got '{kind}'"
        )

    receipt_candidate = getattr(receipt, "candidate_id", None)
    if receipt_candidate is None:
        raise InvalidRequestError(f"Receipt {receipt_id_str} has no candidate_id")

    verdict = getattr(receipt, "verdict", None)
    if verdict is None or not str(verdict).strip():
        raise InvalidRequestError(f"Receipt {receipt_id_str} has no verdict set")
    verdict_str = str(verdict.value) if hasattr(verdict, "value") else str(verdict)

    with store.transaction() as conn:
        use_row = conn.execute(
            "SELECT use_id, candidate_id FROM uses WHERE use_id = ?",
            (use_id_str,),
        ).fetchone()
        if not use_row:
            raise InvalidRequestError(f"Use {use_id_str} not found")

        if str(receipt_candidate) != str(use_row["candidate_id"]):
            raise InvalidRequestError(
                f"Receipt {receipt_id_str} candidate_id ({receipt_candidate}) does not match use candidate_id ({use_row['candidate_id']})"
            )

        outcome_id = str(uuid4())
        recorded_at = datetime.now(timezone.utc).isoformat()

        conn.execute(
            """
            INSERT INTO outcomes (outcome_id, use_id, receipt_id, candidate_id, verdict, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                outcome_id,
                use_id_str,
                receipt_id_str,
                str(receipt_candidate),
                verdict_str,
                recorded_at,
            ),
        )

        return OutcomeRecord(
            outcome_id=outcome_id,
            use_id=use_id_str,
            receipt_id=receipt_id_str,
            candidate_id=str(receipt_candidate),
            verdict=verdict_str,
            recorded_at=recorded_at,
        )


def procedure_history(
    store: LearningStore,
    procedure_id: str | UUID,
) -> ProcedureHistory:
    """Returns supplies, uses, outcomes joined for a given procedure_id."""
    proc_id_str = str(getattr(procedure_id, "procedure_id", procedure_id))

    with store.transaction() as conn:
        supplies_rows = conn.execute(
            """
            SELECT supply_id, procedure_id, version, workspace_id, work_key, context_json, supplied_at
            FROM supplies
            WHERE procedure_id = ?
            ORDER BY supplied_at ASC, supply_id ASC
            """,
            (proc_id_str,),
        ).fetchall()

        supplies: list[SupplyRecord] = []
        supply_ids: list[str] = []
        for r in supplies_rows:
            try:
                ctx = json.loads(r["context_json"])
            except Exception:
                ctx = {}
            supplies.append(
                SupplyRecord(
                    supply_id=r["supply_id"],
                    procedure_id=r["procedure_id"],
                    version=r["version"],
                    workspace_id=r["workspace_id"],
                    work_key=r["work_key"],
                    context=ctx,
                    supplied_at=r["supplied_at"],
                )
            )
            supply_ids.append(r["supply_id"])

        uses: list[UseRecord] = []
        use_ids: list[str] = []
        if supply_ids:
            placeholders = ",".join("?" for _ in supply_ids)
            uses_rows = conn.execute(
                f"""
                SELECT use_id, supply_id, candidate_id, receipt_ids_json, used_at
                FROM uses
                WHERE supply_id IN ({placeholders})
                ORDER BY used_at ASC, use_id ASC
                """,
                tuple(supply_ids),
            ).fetchall()
            for r in uses_rows:
                try:
                    rids = tuple(json.loads(r["receipt_ids_json"]))
                except Exception:
                    rids = ()
                uses.append(
                    UseRecord(
                        use_id=r["use_id"],
                        supply_id=r["supply_id"],
                        candidate_id=r["candidate_id"],
                        receipt_ids=rids,
                        used_at=r["used_at"],
                    )
                )
                use_ids.append(r["use_id"])

        outcomes: list[OutcomeRecord] = []
        if use_ids:
            placeholders = ",".join("?" for _ in use_ids)
            outcomes_rows = conn.execute(
                f"""
                SELECT outcome_id, use_id, receipt_id, candidate_id, verdict, recorded_at
                FROM outcomes
                WHERE use_id IN ({placeholders})
                ORDER BY recorded_at ASC, outcome_id ASC
                """,
                tuple(use_ids),
            ).fetchall()
            for r in outcomes_rows:
                outcomes.append(
                    OutcomeRecord(
                        outcome_id=r["outcome_id"],
                        use_id=r["use_id"],
                        receipt_id=r["receipt_id"],
                        candidate_id=r["candidate_id"] or "",
                        verdict=r["verdict"],
                        recorded_at=r["recorded_at"],
                    )
                )

        joined_rows = conn.execute(
            """
            SELECT
                s.supply_id, s.procedure_id, s.version, s.workspace_id, s.work_key, s.context_json, s.supplied_at,
                u.use_id, u.candidate_id AS use_candidate_id, u.receipt_ids_json, u.used_at,
                o.outcome_id, o.receipt_id AS outcome_receipt_id, o.candidate_id AS outcome_candidate_id, o.verdict, o.recorded_at
            FROM supplies s
            LEFT JOIN uses u ON s.supply_id = u.supply_id
            LEFT JOIN outcomes o ON u.use_id = o.use_id
            WHERE s.procedure_id = ?
            ORDER BY s.supplied_at ASC, u.used_at ASC, o.recorded_at ASC
            """,
            (proc_id_str,),
        ).fetchall()

        entries: list[HistoryEntry] = []
        for r in joined_rows:
            try:
                ctx = json.loads(r["context_json"])
            except Exception:
                ctx = {}
            rids_json = r["receipt_ids_json"]
            rids = tuple(json.loads(rids_json)) if rids_json else ()
            entries.append(
                HistoryEntry(
                    supply_id=r["supply_id"],
                    procedure_id=r["procedure_id"],
                    version=r["version"],
                    workspace_id=r["workspace_id"],
                    work_key=r["work_key"],
                    context=ctx,
                    supplied_at=r["supplied_at"],
                    use_id=r["use_id"],
                    candidate_id=r["use_candidate_id"] or r["outcome_candidate_id"],
                    receipt_ids=rids,
                    used_at=r["used_at"],
                    outcome_id=r["outcome_id"],
                    outcome_receipt_id=r["outcome_receipt_id"],
                    verdict=r["verdict"],
                    recorded_at=r["recorded_at"],
                )
            )

        return ProcedureHistory(
            procedure_id=proc_id_str,
            entries=entries,
            supplies=supplies,
            uses=uses,
            outcomes=outcomes,
        )


__all__ = [
    "HistoryEntry",
    "OutcomeRecord",
    "ProcedureHistory",
    "SupplyRecord",
    "SupplyResult",
    "UseRecord",
    "procedure_history",
    "record_outcome",
    "record_use",
    "supply",
]

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID, uuid4

from omp_work.v1.models import EvidenceReceipt

from ..errors import InvalidRequestError
from .models import Attribution, Precondition, SourceIdentity, StrictModel, StringId
from .policy import REASON_INVALID_CITATION, NativeReceipts
from .store import LearningStore

REASON_NOT_COUNTEREVIDENCE = "not_counterevidence"

_COUNTEREVIDENCE_KINDS = frozenset({"verification", "audit"})
_COUNTEREVIDENCE_VERDICTS = frozenset({"NEEDS_FIX", "BLOCKED"})
CorrectionAction = Literal["narrow", "withdraw"]
CorrectionStatus = Literal["accepted", "rejected"]


class CorrectionRecord(StrictModel):
    correction_id: StringId
    procedure_id: StringId
    receipt_id: StringId
    action: CorrectionAction
    status: CorrectionStatus
    reason: str | None = None
    from_version: int | None = None
    to_version: int | None = None
    preconditions: tuple[Precondition, ...] = ()
    model: str
    profile: str
    source: SourceIdentity
    created_at: str

    @property
    def attribution(self) -> Attribution:
        return Attribution(model=self.model, profile=self.profile, source=self.source)


def _kind_str(kind: Any) -> str:
    if hasattr(kind, "value"):
        return str(kind.value).lower()
    return str(kind).lower()


def _verdict_str(verdict: Any) -> str | None:
    if verdict is None:
        return None
    if hasattr(verdict, "value"):
        return str(verdict.value)
    return str(verdict)


def _counterevidence_reason(reader: NativeReceipts, receipt_id: str | UUID) -> str | None:
    """None when the receipt is native counterevidence.

    Unresolvable citations are invalid_citation. A resolved receipt that is not
    verification/audit with verdict NEEDS_FIX or BLOCKED is not_counterevidence.
    """
    try:
        receipt = reader.receipt(receipt_id)
    except Exception:
        return REASON_INVALID_CITATION
    if receipt is None or not isinstance(receipt, EvidenceReceipt):
        return REASON_INVALID_CITATION
    kind = _kind_str(receipt.kind)
    verdict = _verdict_str(receipt.verdict)
    if kind not in _COUNTEREVIDENCE_KINDS or verdict not in _COUNTEREVIDENCE_VERDICTS:
        return REASON_NOT_COUNTEREVIDENCE
    return None


def _preconditions_json(preconditions: Sequence[Precondition]) -> str | None:
    if not preconditions:
        return None
    return json.dumps([p.model_dump(mode="json") for p in preconditions])


def _insert_correction(conn: sqlite3.Connection, record: CorrectionRecord) -> None:
    conn.execute(
        """
        INSERT INTO corrections (
            correction_id, procedure_id, receipt_id, action, status, reason,
            from_version, to_version, preconditions_json,
            model, profile, source_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record.correction_id,
            record.procedure_id,
            record.receipt_id,
            record.action,
            record.status,
            record.reason,
            record.from_version,
            record.to_version,
            _preconditions_json(record.preconditions),
            record.model,
            record.profile,
            record.source.model_dump_json(),
            record.created_at,
        ),
    )


def _enqueue_cleanup(
    conn: sqlite3.Connection, *, procedure_id: str, action: str, created_at: str
) -> None:
    conn.execute(
        """
        INSERT INTO cleanup_queue (procedure_id, action, created_at)
        VALUES (?, ?, ?)
        """,
        (procedure_id, action, created_at),
    )


def _load_preconditions(raw: str, *, procedure_id: str, version: int) -> list[Any]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidRequestError(
            f"Procedure {procedure_id} version {version} has unreadable preconditions"
        ) from exc
    if not isinstance(parsed, list):
        raise InvalidRequestError(
            f"Procedure {procedure_id} version {version} preconditions are not a list"
        )
    return parsed


def correct(
    store: LearningStore,
    reader: NativeReceipts,
    *,
    procedure_id: str | UUID,
    receipt_id: str | UUID,
    action: CorrectionAction,
    preconditions: Sequence[Precondition] = (),
    attribution: Attribution,
) -> CorrectionRecord:
    """Narrow or withdraw a procedure from one native counterexample.

    narrow appends preconditions as procedure version current+1. withdraw sets
    procedures.status to withdrawn. The corrections row (from/to version and
    attribution) and a pending cleanup_queue row commit together. Nothing
    drains that queue: supply() reads procedures live, so retrieval sees the
    change while cleanup stays pending.

    A receipt that does not resolve, or that is not verification/audit with
    verdict NEEDS_FIX or BLOCKED, writes a rejected corrections row and leaves
    the procedure unchanged.
    """
    if action not in ("narrow", "withdraw"):
        raise InvalidRequestError(f"action must be 'narrow' or 'withdraw', got {action!r}")
    added = tuple(preconditions)
    if action == "narrow" and len(added) < 1:
        raise InvalidRequestError("narrow requires at least one precondition")

    procedure_id_str = str(procedure_id)
    receipt_id_str = str(receipt_id)
    now = datetime.now(timezone.utc).isoformat()
    correction_id = str(uuid4())
    reason = _counterevidence_reason(reader, receipt_id)

    if reason is not None:
        rejected = CorrectionRecord(
            correction_id=correction_id,
            procedure_id=procedure_id_str,
            receipt_id=receipt_id_str,
            action=action,
            status="rejected",
            reason=reason,
            from_version=None,
            to_version=None,
            preconditions=added,
            model=attribution.model,
            profile=attribution.profile,
            source=attribution.source,
            created_at=now,
        )
        with store.transaction() as conn:
            _insert_correction(conn, rejected)
        return rejected

    with store.transaction() as conn:
        proc = conn.execute(
            "SELECT procedure_id, current_version, status FROM procedures WHERE procedure_id = ?",
            (procedure_id_str,),
        ).fetchone()
        if proc is None:
            raise InvalidRequestError(f"Procedure {procedure_id_str} not found")

        current = int(proc["current_version"])
        if action == "narrow":
            version_row = conn.execute(
                """
                SELECT title, steps_json, preconditions_json
                FROM procedure_versions
                WHERE procedure_id = ? AND version = ?
                """,
                (procedure_id_str, current),
            ).fetchone()
            if version_row is None:
                raise InvalidRequestError(
                    f"Procedure {procedure_id_str} version {current} not found"
                )
            merged = _load_preconditions(
                version_row["preconditions_json"],
                procedure_id=procedure_id_str,
                version=current,
            )
            merged.extend(p.model_dump(mode="json") for p in added)
            to_version = current + 1
            conn.execute(
                """
                INSERT INTO procedure_versions (
                    procedure_id, version, title, steps_json, preconditions_json,
                    model, profile, source_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    procedure_id_str,
                    to_version,
                    version_row["title"],
                    version_row["steps_json"],
                    json.dumps(merged),
                    attribution.model,
                    attribution.profile,
                    attribution.source.model_dump_json(),
                    now,
                ),
            )
            conn.execute(
                """
                UPDATE procedures
                SET current_version = ?, updated_at = ?
                WHERE procedure_id = ?
                """,
                (to_version, now, procedure_id_str),
            )
        else:
            to_version = current
            conn.execute(
                """
                UPDATE procedures
                SET status = 'withdrawn', updated_at = ?
                WHERE procedure_id = ?
                """,
                (now, procedure_id_str),
            )

        accepted = CorrectionRecord(
            correction_id=correction_id,
            procedure_id=procedure_id_str,
            receipt_id=receipt_id_str,
            action=action,
            status="accepted",
            reason=None,
            from_version=current,
            to_version=to_version,
            preconditions=added,
            model=attribution.model,
            profile=attribution.profile,
            source=attribution.source,
            created_at=now,
        )
        _insert_correction(conn, accepted)
        _enqueue_cleanup(
            conn,
            procedure_id=procedure_id_str,
            action=action,
            created_at=now,
        )
        return accepted


__all__ = [
    "REASON_NOT_COUNTEREVIDENCE",
    "CorrectionAction",
    "CorrectionRecord",
    "CorrectionStatus",
    "correct",
]

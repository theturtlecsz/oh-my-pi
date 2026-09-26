from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid4

from omp_work.v1 import canonical

from .models import Attribution, Lesson, Precondition, SourceIdentity, StrictModel, StringId
from .policy import NativeReceipts, PolicyDecision, evaluate
from .store import LearningStore


class ProposalRecord(StrictModel):
    proposal_id: StringId
    unit_id: StringId
    status: Literal["accepted", "rejected"]
    reason: str | None = None
    procedure_id: StringId | None = None
    lesson_json: str
    model: str
    profile: str
    source_json: str
    created_at: str

    @property
    def accepted(self) -> bool:
        return self.status == "accepted"

    @property
    def lesson(self) -> Lesson:
        return Lesson.model_validate_json(self.lesson_json)

    @property
    def source(self) -> SourceIdentity:
        return SourceIdentity.model_validate_json(self.source_json)

    @property
    def attribution(self) -> Attribution:
        return Attribution(
            model=self.model,
            profile=self.profile,
            source=self.source,
        )


def normalize_preconditions(
    preconditions: tuple[Precondition, ...] | list[Precondition],
) -> list[dict[str, str]]:
    return sorted(
        [
            {"key": str(p.key), "op": str(p.op), "value": str(p.value)}
            for p in preconditions
        ],
        key=lambda x: (x["key"], x["op"], x["value"]),
    )


def normalize_steps(steps: tuple[str, ...] | list[str]) -> list[str]:
    return [s.strip() for s in steps]


def compute_lesson_fingerprint(lesson: Lesson) -> str:
    normalized = {
        "preconditions": normalize_preconditions(lesson.preconditions),
        "steps": normalize_steps(lesson.steps),
    }
    return canonical.sha256(normalized)


def create_proposal(
    store: LearningStore,
    *,
    unit_id: str | UUID,
    lesson: Lesson,
    attribution: Attribution,
    reader: NativeReceipts,
    created_at: str | datetime | None = None,
) -> ProposalRecord:
    decision: PolicyDecision = evaluate(lesson, reader)

    if created_at is None:
        now = datetime.now(timezone.utc).isoformat()
    elif isinstance(created_at, datetime):
        now = created_at.isoformat()
    else:
        now = str(created_at)

    proposal_id = str(uuid4())
    unit_id_str = str(unit_id)
    lesson_json = lesson.model_dump_json()
    source_json = attribution.source.model_dump_json()

    with store.transaction() as conn:
        if not decision.accepted:
            conn.execute(
                """
                INSERT INTO proposals (
                    proposal_id, unit_id, status, reason, procedure_id,
                    lesson_json, model, profile, source_json, created_at
                ) VALUES (?, ?, 'rejected', ?, NULL, ?, ?, ?, ?, ?)
                """,
                (
                    proposal_id,
                    unit_id_str,
                    decision.reason,
                    lesson_json,
                    attribution.model,
                    attribution.profile,
                    source_json,
                    now,
                ),
            )
            return ProposalRecord(
                proposal_id=proposal_id,
                unit_id=unit_id_str,
                status="rejected",
                reason=decision.reason,
                procedure_id=None,
                lesson_json=lesson_json,
                model=attribution.model,
                profile=attribution.profile,
                source_json=source_json,
                created_at=now,
            )

        # On accept: fingerprint = omp_work.v1.canonical.sha256 of normalized steps+preconditions.
        fingerprint = compute_lesson_fingerprint(lesson)

        existing = conn.execute(
            "SELECT procedure_id, current_version FROM procedures WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()

        if existing is None:
            # A new fingerprint makes a procedure (active, version 1, attribution copied).
            procedure_id = str(uuid4())
            conn.execute(
                """
                INSERT INTO procedures (
                    procedure_id, fingerprint, status, current_version, title, created_at, updated_at
                ) VALUES (?, ?, 'active', 1, ?, ?, ?)
                """,
                (procedure_id, fingerprint, lesson.title, now, now),
            )
            conn.execute(
                """
                INSERT INTO procedure_versions (
                    procedure_id, version, title, steps_json, preconditions_json,
                    model, profile, source_json, created_at
                ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    procedure_id,
                    lesson.title,
                    json.dumps(list(lesson.steps)),
                    json.dumps([p.model_dump(mode="json") for p in lesson.preconditions]),
                    attribution.model,
                    attribution.profile,
                    source_json,
                    now,
                ),
            )
        else:
            # A known fingerprint adds no version;
            procedure_id = existing["procedure_id"]

        # INSERT OR IGNORE procedure_support per receipt (dedup).
        receipt_ids = sorted({str(rid) for claim in lesson.claims for rid in claim.receipt_ids})
        for rid in receipt_ids:
            conn.execute(
                """
                INSERT OR IGNORE INTO procedure_support (
                    procedure_id, receipt_id, proposal_id, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (procedure_id, rid, proposal_id, now),
            )

        # Always writes a proposals row with status and reason.
        conn.execute(
            """
            INSERT INTO proposals (
                proposal_id, unit_id, status, reason, procedure_id,
                lesson_json, model, profile, source_json, created_at
            ) VALUES (?, ?, 'accepted', NULL, ?, ?, ?, ?, ?, ?)
            """,
            (
                proposal_id,
                unit_id_str,
                procedure_id,
                lesson_json,
                attribution.model,
                attribution.profile,
                source_json,
                now,
            ),
        )

        return ProposalRecord(
            proposal_id=proposal_id,
            unit_id=unit_id_str,
            status="accepted",
            reason=None,
            procedure_id=procedure_id,
            lesson_json=lesson_json,
            model=attribution.model,
            profile=attribution.profile,
            source_json=source_json,
            created_at=now,
        )


__all__ = [
    "ProposalRecord",
    "compute_lesson_fingerprint",
    "create_proposal",
    "normalize_preconditions",
    "normalize_steps",
]

"""Short-lived reads of typed intake authority and revision-bound PASS receipts."""
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator
from uuid import UUID

from psycopg import Cursor
from psycopg.rows import tuple_row

from ..contracts.nsi.v1.compat import _ontology, nsi_to_bounded
from ..intake_repository import IntakeRevisionBody, read_revision
from ..jobs.store import NativeJobStore
from ..v1.intake_provenance import evaluate_provenance
from ..v1.models import BoundedIntakeDraft

TYPED_INTAKE_MISSING = "typed_intake_missing"
INTAKE_RECEIPT_MISSING = "intake_receipt_missing"
INTAKE_RECEIPT_STALE = "intake_receipt_stale"
INTAKE_RECEIPT_FORGED = "intake_receipt_forged"
INTAKE_REVISION_ADVANCED = "intake_revision_advanced"


@dataclass(frozen=True)
class Head:
    run_id: str
    revision: int


@dataclass(frozen=True)
class Receipt:
    assessment_id: str
    revision: int


@dataclass(frozen=True)
class Refusal:
    code: str


@contextmanager
def cursor(ops: NativeJobStore, *, workspace_id: UUID, actor_id: UUID) -> Iterator[Cursor]:
    """Commit and close the read transaction before execution resumes."""
    with ops.transaction(workspace_id, actor_id) as native:
        with native.connection.cursor(row_factory=tuple_row) as cur:
            yield cur


def read_head(cur: Cursor, *, workspace_id: UUID, mission_id: UUID) -> Head | None:
    cur.execute(
        "SELECT run_id,current_revision FROM omp_work.intake_heads "
        "WHERE workspace_id=%s AND mission_id=%s", (workspace_id, mission_id),
    )
    row = cur.fetchone()
    return None if row is None else Head(*row)


def latest_pass(cur: Cursor, *, workspace_id: UUID, run_id: str, revision: int) -> Receipt | None:
    cur.execute(
        "SELECT assessment_id,assessed_revision FROM omp_work.intake_assessments "
        "WHERE workspace_id=%s AND run_id=%s AND revision=%s AND assessed_revision=%s "
        "AND full_record IS NOT NULL AND status='ready_for_ratification' "
        "ORDER BY created_at DESC,assessment_id DESC LIMIT 1",
        (workspace_id, run_id, revision, revision),
    )
    row = cur.fetchone()
    return None if row is None else Receipt(*row)


def verify(
    cur: Cursor, *, workspace_id: UUID, mission_id: UUID, revision: int, assessment_id: str,
) -> Refusal | None:
    head = read_head(cur, workspace_id=workspace_id, mission_id=mission_id)
    if head is None:
        return Refusal(TYPED_INTAKE_MISSING)
    if head.revision != revision:
        return Refusal(INTAKE_RECEIPT_STALE)
    if latest_pass(cur, workspace_id=workspace_id, run_id=head.run_id, revision=revision) is None:
        return Refusal(INTAKE_RECEIPT_MISSING)
    cur.execute(
        "SELECT full_record FROM omp_work.intake_assessments "
        "WHERE workspace_id=%s AND run_id=%s AND revision=%s AND assessed_revision=%s "
        "AND assessment_id=%s AND full_record IS NOT NULL AND status='ready_for_ratification'",
        (workspace_id, head.run_id, revision, revision, assessment_id),
    )
    row = cur.fetchone()
    if row is None:
        return Refusal(INTAKE_RECEIPT_FORGED)
    body = read_revision(cur, workspace_id=workspace_id, run_id=head.run_id, revision=revision).body
    binding = evaluate_provenance(cur, body).binding
    assert binding is not None
    stored = row[0]["binding"]
    if any(stored[name] != getattr(binding, name) for name in (
        "contract_hash", "claim_set_hash", "source_hash", "rule_bundle_hash", "schema_hash",
    )):
        return Refusal(INTAKE_RECEIPT_STALE)
    return None


def projection(cur: Cursor, body: IntakeRevisionBody) -> BoundedIntakeDraft | Refusal:
    """Project the stored snapshot without rereading mutable request data."""
    try:
        # Repository rows have canonical SQL order, not vocabulary declaration order.
        ontology = body.snapshot.ontology.model_copy(deep=True)
        vocabulary = _ontology()
        for name in ("predicates", "dimensions"):
            order = {item.id: index for index, item in enumerate(getattr(vocabulary, name))}
            getattr(ontology, name).sort(key=lambda item: order.get(item.id, len(order)))
        # NSI-2 storage omits applicability; restore that vocabulary-owned field.
        dimensions = {item.id: item for item in vocabulary.dimensions}
        ontology.dimensions = [
            item.model_copy(update={"applicability": dimensions[item.id].applicability})
            if item.id in dimensions and not item.applicability.entity_types and not item.applicability.predicate_ids
            else item
            for item in ontology.dimensions
        ]
        return nsi_to_bounded(body.snapshot.model_copy(update={"ontology": ontology}))
    except ValueError:
        return Refusal(TYPED_INTAKE_MISSING)


def execution_criteria(projection: BoundedIntakeDraft) -> tuple[str, ...]:
    return tuple(
        item.observable_outcome or item.statement
        for item in sorted(projection.acceptance_criteria, key=lambda item: item.id)
    )

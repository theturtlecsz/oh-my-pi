"""Read-only qualification evidence boundary; no O9 freeze format exists yet.

Inputs are existing service view payloads, not authority assertions. Validated
candidate evidence remains distinct from qualification protocol evidence.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from pydantic import ValidationError

from omp_work.v1.canonical import canonical_json, sha256
from omp_work.v1.models import CompletionEvidence, EvidenceReceipt, ResearchObservation

Gate = Literal["20-consecutive-accepted", "72h/30-distinct-task", "within-run/100-execution-attempt"]
Outcome = Literal["accepted", "failed", "blocked", "cancelled"]


@dataclass(frozen=True)
class ScopeIdentity:
    release: str
    configuration: str
    recipe: str
    evaluator: str
    workload: str
    criteria: str


@dataclass(frozen=True)
class DeclaredExperiment:
    experiment_id: str
    trial_id: str
    task_id: str
    proof_record_ids: tuple[str, ...]


@dataclass(frozen=True)
class QualifiedBackend:
    backend_id: str
    experiment_ids: tuple[str, ...]
    proof_record_ids: tuple[str, ...]


@dataclass(frozen=True)
class PreregisteredStratum:
    dimension: str
    value: str
    allocation: int
    proof_record_ids: tuple[str, ...]


@dataclass(frozen=True)
class FrozenProtocol:
    """Internal projection only; never inferred from a research protocol hash."""
    proof_record_ids: tuple[str, ...]
    protocol_sha256: str
    scope: ScopeIdentity
    release_baseline_sha256: str
    task_order: tuple[str, ...]
    run_start: datetime
    run_end: datetime
    allocations: tuple[tuple[str, int], ...]
    qualified_backend_proof_ids: tuple[str, ...]
    experiments: tuple[DeclaredExperiment, ...] = ()
    backends: tuple[QualifiedBackend, ...] = ()
    strata: tuple[PreregisteredStratum, ...] = ()


@dataclass(frozen=True)
class ResourceUsage:
    """Keep measurement basis and units separate; None means unknown, not zero."""
    name: str
    basis: str
    unit: str
    amount: float | None
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class StratumEvidence:
    dimension: str
    value: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class ExecutionAttempt:
    """Trusted read projection of linked trial/job/start evidence, not job claims.

    A future adapter must verify these bindings before constructing this type.
    Existing observation payloads cannot populate it merely by naming these keys.
    Resources/outcomes remain attached even when counting excludes the candidate.
    """
    record_id: str
    protocol_sha256: str
    scope: ScopeIdentity
    recorded_at: datetime
    execution_attempt_id: str | None
    experiment_id: str | None
    trial_id: str | None
    task_id: str | None
    job_id: str | None
    backend_id: str | None
    trial_evidence_id: str | None
    job_evidence_id: str | None
    start_evidence_id: str | None
    started_at: datetime | None
    kind: str
    execution_outcome: str
    scientific_outcome: str | None = None
    retry_of: str | None = None
    category: str | None = None
    resources: tuple[ResourceUsage, ...] = ()
    strata: tuple[StratumEvidence, ...] = ()
    replacement_for: str | None = None


@dataclass(frozen=True)
class ScopeEvent:
    record_id: str
    protocol_sha256: str
    observed_at: datetime
    scope: ScopeIdentity


@dataclass(frozen=True)
class Observation:
    record_id: str
    payload_sha256: str
    payload_json: str
    observed_at: datetime
    source_ref: str
    trial_id: str | None
    execution_status: str


@dataclass(frozen=True)
class Receipt:
    record_id: str
    payload_sha256: str
    payload_json: str


@dataclass(frozen=True)
class Exclusion:
    record_id: str
    reason: str


@dataclass(frozen=True)
class Snapshot:
    receipts: tuple[Receipt, ...]
    observations: tuple[Observation, ...]
    acceptance_record_ids: tuple[str, ...]
    exclusions: tuple[Exclusion, ...]
    protocol: FrozenProtocol | None = None
    attempts: tuple[ExecutionAttempt, ...] = ()
    scope_events: tuple[ScopeEvent, ...] = ()


@dataclass(frozen=True)
class Report:
    gate: Gate
    status: Literal["not started", "available"]
    count: int | None
    contributing_record_ids: tuple[str, ...]
    exclusions: tuple[Exclusion, ...]
    strata_status: Literal["not preregistered", "preregistered"]
    allocations: tuple[tuple[str, int], ...]
    missing_proof: tuple[str, ...]


def scope_boundary(expected: ScopeIdentity, actual: ScopeIdentity) -> tuple[str, ...]:
    """Shared exact-match boundary for streak, run, and shared-evidence readers."""
    return tuple(
        name for name in ("release", "configuration", "recipe", "evaluator", "workload", "criteria")
        if getattr(expected, name) != getattr(actual, name)
    )


def frozen_run_boundary(
    protocol: FrozenProtocol, protocol_sha256: str, at: datetime, scope: ScopeIdentity,
) -> str | None:
    """One run only; matching scope never moves a record from another freeze."""
    if protocol_sha256 != protocol.protocol_sha256:
        return "different frozen run"
    if not protocol.run_start <= at < protocol.run_end:
        return "outside frozen 72h run"
    drift = scope_boundary(protocol.scope, scope)
    if drift:
        return "frozen scope drift: " + ", ".join(drift)
    return None


def frozen_run_missing_proof(protocol: FrozenProtocol) -> tuple[str, ...]:
    missing = []
    if not protocol.proof_record_ids or not protocol.protocol_sha256:
        missing.append("acceptance-bound O9 freeze")
    if not protocol.release_baseline_sha256 or not protocol.scope.release:
        missing.append("release baseline")
    if not all(getattr(protocol.scope, name) for name in (
        "configuration", "recipe", "evaluator", "workload", "criteria",
    )):
        missing.append("frozen scope identities")
    if protocol.run_end - protocol.run_start != timedelta(hours=72):
        missing.append("frozen 72h interval")
    return tuple(missing)


def decode_evidence(
    *,
    receipts: Sequence[Mapping[str, object]] = (),
    observations: Sequence[Mapping[str, object]] = (),
    completions: Sequence[tuple[str, Mapping[str, object]]] = (),
) -> Snapshot:
    """Decode existing payloads once, retaining no mutable input references.

    ``completions`` must come from committed complete_work evidence, not an
    unexecuted request. Links bind subject, kind, payload and artifact hashes.
    Even sound completion evidence proves no O9 freeze or qualified backend.
    """
    excluded: list[Exclusion] = []
    by_id: dict[str, EvidenceReceipt] = {}
    decoded_receipts: list[Receipt] = []
    receipt_counts = Counter(str(row.get("receipt_id")) for row in receipts)
    for row in receipts:
        record_id = str(row.get("receipt_id", "<missing>"))
        try:
            receipt = EvidenceReceipt.model_validate(row)
            if receipt_counts[record_id] > 1:
                raise ValueError("duplicate receipt id")
            if sha256(receipt.payload) != receipt.payload_sha256:
                raise ValueError("payload hash mismatch")
        except (ValidationError, ValueError) as error:
            excluded.append(Exclusion(record_id, f"invalid receipt: {error}"))
            continue
        by_id[record_id] = receipt
        decoded_receipts.append(Receipt(record_id, receipt.payload_sha256, canonical_json(receipt.payload)))

    acceptance_ids: list[str] = []
    for record_id, payload in completions:
        try:
            evidence = CompletionEvidence.model_validate(payload)
            subject = evidence.subject
            for ref in evidence.artifacts:
                linked = by_id.get(str(ref.receipt_id))
                if linked is None:
                    raise ValueError("dangling receipt link")
                if (
                    linked.kind.value != ref.kind
                    or linked.payload_sha256 != ref.payload_sha256
                    or linked.artifact_sha256 != ref.artifact_sha256
                    or linked.work_id != subject.work_id
                    or linked.revision_id != subject.revision_id
                    or linked.candidate_id != subject.candidate_id
                    or linked.candidate_sha256 != subject.candidate_sha256
                    or linked.candidate_commit != subject.candidate_commit
                ):
                    raise ValueError("receipt binding mismatch")
            if (
                evidence.delivery.candidate_commit != subject.candidate_commit
                or evidence.delivery.remote_commit != subject.candidate_commit
            ):
                raise ValueError("delivery binding mismatch")
        except (ValidationError, ValueError) as error:
            excluded.append(Exclusion(record_id, f"invalid acceptance evidence: {error}"))
            continue
        acceptance_ids.append(record_id)

    decoded_observations: list[Observation] = []
    observation_counts = Counter(str(row.get("observation_id")) for row in observations)
    for row in observations:
        record_id = str(row.get("observation_id", "<missing>"))
        try:
            observation = ResearchObservation.model_validate(row)
            if observation_counts[record_id] > 1:
                raise ValueError("duplicate observation id")
            if sha256(observation.payload) != observation.payload_sha256:
                raise ValueError("payload hash mismatch")
        except (ValidationError, ValueError) as error:
            excluded.append(Exclusion(record_id, f"invalid observation: {error}"))
            continue
        decoded_observations.append(Observation(
            record_id, observation.payload_sha256, canonical_json(observation.payload),
            observation.observed_at, observation.source_ref,
            str(observation.trial_id) if observation.trial_id else None,
            observation.execution_status,
        ))
    # Existing payload schemas have no acceptance-bound O9 protocol projection.
    return Snapshot(tuple(decoded_receipts), tuple(decoded_observations), tuple(acceptance_ids), tuple(excluded))


def unavailable_report(snapshot: Snapshot, gate: Gate) -> Report:
    """Current read-side result: do not substitute present installation metadata."""
    return Report(
        gate, "not started", None, (), snapshot.exclusions + tuple(
            Exclusion(record.record_id, "O9 freeze and release baseline unavailable")
            for record in (*snapshot.receipts, *snapshot.observations, *snapshot.attempts, *snapshot.scope_events)
        ) + tuple(
            Exclusion(record_id, "O9 freeze and release baseline unavailable")
            for record_id in snapshot.acceptance_record_ids
        ),
        "not preregistered", (),
        ("acceptance-bound O9 freeze", "release baseline", "preregistered strata", "qualified-backend proof"),
    )

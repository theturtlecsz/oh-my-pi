"""Recursive transfer evidence models, consumption history, and trial ingestion (OMP-319-s03-s02).

Provides NativeEvidence and ConsumptionHistory for tracking consumed holdouts and trial
settlements across transfer evaluation, and read_trial for verifying and ingesting
sealed managed trials from database custody.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg

from omp_work.operations.artifacts import read_verified_bytes
from omp_work.research.custody import artifact_path
from omp_work.research.managed import (
    managed_trials,
    verify_managed_trial,
)
from omp_work.research.receipt import (
    EvaluationProtocol,
    TrialReceipt,
    load_protocol,
    managed_job_id,
)

__all__ = [
    "ConfirmationIdentitiesView",
    "ConsumedView",
    "ConsumptionHistory",
    "ManagedTrialEvidence",
    "NativeEvidence",
    "TrialEvidence",
    "TrialRefused",
    "read_trial",
]

_SENTINEL = object()


def _normalize_identity(identity: object) -> object:
    if isinstance(identity, UUID):
        return str(identity)
    if isinstance(identity, tuple):
        return tuple(_normalize_identity(x) for x in identity)
    return identity


def _serialize_item(item: Any) -> Any:
    if isinstance(item, tuple):
        return ["__tuple__", *[_serialize_item(x) for x in item]]
    if isinstance(item, UUID):
        return str(item)
    return item


def _deserialize_item(item: Any) -> Any:
    if isinstance(item, list) and item and item[0] == "__tuple__":
        return tuple(_deserialize_item(x) for x in item[1:])
    return item


class ConsumedView:
    """View over consumed identities with callable and collection semantics."""

    def __init__(self, history: ConsumptionHistory) -> None:
        self._history = history

    def __call__(self, identity: object = _SENTINEL) -> bool | frozenset[Any]:
        if identity is _SENTINEL:
            return frozenset(self._history._consumed)
        return _normalize_identity(identity) in self._history._consumed

    def __contains__(self, identity: object) -> bool:
        return _normalize_identity(identity) in self._history._consumed

    def __iter__(self) -> Iterator[Any]:
        return iter(self._history._consumed)

    def __len__(self) -> int:
        return len(self._history._consumed)

    def __bool__(self) -> bool:
        return bool(self._history._consumed)

    def __repr__(self) -> str:
        return f"ConsumedView({self._history._consumed!r})"


class ConfirmationIdentitiesView:
    """View over confirmation identities with callable and collection semantics."""

    def __init__(self, history: ConsumptionHistory) -> None:
        self._history = history

    def __call__(self) -> frozenset[Any]:
        return frozenset(self._history._confirmation_identities)

    def __contains__(self, identity: object) -> bool:
        return _normalize_identity(identity) in self._history._confirmation_identities

    def __iter__(self) -> Iterator[Any]:
        return iter(self._history._confirmation_identities)

    def __len__(self) -> int:
        return len(self._history._confirmation_identities)

    def __bool__(self) -> bool:
        return bool(self._history._confirmation_identities)

    def __repr__(self) -> str:
        return f"ConfirmationIdentitiesView({self._history._confirmation_identities!r})"


class _RestoreCallable:
    def __get__(self, instance: Any, owner: Any = None) -> Any:
        if instance is None:
            def _cls_restore(snapshot: Any) -> ConsumptionHistory:
                inst = owner()
                inst._restore_dict(snapshot)
                return inst
            return _cls_restore

        def _inst_restore(snapshot: Any) -> ConsumptionHistory:
            instance._restore_dict(snapshot)
            return instance

        return _inst_restore


class ConsumptionHistory:
    """Tracks consumption of tasks, trials, jobs, and confirmation holdouts."""

    restore = _RestoreCallable()

    def __init__(self) -> None:
        self._attempts: set[Any] = set()
        self._successes: set[Any] = set()
        self._failures: set[Any] = set()
        self._duplicates: set[Any] = set()
        self._consumed: set[Any] = set()
        self._confirmation_identities: set[Any] = set()

        self.consumed = ConsumedView(self)
        self.confirmation_identities = ConfirmationIdentitiesView(self)

    def record_attempt(self, identity: object) -> None:
        key = _normalize_identity(identity)
        self._attempts.add(key)
        self._consumed.add(key)

    def record_success(self, identity: object) -> None:
        key = _normalize_identity(identity)
        self._successes.add(key)
        self._consumed.add(key)

    def record_failure(self, identity: object) -> None:
        key = _normalize_identity(identity)
        self._failures.add(key)
        self._consumed.add(key)

    def record_duplicate(self, identity: object) -> None:
        key = _normalize_identity(identity)
        self._duplicates.add(key)

    def note_confirmation(self, identity: object) -> None:
        key = _normalize_identity(identity)
        self._confirmation_identities.add(key)
        self._consumed.add(key)

    def snapshot(self) -> dict[str, Any]:
        return {
            "attempts": sorted([_serialize_item(x) for x in self._attempts], key=str),
            "successes": sorted([_serialize_item(x) for x in self._successes], key=str),
            "failures": sorted([_serialize_item(x) for x in self._failures], key=str),
            "duplicates": sorted([_serialize_item(x) for x in self._duplicates], key=str),
            "consumed": sorted([_serialize_item(x) for x in self._consumed], key=str),
            "confirmation_identities": sorted(
                [_serialize_item(x) for x in self._confirmation_identities], key=str
            ),
        }

    @classmethod
    def from_snapshot(cls, snapshot: Any) -> ConsumptionHistory:
        inst = cls()
        inst._restore_dict(snapshot)
        return inst

    def _restore_dict(self, data: dict[str, Any]) -> None:
        self._attempts = {_deserialize_item(x) for x in data.get("attempts", [])}
        self._successes = {_deserialize_item(x) for x in data.get("successes", [])}
        self._failures = {_deserialize_item(x) for x in data.get("failures", [])}
        self._duplicates = {_deserialize_item(x) for x in data.get("duplicates", [])}
        self._consumed = {_deserialize_item(x) for x in data.get("consumed", [])}
        self._confirmation_identities = {
            _deserialize_item(x) for x in data.get("confirmation_identities", [])
        }


class NativeEvidence:
    """Connection, scope, and authority context for native research evidence."""

    def __init__(
        self,
        store: Any,
        workspace_id: UUID | str,
        actor_id: UUID | str,
        work_id: UUID | str,
        data_dir: Path | str,
        *,
        grant: Any = None,
        policy: Any = None,
        authority: Any = None,
        action: Any = None,
        repositories: Any = None,
        operation_id: UUID | str | None = None,
        request_id: UUID | str | None = None,
        history: ConsumptionHistory | None = None,
    ) -> None:
        self.store = store
        self.workspace_id = UUID(str(workspace_id))
        self.actor_id = UUID(str(actor_id))
        self.work_id = UUID(str(work_id))
        self.data_dir = Path(str(data_dir))
        self.grant = grant
        self.policy = policy
        self.authority = authority
        self.action = action
        self.repositories = repositories
        self.operation_id = str(operation_id) if operation_id is not None else None
        self.request_id = str(request_id) if request_id is not None else None
        self.history = history if history is not None else ConsumptionHistory()

    def snapshot(self) -> dict[str, Any]:
        return self.history.snapshot()

    def restore(self, snapshot: Any) -> None:
        self.history.restore(snapshot)


class TrialRefused(ValueError):
    """Raised when reading a trial is refused by verification or evidence policies."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class TrialEvidence:
    """Ingested trial evidence, verified receipt, protocol, and resource envelope."""

    receipt: TrialReceipt
    protocol: EvaluationProtocol
    cpu: int
    memory_mib: int
    gpu: int
    model_calls: int
    reasons: tuple[str, ...]
    trial_id: str
    job_id: str
    campaign_id: str
    work_id: str
    candidate_id: str
    receipt_id: str
    output_id: str | None
    qualified: bool
    candidate_digest: str = ""
    receipt_sha256: str = ""
    output_sha256: str | None = None
    resources: dict[str, int] = field(default_factory=dict)
    metric: float | None = None
    metrics: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.candidate_digest:
            object.__setattr__(self, "candidate_digest", self.candidate_id)
        if not self.receipt_sha256:
            object.__setattr__(self, "receipt_sha256", self.receipt_id)
        if self.output_sha256 is None:
            object.__setattr__(self, "output_sha256", self.output_id)
        if not self.resources:
            object.__setattr__(
                self,
                "resources",
                {
                    "cpu": self.cpu,
                    "memory_mib": self.memory_mib,
                    "gpu": self.gpu,
                    "model_calls": self.model_calls,
                },
            )
        if self.metric is None and self.protocol and self.receipt:
            object.__setattr__(
                self, "metric", self.receipt.metrics.get(self.protocol.primary)
            )
        if not self.metrics and self.receipt:
            object.__setattr__(self, "metrics", dict(self.receipt.metrics))

    def __getitem__(self, key: str) -> Any:
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key) from None


ManagedTrialEvidence = TrialEvidence


def _custody_bytes(
    cur: psycopg.Cursor[dict[str, object]],
    workspace_id: UUID,
    digest: object,
    data_dir: Path,
) -> bytes | None:
    if not isinstance(digest, str) or not digest:
        return None
    cur.execute(
        "SELECT manifest FROM omp_research.artifacts WHERE workspace_id=%s AND artifact_sha256=%s",
        (workspace_id, digest),
    )
    row = cur.fetchone()
    if row is None:
        return None
    manifest = row.get("manifest")
    if isinstance(manifest, str):
        try:
            manifest = json.loads(manifest)
        except Exception:
            return None
    if not isinstance(manifest, dict):
        return None
    size = manifest.get("size_bytes")
    if isinstance(size, bool) or not isinstance(size, int):
        return None
    try:
        return read_verified_bytes(
            artifact_path(data_dir, workspace_id, digest), digest, size
        )
    except (RuntimeError, OSError):
        return None


def read_trial(native: NativeEvidence, trial_id: object) -> TrialEvidence:
    """Read and verify a managed research trial from native database custody.

    Uses managed_trials and verify_managed_trial within a store transaction.
    Refuses absence from managed_trials, nonempty reasons, candidate_authored
    evidence, and accepted negative controls. A failed settlement stays
    unqualified without refusing the read.
    """
    if hasattr(trial_id, "trial_id"):
        trial_id = trial_id.trial_id
    trial_uuid = UUID(str(trial_id)) if not isinstance(trial_id, UUID) else trial_id

    with native.store.transaction(native.workspace_id, native.actor_id) as cur:
        # 1. Check membership in managed_trials for this work
        all_managed = managed_trials(
            cur, native.workspace_id, native.work_id, data_dir=native.data_dir
        )
        found_trial: dict[str, Any] | None = None
        for item_trial, _, _ in all_managed:
            if str(item_trial.get("trial_id")) == str(trial_uuid):
                found_trial = item_trial
                break
        if found_trial is None:
            raise TrialRefused("missing_managed_trial")

        # 2. Verify managed trial
        receipt, reasons = verify_managed_trial(
            cur, native.workspace_id, trial_uuid, data_dir=native.data_dir
        )

        # 3. Refuse candidate_authored observations
        job_id = managed_job_id(trial_uuid)
        cur.execute(
            """
            SELECT issuer_kind FROM omp_research.observations
            WHERE workspace_id=%s AND (trial_id=%s OR source_ref=%s)
            """,
            (native.workspace_id, trial_uuid, f"job:{job_id}"),
        )
        for obs_row in cur.fetchall():
            if obs_row.get("issuer_kind") == "candidate_authored":
                raise TrialRefused("candidate_authored")

        # 4. Refuse candidate_authored receipt artifact manifest
        if receipt is not None:
            receipt_bytes = receipt.receipt_bytes()
            receipt_digest = hashlib.sha256(receipt_bytes).hexdigest()
            cur.execute(
                "SELECT manifest FROM omp_research.artifacts WHERE workspace_id=%s AND artifact_sha256=%s",
                (native.workspace_id, receipt_digest),
            )
            art_row = cur.fetchone()
            if art_row is not None:
                manifest_val = art_row.get("manifest")
                if isinstance(manifest_val, str):
                    try:
                        manifest_val = json.loads(manifest_val)
                    except Exception:
                        manifest_val = None
                if isinstance(manifest_val, dict) and manifest_val.get("issuer_kind") == "candidate_authored":
                    raise TrialRefused("candidate_authored")

        # 5. Refuse negative_control accepted
        if receipt is not None and receipt.negative_control == "accepted":
            raise TrialRefused("negative_control_accepted")

        # 6. Refuse nonempty verification reasons or missing receipt
        if reasons:
            raise TrialRefused(reasons[0])
        if receipt is None:
            raise TrialRefused("missing_managed_receipt")

        # 7. Check job settlement status and resources
        cur.execute(
            "SELECT status, settlement, resources FROM omp_jobs.jobs WHERE workspace_id=%s AND job_id=%s AND source='native'",
            (native.workspace_id, job_id),
        )
        job_row = cur.fetchone()
        if job_row is None:
            raise TrialRefused("missing_managed_receipt")

        job_status = str(job_row.get("status"))
        settlement = job_row.get("settlement")
        if isinstance(settlement, str):
            try:
                settlement = json.loads(settlement)
            except Exception:
                settlement = None
        outcome = settlement.get("outcome") if isinstance(settlement, dict) else None

        is_sealed = (job_status == "sealed")
        is_succeeded = (outcome == "succeeded")
        is_qualified_receipt = (receipt.verdict == "qualified")
        qualified = bool(is_sealed and is_succeeded and is_qualified_receipt)

        trial_reasons: tuple[str, ...]
        if qualified:
            trial_reasons = ()
        else:
            verdict_str = receipt.verdict if receipt.verdict != "qualified" else "unqualified"
            trial_reasons = (verdict_str,)

        # 8. Extract resource axes
        job_res = job_row.get("resources")
        if isinstance(job_res, str):
            try:
                job_res = json.loads(job_res)
            except Exception:
                job_res = None
        if not isinstance(job_res, dict):
            trial_res = found_trial.get("resource_request")
            if isinstance(trial_res, str):
                try:
                    trial_res = json.loads(trial_res)
                except Exception:
                    trial_res = None
            job_res = trial_res if isinstance(trial_res, dict) else {}

        cpu = int(job_res.get("cpu", 0))
        memory_mib = int(job_res.get("memory_mib", 0))
        gpu = int(job_res.get("gpu", 0))
        model_calls = int(job_res.get("model_calls", 0))

        # 9. Load evaluation protocol
        spec = found_trial.get("spec")
        if isinstance(spec, str):
            try:
                spec = json.loads(spec)
            except Exception:
                spec = None
        spec = spec if isinstance(spec, dict) else {}
        protocol_sha256 = spec.get("evaluation_protocol_sha256")
        if not protocol_sha256:
            raise TrialRefused("missing_protocol")

        proto_bytes = _custody_bytes(cur, native.workspace_id, protocol_sha256, native.data_dir)
        if proto_bytes is None:
            raise TrialRefused("missing_protocol")
        try:
            protocol = load_protocol(proto_bytes, expected_sha256=str(protocol_sha256))
        except ValueError:
            raise TrialRefused("missing_protocol")

        candidate_id = str(found_trial["candidate_digest"])
        receipt_bytes = receipt.receipt_bytes()
        receipt_id = hashlib.sha256(receipt_bytes).hexdigest()
        output_id = receipt.output_sha256

        return TrialEvidence(
            receipt=receipt,
            protocol=protocol,
            cpu=cpu,
            memory_mib=memory_mib,
            gpu=gpu,
            model_calls=model_calls,
            reasons=trial_reasons,
            trial_id=str(trial_uuid),
            job_id=job_id,
            campaign_id=str(found_trial["campaign_id"]),
            work_id=str(native.work_id),
            candidate_id=candidate_id,
            receipt_id=receipt_id,
            output_id=output_id,
            qualified=qualified,
        )

"""Opt-in generated bindings for the sequential research campaign runner.

Selection runs in the strategy jail. Experiments use existing mission admission,
managed workers and custody verification; this adapter issues no authority.
"""

from __future__ import annotations

import hashlib
import math
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator
from uuid import UUID

from omp_work.jobs.campaign_completion import TrialCompletion
from omp_work.jobs.campaign_decisions import GeneratedDecision
from omp_work.jobs.campaign_definition import CampaignDefinition, Criterion, PreparedTrial, SearchConfig
from omp_work.jobs.campaign_setup import ManagedComponents, register_campaign_artifact
from omp_work.jobs.managed_trial import CAPABILITY, ManagedTrialHandler, dispatch_managed_trial
from omp_work.jobs.store import NativeJobStore
from omp_work.jobs.worker import JobWorker
from omp_work.research import mechanism, mechanism_tasks, strategy_execution
from omp_work.research.managed import verify_managed_trial
from omp_work.research.receipt import managed_job_id
from omp_work.v1.canonical import canonical_json, sha256
from omp_work.v1.client import WorkClient
from omp_work.v1.models import ResearchDomain, ResearchTrial


@dataclass(frozen=True, slots=True)
class GeneratedCampaign:
    """Frozen inputs and cumulative caps. Caller supplies mission/standing authority.

    ``source`` may be a stored mechanism.py generation result; absent source is
    generated from the canonical recipe. No generated mode is enabled by default.
    """

    task: mechanism_tasks.MechanismTask
    recipe: dict[str, Any]
    data_dir: Path
    source: bytes | None = None
    node_budget: float = mechanism_tasks.BUDGET
    selection_seconds: float = 30
    selection_timeout: float = 5
    selection_attempts: int = 8
    width: int = mechanism_tasks.WIDTH

    def __post_init__(self) -> None:
        recipe = mechanism.canonical_recipe(self.recipe)
        object.__setattr__(self, "recipe", recipe)
        if self.source is None:
            object.__setattr__(self, "source", mechanism.generation_job({}, recipe).encode())
        if not isinstance(self.source, bytes):
            raise TypeError("source must be bytes")
        for limit in (self.node_budget, self.selection_seconds, self.selection_timeout):
            if isinstance(limit, bool) or not math.isfinite(limit) or limit < 0:
                raise ValueError("campaign caps must be finite and nonnegative")
        if type(self.width) is not int or self.width <= 0:
            raise ValueError("width must be positive")
        if type(self.selection_attempts) is not int or self.selection_attempts < 0:
            raise ValueError("selection_attempts must be nonnegative")

    @property
    def definition(self) -> CampaignDefinition:
        task = self.task
        candidates = dict(task.candidate_tars)
        metric = "score" if task.domain == "simulation" else "ratio"
        return CampaignDefinition(
            objective=f"Evaluate generated mechanism on {task.task_id}",
            domain=ResearchDomain(task.domain),
            timeout_seconds=60,
            criteria=(Criterion(metric, "gte", 0),),
            trials=tuple(PreparedTrial(
                Path(node.id), candidates[node.id], hashlib.sha256(candidates[node.id]).hexdigest(),
                node.id, node.parent, node.cost,
            ) for node in task.nodes),
            search=SearchConfig("fixed", self.node_budget, self.width, task.seed, metric),
        )

    @property
    def components(self) -> ManagedComponents:
        source = self.source
        if source is None:
            raise RuntimeError("generated campaign source missing")
        recipe = canonical_json(self.recipe).encode()
        commitment_payload = {
            "version": 2, "source": hashlib.sha256(source).hexdigest(),
            "recipe": sha256(self.recipe), "task": self.task.manifest_sha256,
            "protocol": hashlib.sha256(self.task.protocol_bytes).hexdigest(),
            "definition": asdict(self.definition.search),
            "selection_seconds": self.selection_seconds,
            "selection_timeout": self.selection_timeout,
            "selection_attempts": self.selection_attempts,
        }
        commitment = sha256(commitment_payload)
        return ManagedComponents(
            commitment, self.task.protocol_bytes, self.task.evaluator_script,
            (canonical_json(commitment_payload).encode(), source, recipe,
             self.task.manifest_bytes, self.task.protocol_bytes,
             self.task.evaluator_script, self.task.sealed, *dict(self.task.candidate_tars).values()),
        )


@contextmanager
def watch_state(gate: Callable[[], object], cancel: threading.Event) -> Iterator[None]:
    """Propagate live campaign cancellation/holds to both jail and native worker."""
    done = threading.Event()
    errors: list[Exception] = []

    def watch() -> None:
        while not done.wait(0.05):
            try:
                gate()
            except Exception as exc:
                errors.append(exc)
                cancel.set()
                return

    gate()
    thread = threading.Thread(target=watch)
    thread.start()
    try:
        yield
    finally:
        done.set()
        thread.join()
    if errors:
        raise errors[0]
    gate()


@dataclass
class GeneratedRun:
    config: GeneratedCampaign
    attempts: list[dict[str, object]] = field(default_factory=list)
    node_attempts: list[dict[str, object]] = field(default_factory=list)
    observations: list[dict[str, object]] = field(default_factory=list)
    failure: str | None = None

    capability = CAPABILITY
    job_id = staticmethod(managed_job_id)
    dispatch = staticmethod(dispatch_managed_trial)

    def handler(self, directory: Path, client: WorkClient) -> ManagedTrialHandler:
        return ManagedTrialHandler(content_dir=directory, execute=client.execute)

    def gate(self) -> None:
        if math.fsum(float(a["duration"]) for a in self.attempts) > self.config.selection_seconds:
            raise RuntimeError("generated selection cumulative budget exceeded")

    def select(
        self, client: WorkClient, workspace_id: UUID, campaign_id: UUID,
        steps: dict[str, Any], opened: set[str], search: SearchConfig,
        gate: Callable[[], object],
    ) -> str | None:
        remaining = self.config.selection_seconds - math.fsum(float(a["duration"]) for a in self.attempts)
        if remaining <= 0 or len(self.attempts) >= self.config.selection_attempts:
            self.failure = "selection_budget_exhausted"
            return None
        cancel = threading.Event()
        if self.config.source is None:
            raise RuntimeError("generated campaign source missing")
        with watch_state(gate, cancel):
            result = strategy_execution.execute_strategy(
                self.config.source, steps, sorted(opened), seed=search.seed, width=search.width,
                allowed_actions=("open",), resource_caps={},
                timeout=min(remaining, self.config.selection_timeout), cancel=cancel,
            )
            failure = result.failure
            if result.selection is not None and result.selection["step_id"] in opened:
                failure = "node_already_open"
            if result.usage.duration > remaining:
                failure = "selection_budget_exceeded"
            self.failure = failure
            attempt = {
                "campaign_id": str(campaign_id), "index": len(self.attempts),
                "source_sha256": hashlib.sha256(self.config.source).hexdigest(),
                "recipe_sha256": sha256(self.config.recipe),
                "observations_sha256": sha256(self.observations),
                "selection": result.selection, "failure": failure,
                "reasons": list(result.reasons), **asdict(result.usage),
            }
            self.attempts.append(attempt)
            register_campaign_artifact(
                client, workspace_id, canonical_json(attempt).encode(),
                source_ref=f"generated-selection:{campaign_id}:{attempt['index']}",
            )
        if self.failure is not None or result.selection is None:
            return None
        return result.selection["step_id"]

    def decision(self, index: int, pick: str, rationale: str) -> GeneratedDecision:
        assert self.config.source is not None
        return GeneratedDecision(
            index, pick, index, sha256(self.observations), rationale,
            hashlib.sha256(self.config.source).hexdigest(), sha256(self.config.recipe),
        )

    def tick(
        self, worker: JobWorker, job_id: str, gate: Callable[[], object],
        client: WorkClient, workspace_id: UUID,
    ) -> str | None:
        started = time.monotonic()
        attempt: dict[str, object] = {"job_id": job_id, "status": "failed"}
        try:
            with watch_state(gate, worker.cancel_requested):
                result = worker.tick(job_id=job_id)
                attempt["status"] = "settled" if result == job_id else "unsettled"
                return result
        finally:
            if worker.cancel_requested.is_set():
                attempt["status"] = "cancelled"
            attempt["duration"] = time.monotonic() - started
            self.node_attempts.append(attempt)
            register_campaign_artifact(
                client, workspace_id, canonical_json(attempt).encode(),
                source_ref=f"generated-node:{job_id}",
            )

    def read(
        self, client: WorkClient, store: NativeJobStore, key: str,
        campaign_id: UUID, trial: ResearchTrial, actor_id: UUID, metric: str,
    ) -> TrialCompletion:
        if trial.campaign_id != campaign_id:
            raise RuntimeError("wrong generated campaign")
        with store.transaction(trial.workspace_id, actor_id) as cur:
            receipt, reasons = verify_managed_trial(
                cur, trial.workspace_id, trial.trial_id, data_dir=self.config.data_dir,
            )
        if receipt is None or reasons:
            raise RuntimeError(f"unverified managed receipt: {reasons}")
        record = {
            "trial_id": str(trial.trial_id), "job_id": receipt.job_id,
            "receipt_sha256": hashlib.sha256(receipt.receipt_bytes()).hexdigest(),
        }
        if record not in self.observations:
            self.observations.append(record)
        value = receipt.metrics.get(metric)
        if receipt.verdict != "qualified" or value is None:
            return TrialCompletion("crashed", dict(receipt.metrics), None, receipt.failure_reason or "missing metric")
        return TrialCompletion("completed", dict(receipt.metrics), value)

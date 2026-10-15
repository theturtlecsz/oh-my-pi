"""Bounded R13 recipe execution and a controller-owned PolicyFn adapter.

method_sha256 pins this method implementation; lower_researcher_sha256
pins the deterministic mechanism and recipe modules. The sealed study recipe
binds the parameters independently of both implementation identities. Source uses a raw byte
hash, while artifact_sha256 uses the existing package_candidate manifest.
Only development evidence enters generation. Receipt verification, admission,
accounting and failure records stay outside generated code.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from time import perf_counter

from omp_work.research import mechanism, mechanism_recipe, recursive, strategy_execution, strategy_protocol
from omp_work.research.accounting import ResourceAccounting
from omp_work.research.harness_release import package_candidate
from omp_work.research.receipt import (
    EvaluationProtocol, ReceiptExpectation, TrialReceipt, verify_receipt,
)
from omp_work.research.search import Steps
from omp_work.standing_change import ChangeAuthority
from omp_work.standing_policy import ActionRequest, RepositoryRecord, StandingPolicy
from omp_work.v1 import canonical


SOURCE_PATH = "research/strategies/recursive.py"
_EXECUTION_CANCEL: ContextVar[threading.Event | None] = ContextVar("recursive_cancel", default=None)


@contextmanager
def cancellation_scope(cancel: threading.Event | None):
    """Propagate controller cancellation through composed transfer execution."""
    token = _EXECUTION_CANCEL.set(cancel)
    try:
        yield
    finally:
        _EXECUTION_CANCEL.reset(token)



def method_sha256() -> str:
    """Identity of this method implementation, independent of recipe/output."""
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def lower_researcher_sha256() -> str:
    """Identity of the deterministic generator implementation, not its output."""
    return hashlib.sha256(
        Path(mechanism.__file__).read_bytes() + Path(mechanism_recipe.__file__).read_bytes()
    ).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class MethodContext:
    """Live controller inputs, never serialized into a recipe or candidate."""

    state: recursive.RecursionState
    grant: recursive.CallerGrant
    policy: StandingPolicy
    authority: ChangeAuthority
    action: ActionRequest
    repositories: Mapping[str, RepositoryRecord] | Sequence[RepositoryRecord]
    store: object | None
    workspace_id: object
    actor_id: object
    work_id: object
    operation_id: str
    request_id: str
    job_id: str | None = None
    now: Callable[[], datetime] = _now
    cancel: threading.Event | None = None


@dataclass(frozen=True)
class MethodResult:
    recipe_bytes: bytes
    spec_bytes: bytes
    source_bytes: bytes | None
    recipe_sha256: str
    source_sha256: str | None
    artifact_sha256: str | None
    failures: tuple[str, ...]
    state: recursive.RecursionState
    denominator: int = 1


class _Execution:
    def __init__(self, study: recursive.RecursiveStudy, context: MethodContext) -> None:
        self.study = study
        self.context = context
        self.state = context.state
        self.accounting = ResourceAccounting(
            study.limits, context.store, context.workspace_id, context.actor_id,
        )

    def gate(self) -> str | None:
        ctx = self.context
        cancel = ctx.cancel or _EXECUTION_CANCEL.get()
        if cancel is not None and cancel.is_set():
            return "canceled"
        now = ctx.now()
        reason = self.state.terminal_reason or recursive._admission_gate(
            self.state, ctx.grant, ctx.policy, ctx.authority, ctx.action, ctx.repositories, now,
        )
        if reason:
            return reason
        admitted = next((s for s in self.state.studies if s.study_id == self.study.study_id), None)
        if admitted is None:
            self.state = recursive.admit_study(
                self.state, self.study, ctx.grant, policy=ctx.policy,
                authority=ctx.authority, action=ctx.action,
                repositories=ctx.repositories, now=now,
            )
            return self.state.terminal_reason
        if admitted != self.study:
            return "study_changed"
        return recursive._study_live_reason(self.state, self.study, now)

    def record(self, started: float) -> None:
        elapsed = perf_counter() - started
        ctx = self.context
        self.accounting.record(
            operation_id=ctx.operation_id, work_id=ctx.work_id, job_id=ctx.job_id,
            request_id=f"{ctx.request_id}:{len(self.state.usage) + 1}",
            wall_clock_seconds=elapsed, tokens=0, usd=Decimal(0),
        )
        # Deterministic local generation uses no model calls or billed tokens.
        self.state = recursive.record_usage(
            self.state, self.study.study_id, compute_seconds=elapsed,
        )


def _bindings(study: recursive.RecursiveStudy, recipe_bytes: bytes, spec_bytes: bytes) -> None:
    recursive.RecursiveStudy.model_validate(study.model_dump())
    if recipe_bytes != study.recipe:
        raise ValueError("changed_recipe")
    if study.method_sha256 != method_sha256():
        raise ValueError("method_changed")
    if spec_bytes != study.spec:
        raise ValueError("spec_changed")
    if study.lower_researcher_sha256 != lower_researcher_sha256():
        raise ValueError("lower_researcher_changed")


def validate_frozen_method(result: MethodResult, study: recursive.RecursiveStudy) -> str:
    """Verify frozen bindings and reconstruct only the pinned deterministic source."""
    _bindings(study, result.recipe_bytes, result.spec_bytes)
    if result.failures:
        raise ValueError(result.failures[0])
    if result.source_bytes is None:
        raise ValueError("changed_source")
    # Reconstruct with the existing generator, so forged result bytes cannot
    # introduce a second executable-code entry point. Never replace the result.
    source = mechanism.generation_job(json.loads(result.spec_bytes), json.loads(result.recipe_bytes))
    source_bytes = source.encode("utf-8")
    if source_bytes != result.source_bytes:
        raise ValueError("changed_source")
    if (
        result.recipe_sha256 != hashlib.sha256(result.recipe_bytes).hexdigest()
        or result.source_sha256 != hashlib.sha256(source_bytes).hexdigest()
        or result.artifact_sha256 != package_candidate({SOURCE_PATH: source_bytes})["bundle_sha256"]
    ):
        raise ValueError("changed_digest")
    return source


def run_method(
    study: recursive.RecursiveStudy,
    recipe_bytes: bytes,
    spec: object,
    development_traces: Mapping[str, Sequence[Mapping[str, object]]],
    *,
    context: MethodContext,
) -> MethodResult:
    """Generate frozen bytes from an admitted recipe and bound development traces.

    Trace mapping must contain exactly the study's development task IDs; each
    trace list must match its TaskEvidence content digest. Failed generation
    still spends measured compute and remains one attempt in the denominator.
    """
    spec_bytes = recursive.seal_json(spec)
    recipe_sha = hashlib.sha256(recipe_bytes).hexdigest()
    execution = _Execution(study, context)
    source = None
    failures: tuple[str, ...] = ()
    try:
        _bindings(study, recipe_bytes, spec_bytes)
        if set(development_traces) != {e.task_id for e in study.dev}:
            raise ValueError("development_cohort_changed")
        traces = []
        for evidence in study.dev:
            trace_list = json.loads(canonical.canonical_json(list(development_traces[evidence.task_id])))
            if canonical.sha256(trace_list) != evidence.content_sha256:
                raise ValueError("development_content_changed")
            traces.extend(trace_list)
        reason = execution.gate()
        if reason:
            raise ValueError(reason)
    except ValueError as error:
        failures = (str(error),)
    else:
        started = perf_counter()
        try:
            pattern = mechanism.failure_pattern(traces)
            if pattern is None:
                raise ValueError("no_pattern")
            # Specification defines the mechanism interface, never objective,
            # evaluator, confirmation evidence, authority, or caller code.
            pinned_spec = json.loads(spec_bytes)
            draft = mechanism.explore(pattern, [{
                "name": pinned_spec["name"], "kind": pinned_spec["candidate_kind"],
                "components": pinned_spec["state_fields"],
                "description": pinned_spec["expected_behavior"],
            }])[0]
            critique = mechanism.critique(draft, pattern, complexity_budget=2)
            if critique["status"] != "accepted":
                raise ValueError("critique_rejected")
            if mechanism.specify(draft) != pinned_spec:
                raise ValueError("spec_changed")
            source = mechanism.generation_job(pinned_spec, json.loads(recipe_bytes)).encode("utf-8")
        except Exception as error:
            failures = (str(error) if isinstance(error, ValueError) else "generation_crashed",)
        finally:
            execution.record(started)
        reason = execution.gate()
        if reason:
            failures += (reason,)
    return MethodResult(
        recipe_bytes, spec_bytes, source, recipe_sha,
        hashlib.sha256(source).hexdigest() if source is not None else None,
        package_candidate({SOURCE_PATH: source})["bundle_sha256"] if source is not None else None,
        failures, execution.state,
    )


def _history_extends(newer: bytes, older: bytes) -> bool:
    current = json.loads(newer)
    return all(
        value in current.get(key, [])
        for key, values in json.loads(older).items() for value in values
    )


def _extends(newer: recursive.RecursionState, older: recursive.RecursionState) -> bool:
    return (
        newer.grant == older.grant
        and newer.studies[:len(older.studies)] == older.studies
        and newer.proposal_history[:len(older.proposal_history)] == older.proposal_history
        and newer.usage[:len(older.usage)] == older.usage
        and _history_extends(newer.confirmation_history, older.confirmation_history)
        and (older.terminal_reason is None or newer.terminal_reason == older.terminal_reason)
    )


@dataclass(frozen=True)
class Observation:
    """Controller-supplied receipt and expected trial identity for one step."""

    receipt_bytes: bytes
    expected: ReceiptExpectation


class TrialFailed(ValueError):
    """A rejected/crashed proposal invalidates the trial; never fallback credit."""


class GeneratedPolicy:
    """PolicyFn callable. Keep this instance to inspect failures and usage.

    Observations are supplied by the controller after evaluation. Tree metric
    fields and tree verification flags are never evidence. Even sound receipts
    for unopened nodes remain hidden. No caller-supplied source is executed.
    """

    def __init__(
        self, result: MethodResult, study: recursive.RecursiveStudy, *,
        context: MethodContext, protocol: EvaluationProtocol,
        observations: Mapping[str, Observation],
    ) -> None:
        self._result = result
        self._execution = _Execution(study, context)
        if _extends(context.state, result.state):
            self._execution.state = context.state
        elif _extends(result.state, context.state):
            self._execution.state = result.state
        else:
            raise TrialFailed("state_changed")
        self._protocol = protocol
        self._observations = observations
        self._failures: list[str] = []
        self._denominator = 0

    @property
    def state(self) -> recursive.RecursionState:
        return self._execution.state

    @property
    def failures(self) -> tuple[str, ...]:
        return tuple(self._failures)

    @property
    def denominator(self) -> int:
        return self._denominator

    def __call__(self, steps: Steps, opened: set[str], *, seed: int = 0, width: int = 1) -> tuple[str | None, int]:
        self._denominator += 1
        execution = self._execution
        result = self._result
        reason = execution.gate()
        if self._failures:
            self._fail(self._failures[0])
        if reason or result.failures:
            self._fail(reason or result.failures[0])
        started = perf_counter()
        failure = None
        pick = None
        new_seed = seed
        try:
            source = validate_frozen_method(result, execution.study)
            if hashlib.sha256(self._protocol.protocol_bytes()).hexdigest() != execution.study.protocol_sha256:
                raise ValueError("protocol_changed")
            snap, opened_copy = strategy_protocol.sanitized_snapshot(steps, opened)
            visible = {}
            for step_id, node in snap.items():
                observation = self._observations.get(step_id) if step_id in opened_copy else None
                metric = None
                if observation is not None:
                    expected = observation.expected
                    study = execution.study
                    if (
                        expected.evaluator_component_sha256 == study.evaluator_component_sha256
                        and expected.evaluator_artifact_sha256 == study.evaluator_artifact_sha256
                        and not verify_receipt(observation.receipt_bytes, expected=expected, protocol=self._protocol)
                    ):
                        receipt = TrialReceipt.model_validate_json(observation.receipt_bytes)
                        if receipt.verdict != "evaluator_failed":
                            metric = receipt.metrics.get(self._protocol.primary)
                visible[step_id] = {
                    "parent_id": node.get("parent_id"), "children": node.get("children"),
                    "metric": metric, "infra_error": None,
                }
            recipe = mechanism_recipe.canonical_recipe(json.loads(result.recipe_bytes))
            if recipe["family"] != mechanism_recipe.ADAPTIVE_RESTART:
                outcome = strategy_execution.execute_strategy(
                    source.encode("utf-8"), visible, opened_copy, seed=seed, width=width,
                    allowed_actions=("open",), resource_caps={},
                    timeout=min(self._protocol.timeout_seconds, execution.study.limits.max_wall_clock_seconds),
                    cancel=execution.context.cancel or _EXECUTION_CANCEL.get(),
                )
                if outcome.failure or outcome.selection is None:
                    raise ValueError(outcome.failure or "invalid_selection")
                pick = outcome.selection["step_id"]
                new_seed = outcome.seed
            else:
                proposal = strategy_protocol.run_candidate(
                    source, visible, opened_copy, seed=seed, width=width,
                    allowed_actions=("open",), resource_caps={},
                )
                if proposal["status"] != "ok":
                    raise ValueError(proposal.get("reason") or ",".join(proposal["reasons"]))
                pick = proposal["proposal"]["step_id"]
                new_seed = proposal["seed"]
            if pick in opened_copy:
                raise ValueError("already_open")
        except Exception as error:
            failure = str(error) if isinstance(error, ValueError) else "crashed"
        finally:
            execution.record(started)
        reason = execution.gate()
        if failure and reason:
            self._failures.append(failure)
        if failure or reason:
            self._fail(reason or failure)
        return pick, new_seed

    def _fail(self, reason: str) -> None:
        self._failures.append(reason)
        raise TrialFailed(reason)

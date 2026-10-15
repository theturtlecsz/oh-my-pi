"""L3 staging and receipt-backed, whole-population confirmation.

The fixed preregistered rule is all-pairs-positive.v1: every policy in both
frozen threshold populations must have a fresh, matched, strictly positive
paired result. A plan grants evaluation only. Admission rereads native custody;
no boolean, cached score or comparison report is a confirmation proof.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from omp_work.research import mechanism, mechanism_recipe, recursive
from omp_work.research.harness_release import package_candidate
from omp_work.research.recursive_method import MethodResult, SOURCE_PATH, _bindings, cancellation_scope
from omp_work.research.recursive_transfer_evidence import ConsumptionHistory, NativeEvidence
from omp_work.research.recursive_transfer_execution import execute_task
from omp_work.research.recursive_transfer_execution_inputs import PreparedExecution
from omp_work.research.recursive_transfer_freshness import CohortTask, PreparedTask, _dataset, _trace_hashes, prepare_cohorts
from omp_work.research.recursive_transfer_methods import FrozenMethod
from omp_work.v1 import canonical


@dataclass(frozen=True)
class ConfirmationPlan:
    binding_sha256: str
    pass_rule: str = "all-pairs-positive.v1"


@dataclass(frozen=True)
class ConfirmationRequest:
    plan: ConfirmationPlan
    # One matched task per threshold, in the generator's frozen population order.
    cohorts: tuple[CohortTask, ...]
    native: NativeEvidence
    cancel: threading.Event | None = None


def _incumbent(state: recursive.RecursionState, proposal: recursive.RecursiveProposal) -> bytes:
    return next(
        (item.recipe for item in reversed(state.proposal_history) if item.study_id == proposal.study_id),
        next(s.recipe for s in state.studies if s.study_id == proposal.study_id),
    )


def structural_change(state: recursive.RecursionState, proposal: recursive.RecursiveProposal) -> bool:
    parent = mechanism_recipe.canonical_recipe(json.loads(_incumbent(state, proposal)))
    proposed = mechanism_recipe.canonical_recipe(json.loads(proposal.recipe))
    return parent["family"] != proposed["family"]


def _populations(state: recursive.RecursionState, proposal: recursive.RecursiveProposal):
    study = next(s for s in state.studies if s.study_id == proposal.study_id)
    _bindings(study, study.recipe, study.spec)
    return tuple(
        mechanism.generation_population(json.loads(proposal.spec),
                                        mechanism_recipe.canonical_recipe(json.loads(recipe))["family"])
        for recipe in (_incumbent(state, proposal), proposal.recipe)
    )


def stage_proposal(state: recursive.RecursionState, proposal: recursive.RecursiveProposal) -> ConfirmationPlan:
    """Freeze exact generator recipes, all sources and evaluator pins; never append."""
    proposal = recursive.RecursiveProposal.model_validate(proposal.model_dump())
    study = next(s for s in state.studies if s.study_id == proposal.study_id)
    reason = recursive._content_reason(state.studies, state.proposal_history, proposal)
    if reason:
        raise ValueError(reason)
    populations = _populations(state, proposal)
    return ConfirmationPlan(canonical.sha256({
        "study": study.model_dump(mode="json"),
        "parent_recipe": json.loads(_incumbent(state, proposal)),
        "proposal": proposal.model_dump(mode="json"),
        "populations": populations,
        "pass_rule": "all-pairs-positive.v1",
    }))


def validate_history(state: recursive.RecursionState) -> None:
    """Future development cannot expose content already used for confirmation."""
    snapshot = json.loads(state.confirmation_history)
    if not isinstance(snapshot, dict) or any(not isinstance(value, list) for value in snapshot.values()):
        raise ValueError("invalid_confirmation_history")
    history = ConsumptionHistory.from_snapshot(snapshot)
    excluded = set()
    for identity in history.confirmation_identities():
        excluded.update(identity if isinstance(identity, tuple) else (identity,))
    for study in state.studies:
        if study.dev_sha256 in excluded and (
            "development_cohort", study.study_id, study.dev_sha256
        ) not in history.consumed:
            raise ValueError("confirmation_reused")
        for item in study.dev:
            if (item.task_id in excluded or item.content_sha256 in excluded) and (
                "development", item.task_id, item.content_sha256
            ) not in history.consumed:
                raise ValueError("confirmation_reused")


def _frozen(state: recursive.RecursionState, study: recursive.RecursiveStudy, policy: dict) -> FrozenMethod:
    # Evaluation uses a separate root carrying the exact original caps and pins.
    # It retains all tree-cumulative expenditure, with no incumbent mutation.
    recipe = recursive.seal_json(policy["recipe"])
    staged = study.model_copy(update={"recipe": recipe, "parent_id": None, "depth": 0})
    usage = tuple(item.model_copy(update={"study_id": study.study_id}) for item in state.usage)
    evaluation = recursive.RecursionState(studies=(staged,), grant=state.grant, usage=usage,
                                         confirmation_history=state.confirmation_history)
    source = policy["source"].encode("utf-8")
    result = MethodResult(recipe, study.spec, source, hashlib.sha256(recipe).hexdigest(),
                          policy["source_sha256"], package_candidate({SOURCE_PATH: source})["bundle_sha256"],
                          (), evaluation)
    return FrozenMethod(result, staged)


def confirm(
    state: recursive.RecursionState, proposal: recursive.RecursiveProposal,
    request: object, now: datetime,
) -> tuple[recursive.RecursionState, str | None]:
    """Consume fresh evidence even on a null; admit only the exact staged proposal."""
    if type(request) is not ConfirmationRequest or type(request.plan) is not ConfirmationPlan:
        return state, "confirmation_required"
    native = request.native
    if not isinstance(native, NativeEvidence):
        return state, "confirmation_required"
    # Merge persisted and caller transfer history; never discard previous use.
    retained = ConsumptionHistory.from_snapshot(json.loads(state.confirmation_history))
    merged = retained.snapshot()
    for key, values in native.history.snapshot().items():
        merged[key].extend(values)
    native.history.restore(merged)
    prepared: tuple[PreparedTask, ...] = ()
    try:
        if request.cancel is not None and request.cancel.is_set():
            raise ValueError("canceled")
        if request.plan != stage_proposal(state, proposal):
            raise ValueError("confirmation_binding_changed")
        study = next(s for s in state.studies if s.study_id == proposal.study_id)
        reason = recursive._admission_gate(state, native.grant, native.policy, native.authority,
                                           native.action, native.repositories, now)
        if reason:
            raise ValueError(reason)
        cohorts = tuple(CohortTask.model_validate(c.model_dump()) for c in request.cohorts)
        base, cand = _populations(state, proposal)
        if len(cohorts) != len(cand) or len(base) != len(cand):
            raise ValueError("confirmation_population_incomplete")
        if any(c.phase != "confirmation" for c in cohorts):
            raise ValueError("confirmation_phase_required")
        # Retain which development evidence preceded this exposure. Failed
        # holdouts that overlap it must remain restorable, while later dev use
        # of newly exposed confirmation content must fail.
        for admitted in state.studies:
            native.history.record_attempt(("development_cohort", admitted.study_id, admitted.dev_sha256))
            for evidence in admitted.dev:
                native.history.record_attempt(("development", evidence.task_id, evidence.content_sha256))
        prepared = prepare_cohorts(cohorts, native, state.studies)
        # Preparation sees the whole input before any consumption (order independent).
        failures = []
        for cohort, task in zip(cohorts, prepared):
            if task.content_sha256 is not None:
                for digest in _trace_hashes(_dataset(native, cohort.dataset_id).payload):
                    native.history.note_confirmation(digest)
        for index, (cohort, task, baseline, candidate) in enumerate(zip(cohorts, prepared, base, cand)):
            reason = recursive._study_live_reason(state, study, now)
            if reason:
                failures.append(reason)
                break
            if request.cancel is not None and request.cancel.is_set():
                failures.append("canceled")
                break
            usage_start = len(state.usage)
            pair = (_frozen(state, study, baseline), _frozen(state, study, candidate))
            scoped = replace_native(native, index)
            with cancellation_scope(request.cancel):
                result = execute_task(*pair, PreparedExecution.from_task(task, cohort), scoped)
            # Retain measured compute on both success and refusal, cumulatively.
            for arm in (result.baseline, result.candidate):
                for delta in arm.state.usage[usage_start:]:
                    state = recursive.record_usage(state, study.study_id,
                                                   compute_seconds=delta.compute_seconds,
                                                   tokens=delta.tokens, usd=delta.usd)
            if not result.ok or result.failures or result.delta is None or result.delta <= 0:
                failures.append("confirmation_failed")
            if request.cancel is not None and request.cancel.is_set():
                failures.insert(0, "canceled")
            if state.terminal_reason:
                failures.append(state.terminal_reason)
        return _retain(state, native, prepared), failures[0] if failures else None
    except (ValueError, TypeError, AttributeError, OSError, RuntimeError) as error:
        return _retain(state, native, prepared), str(error)


def _retain(
    state: recursive.RecursionState, native: NativeEvidence, prepared: Sequence[PreparedTask],
) -> recursive.RecursionState:
    # Preparation has exposed the entire cohort, including tasks not executed
    # after cancellation or quota exhaustion. None may become a new holdout.
    for task in prepared:
        if task.content_sha256 is not None and task.task_id is not None:
            native.history.note_confirmation((task.content_sha256, task.task_id))
    return state.model_copy(update={
        "confirmation_history": canonical.canonical_json(native.history.snapshot()).encode("utf-8"),
    })


def replace_native(native: NativeEvidence, index: int) -> NativeEvidence:
    """Keep shared consumption while separating controller accounting identities."""
    return NativeEvidence(
        native.store, native.workspace_id, native.actor_id, native.work_id, native.data_dir,
        grant=native.grant, policy=native.policy, authority=native.authority, action=native.action,
        repositories=native.repositories, history=native.history,
        operation_id=f"{native.operation_id}:confirmation:{index}",
        request_id=f"{native.request_id}:confirmation:{index}",
    )

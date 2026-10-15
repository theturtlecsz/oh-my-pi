"""Structural L3 admission requires fresh native evidence for every generated policy."""

import json
import os
import threading
from dataclasses import replace
from uuid import uuid4

import psycopg
import pytest

from native_jobs_support import native_jobs  # noqa: F401
from omp_work.research import mechanism, strategy_execution, recursive_confirmation as confirmation
from omp_work.research.recursive import (
    RecursionState, RecursiveProposal, TaskEvidence, admit_proposal, record_usage, seal_json,
)
from omp_work.research.recursive_method import GeneratedPolicy
from omp_work.research.recursive_transfer_evidence import ConsumptionHistory
from omp_work.v1.canonical import sha256
from recursive_transfer_execution_support import build_execution, seal_step
from recursive_transfer_support import register_task_dataset
from test_research_recursive_method import generated, observation, tree

pytest_plugins = ("test_workflow_service",)
native = pytest.mark.skipif(os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
                            reason="set OMP_WORK_POSTGRES_INTEGRATION=1")


def propose(study):
    return RecursiveProposal(proposal_id="structural", study_id=study.study_id,
                             recipe={"family": mechanism.ROOT_FIRST, "min_metric": 0.5}, spec=study.spec)


def admit(state, proposal, ctx, proof=None):
    return admit_proposal(state, proposal, ctx.grant, policy=ctx.policy, authority=ctx.authority,
                          action=ctx.action, repositories=ctx.repositories, now=ctx.now(),
                          confirmation=proof)


def test_equivalent_family_recipe_names_and_numeric_forms_terminate():
    result, _, study, ctx, _ = generated()
    proposal = RecursiveProposal(proposal_id="renamed", study_id=study.study_id,
                                recipe={"family": mechanism.ADAPTIVE_RESTART, "min_metric": 0.5,
                                        "name": "renamed"}, spec=study.spec)
    assert admit(result.state, proposal, ctx).terminal_reason == "equivalent_proposal"
    assert admit(result.state, propose(study), ctx).terminal_reason == "confirmation_required"
    scalar = proposal.model_copy(update={"recipe": seal_json({"min_metric": 0.7})})
    assert admit(result.state, scalar, ctx).proposal_history == (scalar,)


@pytest.mark.parametrize("proof", [None, True, {"status": "compared", "deltas": [999], "keep_both": False}])
def test_boolean_or_report_never_admits_structure(proof):
    result, _, study, ctx, _ = generated()
    outcome = admit(result.state, propose(study), ctx, proof)
    assert not outcome.proposal_history
    assert outcome.studies == result.state.studies
    assert outcome.terminal_reason == "confirmation_required"


def test_frozen_policy_retains_later_confirmation_consumption():
    result, _, study, ctx, protocol = generated()
    history = ConsumptionHistory()
    history.note_confirmation(("a" * 64, "past-holdout"))
    state = result.state.model_copy(update={"confirmation_history": json.dumps(history.snapshot()).encode()})
    policy = GeneratedPolicy(result, study, context=replace(ctx, state=state), protocol=protocol,
                             observations={"a": observation(study, protocol)})
    assert policy(tree(), {"a"}) == ("a1", 0)
    assert policy.state.confirmation_history == state.confirmation_history


def build_request(native_jobs, *, score=0.8, resources=None, source=None, outcome="succeeded"):
    execution = build_execution(native_jobs)
    state = execution.baseline.result.state
    ctx = execution.baseline_context
    execution.native.grant = ctx.grant
    execution.native.policy = ctx.policy
    execution.native.authority = ctx.authority
    execution.native.action = ctx.action
    execution.native.repositories = ctx.repositories
    proposal = propose(execution.baseline.study)
    plan = confirmation.stage_proposal(state, proposal)
    base, cand = confirmation._populations(state, proposal)
    cohorts = []
    for index, (left, right) in enumerate(zip(base, cand)):
        execution.baseline = confirmation._frozen(state, execution.baseline.study, left)
        execution.candidate = confirmation._frozen(state, execution.baseline.study, right)
        execution.arms = {}
        # Both policies select root; receipts still independently bind every source.
        execution.steps = {"root": {"parent_id": None, "children": []}}
        execution.opened = set()
        seal_step(execution, "root", baseline_score=0.4, candidate_score=score,
                  candidate_resources=resources, candidate_source=source, candidate_outcome=outcome)
        cohort = execution.cohort()
        dataset = register_task_dataset(native_jobs, uuid4().hex, {
            "steps": execution.steps, "opened": [], "case": index,
        })
        cohorts.append(cohort.model_copy(update={"dataset_id": dataset}))
    return execution, state, proposal, ctx, confirmation.ConfirmationRequest(plan, tuple(cohorts), execution.native)


@native
def test_fresh_verified_population_admits_exact_proposal(native_jobs):
    _, state, proposal, ctx, request = build_request(native_jobs)
    admitted = admit(state, proposal, ctx, request)
    assert admitted.terminal_reason is None, admitted.terminal_reason
    assert admitted.proposal_history == (proposal,)
    assert admitted.studies == state.studies
    assert len(admitted.usage) == len(state.usage) + 6
    restored = RecursionState.model_validate_json(admitted.model_dump_json())
    history = ConsumptionHistory.from_snapshot(json.loads(restored.confirmation_history))
    assert len(history.confirmation_identities()) == 3
    # Replay after restoration with fresh caller history remains ineligible.
    replay = state.model_copy(update={"confirmation_history": restored.confirmation_history})
    request.native.history = ConsumptionHistory()
    repeated = admit(replay, proposal, ctx, request)
    assert not repeated.proposal_history
    assert repeated.terminal_reason == "confirmation_failed"
    # Development added after confirmation cannot reuse its content under a new ID.
    content = next(iter(history.confirmation_identities()))[0]
    study = restored.studies[0]
    changed = study.model_copy(update={
        "dev": (TaskEvidence(task_id="later-dev", content_sha256=content),),
        "dev_sha256": sha256([content]),
    })
    with pytest.raises(ValueError, match="confirmation_reused"):
        RecursionState.model_validate({**restored.model_dump(), "studies": (changed,)})


@native
@pytest.mark.parametrize("change", ["null", "failed", "resources", "source", "baseline", "judge", "protocol",
                                    "proposal", "incomplete", "forged_score", "renamed_holdout"])
def test_invalid_population_keeps_incumbent(native_jobs, change):
    options = {}
    if change in {"null", "forged_score"}:
        options["score"] = 0.4
    elif change == "failed":
        options["outcome"] = "failed"
    elif change == "resources":
        options["resources"] = {"cpu": 2}
    elif change == "source":
        options["source"] = b'def propose(steps, opened, *, seed=0, width=1):\n    return None, seed\n'
    execution, state, proposal, ctx, request = build_request(native_jobs, **options)
    if change in {"baseline", "judge", "protocol"}:
        field, value = {
            "baseline": ("recipe", seal_json({"min_metric": 0.9})),
            "judge": ("evaluator_artifact_sha256", "a" * 64),
            "protocol": ("protocol_sha256", "a" * 64),
        }[change]
        study = state.studies[0].model_copy(update={field: value})
        state = state.model_copy(update={"studies": (study,)})
    elif change == "proposal":
        proposal = proposal.model_copy(update={"recipe": seal_json({"family": mechanism.ROOT_FIRST,
                                                                    "min_metric": 0.6})})
    elif change == "incomplete":
        request = replace(request, cohorts=request.cohorts[:-1])
    elif change == "forged_score":
        # Observation summary claims victory; managed receipt bytes still say null.
        with psycopg.connect(**native_jobs.service.config.connection_kwargs("postgres")) as conn, conn.cursor() as cur:
            cur.execute("SET LOCAL session_replication_role = replica")
            cur.execute("UPDATE omp_research.observations SET payload=jsonb_set(payload, '{metrics,score}', '999') "
                        "WHERE workspace_id=%s AND trial_id=%s",
                        (request.native.workspace_id, execution.arms["root"][1].trial_id))
    elif change == "renamed_holdout":
        # Same content under a different task ID cannot escape dev exclusion.
        payload = {"steps": execution.steps, "opened": [], "case": 0}
        digest = sha256(payload)
        dev = (TaskEvidence(task_id="old-dev-label", content_sha256=digest),)
        study = state.studies[0].model_copy(update={"dev": dev, "dev_sha256": sha256([digest])})
        state = state.model_copy(update={"studies": (study,)})
        request = replace(request, plan=confirmation.stage_proposal(state, proposal))
    result = admit(state, proposal, ctx, request)
    assert result.terminal_reason, change
    assert not result.proposal_history
    assert result.studies == state.studies
    assert RecursionState.model_validate_json(result.model_dump_json()) == result
    if change in {"null", "failed", "resources", "source", "forged_score", "renamed_holdout"}:
        assert ConsumptionHistory.from_snapshot(json.loads(result.confirmation_history)).confirmation_identities()


@native
def test_live_authority_quota_and_cancellation_still_gate_confirmation(native_jobs, monkeypatch):
    _, state, proposal, ctx, request = build_request(native_jobs)
    revoked = replace(ctx, authority=replace(ctx.authority, decision_id="revoked"))
    assert admit(state, proposal, revoked, request).terminal_reason == "authority_binding_mismatch"
    exhausted = record_usage(state, proposal.study_id, compute_seconds=state.studies[0].limits.max_wall_clock_seconds)
    assert admit(exhausted, proposal, ctx, request).terminal_reason == "quota_exhausted"
    event = threading.Event()
    event.set()
    canceled = admit(state, proposal, ctx, replace(request, cancel=event))
    assert canceled.terminal_reason == "canceled"
    assert not canceled.proposal_history

    # Cancellation arriving at the isolated call reaches the executor too.
    event.clear()
    execute = strategy_execution.execute_strategy
    calls = []

    def cancel_at_call(*args, **kwargs):
        calls.append(kwargs["cancel"])
        event.set()
        return execute(*args, **kwargs)

    monkeypatch.setattr(strategy_execution, "execute_strategy", cancel_at_call)
    interrupted = admit(state, proposal, ctx, replace(request, cancel=event))
    assert interrupted.terminal_reason == "canceled"
    assert not interrupted.proposal_history
    assert calls == [event]
    assert len(interrupted.usage) == len(state.usage) + 2

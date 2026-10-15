"""Run a loaded campaign through native jobs and persisted research evidence."""

from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from omp_work.jobs.campaign_completion import read_trial_completion
from omp_work.jobs.campaign_decisions import (
    Decision,
    decision_id,
    decode,
    encode,
    format_rationale,
    observations_digest,
)
from omp_work.jobs.campaign_definition import (
    CampaignDefinition,
    PreparedTrial,
    SearchConfig,
    score_campaign,
    score_metrics,
    search_steps,
)
from omp_work.jobs.campaign_recovery import PendingTrial, recover_progress
from omp_work.jobs.campaign_setup import CampaignSetup, prepare_campaign
from omp_work.jobs.research_trial import (
    CAPABILITY,
    ResearchTrialHandler,
    dispatch_trial,
    trial_job_id,
)
from omp_work.jobs.store import NativeJobStore
from omp_work.jobs.worker import JobWorker, WorkerConfig
from omp_work.research.search import next_step, winner
from omp_work.v1.api_models import WorkItemView
from omp_work.v1.client import WorkClient, _envelope
from omp_work.v1.models import (
    AdmitResearchCampaignCommand,
    AdmitResearchCampaignPayload,
    ConcludeResearchCampaignCommand,
    ConcludeResearchCampaignPayload,
    CreateResearchCampaignCommand,
    CreateResearchCampaignPayload,
    ProposeResearchTrialCommand,
    ProposeResearchTrialPayload,
    ResearchCampaign,
    SetResearchCampaignStateCommand,
    SetResearchCampaignStatePayload,
)

__all__ = ["DecodedDecision", "run_research_campaign"]


class DecodedDecision(dict):
    """Decoded decision dictionary that exposes codec fields as attributes and serializes cleanly to JSON."""

    @classmethod
    def from_decision(cls, d: Decision) -> DecodedDecision:
        return cls({
            "v": d.v,
            "index": d.index,
            "policy": d.policy,
            "step_id": d.step_id,
            "observations_count": d.observations_count,
            "observations_sha256": d.observations_sha256,
            "rationale": d.rationale,
        })

    @property
    def v(self) -> int:
        return self["v"]

    @property
    def index(self) -> int:
        return self["index"]

    @property
    def policy(self) -> str:
        return self["policy"]

    @property
    def step_id(self) -> str:
        return self["step_id"]

    @property
    def observations_count(self) -> int:
        return self["observations_count"]

    @property
    def observations_sha256(self) -> str:
        return self["observations_sha256"]

    @property
    def rationale(self) -> str:
        return self["rationale"]

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Decision):
            return (
                self.get("index") == other.index
                and self.get("policy") == other.policy
                and self.get("step_id") == other.step_id
                and self.get("observations_count") == other.observations_count
                and self.get("observations_sha256") == other.observations_sha256
                and self.get("rationale") == other.rationale
                and self.get("v") == other.v
            )
        return super().__eq__(other)

    def __hash__(self) -> int:
        return hash((
            self["index"],
            self["policy"],
            self["step_id"],
            self["observations_count"],
            self["observations_sha256"],
            self["rationale"],
        ))


_RESUMABLE_STATES = frozenset({"running", "evaluating"})


def _resumable_campaign(
    client: WorkClient, key: str, item: WorkItemView, setup: CampaignSetup
) -> ResearchCampaign | None:
    """The unique running/evaluating campaign bound to this exact definition, if any."""
    matches = [
        campaign
        for campaign in client.research(key).campaigns
        if campaign.workspace_id == item.workspace_id
        and campaign.work_id == item.work_id
        and campaign.revision_id == item.revision.revision_id
        and campaign.state in _RESUMABLE_STATES
        and campaign.spec_sha256 == setup.spec_sha256
        and campaign.policy_sha256 == setup.policy_sha256
        and campaign.compatibility_sha256 == setup.compatibility_sha256
    ]
    if len(matches) > 1:
        raise RuntimeError(
            "ambiguous resume: campaigns "
            f"{sorted(str(c.campaign_id) for c in matches)} all match this definition"
        )
    return matches[0] if matches else None


def _job_missing(
    store: NativeJobStore, workspace_id: UUID, actor_id: UUID, job_id: str
) -> bool:
    with store.transaction(workspace_id, actor_id) as cur:
        return store.job_view(cur, workspace_id, job_id) is None


def _trial_summary(
    trial_id: UUID, step_id: str, execution_status: str, metrics: Mapping[str, object]
) -> dict[str, object]:
    return {
        "trial_id": str(trial_id),
        "step_id": step_id,
        "execution_status": execution_status,
        "metrics": {name: float(value) for name, value in metrics.items()},
    }


def _propose(
    client: WorkClient,
    setup: CampaignSetup,
    trial: PreparedTrial,
    *,
    campaign_id: UUID,
    work_id: UUID,
    workspace_id: UUID,
    search: SearchConfig,
    steps: dict[str, dict[str, Any]],
    pick: str,
    index: int,
    completions: list[tuple[UUID, float | None]],
) -> tuple[UUID, str]:
    """Persist one new trial proposal carrying its encoded decision."""
    parent = steps[pick].get("parent_id")
    metric_basis = steps[parent].get("metric") if parent is not None else None
    decision = Decision(
        index=index,
        policy=search.policy,
        step_id=pick,
        observations_count=index,
        observations_sha256=observations_digest(completions),
        rationale=format_rationale(pick, parent=parent, metric_basis=metric_basis),
    )
    encoded_reason = encode(decision)
    trial_id = uuid4()
    client.execute(_envelope(workspace_id, ProposeResearchTrialCommand(
        type="propose_research_trial",
        payload=ProposeResearchTrialPayload(
            trial_id=trial_id, campaign_id=campaign_id, work_id=work_id,
            decision_id=decision_id(campaign_id, index), action="evaluate",
            candidate_digest=trial.candidate_digest,
            experiment_spec_sha256=setup.spec_sha256,
            evaluator_sha256=setup.evaluator_by_digest[trial.candidate_digest],
            environment_sha256=setup.environment_sha256,
            input_manifest_sha256=setup.compatibility_sha256,
            policy_sha256=setup.policy_sha256,
            reason=encoded_reason,
            seed=search.seed,
        ),
    )))
    return trial_id, encoded_reason


def run_research_campaign(
    client: WorkClient,
    store: NativeJobStore,
    key: str,
    definition: CampaignDefinition,
    *,
    harness_dir: Path,
    actor_id: UUID,
) -> dict[str, object]:
    """Conclude only after each targeted job settles with a stored observation."""
    item = client.work_item(key)
    workspace_id, work_id = item.workspace_id, item.work_id
    setup = prepare_campaign(client, workspace_id, definition, harness_dir=harness_dir)
    resumed = (
        _resumable_campaign(client, key, item, setup)
        if definition.search is not None
        else None
    )
    if resumed is not None:
        campaign_id = resumed.campaign_id
    else:
        campaign_id = uuid4()
        client.execute(_envelope(workspace_id, CreateResearchCampaignCommand(
            type="create_research_campaign",
            payload=CreateResearchCampaignPayload(
                campaign_id=campaign_id, work_id=work_id,
                revision_id=item.revision.revision_id, domain=definition.domain,
                spec=setup.spec, spec_sha256=setup.spec_sha256,
            ),
        )))
        client.execute(_envelope(workspace_id, AdmitResearchCampaignCommand(
            type="admit_research_campaign",
            payload=AdmitResearchCampaignPayload(
                campaign_id=campaign_id, work_id=work_id,
                revision_id=item.revision.revision_id, spec_sha256=setup.spec_sha256,
                policy_sha256=setup.policy_sha256, compatibility=setup.compatibility,
                compatibility_sha256=setup.compatibility_sha256,
            ),
        )))
        client.execute(_envelope(workspace_id, SetResearchCampaignStateCommand(
            type="set_research_campaign_state",
            payload=SetResearchCampaignStatePayload(
                campaign_id=campaign_id, work_id=work_id,
                expected_state="admitted", target_state="running",
                policy_sha256=setup.policy_sha256,
            ),
        )))
    worker = JobWorker(
        store,
        WorkerConfig(
            workspace_id=workspace_id, actor_id=actor_id,
            worker_id=f"research-campaign:{campaign_id}",
            component_sha256=setup.worker_sha256,
            capabilities=[CAPABILITY], capacity=1,
        ),
        ResearchTrialHandler(harness_dir=harness_dir, execute=client.execute),
    )
    evaluating = resumed is not None and resumed.state == "evaluating"
    if not evaluating:
        worker.start()

    search = definition.search
    if search is None:
        search = SearchConfig(
            policy="fixed",
            budget=math.fsum(trial.cost for trial in definition.trials),
            width=1,
            seed=0,
            metric=definition.criteria[0].metric,
        )
    steps = search_steps(definition)
    trials_by_step = {
        (trial.id if trial.id is not None else f"t{index:03d}"): trial
        for index, trial in enumerate(definition.trials)
    }

    trials: list[dict[str, object]] = []
    verdicts: list[tuple[str, str]] = []
    decisions: list[DecodedDecision] = []
    proposed_costs: list[float] = []
    opened: set[str] = set()
    completions: list[tuple[UUID, float | None]] = []
    index = 0
    pending: PendingTrial | None = None

    if resumed is not None and any(
        trial.campaign_id == campaign_id for trial in client.research(key).trials
    ):
        progress = recover_progress(client, store, key, resumed, definition, actor_id)
        steps = progress.tree
        for result in progress.results:
            opened.add(result.step_id)
            proposed_costs.append(steps[result.step_id]["cost"])
            trials.append(_trial_summary(result.trial_id, result.step_id,
                                         result.execution_status, result.metrics))
        verdicts.extend(progress.verdicts)
        decisions.extend(
            DecodedDecision.from_decision(d)
            for d in progress.decisions[:len(progress.results)]
        )
        completions.extend(progress.pairs)
        index = len(progress.results)
        pending = progress.pending
    if evaluating and pending is not None:
        raise RuntimeError(
            f"evaluating campaign {campaign_id} has unsettled trial {pending.trial_id}"
        )

    while not evaluating:
        if pending is not None:
            # Persisted proposal: keep its trial id, decision, reason, and seed.
            pick, trial_id = pending.step_id, pending.trial_id
            cost = steps[pick]["cost"]
            encoded_reason = next(
                t.reason for t in client.research(key).trials
                if t.trial_id == trial_id
            )
        else:
            pick, _ = next_step(
                search.policy, steps, opened, seed=search.seed, width=search.width,
            )
            if pick is None:
                break
            cost = steps[pick]["cost"]
            try:
                prospective_spent = math.fsum([*proposed_costs, cost])
            except OverflowError:
                break
            if prospective_spent > search.budget:
                break
            trial_id, encoded_reason = _propose(
                client, setup, trials_by_step[pick], campaign_id=campaign_id,
                work_id=work_id, workspace_id=workspace_id, search=search,
                steps=steps, pick=pick, index=index, completions=completions,
            )
        job_id = trial_job_id(trial_id)
        if pending is None or _job_missing(store, workspace_id, actor_id, job_id):
            dispatch_trial(
                store, operation_id=str(uuid4()), workspace_id=workspace_id,
                actor_id=actor_id, work_id=work_id, trial_id=trial_id,
                resources={"cpu": 1, "memory_mib": 0, "gpu": 0, "model_calls": 0},
                lease_seconds=60,
            )
        pending = None
        if worker.tick(job_id=job_id) != job_id:
            raise RuntimeError(f"research worker did not execute target job {job_id}")
        persisted = next(
            (
                candidate for candidate in client.research(key).trials
                if candidate.trial_id == trial_id
            ),
            None,
        )
        if persisted is None:
            raise RuntimeError(f"research trial {trial_id} is not persisted")
        completion = read_trial_completion(
            client, store, key, campaign_id, persisted, actor_id,
            search.metric,
        )
        steps[pick]["metric"] = completion.value
        steps[pick]["infra_error"] = completion.infra_error
        opened.add(pick)
        proposed_costs.append(cost)
        completions.append((trial_id, completion.value))
        decoded_decision = decode(encoded_reason)
        if decoded_decision is not None:
            decisions.append(DecodedDecision.from_decision(decoded_decision))

        verdicts.append(score_metrics(
            completion.metrics, definition.criteria, completion.execution_status,
        ))
        trials.append(_trial_summary(
            trial_id, pick, completion.execution_status, completion.metrics,
        ))
        index += 1

    outcome, reason = score_campaign(verdicts)
    if not evaluating:
        client.execute(_envelope(workspace_id, SetResearchCampaignStateCommand(
            type="set_research_campaign_state",
            payload=SetResearchCampaignStatePayload(
                campaign_id=campaign_id, work_id=work_id,
                expected_state="running", target_state="evaluating",
                policy_sha256=setup.policy_sha256,
            ),
        )))
    client.execute(_envelope(workspace_id, ConcludeResearchCampaignCommand(
        type="conclude_research_campaign",
        payload=ConcludeResearchCampaignPayload(
            campaign_id=campaign_id, work_id=work_id, policy_sha256=setup.policy_sha256,
            outcome=outcome, reason=reason,
        ),
    )))
    winner_id = winner({step_id: steps[step_id] for step_id in opened})
    best = (
        {"step_id": winner_id, "metric": float(steps[winner_id]["metric"])}
        if winner_id is not None
        else None
    )
    spent = float(math.fsum(proposed_costs))
    return {
        "campaign_id": str(campaign_id),
        "state": "concluded",
        "outcome": outcome,
        "reason": reason,
        "trials": trials,
        "decisions": decisions,
        "best": best,
        "spent": spent,
    }

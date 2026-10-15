"""Read-only recovery of an adaptive campaign's persisted progress (OMP-605).

``recover_progress`` replays only what the ledger already holds: the persisted
trial proposals (each carrying one encoded decision), the sealed evaluator
receipts, and the search tree the definition resolves to. It writes nothing,
dispatches nothing, and reselects nothing -- the stored pick stays authoritative,
so a restart mid-campaign resumes at the same next-trial decision.

Settled trials are read through :func:`read_trial_completion`, so a metric can
only come from a verified evaluator receipt; candidate-authored values are never
read. A last proposal whose job is missing or unsettled is returned as the
pending trial and is not a completion.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from omp_work.jobs.campaign_completion import read_trial_completion
from omp_work.jobs.campaign_decisions import (
    Decision,
    decision_id,
    decode,
    observations_digest,
)
from omp_work.jobs.campaign_definition import (
    CampaignDefinition,
    SearchConfig,
    score_metrics,
    search_steps,
)
from omp_work.jobs.research_trial import trial_job_id
from omp_work.jobs.store import NativeJobStore
from omp_work.v1.client import WorkClient
from omp_work.v1.models import ResearchCampaign, ResearchTrial

__all__ = [
    "CampaignProgress",
    "CampaignRecoveryError",
    "PendingTrial",
    "RecoveredTrial",
    "recover_progress",
]

_SETTLED_STATUSES = frozenset({"sealed", "failed"})


class CampaignRecoveryError(RuntimeError):
    """Refused recovery: the persisted history is inconsistent or incomplete."""


@dataclass(frozen=True, slots=True)
class RecoveredTrial:
    """One settled trial replayed from its verified evaluator receipt."""

    index: int
    trial_id: UUID
    step_id: str
    execution_status: str
    metrics: dict[str, object]
    value: float | None
    infra_error: str | None
    verdict: tuple[str, str]


@dataclass(frozen=True, slots=True)
class PendingTrial:
    """The last persisted proposal whose job is missing or unsettled."""

    index: int
    trial_id: UUID
    step_id: str
    candidate_digest: str


@dataclass(frozen=True, slots=True)
class CampaignProgress:
    """Replayed adaptive progress; every member is derived, never written."""

    campaign_id: UUID
    tree: dict[str, dict[str, Any]]
    opened: tuple[str, ...]
    spent: float
    decisions: tuple[Decision, ...]
    results: tuple[RecoveredTrial, ...]
    verdicts: tuple[tuple[str, str], ...]
    pairs: tuple[tuple[str, float | None], ...]
    pending: PendingTrial | None


def _job_settled(
    store: NativeJobStore, workspace_id: UUID, actor_id: UUID, job_id: str
) -> bool:
    with store.transaction(workspace_id, actor_id) as cur:
        job = store.job_view(cur, workspace_id, job_id)
    return (
        job is not None
        and job["status"] in _SETTLED_STATUSES
        and bool(job["settlement"])
    )


def _validate_record(
    *,
    index: int,
    decision: Decision,
    trial: ResearchTrial,
    campaign: ResearchCampaign,
    search: SearchConfig,
    compatibility_sha256: str,
    policy_sha256: str,
    evaluators: tuple[str, ...],
    environments: tuple[str, ...],
    tree: dict[str, dict[str, Any]],
    seen_steps: set[str],
) -> None:
    """Bind one decision/trial pair to the campaign, definition, and admitted components."""
    if decision.index != index:
        raise CampaignRecoveryError(f"decision {index} carries index {decision.index}")
    if decision.observations_count != index:
        raise CampaignRecoveryError(
            f"decision {index} observation count {decision.observations_count} "
            f"does not equal its index"
        )
    if decision.policy != search.policy:
        raise CampaignRecoveryError(
            f"decision {index} policy {decision.policy!r} does not match campaign "
            f"policy {search.policy!r}"
        )
    if decision_id(campaign.campaign_id, index) != trial.decision_id:
        raise CampaignRecoveryError(
            f"trial {trial.trial_id} decision_id does not match decision {index}"
        )
    step_id = decision.step_id
    if step_id not in tree:
        raise CampaignRecoveryError(f"decision {index} names unknown step {step_id!r}")
    if step_id in seen_steps:
        raise CampaignRecoveryError(
            f"decision {index} repeats already-opened step {step_id!r}"
        )
    if trial.candidate_digest != tree[step_id]["candidate_digest"]:
        raise CampaignRecoveryError(
            f"trial {trial.trial_id} candidate digest does not match step {step_id!r}"
        )
    if trial.seed != search.seed:
        raise CampaignRecoveryError(
            f"trial {trial.trial_id} seed {trial.seed!r} does not match campaign "
            f"seed {search.seed!r}"
        )
    if trial.experiment_spec_sha256 != campaign.spec_sha256:
        raise CampaignRecoveryError(
            f"trial {trial.trial_id} experiment spec is not bound to the campaign"
        )
    if trial.input_manifest_sha256 != compatibility_sha256:
        raise CampaignRecoveryError(
            f"trial {trial.trial_id} input manifest is not bound to the campaign"
        )
    if trial.policy_sha256 != policy_sha256:
        raise CampaignRecoveryError(
            f"trial {trial.trial_id} policy is not bound to the campaign"
        )
    if trial.evaluator_sha256 not in evaluators:
        raise CampaignRecoveryError(
            f"trial {trial.trial_id} evaluator is not in the admitted compatibility manifest"
        )
    if trial.environment_sha256 not in environments:
        raise CampaignRecoveryError(
            f"trial {trial.trial_id} environment is not in the admitted compatibility manifest"
        )


def recover_progress(
    client: WorkClient,
    store: NativeJobStore,
    key: str,
    campaign: ResearchCampaign,
    definition: CampaignDefinition,
    actor_id: UUID,
) -> CampaignProgress:
    """Replay an adaptive campaign's ordered trusted prefix without any write.

    Requires a search-configured definition and an admitted campaign whose
    spec/compatibility/policy bindings are recorded. Trials are ordered by the
    decoded decision index, which must be contiguous and unique. Each decision
    must name a distinct step whose candidate digest the trial carries, and its
    observation count and digest must match the preceding settled trials'
    receipt-derived ``(trial_id, metric|null)`` pairs. Only the last proposal may
    be unresolved; an earlier one refuses recovery. Every persisted proposal is
    charged once, including the pending last one.
    """
    search = definition.search
    if search is None:
        raise CampaignRecoveryError(
            "campaign definition has no search configuration to recover"
        )
    compatibility = campaign.compatibility
    compatibility_sha256 = campaign.compatibility_sha256
    policy_sha256 = campaign.policy_sha256
    if compatibility is None or compatibility_sha256 is None or policy_sha256 is None:
        raise CampaignRecoveryError(
            f"campaign {campaign.campaign_id} is missing its admission bindings"
        )
    campaign_id = campaign.campaign_id
    workspace_id = campaign.workspace_id

    persisted = [
        trial
        for trial in client.research(key).trials
        if trial.campaign_id == campaign_id
    ]
    if not persisted:
        raise CampaignRecoveryError(
            f"campaign {campaign_id} has no persisted trial proposals"
        )

    decoded: list[tuple[Decision, ResearchTrial]] = []
    for trial in persisted:
        try:
            decision = decode(trial.reason)
        except (ValueError, TypeError) as exc:
            raise CampaignRecoveryError(
                f"trial {trial.trial_id} has a malformed adaptive record"
            ) from exc
        if decision is None:
            raise CampaignRecoveryError(
                f"trial {trial.trial_id} has no adaptive decision record"
            )
        decoded.append((decision, trial))
    decoded.sort(key=lambda item: item[0].index)

    indices = [decision.index for decision, _ in decoded]
    if indices != list(range(len(decoded))):
        raise CampaignRecoveryError(
            "adaptive decision indices are not contiguous and unique"
        )

    tree = search_steps(definition)
    opened: list[str] = []
    spent_costs: list[float] = []
    decisions: list[Decision] = []
    results: list[RecoveredTrial] = []
    verdicts: list[tuple[str, str]] = []
    pairs: list[tuple[str, float | None]] = []
    seen_steps: set[str] = set()
    pending: PendingTrial | None = None

    for index, (decision, trial) in enumerate(decoded):
        _validate_record(
            index=index,
            decision=decision,
            trial=trial,
            campaign=campaign,
            search=search,
            compatibility_sha256=compatibility_sha256,
            policy_sha256=policy_sha256,
            evaluators=compatibility.evaluators,
            environments=compatibility.environments,
            tree=tree,
            seen_steps=seen_steps,
        )
        seen_steps.add(decision.step_id)

        if decision.observations_sha256 != observations_digest(pairs):
            raise CampaignRecoveryError(
                f"decision {index} digest does not match its trusted observations"
            )

        opened.append(decision.step_id)
        spent_costs.append(tree[decision.step_id]["cost"])
        decisions.append(decision)

        if not _job_settled(
            store, workspace_id, actor_id, trial_job_id(trial.trial_id)
        ):
            if index != len(decoded) - 1:
                raise CampaignRecoveryError(
                    f"trial {trial.trial_id} is unsettled before the last proposal"
                )
            pending = PendingTrial(
                index=index,
                trial_id=trial.trial_id,
                step_id=decision.step_id,
                candidate_digest=trial.candidate_digest,
            )
            break

        completion = read_trial_completion(
            client, store, key, campaign_id, trial, actor_id, search.metric
        )
        tree[decision.step_id]["metric"] = completion.value
        tree[decision.step_id]["infra_error"] = completion.infra_error
        pairs.append((str(trial.trial_id), completion.value))
        verdict = score_metrics(
            completion.metrics, definition.criteria, completion.execution_status
        )
        verdicts.append(verdict)
        results.append(
            RecoveredTrial(
                index=index,
                trial_id=trial.trial_id,
                step_id=decision.step_id,
                execution_status=completion.execution_status,
                metrics=dict(completion.metrics),
                value=completion.value,
                infra_error=completion.infra_error,
                verdict=verdict,
            )
        )

    try:
        spent = float(math.fsum(spent_costs))
    except OverflowError as exc:
        raise CampaignRecoveryError("campaign costs are not finite") from exc

    return CampaignProgress(
        campaign_id=campaign_id,
        tree=tree,
        opened=tuple(opened),
        spent=spent,
        decisions=tuple(decisions),
        results=tuple(results),
        verdicts=tuple(verdicts),
        pairs=tuple(pairs),
        pending=pending,
    )

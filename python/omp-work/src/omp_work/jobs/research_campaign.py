"""Run a loaded campaign through native jobs and persisted research evidence."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

from omp_work.jobs.campaign_completion import read_trial_completion
from omp_work.jobs.campaign_definition import (
    CampaignDefinition,
    score_campaign,
    score_metrics,
)
from omp_work.jobs.campaign_setup import prepare_campaign
from omp_work.jobs.research_trial import (
    CAPABILITY,
    ResearchTrialHandler,
    dispatch_trial,
    trial_job_id,
)
from omp_work.jobs.store import NativeJobStore
from omp_work.jobs.worker import JobWorker, WorkerConfig
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
    SetResearchCampaignStateCommand,
    SetResearchCampaignStatePayload,
)

__all__ = ["run_research_campaign"]


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
    worker.start()
    trials: list[dict[str, object]] = []
    verdicts: list[tuple[str, str]] = []
    for trial in definition.trials:
        trial_id = uuid4()
        client.execute(_envelope(workspace_id, ProposeResearchTrialCommand(
            type="propose_research_trial",
            payload=ProposeResearchTrialPayload(
                trial_id=trial_id, campaign_id=campaign_id, work_id=work_id,
                decision_id=uuid4(), action="evaluate",
                candidate_digest=trial.candidate_digest,
                experiment_spec_sha256=setup.spec_sha256,
                evaluator_sha256=setup.evaluator_by_digest[trial.candidate_digest],
                environment_sha256=setup.environment_sha256,
                input_manifest_sha256=setup.compatibility_sha256,
                policy_sha256=setup.policy_sha256,
            ),
        )))
        dispatch_trial(
            store, operation_id=str(uuid4()), workspace_id=workspace_id,
            actor_id=actor_id, work_id=work_id, trial_id=trial_id,
            resources={"cpu": 1, "memory_mib": 0, "gpu": 0, "model_calls": 0},
            lease_seconds=60,
        )
        job_id = trial_job_id(trial_id)
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
            definition.criteria[0].metric,
        )
        verdicts.append(score_metrics(
            completion.metrics, definition.criteria, completion.execution_status,
        ))
        trials.append({
            "trial_id": str(trial_id),
            "execution_status": completion.execution_status,
            "metrics": {
                name: float(value) for name, value in completion.metrics.items()
            },
        })
    outcome, reason = score_campaign(verdicts)
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
    return {
        "campaign_id": str(campaign_id), "state": "concluded",
        "outcome": outcome, "reason": reason, "trials": trials,
    }

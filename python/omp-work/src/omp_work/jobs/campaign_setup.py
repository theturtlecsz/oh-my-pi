"""Register content-addressed components and prepare a research campaign."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import UUID, uuid4

from omp_work.jobs.campaign_definition import CampaignDefinition
from omp_work.operations.fingerprints import code_fingerprint
from omp_work.v1.canonical import sha256
from omp_work.v1.client import WorkClient
from omp_work.v1.models import (
    CommandEnvelope,
    RegisterResearchComponentCommand,
    RegisterResearchComponentPayload,
    ResearchCampaignSpec,
    ResearchCompatibilityManifest,
    ResearchComponentDescriptor,
    ResearchComponentKind,
    ResearchResourceVector,
)

__all__ = ["CampaignSetup", "prepare_campaign"]


@dataclass(frozen=True, slots=True)
class CampaignSetup:
    spec: ResearchCampaignSpec
    spec_sha256: str
    compatibility: ResearchCompatibilityManifest
    compatibility_sha256: str
    worker_sha256: str
    policy_sha256: str
    environment_sha256: str
    evaluator_by_digest: dict[str, str]


def _register(
    client: WorkClient,
    workspace_id: UUID,
    kind: ResearchComponentKind,
    name: str,
    artifact_sha256: str,
    *,
    capabilities: tuple[str, ...] = (),
) -> str:
    descriptor = ResearchComponentDescriptor(
        contract_version="research-component.v1",
        kind=kind,
        name=name,
        version="1",
        artifact_sha256=artifact_sha256,
        capabilities=capabilities,
    )
    digest = sha256(descriptor.model_dump(mode="json"))
    client.execute(
        CommandEnvelope(
            api_version="work.omp.dev/v1",
            workspace_id=workspace_id,
            operation_id=uuid4(),
            request_id=uuid4(),
            correlation_id=uuid4(),
            command=RegisterResearchComponentCommand(
                type="register_research_component",
                payload=RegisterResearchComponentPayload(
                    component_sha256=digest, descriptor=descriptor,
                ),
            ),
        )
    )
    return digest


def prepare_campaign(
    client: WorkClient,
    workspace_id: UUID,
    definition: CampaignDefinition,
    *,
    harness_dir: Path,
) -> CampaignSetup:
    """Prepare draft/admission payloads without creating or admitting a campaign.

    Registrations accept service replay. Harnesses come from the definition's
    cached bytes, never from a second read of their source paths.
    """
    worker = _register(
        client, workspace_id, ResearchComponentKind.WORKER,
        "research-run-worker", code_fingerprint(), capabilities=("research.trial",),
    )
    policy = _register(
        client, workspace_id, ResearchComponentKind.POLICY,
        "research-run-policy", sha256({"component": "research-run:policy"}),
    )
    environment = _register(
        client, workspace_id, ResearchComponentKind.ENVIRONMENT,
        "research-run-environment", sha256({"component": "research-run:environment"}),
    )
    harness_dir.mkdir(parents=True, exist_ok=True)
    evaluators: dict[str, str] = {}
    for trial in definition.trials:
        digest = trial.candidate_digest
        if digest in evaluators:
            continue
        (harness_dir / digest).write_bytes(trial.harness_bytes)
        evaluators[digest] = _register(
            client, workspace_id, ResearchComponentKind.EVALUATOR,
            f"research-run-evaluator-{digest}", digest,
        )

    spec = ResearchCampaignSpec(
        objective=definition.objective,
        evaluation_protocol_id="research-run",
        evaluation_protocol_sha256=sha256([asdict(c) for c in definition.criteria]),
        resource_policy_ref="res-policy-standard",
        resource_vector=ResearchResourceVector(
            cpu_seconds=60, gpu_seconds=0, memory_mib=512,
            max_wall_seconds=definition.timeout_seconds, model_calls=0,
            input_tokens=0, output_tokens=0, retrieval_requests=0,
        ),
        authorized_data_classification=("internal",),
        candidate_mapping_policy="strict_hash_match",
    )
    compatibility = ResearchCompatibilityManifest(
        contract_version="research-compatibility.v1",
        workers=(worker,),
        evaluators=tuple(sorted(set(evaluators.values()))),
        environments=(environment,),
    )
    return CampaignSetup(
        spec=spec,
        spec_sha256=sha256(spec.model_dump(mode="json")),
        compatibility=compatibility,
        compatibility_sha256=sha256(compatibility.model_dump(mode="json")),
        worker_sha256=worker,
        policy_sha256=policy,
        environment_sha256=environment,
        evaluator_by_digest=evaluators,
    )

"""Register content-addressed components and prepare a research campaign."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import UUID, uuid4

from omp_work.jobs.campaign_definition import CampaignDefinition, definition_commitment
from omp_work.operations.fingerprints import code_fingerprint
from omp_work.v1.canonical import sha256
from omp_work.v1.client import WorkClient
from omp_work.v1.models import (
    CommandEnvelope,
    RegisterResearchComponentCommand,
    RegisterResearchComponentPayload,
    RegisterResearchArtifactCommand,
    RegisterResearchArtifactPayload,
    ResearchArtifactManifest,
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


@dataclass(frozen=True, slots=True)
class ManagedComponents:
    """Prepared managed bytes and commitment; no authority is created here."""

    commitment: str
    protocol: bytes
    evaluator: bytes
    contents: tuple[bytes, ...]


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
    managed: ManagedComponents | None = None,
) -> CampaignSetup:
    """Prepare draft/admission payloads without creating or admitting a campaign.

    Registrations accept service replay. Harnesses come from the definition's
    cached bytes, never from a second read of their source paths.
    """
    worker = _register(
        client, workspace_id, ResearchComponentKind.WORKER,
        "research-run-worker", code_fingerprint(), capabilities=("research.trial" if managed is None else "research.managed_trial",),
    )
    policy = _register(
        client, workspace_id, ResearchComponentKind.POLICY,
        "research-run-policy" if managed is None else "managed-experiment.v1",
        sha256({"component": "research-run:policy"}) if managed is None else managed.commitment,
    )
    environment = _register(
        client, workspace_id, ResearchComponentKind.ENVIRONMENT,
        "research-run-environment",
        sha256({"component": "research-run:environment"}) if managed is None else managed.commitment,
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
            f"research-run-evaluator-{digest}",
            digest if managed is None else hashlib.sha256(managed.evaluator).hexdigest(),
        )

    protocol_sha256 = (
        sha256([asdict(c) for c in definition.criteria])
        if definition.search is None
        else definition_commitment(definition)
    )
    if managed is not None:
        protocol_sha256 = hashlib.sha256(managed.protocol).hexdigest()
        for content in managed.contents:
            digest = hashlib.sha256(content).hexdigest()
            (harness_dir / digest).write_bytes(content)
            register_campaign_artifact(client, workspace_id, content, source_ref=f"generated-content:{digest}")
    spec = ResearchCampaignSpec(
        objective=definition.objective,
        evaluation_protocol_id="research-run" if managed is None else f"generated:{managed.commitment}",
        evaluation_protocol_sha256=protocol_sha256,
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


def register_campaign_artifact(
    client: WorkClient, workspace_id: UUID, content: bytes, *, source_ref: str,
) -> str:
    """Persist controller-owned campaign bytes through existing artifact custody."""
    digest = hashlib.sha256(content).hexdigest()
    manifest = ResearchArtifactManifest(
        contract_version="research-artifact.v1", artifact_sha256=digest,
        size_bytes=len(content), media_type="application/octet-stream",
        name=f"generated-campaign-{digest}", access_class="workspace",
        issuer_kind="legacy_autoresearch", source_ref=source_ref,
        valid_until=None,
    )
    client.execute(CommandEnvelope(
        api_version="work.omp.dev/v1", workspace_id=workspace_id,
        operation_id=uuid4(), request_id=uuid4(), correlation_id=uuid4(),
        command=RegisterResearchArtifactCommand(
            type="register_research_artifact",
            payload=RegisterResearchArtifactPayload(
                manifest_sha256=sha256(manifest.model_dump(mode="json")),
                manifest=manifest,
                content_base64=base64.b64encode(content).decode("ascii"),
            ),
        ),
    ))
    return digest

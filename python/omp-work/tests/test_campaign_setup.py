"""Campaign preparation uses replayable HTTP registration and cached harnesses."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from omp_work.jobs.campaign_definition import load_definition
from omp_work.jobs.campaign_setup import prepare_campaign
from omp_work.operations.fingerprints import code_fingerprint
from omp_work.v1.canonical import sha256
from omp_work.v1.client import WorkClient
from omp_work.v1.models import CommandEnvelope
from test_workflow_service import _create, _grant

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)


def test_setup_replays_snapshots_and_admits_changed_harness(service, tmp_path: Path) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "campaign setup")
    bearer = service.capabilities / f"campaign-{workspace_id}.json"
    bearer.write_text(json.dumps({
        "token": f"campaign-{workspace_id}",
        "actor_id": str(uuid4()),
        "actor_kind": "automation",
        "workspaces": [str(workspace_id)],
        "scopes": ["work.read", "work.execute", "work.approve"],
    }))
    bearer.chmod(0o600)
    harness = tmp_path / "trial.sh"
    original = b'echo "METRIC score=1"\n'
    changed = b'echo "METRIC score=2"\n'
    harness.write_bytes(original)
    other = tmp_path / "other.sh"
    other_bytes = b'echo "METRIC score=3"\n'
    other.write_bytes(other_bytes)
    other_digest = hashlib.sha256(other_bytes).hexdigest()
    criteria = [{"metric": "score", "op": "gte", "threshold": 1}]
    path = tmp_path / "definition.json"
    path.write_text(json.dumps({
        "objective": "Evaluate score", "domain": "machine_learning",
        "timeout_seconds": 17, "criteria": criteria,
        "trials": [
            {"harness": "trial.sh"}, {"harness": "other.sh"}, {"harness": "trial.sh"},
        ],
    }))
    definition = load_definition(path)
    harness.write_bytes(changed)
    harness_dir = tmp_path / "cached" / "harnesses"
    client = WorkClient(
        "http://testserver", workspace_id, bearer,
        transport=service.client._transport,
    )
    try:
        setup = prepare_campaign(client, workspace_id, definition, harness_dir=harness_dir)
        replay = prepare_campaign(client, workspace_id, definition, harness_dir=harness_dir)
        assert replay == setup
        digest = hashlib.sha256(original).hexdigest()
        assert (harness_dir / digest).read_bytes() == original
        assert set(setup.evaluator_by_digest) == {digest, other_digest}

        new_definition = load_definition(path)
        new_setup = prepare_campaign(
            client, workspace_id, new_definition, harness_dir=harness_dir,
        )
        new_digest = hashlib.sha256(changed).hexdigest()
        assert (harness_dir / new_digest).read_bytes() == changed
        assert new_setup.evaluator_by_digest[new_digest] != setup.evaluator_by_digest[digest]
        assert new_setup.worker_sha256 == setup.worker_sha256
        assert new_setup.policy_sha256 == setup.policy_sha256
        assert new_setup.environment_sha256 == setup.environment_sha256

        spec = setup.spec.model_dump(mode="json")
        assert spec == {
            "objective": definition.objective,
            "evaluation_protocol_id": "research-run",
            "evaluation_protocol_sha256": sha256(criteria),
            "resource_policy_ref": "res-policy-standard",
            "resource_vector": {
                "cpu_seconds": 60, "gpu_seconds": 0, "memory_mib": 512,
                "max_wall_seconds": 17, "model_calls": 0, "input_tokens": 0,
                "output_tokens": 0, "retrieval_requests": 0,
            },
            "authorized_data_classification": ["internal"],
            "candidate_mapping_policy": "strict_hash_match",
        }
        assert setup.spec_sha256 == sha256(spec)
        manifest = setup.compatibility.model_dump(mode="json")
        assert manifest == {
            "contract_version": "research-compatibility.v1",
            "workers": [setup.worker_sha256],
            "evaluators": sorted(set(setup.evaluator_by_digest.values())),
            "environments": [setup.environment_sha256], "audits": [], "releases": [],
        }
        assert setup.compatibility_sha256 == sha256(manifest)

        for prepared in (setup, new_setup):
            campaign_id = uuid4()
            spec = prepared.spec.model_dump(mode="json")
            manifest = prepared.compatibility.model_dump(mode="json")
            for command in [
                {
                    "type": "create_research_campaign",
                    "payload": {
                        "campaign_id": str(campaign_id), "work_id": item["work_id"],
                        "revision_id": item["revision_id"], "domain": definition.domain,
                        "spec": spec, "spec_sha256": prepared.spec_sha256,
                    },
                },
                {
                    "type": "admit_research_campaign",
                    "payload": {
                        "campaign_id": str(campaign_id), "work_id": item["work_id"],
                        "revision_id": item["revision_id"], "spec_sha256": prepared.spec_sha256,
                        "policy_sha256": prepared.policy_sha256, "compatibility": manifest,
                        "compatibility_sha256": prepared.compatibility_sha256,
                    },
                },
            ]:
                result = client.execute(CommandEnvelope.model_validate({
                    "api_version": "work.omp.dev/v1", "workspace_id": workspace_id,
                    "operation_id": uuid4(), "request_id": uuid4(),
                    "correlation_id": uuid4(), "command": command,
                }))
                assert result.result.status == "applied"
        view = client.research(item["key"])
        assert len(view.campaigns) == 2
        assert all(c.state == "admitted" for c in view.campaigns)
        components = {c.component_sha256: c.descriptor.model_dump(mode="json") for c in view.components}
        assert len(components) == 6  # Three shared components, three harness identities.
        for component_sha, descriptor in components.items():
            assert component_sha == sha256(descriptor)
            assert descriptor["contract_version"] == "research-component.v1"
            assert descriptor["version"] == "1"
            assert descriptor["roles"] == []
            kind = descriptor["kind"]
            if kind == "evaluator":
                artifact = descriptor["artifact_sha256"]
                assert descriptor["name"] == f"research-run-evaluator-{artifact}"
                assert artifact in {digest, new_digest, other_digest}
            else:
                assert descriptor["name"] == f"research-run-{kind}"
                assert descriptor["artifact_sha256"] == (
                    code_fingerprint() if kind == "worker"
                    else sha256({"component": f"research-run:{kind}"})
                )
            assert descriptor["capabilities"] == (["research.trial"] if kind == "worker" else [])
    finally:
        client.close()

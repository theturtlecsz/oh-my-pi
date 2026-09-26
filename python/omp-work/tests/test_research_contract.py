"""PostgreSQL and HTTP contract integration tests for research contract core (R02-S1).

Verifies durable campaigns, trials, untrusted observations, deliverable bindings,
RLS isolation, immutability, admission authority under work.approve,
and strict candidate-bounded read denial.
"""

from __future__ import annotations

import json
import os
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import UUID, uuid4

import psycopg
import pytest
from omp_work import contract_sha256
from omp_work.v1.canonical import sha256
from pg_native import native_postgres
from test_workflow_service import (
    OWNER,
    _command,
    _create,
    _grant,
    _owner_headers,
    _receipt,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)


def _sample_spec() -> tuple[dict[str, object], str]:
    spec = {
        "objective": "Evaluate candidate architectures for prompt optimization",
        "evaluation_protocol_id": "eval-proto-001",
        "evaluation_protocol_sha256": "e" * 64,
        "resource_policy_ref": "res-policy-standard",
        "resource_vector": {
            "cpu_seconds": 1800,
            "gpu_seconds": 600,
            "max_wall_seconds": 3600,
            "memory_mib": 8192,
            "model_calls": 100,
            "input_tokens": 100000,
            "output_tokens": 50000,
            "retrieval_requests": 0,
        },
        "authorized_data_classification": ["internal", "eval_tier1"],
        "candidate_mapping_policy": "strict_hash_match",
    }
    return spec, sha256(spec)


def _register_component(
    service,
    workspace_id: UUID,
    kind: str,
    *,
    name: str = "test-comp",
    version: str = "1",
    roles: tuple[str, ...] = (),
    capabilities: tuple[str, ...] = (),
) -> str:
    art = sha256({"component": f"{name}:{version}:{kind}"})
    descriptor = {
        "contract_version": "research-component.v1",
        "kind": kind,
        "name": name,
        "version": version,
        "artifact_sha256": art,
        "roles": sorted(list(roles)),
        "capabilities": sorted(list(capabilities)),
    }
    comp_sha = sha256(descriptor)
    cmd = {
        "type": "register_research_component",
        "payload": {
            "component_sha256": comp_sha,
            "descriptor": descriptor,
        },
    }
    status, body = _command(service, workspace_id, cmd)
    assert status == 200, body
    return comp_sha


def _manifest(
    *,
    evaluators: tuple[str, ...] = (),
    environments: tuple[str, ...] = (),
    workers: tuple[str, ...] = (),
    audits: tuple[str, ...] = (),
    releases: tuple[str, ...] = (),
) -> tuple[dict[str, object], str]:
    manifest = {
        "contract_version": "research-compatibility.v1",
        "workers": sorted(list(set(workers))),
        "evaluators": sorted(list(set(evaluators))),
        "audits": sorted(list(set(audits))),
        "releases": sorted(list(set(releases))),
        "environments": sorted(list(set(environments))),
    }
    digest = sha256(manifest)
    return manifest, digest


def _setup_research_fixtures(
    service, workspace_id: UUID
) -> tuple[str, str, str, dict[str, object], str]:
    policy_sha = _register_component(
        service, workspace_id, "policy", name="std-policy"
    )
    evaluator_sha = _register_component(
        service, workspace_id, "evaluator", name="std-evaluator"
    )
    environment_sha = _register_component(
        service, workspace_id, "environment", name="std-environment"
    )
    manifest, manifest_sha = _manifest(
        evaluators=(evaluator_sha,), environments=(environment_sha,)
    )
    return policy_sha, evaluator_sha, environment_sha, manifest, manifest_sha


def _admit_payload(
    campaign_id: UUID | str,
    work_id: UUID | str,
    revision_id: UUID | str,
    spec_sha256: str,
    policy_sha256: str,
    compatibility: dict[str, object],
    compatibility_sha256: str,
) -> dict[str, object]:
    return {
        "campaign_id": str(campaign_id),
        "work_id": str(work_id),
        "revision_id": str(revision_id),
        "spec_sha256": spec_sha256,
        "policy_sha256": policy_sha256,
        "compatibility": compatibility,
        "compatibility_sha256": compatibility_sha256,
    }


def _trial_payload(
    campaign_id: UUID | str,
    work_id: UUID | str,
    policy_sha256: str,
    evaluator_sha256: str,
    environment_sha256: str,
    *,
    trial_id: UUID | str | None = None,
    decision_id: UUID | str | None = None,
    candidate_digest: str = "c" * 64,
    experiment_spec_sha256: str = "e" * 64,
    input_manifest_sha256: str = "d" * 64,
    action: str = "evaluate",
) -> dict[str, object]:
    return {
        "trial_id": str(trial_id or uuid4()),
        "campaign_id": str(campaign_id),
        "work_id": str(work_id),
        "decision_id": str(decision_id or uuid4()),
        "action": action,
        "candidate_digest": candidate_digest,
        "experiment_spec_sha256": experiment_spec_sha256,
        "evaluator_sha256": evaluator_sha256,
        "environment_sha256": environment_sha256,
        "input_manifest_sha256": input_manifest_sha256,
        "policy_sha256": policy_sha256,
    }


def _plan_and_finalize(
    service,
    workspace_id: UUID,
    item: dict,
    candidate_hash: str = "c" * 64,
    commit_sha: str = "f" * 40,
) -> UUID:
    plan_candidate_hash = secrets.token_hex(32)
    receipt = _receipt(
        item["work_id"],
        item["revision_id"],
        str(uuid4()),
        "plan",
        body={"body": "## Approach\n1. do it\n\n## Verification\n1. prove it"},
        candidate_sha256=plan_candidate_hash,
    )
    status, body = _command(
        service,
        workspace_id,
        {"type": "append_evidence", "payload": {"receipt": receipt}},
    )
    assert status == 200, body
    planned = body["result"]["receipt"]["candidate_id"]

    final_id = str(uuid4())
    payload = {
        "work_id": item["work_id"],
        "revision_id": item["revision_id"],
        "planned_candidate_id": planned,
        "candidate_id": final_id,
        "candidate_sha256": candidate_hash,
        "commit_sha": commit_sha,
    }
    status, body = _command(
        service, workspace_id, {"type": "finalize_candidate", "payload": payload}
    )
    assert status == 200, body
    return UUID(final_id)


def _revise(
    service,
    workspace_id: UUID,
    work_id: str | UUID,
    expected_rev_id: str | UUID,
    rev_num: int,
    title: str,
) -> UUID:
    new_rev_id = str(uuid4())
    payload = {
        "work_id": str(work_id),
        "expected_revision_id": str(expected_rev_id),
        "revision": {
            "revision_id": new_rev_id,
            "work_id": str(work_id),
            "revision_number": rev_num,
            "title": title,
            "description": f"{title} description",
            "scope": "component:test",
            "acceptance_criteria": ["AC-1"],
            "content_sha256": sha256({"title": title, "description": f"{title} description"}),
            "created_by": "owner",
            "created_at": datetime.now(UTC).isoformat(),
        },
    }
    status, body = _command(
        service, workspace_id, {"type": "revise_work", "payload": payload}
    )
    assert status == 200, body
    return UUID(new_rev_id)


def test_campaign_creation_and_admission_lifecycle(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "research target")
    work_id = UUID(item["work_id"])
    revision_id = UUID(item["revision_id"])

    campaign_id = uuid4()
    spec, spec_sha = _sample_spec()
    policy_sha, evaluator_sha, environment_sha, manifest, manifest_sha = (
        _setup_research_fixtures(service, workspace_id)
    )

    # 1. Create draft campaign
    create_cmd = {
        "type": "create_research_campaign",
        "payload": {
            "campaign_id": str(campaign_id),
            "work_id": str(work_id),
            "revision_id": str(revision_id),
            "domain": "machine_learning",
            "spec": spec,
            "spec_sha256": spec_sha,
        },
    }
    status, body = _command(service, workspace_id, create_cmd)
    assert status == 200, body
    assert body["result"]["status"] == "applied"
    camp = body["result"]["campaign"]
    assert camp["campaign_id"] == str(campaign_id)
    assert camp["state"] == "draft"
    assert camp["spec_sha256"] == spec_sha
    assert camp["policy_sha256"] is None

    # 2. Replay create campaign (idempotent)
    status, replay = _command(service, workspace_id, create_cmd)
    assert status == 200, replay
    assert replay["result"]["status"] == "replayed"
    assert replay["result"]["campaign"]["campaign_id"] == str(campaign_id)

    # 3. Conflicting create campaign (same campaign_id, different domain/spec)
    forged_cmd = {
        "type": "create_research_campaign",
        "payload": {
            "campaign_id": str(campaign_id),
            "work_id": str(work_id),
            "revision_id": str(revision_id),
            "domain": "engineering",
            "spec": spec,
            "spec_sha256": spec_sha,
        },
    }
    status, body = _command(service, workspace_id, forged_cmd)
    assert status == 409, body
    assert body["error"]["code"] == "idempotency_conflict"

    # 4. Admit campaign
    admit_cmd = {
        "type": "admit_research_campaign",
        "payload": _admit_payload(
            campaign_id,
            work_id,
            revision_id,
            spec_sha,
            policy_sha,
            manifest,
            manifest_sha,
        ),
    }
    status, body = _command(service, workspace_id, admit_cmd)
    assert status == 200, body
    assert body["result"]["status"] == "applied"
    camp = body["result"]["campaign"]
    assert camp["state"] == "admitted"
    assert camp["policy_sha256"] == policy_sha
    assert camp["compatibility_sha256"] == manifest_sha
    assert camp["compatibility"] == manifest
    assert camp["admitted_at"] is not None

    # 5. Replay admit campaign
    status, replay = _command(service, workspace_id, admit_cmd)
    assert status == 200, replay
    assert replay["result"]["status"] == "replayed"

    # 6. Read research view
    res = service.client.get(
        f"/v1/work-items/{item['key']}/research", headers=_owner_headers(workspace_id)
    )
    assert res.status_code == 200, res.json()
    data = res.json()
    assert data["work_id"] == str(work_id)
    assert len(data["campaigns"]) == 1
    assert data["campaigns"][0]["campaign_id"] == str(campaign_id)
    assert data["campaigns"][0]["state"] == "admitted"
    assert data["campaigns"][0]["compatibility_sha256"] == manifest_sha
    assert len(data["components"]) == 3


def test_stale_campaign_revision_and_drift_refusal(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "drift target")
    work_id = UUID(item["work_id"])
    rev1 = UUID(item["revision_id"])

    # Revise work item to rev2
    rev2 = _revise(service, workspace_id, work_id, rev1, 2, "drift target v2")
    assert rev1 != rev2

    spec, spec_sha = _sample_spec()
    policy_sha, _, _, manifest, manifest_sha = _setup_research_fixtures(
        service, workspace_id
    )

    # Attempt create on old rev1 -> stale_evidence
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "create_research_campaign",
            "payload": {
                "campaign_id": str(uuid4()),
                "work_id": str(work_id),
                "revision_id": str(rev1),
                "domain": "machine_learning",
                "spec": spec,
                "spec_sha256": spec_sha,
            },
        },
    )
    assert status == 409, body
    assert body["error"]["code"] == "stale_evidence"

    # Create campaign on rev2
    camp_id = uuid4()
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "create_research_campaign",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "revision_id": str(rev2),
                "domain": "machine_learning",
                "spec": spec,
                "spec_sha256": spec_sha,
            },
        },
    )
    assert status == 200, body

    # Revise work item to rev3
    rev3 = _revise(service, workspace_id, work_id, rev2, 3, "drift target v3")
    assert rev2 != rev3

    # Attempt to admit camp_id (created on rev2) -> stale_evidence
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "admit_research_campaign",
            "payload": _admit_payload(
                camp_id,
                work_id,
                rev2,
                spec_sha,
                policy_sha,
                manifest,
                manifest_sha,
            ),
        },
    )
    assert status == 409, body
    assert body["error"]["code"] == "stale_evidence"


def test_spec_digest_mismatch_and_evaluation_identity_refusal(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "digest test")
    work_id = UUID(item["work_id"])
    rev_id = UUID(item["revision_id"])

    spec, _ = _sample_spec()
    wrong_spec_sha = "9" * 64

    # Mismatching spec digest in create -> stale_evidence
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "create_research_campaign",
            "payload": {
                "campaign_id": str(uuid4()),
                "work_id": str(work_id),
                "revision_id": str(rev_id),
                "domain": "machine_learning",
                "spec": spec,
                "spec_sha256": wrong_spec_sha,
            },
        },
    )
    assert status == 409, body
    assert body["error"]["code"] == "stale_evidence"


def test_draft_or_cancelled_campaign_cannot_accept_trial(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "trial acceptance test")
    work_id = UUID(item["work_id"])
    rev_id = UUID(item["revision_id"])

    camp_id = uuid4()
    spec, spec_sha = _sample_spec()
    policy_sha, evaluator_sha, environment_sha, manifest, manifest_sha = (
        _setup_research_fixtures(service, workspace_id)
    )

    # Create draft campaign
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "create_research_campaign",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "revision_id": str(rev_id),
                "domain": "machine_learning",
                "spec": spec,
                "spec_sha256": spec_sha,
            },
        },
    )
    assert status == 200, body

    trial_id = uuid4()
    trial_payload = _trial_payload(
        camp_id,
        work_id,
        policy_sha,
        evaluator_sha,
        environment_sha,
        trial_id=trial_id,
    )

    # Propose trial on draft campaign -> invalid_request
    status, body = _command(
        service,
        workspace_id,
        {"type": "propose_research_trial", "payload": trial_payload},
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"
    # Cancel draft campaign
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "cancel_research_campaign",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "reason": "abandoning",
            },
        },
    )
    assert status == 200, body
    assert body["result"]["campaign"]["state"] == "cancelled"

    # Propose trial on cancelled campaign -> invalid_request
    status, body = _command(
        service,
        workspace_id,
        {"type": "propose_research_trial", "payload": trial_payload},
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"

    # Observation on cancelled campaign -> invalid_request
    obs_payload = {
        "observation_id": str(uuid4()),
        "campaign_id": str(camp_id),
        "trial_id": None,
        "issuer_kind": "candidate_authored",
        "source_ref": "obs/1",
        "execution_status": "completed",
        "payload": {"result": 1},
        "payload_sha256": sha256({"result": 1}),
        "observed_at": datetime.now(UTC).isoformat(),
    }
    status, body = _command(
        service,
        workspace_id,
        {"type": "record_research_observation", "payload": obs_payload},
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"

    # Cannot admit cancelled campaign -> invalid_request
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "admit_research_campaign",
            "payload": _admit_payload(
                camp_id,
                work_id,
                rev_id,
                spec_sha,
                policy_sha,
                manifest,
                manifest_sha,
            ),
        },
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"


def test_cancellation_archives_proposed_trials_atomically(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "cancellation atomicity test")
    work_id = UUID(item["work_id"])
    rev_id = UUID(item["revision_id"])

    camp_id = uuid4()
    spec, spec_sha = _sample_spec()
    policy_sha, evaluator_sha, environment_sha, manifest, manifest_sha = (
        _setup_research_fixtures(service, workspace_id)
    )

    # Create & admit campaign
    _command(
        service,
        workspace_id,
        {
            "type": "create_research_campaign",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "revision_id": str(rev_id),
                "domain": "machine_learning",
                "spec": spec,
                "spec_sha256": spec_sha,
            },
        },
    )
    _command(
        service,
        workspace_id,
        {
            "type": "admit_research_campaign",
            "payload": _admit_payload(
                camp_id,
                work_id,
                rev_id,
                spec_sha,
                policy_sha,
                manifest,
                manifest_sha,
            ),
        },
    )

    # Propose 2 trials
    t1_id, t2_id = uuid4(), uuid4()
    for tid in (t1_id, t2_id):
        status, body = _command(
            service,
            workspace_id,
            {
                "type": "propose_research_trial",
                "payload": _trial_payload(
                    camp_id,
                    work_id,
                    policy_sha,
                    evaluator_sha,
                    environment_sha,
                    trial_id=tid,
                ),
            },
        )
        assert status == 200, body
        assert body["result"]["trial"]["state"] == "proposed"

    # Cancel campaign
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "cancel_research_campaign",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "reason": "campaign cancelled by operator",
            },
        },
    )
    assert status == 200, body
    assert body["result"]["campaign"]["state"] == "cancelled"

    # Replay cancel campaign
    status, replay = _command(
        service,
        workspace_id,
        {
            "type": "cancel_research_campaign",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "reason": "campaign cancelled by operator",
            },
        },
    )
    assert status == 200, replay
    assert replay["result"]["status"] == "replayed"

    # Verify both trials are atomically archived
    res = service.client.get(
        f"/v1/work-items/{item['key']}/research", headers=_owner_headers(workspace_id)
    )
    assert res.status_code == 200
    trials = res.json()["trials"]
    assert len(trials) == 2
    for t in trials:
        assert t["state"] == "archived"
        assert t["archived_reason"] == "campaign_cancelled"
        assert t["archived_at"] is not None


def test_trial_policy_mismatch_and_decision_idempotency(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "trial policy test")
    work_id = UUID(item["work_id"])
    rev_id = UUID(item["revision_id"])

    camp_id = uuid4()
    spec, spec_sha = _sample_spec()
    policy_sha, evaluator_sha, environment_sha, manifest, manifest_sha = (
        _setup_research_fixtures(service, workspace_id)
    )

    _command(
        service,
        workspace_id,
        {
            "type": "create_research_campaign",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "revision_id": str(rev_id),
                "domain": "machine_learning",
                "spec": spec,
                "spec_sha256": spec_sha,
            },
        },
    )
    _command(
        service,
        workspace_id,
        {
            "type": "admit_research_campaign",
            "payload": _admit_payload(
                camp_id,
                work_id,
                rev_id,
                spec_sha,
                policy_sha,
                manifest,
                manifest_sha,
            ),
        },
    )

    trial_id = uuid4()
    decision_id = uuid4()

    # Mismatching policy_sha256 on trial -> stale_evidence
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "propose_research_trial",
            "payload": _trial_payload(
                camp_id,
                work_id,
                "0" * 64,
                evaluator_sha,
                environment_sha,
                trial_id=trial_id,
                decision_id=decision_id,
            ),
        },
    )
    assert status == 409, body
    assert body["error"]["code"] == "stale_evidence"

    # Valid propose trial
    valid_payload = _trial_payload(
        camp_id,
        work_id,
        policy_sha,
        evaluator_sha,
        environment_sha,
        trial_id=trial_id,
        decision_id=decision_id,
    )
    status, body = _command(
        service,
        workspace_id,
        {"type": "propose_research_trial", "payload": valid_payload},
    )
    assert status == 200, body
    assert body["result"]["status"] == "applied"

    # Replay propose trial
    status, replay = _command(
        service,
        workspace_id,
        {"type": "propose_research_trial", "payload": valid_payload},
    )
    assert status == 200, replay
    assert replay["result"]["status"] == "replayed"

    # Reusing same decision_id for different trial -> idempotency_conflict
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "propose_research_trial",
            "payload": {
                **valid_payload,
                "trial_id": str(uuid4()),
                "candidate_digest": "d" * 64,
            },
        },
    )
    assert status == 409, body
    assert body["error"]["code"] == "idempotency_conflict"


def test_observation_recording_immutability_and_conflict(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "observation test")
    work_id = UUID(item["work_id"])
    rev_id = UUID(item["revision_id"])

    camp_id = uuid4()
    spec, spec_sha = _sample_spec()
    policy_sha, _, _, manifest, manifest_sha = _setup_research_fixtures(
        service, workspace_id
    )

    _command(
        service,
        workspace_id,
        {
            "type": "create_research_campaign",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "revision_id": str(rev_id),
                "domain": "machine_learning",
                "spec": spec,
                "spec_sha256": spec_sha,
            },
        },
    )
    _command(
        service,
        workspace_id,
        {
            "type": "admit_research_campaign",
            "payload": _admit_payload(
                camp_id,
                work_id,
                rev_id,
                spec_sha,
                policy_sha,
                manifest,
                manifest_sha,
            ),
        },
    )

    obs_id = uuid4()
    obs_payload_data = {"loss": 0.123, "epoch": 10}
    correct_hash = sha256(obs_payload_data)

    # 1. Payload digest mismatch -> stale_evidence
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "record_research_observation",
            "payload": {
                "observation_id": str(obs_id),
                "campaign_id": str(camp_id),
                "trial_id": None,
                "issuer_kind": "candidate_authored",
                "source_ref": "epoch-10",
                "execution_status": "completed",
                "payload": obs_payload_data,
                "payload_sha256": "0" * 64,
                "observed_at": datetime.now(UTC).isoformat(),
            },
        },
    )
    assert status == 409, body
    assert body["error"]["code"] == "stale_evidence"

    # 2. Applied observation
    valid_obs = {
        "observation_id": str(obs_id),
        "campaign_id": str(camp_id),
        "trial_id": None,
        "issuer_kind": "candidate_authored",
        "source_ref": "epoch-10",
        "execution_status": "completed",
        "payload": obs_payload_data,
        "payload_sha256": correct_hash,
        "observed_at": datetime.now(UTC).isoformat(),
    }
    status, body = _command(
        service,
        workspace_id,
        {"type": "record_research_observation", "payload": valid_obs},
    )
    assert status == 200, body
    assert body["result"]["status"] == "applied"
    assert body["result"]["observation"]["observation_id"] == str(obs_id)

    # 3. Replay observation
    status, replay = _command(
        service,
        workspace_id,
        {"type": "record_research_observation", "payload": valid_obs},
    )
    assert status == 200, replay
    assert replay["result"]["status"] == "replayed"

    # 4. Same (workspace, issuer_kind, source_ref) with different payload -> conflict
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "record_research_observation",
            "payload": {
                "observation_id": str(uuid4()),
                "campaign_id": str(camp_id),
                "trial_id": None,
                "issuer_kind": "candidate_authored",
                "source_ref": "epoch-10",
                "execution_status": "completed",
                "payload": {"loss": 0.999},
                "payload_sha256": sha256({"loss": 0.999}),
                "observed_at": datetime.now(UTC).isoformat(),
            },
        },
    )
    assert status == 409, body
    assert body["error"]["code"] == "idempotency_conflict"

    # 5. Direct SQL UPDATE/DELETE triggers reject_immutable()
    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        with pytest.raises(psycopg.Error, match="reject_immutable"):
            conn.execute(
                "UPDATE omp_research.observations SET execution_status='crashed' WHERE observation_id=%s",
                (obs_id,),
            )
        with pytest.raises(psycopg.Error, match="reject_immutable"):
            conn.execute(
                "DELETE FROM omp_research.observations WHERE observation_id=%s",
                (obs_id,),
            )


def test_observation_issuer_kinds_and_execution_status(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "issuer kind test")
    work_id = UUID(item["work_id"])
    rev_id = UUID(item["revision_id"])

    camp_id = uuid4()
    spec, spec_sha = _sample_spec()
    policy_sha, _, _, manifest, manifest_sha = _setup_research_fixtures(
        service, workspace_id
    )

    _command(
        service,
        workspace_id,
        {
            "type": "create_research_campaign",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "revision_id": str(rev_id),
                "domain": "literature",
                "spec": spec,
                "spec_sha256": spec_sha,
            },
        },
    )
    _command(
        service,
        workspace_id,
        {
            "type": "admit_research_campaign",
            "payload": _admit_payload(
                camp_id,
                work_id,
                rev_id,
                spec_sha,
                policy_sha,
                manifest,
                manifest_sha,
            ),
        },
    )

    # Trusted evaluator issuer kind is refused (not in schema)
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "record_research_observation",
            "payload": {
                "observation_id": str(uuid4()),
                "campaign_id": str(camp_id),
                "trial_id": None,
                "issuer_kind": "trusted_evaluator",
                "source_ref": "eval/1",
                "execution_status": "completed",
                "payload": {"score": 1.0},
                "payload_sha256": sha256({"score": 1.0}),
                "observed_at": datetime.now(UTC).isoformat(),
            },
        },
    )
    assert status == 400, body

    # Scientific success status (like 'passed' or 'accepted') is refused
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "record_research_observation",
            "payload": {
                "observation_id": str(uuid4()),
                "campaign_id": str(camp_id),
                "trial_id": None,
                "issuer_kind": "legacy_autoresearch",
                "source_ref": "legacy/run-1",
                "execution_status": "passed",
                "payload": {"val": 1},
                "payload_sha256": sha256({"val": 1}),
                "observed_at": datetime.now(UTC).isoformat(),
            },
        },
    )
    assert status == 400, body

    # Legacy autoresearch observation is recorded and read without evidence/receipt authority
    legacy_obs_id = uuid4()
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "record_research_observation",
            "payload": {
                "observation_id": str(legacy_obs_id),
                "campaign_id": str(camp_id),
                "trial_id": None,
                "issuer_kind": "legacy_autoresearch",
                "source_ref": "legacy/run-1",
                "execution_status": "completed",
                "payload": {"raw_score": 42},
                "payload_sha256": sha256({"raw_score": 42}),
                "observed_at": datetime.now(UTC).isoformat(),
            },
        },
    )
    assert status == 200, body
    assert body["result"]["status"] == "applied"

    res = service.client.get(
        f"/v1/work-items/{item['key']}/research", headers=_owner_headers(workspace_id)
    )
    assert res.status_code == 200
    observations = res.json()["observations"]
    assert len(observations) == 1
    assert observations[0]["issuer_kind"] == "legacy_autoresearch"


def test_deliverable_binding_invariants(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "binding test target")
    work_id = UUID(item["work_id"])
    rev_id = UUID(item["revision_id"])

    camp_id = uuid4()
    spec, spec_sha = _sample_spec()
    policy_sha, evaluator_sha, environment_sha, manifest, manifest_sha = (
        _setup_research_fixtures(service, workspace_id)
    )

    _command(
        service,
        workspace_id,
        {
            "type": "create_research_campaign",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "revision_id": str(rev_id),
                "domain": "machine_learning",
                "spec": spec,
                "spec_sha256": spec_sha,
            },
        },
    )
    _command(
        service,
        workspace_id,
        {
            "type": "admit_research_campaign",
            "payload": _admit_payload(
                camp_id,
                work_id,
                rev_id,
                spec_sha,
                policy_sha,
                manifest,
                manifest_sha,
            ),
        },
    )

    trial_id = uuid4()
    candidate_digest = "c" * 64
    _command(
        service,
        workspace_id,
        {
            "type": "propose_research_trial",
            "payload": _trial_payload(
                camp_id,
                work_id,
                policy_sha,
                evaluator_sha,
                environment_sha,
                trial_id=trial_id,
                candidate_digest=candidate_digest,
            ),
        },
    )

    # 1. Planned candidate only (not final) -> stale_evidence
    receipt = _receipt(
        str(work_id),
        str(rev_id),
        str(uuid4()),
        "plan",
        body={"body": "## Approach\n1. plan\n\n## Verification\n1. ver"},
        candidate_sha256=secrets.token_hex(32),
    )
    status, body = _command(
        service,
        workspace_id,
        {"type": "append_evidence", "payload": {"receipt": receipt}},
    )
    assert status == 200, body
    planned_cand_id = UUID(body["result"]["receipt"]["candidate_id"])

    binding_ident = {
        "candidate_digest": candidate_digest,
        "campaign_id": str(camp_id),
        "native_candidate_id": str(planned_cand_id),
        "revision_id": str(rev_id),
        "trial_id": str(trial_id),
        "work_id": str(work_id),
        "workspace_id": str(workspace_id),
    }
    binding_sha = sha256(binding_ident)

    status, body = _command(
        service,
        workspace_id,
        {
            "type": "bind_research_deliverable",
            "payload": {
                "trial_id": str(trial_id),
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "revision_id": str(rev_id),
                "candidate_digest": candidate_digest,
                "native_candidate_id": str(planned_cand_id),
                "binding_sha256": binding_sha,
            },
        },
    )
    assert status == 409, body
    assert body["error"]["code"] == "stale_evidence"

    # Finalize candidate
    final_cand_id = str(uuid4())
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "finalize_candidate",
            "payload": {
                "work_id": str(work_id),
                "revision_id": str(rev_id),
                "planned_candidate_id": str(planned_cand_id),
                "candidate_id": final_cand_id,
                "candidate_sha256": candidate_digest,
                "commit_sha": "a" * 40,
            },
        },
    )
    assert status == 200, body

    binding_ident["native_candidate_id"] = final_cand_id
    binding_sha = sha256(binding_ident)

    # 3. Forged binding_sha256 -> stale_evidence
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "bind_research_deliverable",
            "payload": {
                "trial_id": str(trial_id),
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "revision_id": str(rev_id),
                "candidate_digest": candidate_digest,
                "native_candidate_id": final_cand_id,
                "binding_sha256": "0" * 64,
            },
        },
    )
    assert status == 409, body
    assert body["error"]["code"] == "stale_evidence"

    # 4. Valid deliverable binding
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "bind_research_deliverable",
            "payload": {
                "trial_id": str(trial_id),
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "revision_id": str(rev_id),
                "candidate_digest": candidate_digest,
                "native_candidate_id": final_cand_id,
                "binding_sha256": binding_sha,
            },
        },
    )
    assert status == 200, body
    assert body["result"]["status"] == "applied"
    binding = body["result"]["deliverable_binding"]
    assert binding["trial_id"] == str(trial_id)
    assert binding["native_candidate_id"] == final_cand_id

    # 5. Replay deliverable binding
    status, replay = _command(
        service,
        workspace_id,
        {
            "type": "bind_research_deliverable",
            "payload": {
                "trial_id": str(trial_id),
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "revision_id": str(rev_id),
                "candidate_digest": candidate_digest,
                "native_candidate_id": final_cand_id,
                "binding_sha256": binding_sha,
            },
        },
    )
    assert status == 200, replay
    assert replay["result"]["status"] == "replayed"

    # 6. Attempt SQL UPDATE/DELETE triggers reject_immutable()
    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        with pytest.raises(psycopg.Error, match="reject_immutable"):
            conn.execute(
                "UPDATE omp_research.deliverable_bindings SET candidate_digest='f' WHERE trial_id=%s",
                (trial_id,),
            )
        with pytest.raises(psycopg.Error, match="reject_immutable"):
            conn.execute(
                "DELETE FROM omp_research.deliverable_bindings WHERE trial_id=%s",
                (trial_id,),
            )


def test_candidate_bounded_read_denial(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "candidate bounded read item")
    final_id = _plan_and_finalize(service, workspace_id, item)

    reader = service.capabilities / "candidate-reader.json"
    reader.write_text(
        json.dumps(
            {
                "token": "candidate-reader-token",
                "actor_id": str(uuid4()),
                "actor_kind": "task-agent",
                "workspaces": [str(workspace_id)],
                "scopes": ["work.candidate.read"],
                "candidate_ids": [str(final_id)],
            }
        )
    )
    reader.chmod(0o600)

    headers = {
        "Authorization": "Bearer candidate-reader-token",
        "X-OMP-Workspace-ID": str(workspace_id),
        "X-OMP-Contract-SHA256": contract_sha256(),
    }

    # Allowed to read /workflow
    workflow = service.client.get(
        f"/v1/work-items/{item['key']}/workflow", headers=headers
    )
    assert workflow.status_code == 200

    # Denied access to /research (403 Forbidden)
    research = service.client.get(
        f"/v1/work-items/{item['key']}/research", headers=headers
    )
    assert research.status_code == 403


def test_migration_0025_preserves_historical_bytes_and_behavior(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OMP-R02 migration upgrade rehearsal: pre-0025 data is preserved and RLS functions cleanly."""
    import omp_work.operations.database as database_module
    from omp_work.operations.database import bootstrap
    from test_workflow_service import _config

    root = tmp_path_factory.mktemp("migration-0025-upgrade")
    config = _config(root)
    with native_postgres(root, config.port):
        original_migrate = database_module.migrate
        monkeypatch.setattr(database_module, "validate_bundle", lambda **kw: None)
        monkeypatch.setattr(
            database_module,
            "migrate",
            lambda cfg, **kw: original_migrate(cfg, target=24, **kw),
        )
        bootstrap(config)
        monkeypatch.setattr(database_module, "migrate", original_migrate)

        workspace_id = uuid4()
        work_id = uuid4()
        revision_id = uuid4()
        now = datetime.now(UTC)

        with psycopg.connect(
            **config.connection_kwargs("postgres"), autocommit=True
        ) as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO omp_control.workspaces(workspace_id) VALUES (%s)",
                (workspace_id,),
            )
            cur.execute(
                "INSERT INTO omp_work.work_items(work_id, workspace_id, state) VALUES (%s, %s, 'NOW')",
                (work_id, workspace_id),
            )
            cur.execute(
                "INSERT INTO omp_work.work_revisions(revision_id, work_id, workspace_id, revision_number, title, description, scope, content_sha256, created_by, supplied_at) "
                "VALUES (%s, %s, %s, 1, 'pre-0025 item', '', '', %s, 'test', %s)",
                (revision_id, work_id, workspace_id, "a" * 64, now),
            )
            cur.execute(
                "UPDATE omp_work.work_items SET current_revision_id=%s WHERE work_id=%s",
                (revision_id, work_id),
            )

        # Run migration 0025
        original_migrate(config, target=25)

        # Verify historical data is intact
        with psycopg.connect(**config.connection_kwargs("postgres")) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT work_id, state FROM omp_work.work_items WHERE work_id=%s",
                (work_id,),
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] == work_id
            assert row[1] == "NOW"

            cur.execute(
                "SELECT title, content_sha256 FROM omp_work.work_revisions WHERE revision_id=%s",
                (revision_id,),
            )
            rev_row = cur.fetchone()
            assert rev_row is not None
            assert rev_row[0] == "pre-0025 item"
            assert rev_row[1] == "a" * 64

            # Verify omp_research schema and tables exist
            cur.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'omp_research' ORDER BY table_name"
            )
            tables = [r[0] for r in cur.fetchall()]
            assert tables == [
                "campaigns",
                "deliverable_bindings",
                "observations",
                "trials",
            ]

            # Verify RLS is enabled and active on omp_research tables
            for t in tables:
                cur.execute(
                    f"SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname='{t}'"
                )
                rls_row = cur.fetchone()
                assert rls_row[0] is True, f"RLS not enabled on {t}"
                assert rls_row[1] is True, f"FORCE RLS not enabled on {t}"


def test_exact_campaign_and_cancellation_replay_conflicts(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "replay test target")
    work_id = UUID(item["work_id"])
    rev_id = UUID(item["revision_id"])

    campaign_id = uuid4()
    spec1, _ = _sample_spec()
    spec1["objective"] = "Initial objective"
    spec_sha1 = sha256(spec1)

    # 1. Create initial campaign
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "create_research_campaign",
            "payload": {
                "campaign_id": str(campaign_id),
                "work_id": str(work_id),
                "revision_id": str(rev_id),
                "domain": "machine_learning",
                "spec": spec1,
                "spec_sha256": spec_sha1,
            },
        },
    )
    assert status == 200 and body["result"]["status"] == "applied", body

    # Happy replay
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "create_research_campaign",
            "payload": {
                "campaign_id": str(campaign_id),
                "work_id": str(work_id),
                "revision_id": str(rev_id),
                "domain": "machine_learning",
                "spec": spec1,
                "spec_sha256": spec_sha1,
            },
        },
    )
    assert status == 200 and body["result"]["status"] == "replayed", body

    # Create replay with changed canonical spec (even if caller asserts the stored spec_sha1)
    spec2 = dict(spec1, objective="Changed objective")
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "create_research_campaign",
            "payload": {
                "campaign_id": str(campaign_id),
                "work_id": str(work_id),
                "revision_id": str(rev_id),
                "domain": "machine_learning",
                "spec": spec2,
                "spec_sha256": spec_sha1,
            },
        },
    )
    assert status == 409, body
    assert body["error"]["code"] == "idempotency_conflict"

    # New campaign with wrong self-digest is stale_evidence
    new_camp_id = uuid4()
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "create_research_campaign",
            "payload": {
                "campaign_id": str(new_camp_id),
                "work_id": str(work_id),
                "revision_id": str(rev_id),
                "domain": "machine_learning",
                "spec": spec1,
                "spec_sha256": "0" * 64,
            },
        },
    )
    assert status == 409, body
    assert body["error"]["code"] == "stale_evidence"

    # Cancellation replay checks
    # Cancel with initial reason
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "cancel_research_campaign",
            "payload": {
                "campaign_id": str(campaign_id),
                "work_id": str(work_id),
                "reason": "Initial cancel reason",
            },
        },
    )
    assert status == 200 and body["result"]["status"] == "applied", body

    # Replay cancel with identical reason
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "cancel_research_campaign",
            "payload": {
                "campaign_id": str(campaign_id),
                "work_id": str(work_id),
                "reason": "Initial cancel reason",
            },
        },
    )
    assert status == 200 and body["result"]["status"] == "replayed", body

    # Replay cancel with changed reason -> typed idempotency_conflict
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "cancel_research_campaign",
            "payload": {
                "campaign_id": str(campaign_id),
                "work_id": str(work_id),
                "reason": "Different cancel reason",
            },
        },
    )
    assert status == 409, body
    assert body["error"]["code"] == "idempotency_conflict"

    # Replay cancel with changed work -> typed idempotency_conflict (not generic invalid_request)
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "cancel_research_campaign",
            "payload": {
                "campaign_id": str(campaign_id),
                "work_id": str(uuid4()),
                "reason": "Initial cancel reason",
            },
        },
    )
    assert status == 409, body
    assert body["error"]["code"] == "idempotency_conflict"


def test_exact_observation_replay_timestamp_and_payload_conflicts(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "observation replay item")
    work_id = UUID(item["work_id"])
    rev_id = UUID(item["revision_id"])

    campaign_id = uuid4()
    spec, spec_sha = _sample_spec()
    _command(
        service,
        workspace_id,
        {
            "type": "create_research_campaign",
            "payload": {
                "campaign_id": str(campaign_id),
                "work_id": str(work_id),
                "revision_id": str(rev_id),
                "domain": "machine_learning",
                "spec": spec,
                "spec_sha256": spec_sha,
            },
        },
    )

    # 1. Naive observation timestamp is rejected at validation boundary (HTTP 400 invalid_request)
    obs_id = uuid4()
    payload_content = {"metric": 0.85, "step": 100}
    payload_sha = sha256(payload_content)
    naive_ts_payload = {
        "observation_id": str(obs_id),
        "campaign_id": str(campaign_id),
        "trial_id": None,
        "issuer_kind": "legacy_autoresearch",
        "source_ref": "legacy-run-101",
        "execution_status": "completed",
        "commit_sha": None,
        "payload": payload_content,
        "payload_sha256": payload_sha,
        "observed_at": "2026-09-17T12:00:00",
    }
    status, body = _command(
        service,
        workspace_id,
        {"type": "record_research_observation", "payload": naive_ts_payload},
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"

    # 2. Nullable campaign-level observation (trial_id is None) succeeds
    aware_ts = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC).isoformat()
    valid_obs_payload = dict(naive_ts_payload, observed_at=aware_ts)
    status, body = _command(
        service,
        workspace_id,
        {"type": "record_research_observation", "payload": valid_obs_payload},
    )
    assert status == 200 and body["result"]["status"] == "applied", body
    assert body["result"]["observation"]["trial_id"] is None

    # Happy replay
    status, body = _command(
        service,
        workspace_id,
        {"type": "record_research_observation", "payload": valid_obs_payload},
    )
    assert status == 200 and body["result"]["status"] == "replayed", body

    # 3. Observation replay with changed timestamp conflicts
    changed_ts = datetime(2026, 9, 17, 13, 0, 0, tzinfo=UTC).isoformat()
    changed_ts_payload = dict(valid_obs_payload, observed_at=changed_ts)
    status, body = _command(
        service,
        workspace_id,
        {"type": "record_research_observation", "payload": changed_ts_payload},
    )
    assert status == 409, body
    assert body["error"]["code"] == "idempotency_conflict"

    # 4. Observation replay with changed canonical payload conflicts even if caller reuses an asserted digest
    new_obs_id = uuid4()
    init_payload = {"run": "alpha", "score": 1}
    init_sha = sha256(init_payload)
    init_command = {
        "observation_id": str(new_obs_id),
        "campaign_id": str(campaign_id),
        "trial_id": None,
        "issuer_kind": "candidate_authored",
        "source_ref": "cand-obs-1",
        "execution_status": "completed",
        "commit_sha": "a" * 40,
        "payload": init_payload,
        "payload_sha256": init_sha,
        "observed_at": aware_ts,
    }
    status, body = _command(
        service,
        workspace_id,
        {"type": "record_research_observation", "payload": init_command},
    )
    assert status == 200 and body["result"]["status"] == "applied", body

    # Replay with changed payload content (score: 2) even if asserting the old digest
    mismatched_content_command = dict(
        init_command,
        payload={"run": "alpha", "score": 2},
        payload_sha256=init_sha,
    )
    status, body = _command(
        service,
        workspace_id,
        {"type": "record_research_observation", "payload": mismatched_content_command},
    )
    assert status == 409, body
    assert body["error"]["code"] == "idempotency_conflict"

    # New observation with wrong self-digest is stale_evidence
    bad_digest_obs_id = uuid4()
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "record_research_observation",
            "payload": {
                "observation_id": str(bad_digest_obs_id),
                "campaign_id": str(campaign_id),
                "trial_id": None,
                "issuer_kind": "candidate_authored",
                "source_ref": "cand-obs-bad-digest",
                "execution_status": "completed",
                "commit_sha": "a" * 40,
                "payload": {"metric": 1},
                "payload_sha256": "0" * 64,
                "observed_at": aware_ts,
            },
        },
    )
    assert status == 409, body
    assert body["error"]["code"] == "stale_evidence"


def test_cross_workspace_isolation_and_foreign_reference_rejection(service) -> None:
    ws_a = uuid4()
    ws_b = uuid4()
    _grant(service, ws_a)
    _grant(service, ws_b)

    item_a = _create(service, ws_a, "workspace A target")
    work_a = UUID(item_a["work_id"])
    rev_a = UUID(item_a["revision_id"])

    camp_a = uuid4()
    spec_a, spec_sha_a = _sample_spec()
    policy_sha_a, evaluator_sha_a, env_sha_a, manifest_a, manifest_sha_a = (
        _setup_research_fixtures(service, ws_a)
    )

    _command(
        service,
        ws_a,
        {
            "type": "create_research_campaign",
            "payload": {
                "campaign_id": str(camp_a),
                "work_id": str(work_a),
                "revision_id": str(rev_a),
                "domain": "machine_learning",
                "spec": spec_a,
                "spec_sha256": spec_sha_a,
            },
        },
    )
    _command(
        service,
        ws_a,
        {
            "type": "admit_research_campaign",
            "payload": _admit_payload(
                camp_a,
                work_a,
                rev_a,
                spec_sha_a,
                policy_sha_a,
                manifest_a,
                manifest_sha_a,
            ),
        },
    )

    trial_a = uuid4()
    _command(
        service,
        ws_a,
        {
            "type": "propose_research_trial",
            "payload": _trial_payload(
                camp_a,
                work_a,
                policy_sha_a,
                evaluator_sha_a,
                env_sha_a,
                trial_id=trial_a,
            ),
        },
    )

    # 1. Foreign read denial from Workspace B
    res = service.client.get(
        f"/v1/work-items/{item_a['key']}/research",
        headers=_owner_headers(ws_b),
    )
    assert res.status_code in (404, 400), res.text

    # 2. Workspace B tries to create campaign on Workspace A's work_id
    item_b = _create(service, ws_b, "workspace B target")
    _ = item_b
    status, body = _command(
        service,
        ws_b,
        {
            "type": "create_research_campaign",
            "payload": {
                "campaign_id": str(uuid4()),
                "work_id": str(work_a),
                "revision_id": str(rev_a),
                "domain": "machine_learning",
                "spec": spec_a,
                "spec_sha256": spec_sha_a,
            },
        },
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"

    # 3. Workspace B tries to admit Workspace A's campaign
    status, body = _command(
        service,
        ws_b,
        {
            "type": "admit_research_campaign",
            "payload": _admit_payload(
                camp_a,
                work_a,
                rev_a,
                spec_sha_a,
                policy_sha_a,
                manifest_a,
                manifest_sha_a,
            ),
        },
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"

    # 4. Workspace B tries to cancel Workspace A's campaign
    status, body = _command(
        service,
        ws_b,
        {
            "type": "cancel_research_campaign",
            "payload": {
                "campaign_id": str(camp_a),
                "work_id": str(work_a),
                "reason": "malicious cross-workspace cancel",
            },
        },
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"

    # 5. Workspace B tries to propose trial on Workspace A's campaign
    status, body = _command(
        service,
        ws_b,
        {
            "type": "propose_research_trial",
            "payload": _trial_payload(
                camp_a,
                work_a,
                policy_sha_a,
                evaluator_sha_a,
                env_sha_a,
            ),
        },
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"

    # 6. Workspace B tries to record observation on Workspace A's campaign
    status, body = _command(
        service,
        ws_b,
        {
            "type": "record_research_observation",
            "payload": {
                "observation_id": str(uuid4()),
                "campaign_id": str(camp_a),
                "trial_id": None,
                "issuer_kind": "legacy_autoresearch",
                "source_ref": "cross-ws-ref",
                "execution_status": "completed",
                "commit_sha": None,
                "payload": {"data": 1},
                "payload_sha256": sha256({"data": 1}),
                "observed_at": datetime.now(UTC).isoformat(),
            },
        },
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"

    # 7. Workspace B tries to bind deliverable on Workspace A's campaign
    status, body = _command(
        service,
        ws_b,
        {
            "type": "bind_research_deliverable",
            "payload": {
                "trial_id": str(trial_a),
                "campaign_id": str(camp_a),
                "work_id": str(work_a),
                "revision_id": str(rev_a),
                "candidate_digest": "c" * 64,
                "native_candidate_id": str(uuid4()),
                "binding_sha256": "0" * 64,
            },
        },
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"

    # Verify no rows created in Workspace B
    with (
        psycopg.connect(**service.config.connection_kwargs("postgres")) as conn,
        conn.cursor() as cur,
    ):
        cur.execute("SELECT count(*) FROM omp_research.campaigns WHERE workspace_id=%s", (ws_b,))
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM omp_research.trials WHERE workspace_id=%s", (ws_b,))
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM omp_research.observations WHERE workspace_id=%s", (ws_b,))
        assert cur.fetchone()[0] == 0


def test_direct_app_role_database_relational_integrity_constraints(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item1 = _create(service, workspace_id, "relational target 1")
    item2 = _create(service, workspace_id, "relational target 2")
    w1, r1 = UUID(item1["work_id"]), UUID(item1["revision_id"])
    w2, r2 = UUID(item2["work_id"]), UUID(item2["revision_id"])

    c1 = uuid4()
    c2 = uuid4()
    spec1, spec_sha1 = _sample_spec()
    spec2, _ = _sample_spec()
    spec2["title"] = "Spec 2"
    spec_sha2 = sha256(spec2)

    _command(
        service,
        workspace_id,
        {
            "type": "create_research_campaign",
            "payload": {
                "campaign_id": str(c1),
                "work_id": str(w1),
                "revision_id": str(r1),
                "domain": "machine_learning",
                "spec": spec1,
                "spec_sha256": spec_sha1,
            },
        },
    )
    _command(
        service,
        workspace_id,
        {
            "type": "create_research_campaign",
            "payload": {
                "campaign_id": str(c2),
                "work_id": str(w2),
                "revision_id": str(r2),
                "domain": "machine_learning",
                "spec": spec2,
                "spec_sha256": spec_sha2,
            },
        },
    )

    policy_sha, evaluator_sha, env_sha, manifest, manifest_sha = (
        _setup_research_fixtures(service, workspace_id)
    )

    _command(
        service,
        workspace_id,
        {
            "type": "admit_research_campaign",
            "payload": _admit_payload(
                c1,
                w1,
                r1,
                spec_sha1,
                policy_sha,
                manifest,
                manifest_sha,
            ),
        },
    )

    t1 = uuid4()
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "propose_research_trial",
            "payload": _trial_payload(
                c1,
                w1,
                policy_sha,
                evaluator_sha,
                env_sha,
                trial_id=t1,
            ),
        },
    )
    assert status == 200, body

    # Connect directly as omp_work_app with RLS active
    with (
        psycopg.connect(**service.config.connection_kwargs("omp_work_app")) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
            (str(workspace_id), str(OWNER)),
        )

        # 1. SQL constraint refusal: trial whose campaign (c1) and work (w2) belong to different identities
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            cur.execute(
                """
                INSERT INTO omp_research.trials(
                    trial_id, workspace_id, campaign_id, work_id, decision_id,
                    candidate_digest, experiment_spec_sha256, evaluator_sha256,
                    environment_sha256, input_manifest_sha256, policy_sha256, action, state
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, 'evaluate', 'proposed'
                )
                """,
                (
                    uuid4(), workspace_id, c1, w2, uuid4(),
                    "c" * 64, "e" * 64, "b" * 64, "c" * 64, "d" * 64, "a" * 64,
                ),
            )
        conn.rollback()

        # 2. SQL constraint refusal: observation whose trial (t1, on c1) belongs to another campaign (c2)
        cur.execute(
            "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
            (str(workspace_id), str(OWNER)),
        )
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            cur.execute(
                """
                INSERT INTO omp_research.observations(
                    observation_id, workspace_id, campaign_id, trial_id,
                    issuer_kind, source_ref, execution_status,
                    payload, payload_sha256, observed_at
                ) VALUES (
                    %s, %s, %s, %s,
                    'legacy_autoresearch', 'ref-cross-camp', 'completed',
                    %s, %s, clock_timestamp()
                )
                """,
                (
                    uuid4(), workspace_id, c2, t1,
                    json.dumps({"test": 1}), sha256({"test": 1}),
                ),
            )
        conn.rollback()

        # 3. SQL constraint refusal: deliverable binding that mixes trial (t1, on c1, w1) with work w2
        cur.execute(
            "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
            (str(workspace_id), str(OWNER)),
        )
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            cur.execute(
                """
                INSERT INTO omp_research.deliverable_bindings(
                    trial_id, workspace_id, campaign_id, work_id, revision_id,
                    candidate_digest, native_candidate_id, binding_sha256
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s
                )
                """,
                (
                    t1, workspace_id, c1, w2, r1,
                    "c" * 64, uuid4(), "0" * 64,
                ),
            )
        conn.rollback()

        # 4. SQL constraint success: observation with trial_id NULL succeeds
        cur.execute(
            "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
            (str(workspace_id), str(OWNER)),
        )
        cur.execute(
            """
            INSERT INTO omp_research.observations(
                observation_id, workspace_id, campaign_id, trial_id,
                issuer_kind, source_ref, execution_status,
                payload, payload_sha256, observed_at
            ) VALUES (
                %s, %s, %s, NULL,
                'legacy_autoresearch', 'ref-null-trial-direct-sql', 'completed',
                %s, %s, clock_timestamp()
            )
            """,
            (
                uuid4(), workspace_id, c1,
                json.dumps({"test": 2}), sha256({"test": 2}),
            ),
        )
        conn.commit()


def test_exact_trial_and_observation_replay_after_cancellation(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "trial replay after cancel target")
    work_id = UUID(item["work_id"])
    rev_id = UUID(item["revision_id"])

    camp_id = uuid4()
    spec, spec_sha = _sample_spec()
    policy_sha, evaluator_sha, environment_sha, manifest, manifest_sha = (
        _setup_research_fixtures(service, workspace_id)
    )

    # 1. Create and admit campaign
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "create_research_campaign",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "revision_id": str(rev_id),
                "domain": "machine_learning",
                "spec": spec,
                "spec_sha256": spec_sha,
            },
        },
    )
    assert status == 200, body

    status, body = _command(
        service,
        workspace_id,
        {
            "type": "admit_research_campaign",
            "payload": _admit_payload(
                camp_id,
                work_id,
                rev_id,
                spec_sha,
                policy_sha,
                manifest,
                manifest_sha,
            ),
        },
    )
    assert status == 200, body

    # 2. Propose trial
    trial_id = uuid4()
    decision_id = uuid4()
    cand_digest = "c" * 64
    trial_payload = dict(
        _trial_payload(
            camp_id,
            work_id,
            policy_sha,
            evaluator_sha,
            environment_sha,
            trial_id=trial_id,
            decision_id=decision_id,
            candidate_digest=cand_digest,
        ),
        seed=42,
        hardware_class="gpu_h100",
    )
    status, body = _command(
        service,
        workspace_id,
        {"type": "propose_research_trial", "payload": trial_payload},
    )
    assert status == 200 and body["result"]["status"] == "applied", body

    # 3. Record observation on trial
    obs_id = uuid4()
    obs_payload = {
        "observation_id": str(obs_id),
        "campaign_id": str(camp_id),
        "trial_id": str(trial_id),
        "issuer_kind": "candidate_authored",
        "source_ref": "cand-obs-trial-1",
        "execution_status": "completed",
        "commit_sha": "a" * 40,
        "payload": {"accuracy": 0.99},
        "payload_sha256": sha256({"accuracy": 0.99}),
        "observed_at": datetime(2026, 9, 17, 14, 0, 0, tzinfo=UTC).isoformat(),
    }
    status, body = _command(
        service,
        workspace_id,
        {"type": "record_research_observation", "payload": obs_payload},
    )
    assert status == 200 and body["result"]["status"] == "applied", body

    # 4. Cancel campaign (archives trial)
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "cancel_research_campaign",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "reason": "Cancellation for test",
            },
        },
    )
    assert status == 200 and body["result"]["status"] == "applied", body

    # 5. Exact trial replay succeeds even after campaign cancellation archived it
    status, body = _command(
        service,
        workspace_id,
        {"type": "propose_research_trial", "payload": trial_payload},
    )
    assert status == 200, body
    assert body["result"]["status"] == "replayed"
    assert body["result"]["trial"]["state"] == "archived"
    assert body["result"]["trial"]["archived_reason"] == "campaign_cancelled"

    # Changed trial payload conflicts
    changed_trial_payload = dict(trial_payload, seed=99)
    status, body = _command(
        service,
        workspace_id,
        {"type": "propose_research_trial", "payload": changed_trial_payload},
    )
    assert status == 409, body
    assert body["error"]["code"] == "idempotency_conflict"

    # 6. Exact observation replay succeeds even after campaign cancellation
    status, body = _command(
        service,
        workspace_id,
        {"type": "record_research_observation", "payload": obs_payload},
    )
    assert status == 200, body
    assert body["result"]["status"] == "replayed"

    # Changed observation payload conflicts
    changed_obs_payload = dict(
        obs_payload,
        payload={"accuracy": 0.50},
        payload_sha256=sha256({"accuracy": 0.50}),
    )
    status, body = _command(
        service,
        workspace_id,
        {"type": "record_research_observation", "payload": changed_obs_payload},
    )
    assert status == 409, body
    assert body["error"]["code"] == "idempotency_conflict"

    # 7. New trial or new observation on cancelled campaign is refused
    new_trial_payload = dict(trial_payload, trial_id=str(uuid4()), decision_id=str(uuid4()))
    status, body = _command(
        service,
        workspace_id,
        {"type": "propose_research_trial", "payload": new_trial_payload},
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"

    new_obs_payload = dict(obs_payload, observation_id=str(uuid4()), source_ref="new-obs-cancelled")
    status, body = _command(
        service,
        workspace_id,
        {"type": "record_research_observation", "payload": new_obs_payload},
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"


def test_deliverable_binding_exact_replay_and_drift_conflicts(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "binding drift test target")
    work_id = UUID(item["work_id"])
    rev_id = UUID(item["revision_id"])

    camp_id = uuid4()
    spec, spec_sha = _sample_spec()
    policy_sha, evaluator_sha, environment_sha, manifest, manifest_sha = (
        _setup_research_fixtures(service, workspace_id)
    )

    _command(
        service,
        workspace_id,
        {
            "type": "create_research_campaign",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "revision_id": str(rev_id),
                "domain": "machine_learning",
                "spec": spec,
                "spec_sha256": spec_sha,
            },
        },
    )
    _command(
        service,
        workspace_id,
        {
            "type": "admit_research_campaign",
            "payload": _admit_payload(
                camp_id,
                work_id,
                rev_id,
                spec_sha,
                policy_sha,
                manifest,
                manifest_sha,
            ),
        },
    )

    trial_id = uuid4()
    candidate_digest = "c" * 64
    _command(
        service,
        workspace_id,
        {
            "type": "propose_research_trial",
            "payload": _trial_payload(
                camp_id,
                work_id,
                policy_sha,
                evaluator_sha,
                environment_sha,
                trial_id=trial_id,
                candidate_digest=candidate_digest,
            ),
        },
    )

    # Finalize candidate 1
    cand_1_id = _plan_and_finalize(service, workspace_id, item, candidate_hash=candidate_digest)

    binding_ident = {
        "candidate_digest": candidate_digest,
        "campaign_id": str(camp_id),
        "native_candidate_id": str(cand_1_id),
        "revision_id": str(rev_id),
        "trial_id": str(trial_id),
        "work_id": str(work_id),
        "workspace_id": str(workspace_id),
    }
    binding_sha = sha256(binding_ident)

    binding_payload = {
        "trial_id": str(trial_id),
        "campaign_id": str(camp_id),
        "work_id": str(work_id),
        "revision_id": str(rev_id),
        "candidate_digest": candidate_digest,
        "native_candidate_id": str(cand_1_id),
        "binding_sha256": binding_sha,
    }
    status, body = _command(
        service,
        workspace_id,
        {"type": "bind_research_deliverable", "payload": binding_payload},
    )
    assert status == 200 and body["result"]["status"] == "applied", body

    # 1. Exact replay succeeds
    status, body = _command(
        service,
        workspace_id,
        {"type": "bind_research_deliverable", "payload": binding_payload},
    )
    assert status == 200 and body["result"]["status"] == "replayed", body

    # 2. Drift mutable prerequisites: create candidate 2 and make it current candidate on work item
    cand_2_digest = "d" * 64
    cand_2_id = _plan_and_finalize(service, workspace_id, item, candidate_hash=cand_2_digest)

    # 3. Exact replay of candidate 1 binding STILL succeeds despite work item candidate drift
    status, body = _command(
        service,
        workspace_id,
        {"type": "bind_research_deliverable", "payload": binding_payload},
    )
    assert status == 200 and body["result"]["status"] == "replayed", body

    # 4. Changed deliverable binding payload conflicts before stale/current validation
    changed_binding_payload = dict(binding_payload, native_candidate_id=str(cand_2_id))
    status, body = _command(
        service,
        workspace_id,
        {"type": "bind_research_deliverable", "payload": changed_binding_payload},
    )
    assert status == 409, body
    assert body["error"]["code"] == "idempotency_conflict"


def test_admit_campaign_exact_replay_and_conflict_lifecycle(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "admit replay lifecycle target")
    work_id = UUID(item["work_id"])
    rev_id = UUID(item["revision_id"])

    camp_1_id = uuid4()
    spec, spec_sha = _sample_spec()
    policy_sha_1 = _register_component(service, workspace_id, "policy", name="p1")
    policy_sha_2 = _register_component(service, workspace_id, "policy", name="p2")
    manifest, manifest_sha = _manifest()

    # 1. Create draft campaign 1
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "create_research_campaign",
            "payload": {
                "campaign_id": str(camp_1_id),
                "work_id": str(work_id),
                "revision_id": str(rev_id),
                "domain": "machine_learning",
                "spec": spec,
                "spec_sha256": spec_sha,
            },
        },
    )
    assert status == 200, body

    admit_payload_1 = _admit_payload(
        camp_1_id,
        work_id,
        rev_id,
        spec_sha,
        policy_sha_1,
        manifest,
        manifest_sha,
    )
    # 2. Admit campaign 1
    status, body = _command(
        service,
        workspace_id,
        {"type": "admit_research_campaign", "payload": admit_payload_1},
    )
    assert status == 200 and body["result"]["status"] == "applied", body

    # 3. Exact replay of admit on admitted campaign -> replayed
    status, body = _command(
        service,
        workspace_id,
        {"type": "admit_research_campaign", "payload": admit_payload_1},
    )
    assert status == 200 and body["result"]["status"] == "replayed", body

    # 4. Changed policy on admitted campaign -> idempotency_conflict
    changed_admit_payload = dict(admit_payload_1, policy_sha256=policy_sha_2)
    status, body = _command(
        service,
        workspace_id,
        {"type": "admit_research_campaign", "payload": changed_admit_payload},
    )
    assert status == 409, body
    assert body["error"]["code"] == "idempotency_conflict"

    # 5. Changed revision on admitted campaign -> idempotency_conflict
    changed_rev_admit = dict(admit_payload_1, revision_id=str(uuid4()))
    status, body = _command(
        service,
        workspace_id,
        {"type": "admit_research_campaign", "payload": changed_rev_admit},
    )
    assert status == 409, body
    assert body["error"]["code"] == "idempotency_conflict"

    # 6. Cancel campaign 1
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "cancel_research_campaign",
            "payload": {
                "campaign_id": str(camp_1_id),
                "work_id": str(work_id),
                "reason": "Finished study",
            },
        },
    )
    assert status == 200 and body["result"]["status"] == "applied", body

    # 7. Exact replay of admit on cancelled (previously admitted) campaign -> replayed
    status, body = _command(
        service,
        workspace_id,
        {"type": "admit_research_campaign", "payload": admit_payload_1},
    )
    assert status == 200 and body["result"]["status"] == "replayed", body

    # 8. Changed policy on cancelled (previously admitted) campaign -> idempotency_conflict
    status, body = _command(
        service,
        workspace_id,
        {"type": "admit_research_campaign", "payload": changed_admit_payload},
    )
    assert status == 409, body
    assert body["error"]["code"] == "idempotency_conflict"

    # 9. Campaign 2 created in draft and cancelled immediately (never admitted)
    camp_2_id = uuid4()
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "create_research_campaign",
            "payload": {
                "campaign_id": str(camp_2_id),
                "work_id": str(work_id),
                "revision_id": str(rev_id),
                "domain": "machine_learning",
                "spec": spec,
                "spec_sha256": spec_sha,
            },
        },
    )
    assert status == 200, body

    status, body = _command(
        service,
        workspace_id,
        {
            "type": "cancel_research_campaign",
            "payload": {
                "campaign_id": str(camp_2_id),
                "work_id": str(work_id),
                "reason": "Cancelled from draft",
            },
        },
    )
    assert status == 200 and body["result"]["status"] == "applied", body

    # 10. Attempt to admit campaign 2 (cancelled without prior admission) -> invalid_request
    admit_payload_2 = dict(admit_payload_1, campaign_id=str(camp_2_id))
    status, body = _command(
        service,
        workspace_id,
        {"type": "admit_research_campaign", "payload": admit_payload_2},
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"


def test_campaign_lifecycle_transitions_replay_and_read(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "lifecycle transitions target")
    work_id = UUID(item["work_id"])
    rev_id = UUID(item["revision_id"])

    camp_id = uuid4()
    spec, spec_sha = _sample_spec()
    policy_sha, _, _, manifest, manifest_sha = _setup_research_fixtures(
        service, workspace_id
    )

    # 1. Create campaign (draft)
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "create_research_campaign",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "revision_id": str(rev_id),
                "domain": "machine_learning",
                "spec": spec,
                "spec_sha256": spec_sha,
            },
        },
    )
    assert status == 200 and body["result"]["status"] == "applied", body

    # 2. Admit campaign (draft -> admitted)
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "admit_research_campaign",
            "payload": _admit_payload(
                camp_id,
                work_id,
                rev_id,
                spec_sha,
                policy_sha,
                manifest,
                manifest_sha,
            ),
        },
    )
    assert status == 200 and body["result"]["status"] == "applied", body
    assert body["result"]["campaign"]["state"] == "admitted"

    # 3. admitted -> running
    op_run_1 = uuid4()
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_research_campaign_state",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "expected_state": "admitted",
                "target_state": "running",
                "policy_sha256": policy_sha,
            },
        },
        operation_id=op_run_1,
    )
    assert status == 200 and body["result"]["status"] == "applied", body
    assert body["result"]["campaign"]["state"] == "running"

    # 4. running -> paused
    op_pause_1 = uuid4()
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_research_campaign_state",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "expected_state": "running",
                "target_state": "paused",
                "policy_sha256": policy_sha,
            },
        },
        operation_id=op_pause_1,
    )
    assert status == 200 and body["result"]["status"] == "applied", body
    assert body["result"]["campaign"]["state"] == "paused"

    # 5. paused -> running
    op_resume_1 = uuid4()
    resume_payload = {
        "campaign_id": str(camp_id),
        "work_id": str(work_id),
        "expected_state": "paused",
        "target_state": "running",
        "policy_sha256": policy_sha,
    }
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_research_campaign_state",
            "payload": resume_payload,
        },
        operation_id=op_resume_1,
    )
    assert status == 200 and body["result"]["status"] == "applied", body
    assert body["result"]["campaign"]["state"] == "running"

    # 6. running -> evaluating
    op_eval_1 = uuid4()
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_research_campaign_state",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "expected_state": "running",
                "target_state": "evaluating",
                "policy_sha256": policy_sha,
            },
        },
        operation_id=op_eval_1,
    )
    assert status == 200 and body["result"]["status"] == "applied", body
    assert body["result"]["campaign"]["state"] == "evaluating"

    # 7. evaluating -> running
    op_run_2 = uuid4()
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_research_campaign_state",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "expected_state": "evaluating",
                "target_state": "running",
                "policy_sha256": policy_sha,
            },
        },
        operation_id=op_run_2,
    )
    assert status == 200 and body["result"]["status"] == "applied", body
    assert body["result"]["campaign"]["state"] == "running"

    # 8. running -> evaluating
    op_eval_2 = uuid4()
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_research_campaign_state",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "expected_state": "running",
                "target_state": "evaluating",
                "policy_sha256": policy_sha,
            },
        },
        operation_id=op_eval_2,
    )
    assert status == 200 and body["result"]["status"] == "applied", body
    assert body["result"]["campaign"]["state"] == "evaluating"

    # 9. evaluating -> concluded (supported)
    op_conclude = uuid4()
    conclude_payload = {
        "campaign_id": str(camp_id),
        "work_id": str(work_id),
        "policy_sha256": policy_sha,
        "outcome": "supported",
        "reason": "Hypothesis validated by experimental trials",
    }
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "conclude_research_campaign",
            "payload": conclude_payload,
        },
        operation_id=op_conclude,
    )
    assert status == 200 and body["result"]["status"] == "applied", body
    assert body["result"]["campaign"]["state"] == "concluded"
    assert body["result"]["campaign"]["outcome"] == "supported"
    assert body["result"]["campaign"]["outcome_reason"] == "Hypothesis validated by experimental trials"
    assert body["result"]["campaign"]["concluded_at"] is not None

    # 10. Replay earlier command (paused -> running) with same operation_id after drift returns replayed
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_research_campaign_state",
            "payload": resume_payload,
        },
        operation_id=op_resume_1,
    )
    assert status == 200, body
    assert body["receipt"]["state"] == "replayed"

    # 11. Read via GET /v1/work-items/{key}/research
    resp = service.client.get(
        f"/v1/work-items/{item['key']}/research", headers=_owner_headers(workspace_id)
    )
    assert resp.status_code == 200, resp.json()
    campaign_view = resp.json()["campaigns"][0]
    assert campaign_view["state"] == "concluded"
    assert campaign_view["outcome"] == "supported"
    assert campaign_view["outcome_reason"] == "Hypothesis validated by experimental trials"
    assert campaign_view["concluded_at"] is not None

    # 12. cancel_research_campaign on concluded campaign -> 400 invalid_request
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "cancel_research_campaign",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "reason": "Attempt cancel after conclusion",
            },
        },
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"


def test_campaign_illegal_transitions_and_expected_state_conflict(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "illegal transitions target")
    work_id = UUID(item["work_id"])
    rev_id = UUID(item["revision_id"])

    camp_id = uuid4()
    spec, spec_sha = _sample_spec()
    policy_sha, _, _, manifest, manifest_sha = _setup_research_fixtures(
        service, workspace_id
    )

    # Create campaign (draft)
    _command(
        service,
        workspace_id,
        {
            "type": "create_research_campaign",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "revision_id": str(rev_id),
                "domain": "machine_learning",
                "spec": spec,
                "spec_sha256": spec_sha,
            },
        },
    )

    # 1. draft -> running (draft is invalid expected_state in schema) -> 400 invalid_request
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_research_campaign_state",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "expected_state": "draft",
                "target_state": "running",
                "policy_sha256": policy_sha,
            },
        },
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"

    # Admit campaign and move to running, then paused
    _command(
        service,
        workspace_id,
        {
            "type": "admit_research_campaign",
            "payload": _admit_payload(
                camp_id,
                work_id,
                rev_id,
                spec_sha,
                policy_sha,
                manifest,
                manifest_sha,
            ),
        },
    )
    _command(
        service,
        workspace_id,
        {
            "type": "set_research_campaign_state",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "expected_state": "admitted",
                "target_state": "running",
                "policy_sha256": policy_sha,
            },
        },
    )
    _command(
        service,
        workspace_id,
        {
            "type": "set_research_campaign_state",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "expected_state": "running",
                "target_state": "paused",
                "policy_sha256": policy_sha,
            },
        },
    )

    # 2. paused -> evaluating -> 400 invalid_request
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_research_campaign_state",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "expected_state": "paused",
                "target_state": "evaluating",
                "policy_sha256": policy_sha,
            },
        },
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"

    # Move to evaluating and conclude
    _command(
        service,
        workspace_id,
        {
            "type": "set_research_campaign_state",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "expected_state": "paused",
                "target_state": "running",
                "policy_sha256": policy_sha,
            },
        },
    )
    _command(
        service,
        workspace_id,
        {
            "type": "set_research_campaign_state",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "expected_state": "running",
                "target_state": "evaluating",
                "policy_sha256": policy_sha,
            },
        },
    )
    _command(
        service,
        workspace_id,
        {
            "type": "conclude_research_campaign",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "policy_sha256": policy_sha,
                "outcome": "supported",
                "reason": "Concluding for test",
            },
        },
    )

    # 3. concluded -> running (concluded is invalid expected_state) -> 400 invalid_request
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_research_campaign_state",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "expected_state": "concluded",
                "target_state": "running",
                "policy_sha256": policy_sha,
            },
        },
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"

    # 4. expected_state stale conflict on a fresh operation:
    # Create campaign 2, move to running
    camp_2_id = uuid4()
    _command(
        service,
        workspace_id,
        {
            "type": "create_research_campaign",
            "payload": {
                "campaign_id": str(camp_2_id),
                "work_id": str(work_id),
                "revision_id": str(rev_id),
                "domain": "machine_learning",
                "spec": spec,
                "spec_sha256": spec_sha,
            },
        },
    )
    _command(
        service,
        workspace_id,
        {
            "type": "admit_research_campaign",
            "payload": _admit_payload(
                camp_2_id,
                work_id,
                rev_id,
                spec_sha,
                policy_sha,
                manifest,
                manifest_sha,
            ),
        },
    )
    _command(
        service,
        workspace_id,
        {
            "type": "set_research_campaign_state",
            "payload": {
                "campaign_id": str(camp_2_id),
                "work_id": str(work_id),
                "expected_state": "admitted",
                "target_state": "running",
                "policy_sha256": policy_sha,
            },
        },
    )
    # Payload says expected_state: paused, but campaign row is running -> 409 revision_conflict
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_research_campaign_state",
            "payload": {
                "campaign_id": str(camp_2_id),
                "work_id": str(work_id),
                "expected_state": "paused",
                "target_state": "evaluating",
                "policy_sha256": policy_sha,
            },
        },
    )
    assert status == 409, body
    assert body["error"]["code"] == "revision_conflict"

    # 5. target_state=blocked without dependency -> 400 invalid_request
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_research_campaign_state",
            "payload": {
                "campaign_id": str(camp_2_id),
                "work_id": str(work_id),
                "expected_state": "running",
                "target_state": "blocked",
                "policy_sha256": policy_sha,
            },
        },
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"

    # 6. non-blocked with dependency -> 400 invalid_request
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_research_campaign_state",
            "payload": {
                "campaign_id": str(camp_2_id),
                "work_id": str(work_id),
                "expected_state": "running",
                "target_state": "paused",
                "policy_sha256": policy_sha,
                "blocked_dependency": {
                    "kind": "budget_scope",
                    "ref": "b-ref",
                    "reason": "need budget",
                },
            },
        },
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"

    # 7. expected_state == target_state -> 400 invalid_request
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_research_campaign_state",
            "payload": {
                "campaign_id": str(camp_2_id),
                "work_id": str(work_id),
                "expected_state": "running",
                "target_state": "running",
                "policy_sha256": policy_sha,
            },
        },
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"


def test_campaign_transitions_refuse_incompatible_policy_fingerprint(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "policy fingerprint compatibility target")
    work_id = UUID(item["work_id"])
    rev_id = UUID(item["revision_id"])

    camp_id = uuid4()
    spec, spec_sha = _sample_spec()
    policy_sha, _, _, manifest, manifest_sha = _setup_research_fixtures(
        service, workspace_id
    )
    wrong_policy_sha = _register_component(
        service, workspace_id, "policy", name="p-wrong"
    )

    # Create and admit campaign
    _command(
        service,
        workspace_id,
        {
            "type": "create_research_campaign",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "revision_id": str(rev_id),
                "domain": "machine_learning",
                "spec": spec,
                "spec_sha256": spec_sha,
            },
        },
    )
    _command(
        service,
        workspace_id,
        {
            "type": "admit_research_campaign",
            "payload": _admit_payload(
                camp_id,
                work_id,
                rev_id,
                spec_sha,
                policy_sha,
                manifest,
                manifest_sha,
            ),
        },
    )
    # Move to running, then paused
    _command(
        service,
        workspace_id,
        {
            "type": "set_research_campaign_state",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "expected_state": "admitted",
                "target_state": "running",
                "policy_sha256": policy_sha,
            },
        },
    )
    _command(
        service,
        workspace_id,
        {
            "type": "set_research_campaign_state",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "expected_state": "running",
                "target_state": "paused",
                "policy_sha256": policy_sha,
            },
        },
    )

    # 1. paused -> running with mismatched policy_sha256 -> 409 stale_evidence
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_research_campaign_state",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "expected_state": "paused",
                "target_state": "running",
                "policy_sha256": wrong_policy_sha,
            },
        },
    )
    assert status == 409, body
    assert body["error"]["code"] == "stale_evidence"

    # State unchanged on read
    resp = service.client.get(
        f"/v1/work-items/{item['key']}/research", headers=_owner_headers(workspace_id)
    )
    assert resp.status_code == 200
    assert resp.json()["campaigns"][0]["state"] == "paused"

    # Resume with correct policy_sha, then move to evaluating
    _command(
        service,
        workspace_id,
        {
            "type": "set_research_campaign_state",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "expected_state": "paused",
                "target_state": "running",
                "policy_sha256": policy_sha,
            },
        },
    )
    _command(
        service,
        workspace_id,
        {
            "type": "set_research_campaign_state",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "expected_state": "running",
                "target_state": "evaluating",
                "policy_sha256": policy_sha,
            },
        },
    )

    # 2. Conclude with mismatched policy_sha256 -> 409 stale_evidence
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "conclude_research_campaign",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "policy_sha256": wrong_policy_sha,
                "outcome": "supported",
                "reason": "Attempt conclude with bad policy fingerprint",
            },
        },
    )
    assert status == 409, body
    assert body["error"]["code"] == "stale_evidence"

    # State unchanged on read
    resp = service.client.get(
        f"/v1/work-items/{item['key']}/research", headers=_owner_headers(workspace_id)
    )
    assert resp.status_code == 200
    assert resp.json()["campaigns"][0]["state"] == "evaluating"


def test_blocked_campaign_records_dependency_and_resumes_only_to_prior_state(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "blocked campaign target")
    work_id = UUID(item["work_id"])
    rev_id = UUID(item["revision_id"])

    camp_id = uuid4()
    spec, spec_sha = _sample_spec()
    policy_sha, evaluator_sha, environment_sha, manifest, manifest_sha = (
        _setup_research_fixtures(service, workspace_id)
    )

    # Create and admit campaign
    _command(
        service,
        workspace_id,
        {
            "type": "create_research_campaign",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "revision_id": str(rev_id),
                "domain": "machine_learning",
                "spec": spec,
                "spec_sha256": spec_sha,
            },
        },
    )
    _command(
        service,
        workspace_id,
        {
            "type": "admit_research_campaign",
            "payload": _admit_payload(
                camp_id,
                work_id,
                rev_id,
                spec_sha,
                policy_sha,
                manifest,
                manifest_sha,
            ),
        },
    )
    _command(
        service,
        workspace_id,
        {
            "type": "set_research_campaign_state",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "expected_state": "admitted",
                "target_state": "running",
                "policy_sha256": policy_sha,
            },
        },
    )

    # 1. running -> blocked with dependency
    op_block = uuid4()
    dep_payload = {
        "kind": "budget_scope",
        "ref": "scope-gpu-cluster-1",
        "reason": "Quota exhaustion on compute cluster",
    }
    block_cmd_payload = {
        "campaign_id": str(camp_id),
        "work_id": str(work_id),
        "expected_state": "running",
        "target_state": "blocked",
        "policy_sha256": policy_sha,
        "blocked_dependency": dep_payload,
    }
    status, body = _command(
        service,
        workspace_id,
        {"type": "set_research_campaign_state", "payload": block_cmd_payload},
        operation_id=op_block,
    )
    assert status == 200 and body["result"]["status"] == "applied", body
    assert body["result"]["campaign"]["state"] == "blocked"
    assert body["result"]["campaign"]["blocked_from_state"] == "running"
    assert body["result"]["campaign"]["blocked_dependency"] == dep_payload

    # 2. Read shows blocked_dependency and blocked_from_state = running
    resp = service.client.get(
        f"/v1/work-items/{item['key']}/research", headers=_owner_headers(workspace_id)
    )
    assert resp.status_code == 200
    camp_view = resp.json()["campaigns"][0]
    assert camp_view["state"] == "blocked"
    assert camp_view["blocked_from_state"] == "running"
    assert camp_view["blocked_dependency"] == dep_payload

    # 3. blocked -> evaluating returns 400 invalid_request
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_research_campaign_state",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "expected_state": "blocked",
                "target_state": "evaluating",
                "policy_sha256": policy_sha,
            },
        },
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"

    # 4. Blocked replay with different dependency under same operation_id -> 409 idempotency_conflict
    diff_dep_payload = dict(
        block_cmd_payload,
        blocked_dependency={
            "kind": "external",
            "ref": "gov-999",
            "reason": "Policy review required",
        },
    )
    status, body = _command(
        service,
        workspace_id,
        {"type": "set_research_campaign_state", "payload": diff_dep_payload},
        operation_id=op_block,
    )
    assert status == 409, body
    assert body["error"]["code"] == "idempotency_conflict"

    # 5. blocked -> running clears both fields
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_research_campaign_state",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "expected_state": "blocked",
                "target_state": "running",
                "policy_sha256": policy_sha,
            },
        },
    )
    assert status == 200 and body["result"]["status"] == "applied", body
    assert body["result"]["campaign"]["state"] == "running"
    assert body["result"]["campaign"]["blocked_dependency"] is None
    assert body["result"]["campaign"]["blocked_from_state"] is None

    # Propose a trial while running
    t_id = uuid4()
    _command(
        service,
        workspace_id,
        {
            "type": "propose_research_trial",
            "payload": _trial_payload(
                camp_id,
                work_id,
                policy_sha,
                evaluator_sha,
                environment_sha,
                trial_id=t_id,
            ),
        },
    )

    # Move back to blocked
    _command(
        service,
        workspace_id,
        {
            "type": "set_research_campaign_state",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "expected_state": "running",
                "target_state": "blocked",
                "policy_sha256": policy_sha,
                "blocked_dependency": dep_payload,
            },
        },
    )

    # 6. cancel from blocked succeeds and atomically clears blocked fields and archives proposed trials
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "cancel_research_campaign",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "reason": "Cancelled while blocked",
            },
        },
    )
    assert status == 200 and body["result"]["status"] == "applied", body
    assert body["result"]["campaign"]["state"] == "cancelled"
    assert body["result"]["campaign"]["blocked_dependency"] is None
    assert body["result"]["campaign"]["blocked_from_state"] is None

    # Read back confirms cancellation, null blocked fields, and archived trial
    resp = service.client.get(
        f"/v1/work-items/{item['key']}/research", headers=_owner_headers(workspace_id)
    )
    assert resp.status_code == 200
    camp_view = resp.json()["campaigns"][0]
    assert camp_view["state"] == "cancelled"
    assert camp_view["blocked_dependency"] is None
    assert camp_view["blocked_from_state"] is None
    trial_view = resp.json()["trials"][0]
    assert trial_view["state"] == "archived"
    assert trial_view["archived_reason"] == "campaign_cancelled"


def test_conclusion_requires_approve_scope_evaluating_state_and_closed_outcome(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "conclusion requirements target")
    work_id = UUID(item["work_id"])
    rev_id = UUID(item["revision_id"])

    camp_id = uuid4()
    spec, spec_sha = _sample_spec()
    policy_sha, evaluator_sha, env_sha, manifest, manifest_sha = (
        _setup_research_fixtures(service, workspace_id)
    )

    # 1. work.execute-only capability token
    cap_file = service.capabilities / "exec-only.json"
    cap_file.write_text(
        json.dumps(
            {
                "token": "exec-only-token",
                "actor_id": str(uuid4()),
                "actor_kind": "worker",
                "workspaces": [str(workspace_id)],
                "scopes": ["work.read", "work.mutate", "work.execute"],
            }
        )
    )
    cap_file.chmod(0o600)

    try:
        # Create, admit, running, evaluating
        _command(
            service,
            workspace_id,
            {
                "type": "create_research_campaign",
                "payload": {
                    "campaign_id": str(camp_id),
                    "work_id": str(work_id),
                    "revision_id": str(rev_id),
                    "domain": "machine_learning",
                    "spec": spec,
                    "spec_sha256": spec_sha,
                },
            },
        )
        _command(
            service,
            workspace_id,
            {
                "type": "admit_research_campaign",
                "payload": _admit_payload(
                    camp_id,
                    work_id,
                    rev_id,
                    spec_sha,
                    policy_sha,
                    manifest,
                    manifest_sha,
                ),
            },
        )
        _command(
            service,
            workspace_id,
            {
                "type": "set_research_campaign_state",
                "payload": {
                    "campaign_id": str(camp_id),
                    "work_id": str(work_id),
                    "expected_state": "admitted",
                    "target_state": "running",
                    "policy_sha256": policy_sha,
                },
            },
        )
        # Propose trial while running so we can test observation after conclusion
        t_id = uuid4()
        _command(
            service,
            workspace_id,
            {
                "type": "propose_research_trial",
                "payload": _trial_payload(
                    camp_id,
                    work_id,
                    policy_sha,
                    evaluator_sha,
                    env_sha,
                    trial_id=t_id,
                ),
            },
        )
        _command(
            service,
            workspace_id,
            {
                "type": "set_research_campaign_state",
                "payload": {
                    "campaign_id": str(camp_id),
                    "work_id": str(work_id),
                    "expected_state": "running",
                    "target_state": "evaluating",
                    "policy_sha256": policy_sha,
                },
            },
        )

        # 2. Conclude attempt with work.execute token -> 403 forbidden
        status, body = _command(
            service,
            workspace_id,
            {
                "type": "conclude_research_campaign",
                "payload": {
                    "campaign_id": str(camp_id),
                    "work_id": str(work_id),
                    "policy_sha256": policy_sha,
                    "outcome": "supported",
                    "reason": "Unauthorized conclusion attempt",
                },
            },
            token="exec-only-token",
        )
        assert status == 403, body
        assert body["error"]["code"] == "forbidden"

        # 3. Conclude from running campaign -> 400 invalid_request
        camp_2_id = uuid4()
        _command(
            service,
            workspace_id,
            {
                "type": "create_research_campaign",
                "payload": {
                    "campaign_id": str(camp_2_id),
                    "work_id": str(work_id),
                    "revision_id": str(rev_id),
                    "domain": "machine_learning",
                    "spec": spec,
                    "spec_sha256": spec_sha,
                },
            },
        )
        _command(
            service,
            workspace_id,
            {
                "type": "admit_research_campaign",
                "payload": _admit_payload(
                    camp_2_id,
                    work_id,
                    rev_id,
                    spec_sha,
                    policy_sha,
                    manifest,
                    manifest_sha,
                ),
            },
        )
        _command(
            service,
            workspace_id,
            {
                "type": "set_research_campaign_state",
                "payload": {
                    "campaign_id": str(camp_2_id),
                    "work_id": str(work_id),
                    "expected_state": "admitted",
                    "target_state": "running",
                    "policy_sha256": policy_sha,
                },
            },
        )
        status, body = _command(
            service,
            workspace_id,
            {
                "type": "conclude_research_campaign",
                "payload": {
                    "campaign_id": str(camp_2_id),
                    "work_id": str(work_id),
                    "policy_sha256": policy_sha,
                    "outcome": "supported",
                    "reason": "Direct conclusion from running",
                },
            },
        )
        assert status == 400, body
        assert body["error"]["code"] == "invalid_request"

        # 4. Invalid outcome string ("passed") -> 400 invalid_request
        status, body = _command(
            service,
            workspace_id,
            {
                "type": "conclude_research_campaign",
                "payload": {
                    "campaign_id": str(camp_id),
                    "work_id": str(work_id),
                    "policy_sha256": policy_sha,
                    "outcome": "passed",
                    "reason": "Invalid outcome name",
                },
            },
        )
        assert status == 400, body
        assert body["error"]["code"] == "invalid_request"

        # 5. Conclude from blocked allowed ONLY for resource_exhausted / externally_blocked
        _command(
            service,
            workspace_id,
            {
                "type": "set_research_campaign_state",
                "payload": {
                    "campaign_id": str(camp_2_id),
                    "work_id": str(work_id),
                    "expected_state": "running",
                    "target_state": "blocked",
                    "policy_sha256": policy_sha,
                    "blocked_dependency": {
                        "kind": "budget_scope",
                        "ref": "b-ref",
                        "reason": "Cluster down",
                    },
                },
            },
        )
        # blocked with supported -> 400 invalid_request
        status, body = _command(
            service,
            workspace_id,
            {
                "type": "conclude_research_campaign",
                "payload": {
                    "campaign_id": str(camp_2_id),
                    "work_id": str(work_id),
                    "policy_sha256": policy_sha,
                    "outcome": "supported",
                    "reason": "Not allowed from blocked",
                },
            },
        )
        assert status == 400, body
        assert body["error"]["code"] == "invalid_request"

        # blocked with resource_exhausted -> 200 applied
        status, body = _command(
            service,
            workspace_id,
            {
                "type": "conclude_research_campaign",
                "payload": {
                    "campaign_id": str(camp_2_id),
                    "work_id": str(work_id),
                    "policy_sha256": policy_sha,
                    "outcome": "resource_exhausted",
                    "reason": "All budget consumed",
                },
            },
        )
        assert status == 200 and body["result"]["status"] == "applied", body
        assert body["result"]["campaign"]["state"] == "concluded"
        assert body["result"]["campaign"]["outcome"] == "resource_exhausted"

        # 6. Conclude evaluating campaign 1:
        op_conclude = uuid4()
        conclude_payload = {
            "campaign_id": str(camp_id),
            "work_id": str(work_id),
            "policy_sha256": policy_sha,
            "outcome": "supported",
            "reason": "Hypothesis validated",
        }
        status, body = _command(
            service,
            workspace_id,
            {"type": "conclude_research_campaign", "payload": conclude_payload},
            operation_id=op_conclude,
        )
        assert status == 200 and body["result"]["status"] == "applied", body
        assert body["result"]["campaign"]["state"] == "concluded"
        assert body["result"]["campaign"]["outcome"] == "supported"

        # 7. Same operation replay -> replayed
        status, body = _command(
            service,
            workspace_id,
            {"type": "conclude_research_campaign", "payload": conclude_payload},
            operation_id=op_conclude,
        )
        assert status == 200, body
        assert body["receipt"]["state"] == "replayed"

        # 8. Different outcome under same operation_id -> 409 idempotency_conflict
        diff_conclude = dict(conclude_payload, outcome="refuted")
        status, body = _command(
            service,
            workspace_id,
            {"type": "conclude_research_campaign", "payload": diff_conclude},
            operation_id=op_conclude,
        )
        assert status == 409, body
        assert body["error"]["code"] == "idempotency_conflict"

        # 9. Record observation with execution_status: completed after conclusion
        obs_id = uuid4()
        obs_payload = {
            "observation_id": str(obs_id),
            "campaign_id": str(camp_id),
            "trial_id": str(t_id),
            "issuer_kind": "candidate_authored",
            "source_ref": "late-obs",
            "execution_status": "completed",
            "commit_sha": "a" * 40,
            "payload": {"loss": 0.01},
            "payload_sha256": sha256({"loss": 0.01}),
            "observed_at": datetime(2026, 9, 17, 16, 0, 0, tzinfo=UTC).isoformat(),
        }
        status, body = _command(
            service,
            workspace_id,
            {"type": "record_research_observation", "payload": obs_payload},
        )
        assert status == 200 and body["result"]["status"] == "applied", body

        # Verify campaign outcome remains unchanged on read
        resp = service.client.get(
            f"/v1/work-items/{item['key']}/research", headers=_owner_headers(workspace_id)
        )
        assert resp.status_code == 200
        camp_view = next(c for c in resp.json()["campaigns"] if c["campaign_id"] == str(camp_id))
        assert camp_view["state"] == "concluded"
        assert camp_view["outcome"] == "supported"
    finally:
        cap_file.unlink(missing_ok=True)


def test_trial_proposal_requires_closed_action_vocabulary_and_running_or_admitted_campaign(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "trial action vocabulary target")
    work_id = UUID(item["work_id"])
    rev_id = UUID(item["revision_id"])

    camp_id = uuid4()
    spec, spec_sha = _sample_spec()
    policy_sha, evaluator_sha, env_sha, manifest, manifest_sha = _setup_research_fixtures(service, workspace_id)

    # Create and admit campaign
    _command(
        service,
        workspace_id,
        {
            "type": "create_research_campaign",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "revision_id": str(rev_id),
                "domain": "machine_learning",
                "spec": spec,
                "spec_sha256": spec_sha,
            },
        },
    )
    _command(
        service,
        workspace_id,
        {
            "type": "admit_research_campaign",
            "payload": _admit_payload(
                camp_id,
                work_id,
                rev_id,
                spec_sha,
                policy_sha,
                manifest,
                manifest_sha,
            ),
        },
    )
    _command(
        service,
        workspace_id,
        {
            "type": "set_research_campaign_state",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "expected_state": "admitted",
                "target_state": "running",
                "policy_sha256": policy_sha,
            },
        },
    )

    base_trial_payload = {
        "trial_id": str(uuid4()),
        "campaign_id": str(camp_id),
        "work_id": str(work_id),
        "decision_id": str(uuid4()),
        "candidate_digest": "c" * 64,
        "experiment_spec_sha256": "e" * 64,
        "evaluator_sha256": evaluator_sha,
        "environment_sha256": env_sha,
        "input_manifest_sha256": "d" * 64,
        "policy_sha256": policy_sha,
    }

    # 1. Missing action -> 400 invalid_request
    status, body = _command(
        service,
        workspace_id,
        {"type": "propose_research_trial", "payload": base_trial_payload},
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"

    # 2. Invalid action ("launch") -> 400 invalid_request
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "propose_research_trial",
            "payload": dict(base_trial_payload, action="launch"),
        },
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"

    # 3. Valid action ("replicate") with reason on running campaign -> applied and read back
    t1_id = uuid4()
    t1_payload = dict(
        base_trial_payload,
        trial_id=str(t1_id),
        action="replicate",
        reason="Replicating previous benchmark baseline",
    )
    status, body = _command(
        service,
        workspace_id,
        {"type": "propose_research_trial", "payload": t1_payload},
    )
    assert status == 200 and body["result"]["status"] == "applied", body
    assert body["result"]["trial"]["action"] == "replicate"
    assert body["result"]["trial"]["reason"] == "Replicating previous benchmark baseline"

    # Read back over HTTP
    resp = service.client.get(
        f"/v1/work-items/{item['key']}/research", headers=_owner_headers(workspace_id)
    )
    assert resp.status_code == 200
    t_read = next(t for t in resp.json()["trials"] if t["trial_id"] == str(t1_id))
    assert t_read["action"] == "replicate"
    assert t_read["reason"] == "Replicating previous benchmark baseline"

    # 4. Move campaign to paused, try propose trial -> 400 invalid_request
    _command(
        service,
        workspace_id,
        {
            "type": "set_research_campaign_state",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "expected_state": "running",
                "target_state": "paused",
                "policy_sha256": policy_sha,
            },
        },
    )
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "propose_research_trial",
            "payload": dict(
                base_trial_payload,
                trial_id=str(uuid4()),
                decision_id=str(uuid4()),
                action="evaluate",
            ),
        },
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"

    # Resume running
    _command(
        service,
        workspace_id,
        {
            "type": "set_research_campaign_state",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "expected_state": "paused",
                "target_state": "running",
                "policy_sha256": policy_sha,
            },
        },
    )

    # 5. Replay with different action -> 409 idempotency_conflict
    diff_action_payload = dict(t1_payload, action="evaluate")
    status, body = _command(
        service,
        workspace_id,
        {"type": "propose_research_trial", "payload": diff_action_payload},
    )
    assert status == 409, body
    assert body["error"]["code"] == "idempotency_conflict"


def test_migration_0026_preserves_r02_s1_rows_and_constraints(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OMP-R02 migration 0026 upgrade rehearsal: pre-0026 data preserved and lifecycle constraints enforced."""
    from types import SimpleNamespace

    import omp_work.operations.database as database_module
    from omp_work.operations.database import bootstrap
    from omp_work.v1.server import create_app
    from pg_native import seed_authority
    from starlette.testclient import TestClient
    from test_workflow_service import _config

    root = tmp_path_factory.mktemp("migration-0026-upgrade")
    config = _config(root)
    with native_postgres(root, config.port):
        original_migrate = database_module.migrate
        monkeypatch.setattr(database_module, "validate_bundle", lambda **kw: None)
        monkeypatch.setattr(
            database_module,
            "migrate",
            lambda cfg, **kw: original_migrate(cfg, target=25, **kw),
        )
        bootstrap(config)
        monkeypatch.setattr(database_module, "migrate", original_migrate)

        workspace_id = uuid4()
        work_id = uuid4()
        revision_id = uuid4()
        camp_id = uuid4()
        trial_id = uuid4()
        now = datetime.now(UTC)
        spec, spec_sha = _sample_spec()
        policy_sha = "a" * 64

        with psycopg.connect(
            **config.connection_kwargs("postgres"), autocommit=True
        ) as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO omp_control.workspaces(workspace_id) VALUES (%s)",
                (workspace_id,),
            )
            cur.execute(
                "INSERT INTO omp_work.work_items(work_id, workspace_id, state) VALUES (%s, %s, 'NOW')",
                (work_id, workspace_id),
            )
            cur.execute(
                "INSERT INTO omp_work.work_revisions(revision_id, work_id, workspace_id, revision_number, title, description, scope, content_sha256, created_by, supplied_at) "
                "VALUES (%s, %s, %s, 1, 'migration 0026 test item', '', '', %s, 'test', %s)",
                (revision_id, work_id, workspace_id, "a" * 64, now),
            )
            cur.execute(
                "UPDATE omp_work.work_items SET current_revision_id=%s WHERE work_id=%s",
                (revision_id, work_id),
            )
            cur.execute(
                "INSERT INTO omp_work.work_aliases(work_id, workspace_id, key, origin, primary_alias) VALUES (%s, %s, 'OMP-1', 'local', true)",
                (work_id, workspace_id),
            )
            # Insert admitted campaign under migration 0025 schema (no outcome, concluded_at, blocked columns)
            cur.execute(
                """
                INSERT INTO omp_research.campaigns(
                    campaign_id, workspace_id, work_id, revision_id,
                    domain, state, spec, spec_sha256, policy_sha256,
                    created_at, admitted_at
                ) VALUES (
                    %s, %s, %s, %s,
                    'machine_learning', 'admitted', %s, %s, %s,
                    %s, %s
                )
                """,
                (
                    camp_id, workspace_id, work_id, revision_id,
                    json.dumps(spec), spec_sha, policy_sha,
                    now, now,
                ),
            )
            # Insert proposed trial under migration 0025 schema (no action, reason columns)
            cur.execute(
                """
                INSERT INTO omp_research.trials(
                    trial_id, workspace_id, campaign_id, work_id, decision_id,
                    candidate_digest, experiment_spec_sha256, evaluator_sha256,
                    environment_sha256, input_manifest_sha256, policy_sha256,
                    state
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    'proposed'
                )
                """,
                (
                    trial_id, workspace_id, camp_id, work_id, uuid4(),
                    "c" * 64, "e" * 64, "b" * 64,
                    "c" * 64, "d" * 64, policy_sha,
                ),
            )

        # Run migration 0026
        original_migrate(config)

        # Set up capabilities and authority for HTTP tests
        capabilities = root / "capabilities"
        capabilities.mkdir(mode=0o700, exist_ok=True)
        owner = capabilities / "owner.json"
        owner.write_text(
            json.dumps(
                {
                    "token": "owner-token",
                    "actor_id": str(OWNER),
                    "actor_kind": "owner",
                    "workspaces": [str(workspace_id)],
                    "scopes": [
                        "work.read",
                        "work.mutate",
                        "work.approve",
                        "work.close",
                        "work.execute",
                    ],
                }
            )
        )
        owner.chmod(0o600)
        seed_authority(config.connection_kwargs("postgres"), workspace_id, OWNER)

        svc = SimpleNamespace(
            client=TestClient(create_app(config, capabilities_dir=capabilities)),
            capabilities=capabilities,
            config=config,
        )

        item_key = "OMP-1"

        # 1. Read back historical rows over HTTP
        resp = svc.client.get(
            f"/v1/work-items/{item_key}/research", headers=_owner_headers(workspace_id)
        )
        assert resp.status_code == 200, resp.json()
        research_data = resp.json()
        camp = research_data["campaigns"][0]
        assert camp["state"] == "admitted"
        assert camp["outcome"] is None
        assert camp["outcome_reason"] is None
        assert camp["concluded_at"] is None
        assert camp["blocked_dependency"] is None
        assert camp["blocked_from_state"] is None

        trial = research_data["trials"][0]
        assert trial["action"] is None
        assert trial["reason"] is None

        # 2. set_research_campaign_state admitted -> running succeeds on historical campaign with original policy_sha
        status, body = _command(
            svc,
            workspace_id,
            {
                "type": "set_research_campaign_state",
                "payload": {
                    "campaign_id": str(camp_id),
                    "work_id": str(work_id),
                    "expected_state": "admitted",
                    "target_state": "running",
                    "policy_sha256": policy_sha,
                },
            },
        )
        assert status == 200 and body["result"]["status"] == "applied", body
        assert body["result"]["campaign"]["state"] == "running"

        # 3. Verify pg_constraint contains all named constraints and 8 states
        with psycopg.connect(**config.connection_kwargs("postgres")) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT conname, pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE conrelid = 'omp_research.campaigns'::regclass
                """
            )
            constraints = {row[0]: row[1] for row in cur.fetchall()}
            assert "campaigns_state_check" in constraints
            assert "campaigns_concluded_outcome" in constraints
            assert "campaigns_blocked_dependency" in constraints
            assert "campaigns_post_admission_policy" in constraints

            state_def = constraints["campaigns_state_check"]
            for s in ("draft", "admitted", "running", "paused", "evaluating", "blocked", "concluded", "cancelled"):
                assert s in state_def

        # 4. Direct app-role UPDATEs are refused by new constraints
        with (
            psycopg.connect(**config.connection_kwargs("omp_work_app")) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
                (str(workspace_id), str(OWNER)),
            )

            # campaigns_post_admission_policy: running row with policy_sha256 = NULL fails
            with pytest.raises(psycopg.errors.RaiseException, match="campaign policy_sha256 is immutable once set"):
                cur.execute(
                    "UPDATE omp_research.campaigns SET policy_sha256 = NULL WHERE campaign_id = %s",
                    (camp_id,),
                )
            conn.rollback()

            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
                (str(workspace_id), str(OWNER)),
            )
            # Move running -> evaluating
            cur.execute(
                "UPDATE omp_research.campaigns SET state = 'evaluating' WHERE campaign_id = %s",
                (camp_id,),
            )
            # campaigns_concluded_outcome: concluded row without outcome fails
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute(
                    "UPDATE omp_research.campaigns SET state = 'concluded', outcome = NULL, concluded_at = clock_timestamp() WHERE campaign_id = %s",
                    (camp_id,),
                )
            conn.rollback()

            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
                (str(workspace_id), str(OWNER)),
            )
            # Cancel the admitted/running campaign
            cur.execute(
                "UPDATE omp_research.campaigns SET state = 'cancelled', cancel_reason = 'cancelled after migration', cancelled_at = clock_timestamp() WHERE campaign_id = %s",
                (camp_id,),
            )
            cur.execute(
                "SELECT policy_sha256, admitted_at FROM omp_research.campaigns WHERE campaign_id = %s",
                (camp_id,),
            )
            row = cur.fetchone()
            assert row[0] == policy_sha
            assert row[1] is not None

            # Negative tests: direct app-role UPDATE cannot clear or alter provenance on cancelled campaign
            with pytest.raises(psycopg.errors.RaiseException, match="campaign policy_sha256 is immutable once set"):
                cur.execute(
                    "UPDATE omp_research.campaigns SET policy_sha256 = NULL WHERE campaign_id = %s",
                    (camp_id,),
                )
            conn.rollback()

            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
                (str(workspace_id), str(OWNER)),
            )
            with pytest.raises(psycopg.errors.RaiseException, match="campaign admitted_at is immutable once set"):
                cur.execute(
                    "UPDATE omp_research.campaigns SET admitted_at = NULL WHERE campaign_id = %s",
                    (camp_id,),
                )
            conn.rollback()

            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
                (str(workspace_id), str(OWNER)),
            )
            with pytest.raises(psycopg.errors.RaiseException, match="campaign policy_sha256 is immutable once set"):
                cur.execute(
                    "UPDATE omp_research.campaigns SET policy_sha256 = %s WHERE campaign_id = %s",
                    ("f" * 64, camp_id),
                )
            conn.rollback()

            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
                (str(workspace_id), str(OWNER)),
            )
            with pytest.raises(psycopg.errors.RaiseException, match="campaign admitted_at is immutable once set"):
                cur.execute(
                    "UPDATE omp_research.campaigns SET admitted_at = clock_timestamp() WHERE campaign_id = %s",
                    (camp_id,),
                )
            conn.rollback()


def test_admitted_campaign_provenance_immutable_after_cancellation(service) -> None:
    """PLAN-DISPOSITION: campaign cancelled after admission retains policy_sha256 and admitted_at.

    Direct app-role UPDATEs cannot clear or alter provenance on cancelled campaigns.
    Draft campaigns cancelled before admission preserve null provenance and refuse modification.
    """
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "provenance immutability test item")
    w, r = UUID(item["work_id"]), UUID(item["revision_id"])

    # 1. Draft campaign cancelled before admission: retains nulls, direct UPDATE cannot set provenance
    c_draft_id = uuid4()
    spec_draft, spec_draft_sha = _sample_spec()
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "create_research_campaign",
            "payload": {
                "campaign_id": str(c_draft_id),
                "work_id": str(w),
                "revision_id": str(r),
                "domain": "machine_learning",
                "spec": spec_draft,
                "spec_sha256": spec_draft_sha,
            },
        },
    )
    assert status == 200, body

    status, body = _command(
        service,
        workspace_id,
        {
            "type": "cancel_research_campaign",
            "payload": {
                "campaign_id": str(c_draft_id),
                "work_id": str(w),
                "reason": "cancelled before admission",
            },
        },
    )
    assert status == 200, body
    assert body["result"]["campaign"]["state"] == "cancelled"
    assert body["result"]["campaign"]["policy_sha256"] is None
    assert body["result"]["campaign"]["admitted_at"] is None

    # Connect as app role: direct UPDATE on cancelled draft cannot set provenance
    with (
        psycopg.connect(**service.config.connection_kwargs("omp_work_app")) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
            (str(workspace_id), str(OWNER)),
        )
        with pytest.raises(psycopg.errors.RaiseException, match="unadmitted campaign cannot set admission provenance"):
            cur.execute(
                "UPDATE omp_research.campaigns SET policy_sha256 = %s WHERE campaign_id = %s",
                ("a" * 64, c_draft_id),
            )
        conn.rollback()

        cur.execute(
            "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
            (str(workspace_id), str(OWNER)),
        )
        with pytest.raises(psycopg.errors.RaiseException, match="unadmitted campaign cannot set admission provenance"):
            cur.execute(
                "UPDATE omp_research.campaigns SET admitted_at = clock_timestamp() WHERE campaign_id = %s",
                (c_draft_id,),
            )
        conn.rollback()

    # 2. Admitted campaign cancelled after admission: retains provenance
    c_admitted_id = uuid4()
    spec_admitted, spec_admitted_sha = _sample_spec()
    policy_sha = _register_component(service, workspace_id, "policy", name="p-cancel")
    manifest, manifest_sha = _manifest()
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "create_research_campaign",
            "payload": {
                "campaign_id": str(c_admitted_id),
                "work_id": str(w),
                "revision_id": str(r),
                "domain": "machine_learning",
                "spec": spec_admitted,
                "spec_sha256": spec_admitted_sha,
            },
        },
    )
    assert status == 200, body

    status, body = _command(
        service,
        workspace_id,
        {
            "type": "admit_research_campaign",
            "payload": _admit_payload(
                c_admitted_id,
                w,
                r,
                spec_admitted_sha,
                policy_sha,
                manifest,
                manifest_sha,
            ),
        },
    )
    assert status == 200, body
    admitted_at = body["result"]["campaign"]["admitted_at"]
    assert admitted_at is not None

    # Cancel the admitted campaign
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "cancel_research_campaign",
            "payload": {
                "campaign_id": str(c_admitted_id),
                "work_id": str(w),
                "reason": "cancelled after admission",
            },
        },
    )
    assert status == 200, body
    camp = body["result"]["campaign"]
    assert camp["state"] == "cancelled"
    assert camp["policy_sha256"] == policy_sha
    assert camp["admitted_at"] == admitted_at
    assert camp["compatibility_sha256"] == manifest_sha

    # Read back over HTTP
    resp = service.client.get(
        f"/v1/work-items/{item['key']}/research", headers=_owner_headers(workspace_id)
    )
    assert resp.status_code == 200
    read_c = next(c for c in resp.json()["campaigns"] if c["campaign_id"] == str(c_admitted_id))
    assert read_c["state"] == "cancelled"
    assert read_c["policy_sha256"] == policy_sha
    assert read_c["compatibility_sha256"] == manifest_sha
    assert read_c["compatibility"] == manifest
    assert datetime.fromisoformat(read_c["admitted_at"]) == datetime.fromisoformat(admitted_at)

    # 3. Direct app-role PostgreSQL negative tests: provenance cannot be cleared or altered after cancellation
    with (
        psycopg.connect(**service.config.connection_kwargs("omp_work_app")) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
            (str(workspace_id), str(OWNER)),
        )
        # Cannot clear policy_sha256
        with pytest.raises(psycopg.errors.RaiseException, match="campaign policy_sha256 is immutable once set"):
            cur.execute(
                "UPDATE omp_research.campaigns SET policy_sha256 = NULL WHERE campaign_id = %s",
                (c_admitted_id,),
            )
        conn.rollback()

        cur.execute(
            "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
            (str(workspace_id), str(OWNER)),
        )
        # Cannot clear admitted_at
        with pytest.raises(psycopg.errors.RaiseException, match="campaign admitted_at is immutable once set"):
            cur.execute(
                "UPDATE omp_research.campaigns SET admitted_at = NULL WHERE campaign_id = %s",
                (c_admitted_id,),
            )
        conn.rollback()

        cur.execute(
            "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
            (str(workspace_id), str(OWNER)),
        )
        # Cannot clear both policy_sha256 and admitted_at
        with pytest.raises(psycopg.errors.RaiseException):
            cur.execute(
                "UPDATE omp_research.campaigns SET policy_sha256 = NULL, admitted_at = NULL WHERE campaign_id = %s",
                (c_admitted_id,),
            )
        conn.rollback()

        cur.execute(
            "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
            (str(workspace_id), str(OWNER)),
        )
        # Cannot alter policy_sha256
        with pytest.raises(psycopg.errors.RaiseException, match="campaign policy_sha256 is immutable once set"):
            cur.execute(
                "UPDATE omp_research.campaigns SET policy_sha256 = %s WHERE campaign_id = %s",
                ("f" * 64, c_admitted_id),
            )
        conn.rollback()

        cur.execute(
            "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
            (str(workspace_id), str(OWNER)),
        )
        # Cannot alter admitted_at
        with pytest.raises(psycopg.errors.RaiseException, match="campaign admitted_at is immutable once set"):
            cur.execute(
                "UPDATE omp_research.campaigns SET admitted_at = clock_timestamp() WHERE campaign_id = %s",
                (c_admitted_id,),
            )
        conn.rollback()

        cur.execute(
            "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
            (str(workspace_id), str(OWNER)),
        )
        # Cannot clear compatibility_sha256
        with pytest.raises(psycopg.errors.RaiseException, match="campaign compatibility is immutable once set"):
            cur.execute(
                "UPDATE omp_research.campaigns SET compatibility_sha256 = NULL, compatibility = NULL WHERE campaign_id = %s",
                (c_admitted_id,),
            )
        conn.rollback()

        cur.execute(
            "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
            (str(workspace_id), str(OWNER)),
        )
        # Cannot alter compatibility_sha256
        with pytest.raises(psycopg.errors.RaiseException, match="campaign compatibility is immutable once set"):
            cur.execute(
                "UPDATE omp_research.campaigns SET compatibility_sha256 = %s WHERE campaign_id = %s",
                ("f" * 64, c_admitted_id),
            )
        conn.rollback()

        cur.execute(
            "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
            (str(workspace_id), str(OWNER)),
        )
        # Terminal state cannot transition to active states
        with pytest.raises(psycopg.errors.RaiseException, match="terminal campaign state is immutable"):
            cur.execute(
                "UPDATE omp_research.campaigns SET state = 'running' WHERE campaign_id = %s",
                (c_admitted_id,),
            )
        conn.rollback()


def test_component_registration_is_content_addressed_and_immutable(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    _create(service, workspace_id, "comp test item")

    descriptor = {
        "contract_version": "research-component.v1",
        "kind": "worker",
        "name": "worker-alpha",
        "version": "1.0.0",
        "artifact_sha256": "a" * 64,
        "roles": ["analyst", "implementer"],
        "capabilities": ["experiment.gpu", "retrieval.web"],
    }
    comp_sha = sha256(descriptor)

    # 1. Successful registration -> applied
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "register_research_component",
            "payload": {
                "component_sha256": comp_sha,
                "descriptor": descriptor,
            },
        },
    )
    assert status == 200, body
    assert body["result"]["status"] == "applied"
    comp = body["result"]["component"]
    assert comp["component_sha256"] == comp_sha
    assert comp["kind"] == "worker"
    assert comp["descriptor"]["name"] == "worker-alpha"
    assert comp["descriptor"]["roles"] == ["analyst", "implementer"]
    assert comp["descriptor"]["capabilities"] == ["experiment.gpu", "retrieval.web"]

    # 2. Second registration under new operation_id -> replayed, exactly one row
    status2, body2 = _command(
        service,
        workspace_id,
        {
            "type": "register_research_component",
            "payload": {
                "component_sha256": comp_sha,
                "descriptor": descriptor,
            },
        },
        operation_id=uuid4(),
    )
    assert status2 == 200, body2
    assert body2["result"]["status"] == "replayed"
    assert body2["result"]["component"]["component_sha256"] == comp_sha

    with psycopg.connect(**service.config.connection_kwargs("postgres")) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT count(*) FROM omp_research.components WHERE workspace_id = %s AND component_sha256 = %s",
            (workspace_id, comp_sha),
        )
        assert cur.fetchone()[0] == 1

    # 2b. Concurrent identical registrations under distinct operation_ids:
    # Under a real race, exactly one claims applied and the other claims replayed; exactly one row persisted.
    conc_desc = {
        "contract_version": "research-component.v1",
        "kind": "worker",
        "name": "worker-concurrent",
        "version": "1.0.0",
        "artifact_sha256": "b" * 64,
        "roles": ["analyst"],
        "capabilities": ["experiment.gpu"],
    }
    conc_sha = sha256(conc_desc)
    conc_cmd = {
        "type": "register_research_component",
        "payload": {
            "component_sha256": conc_sha,
            "descriptor": conc_desc,
        },
    }

    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(
            _command,
            service,
            workspace_id,
            conc_cmd,
            operation_id=uuid4(),
        )
        f2 = executor.submit(
            _command,
            service,
            workspace_id,
            conc_cmd,
            operation_id=uuid4(),
        )
        r1 = f1.result()
        r2 = f2.result()

    assert r1[0] == 200, r1
    assert r2[0] == 200, r2
    applied_count = sum(1 for (st, bd) in (r1, r2) if st == 200 and bd.get("result", {}).get("status") == "applied")
    replayed_count = sum(1 for (st, bd) in (r1, r2) if st == 200 and bd.get("result", {}).get("status") == "replayed")
    assert applied_count == 1, f"Expected exactly 1 applied, got {r1} and {r2}"
    assert replayed_count == 1, f"Expected exactly 1 replayed, got {r1} and {r2}"

    with psycopg.connect(**service.config.connection_kwargs("postgres")) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT count(*) FROM omp_research.components WHERE workspace_id = %s AND component_sha256 = %s",
            (workspace_id, conc_sha),
        )
        assert cur.fetchone()[0] == 1

    # 3. Caller digest mismatch -> 409 stale_evidence
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "register_research_component",
            "payload": {
                "component_sha256": "f" * 64,
                "descriptor": descriptor,
            },
        },
    )
    assert status == 409, body
    assert body["error"]["code"] == "stale_evidence"
    assert any("component digest mismatch" in d for d in body["error"].get("diagnostics", []))

    # 4. Validation errors: unknown kind, unknown role, unsorted capabilities, bad token -> 400
    for bad_desc in (
        dict(descriptor, kind="superworker"),
        dict(descriptor, roles=["lead_architect"]),
        dict(descriptor, capabilities=["retrieval.web", "experiment.gpu"]),
        dict(descriptor, capabilities=["Experiment.Gpu"]),
        dict(descriptor, capabilities=["singleword"]),
    ):
        status, body = _command(
            service,
            workspace_id,
            {
                "type": "register_research_component",
                "payload": {
                    "component_sha256": sha256(bad_desc),
                    "descriptor": bad_desc,
                },
            },
        )
        assert status == 400, (bad_desc, body)

    # 5. Immutability: direct app-role UPDATE/DELETE fails
    with (
        psycopg.connect(**service.config.connection_kwargs("omp_work_app")) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
            (str(workspace_id), str(OWNER)),
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute(
                "UPDATE omp_research.components SET descriptor = %s WHERE component_sha256 = %s",
                (json.dumps(descriptor), comp_sha),
            )
        conn.rollback()

        cur.execute(
            "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
            (str(workspace_id), str(OWNER)),
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute(
                "DELETE FROM omp_research.components WHERE component_sha256 = %s",
                (comp_sha,),
            )
        conn.rollback()

    # Trigger test with postgres superuser role
    with (
        psycopg.connect(**service.config.connection_kwargs("postgres")) as conn,
        conn.cursor() as cur,
    ):
        with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
            cur.execute(
                "UPDATE omp_research.components SET descriptor = %s WHERE component_sha256 = %s",
                (json.dumps(descriptor), comp_sha),
            )
        conn.rollback()
        with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
            cur.execute(
                "DELETE FROM omp_research.components WHERE component_sha256 = %s",
                (comp_sha,),
            )
        conn.rollback()


def test_admission_binds_compatibility_and_refuses_unknown_or_mismatched_components(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "compatibility admission target")
    work_id = UUID(item["work_id"])
    rev_id = UUID(item["revision_id"])

    camp_id = uuid4()
    spec, spec_sha = _sample_spec()
    _command(
        service,
        workspace_id,
        {
            "type": "create_research_campaign",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "revision_id": str(rev_id),
                "domain": "machine_learning",
                "spec": spec,
                "spec_sha256": spec_sha,
            },
        },
    )

    manifest, manifest_sha = _manifest()

    # 1. Unregistered policy -> 400 invalid_request
    unregistered_policy = "0" * 64
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "admit_research_campaign",
            "payload": _admit_payload(
                camp_id, work_id, rev_id, spec_sha, unregistered_policy, manifest, manifest_sha
            ),
        },
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"
    assert any("unknown policy component" in d for d in body["error"].get("diagnostics", []))

    # 2. Evaluator component used as policy -> 400 invalid_request
    eval_sha = _register_component(service, workspace_id, "evaluator", name="eval-as-policy")
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "admit_research_campaign",
            "payload": _admit_payload(
                camp_id, work_id, rev_id, spec_sha, eval_sha, manifest, manifest_sha
            ),
        },
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"
    assert any("policy fingerprint is not a policy component" in d for d in body["error"].get("diagnostics", []))

    # Register real policy
    policy_sha = _register_component(service, workspace_id, "policy", name="p-main")

    # 3. Manifest naming unregistered component -> 400 invalid_request
    unreg_worker = "1" * 64
    unreg_manifest, unreg_manifest_sha = _manifest(workers=(unreg_worker,))
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "admit_research_campaign",
            "payload": _admit_payload(
                camp_id, work_id, rev_id, spec_sha, policy_sha, unreg_manifest, unreg_manifest_sha
            ),
        },
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"
    assert any("unknown worker component" in d for d in body["error"].get("diagnostics", []))

    # 4. Manifest naming component of wrong kind -> 400 invalid_request
    wrong_axis_manifest, wrong_axis_manifest_sha = _manifest(workers=(eval_sha,))
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "admit_research_campaign",
            "payload": _admit_payload(
                camp_id, work_id, rev_id, spec_sha, policy_sha, wrong_axis_manifest, wrong_axis_manifest_sha
            ),
        },
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"
    assert any("worker fingerprint is not a worker component" in d for d in body["error"].get("diagnostics", []))

    # 5. Manifest digest mismatch -> 409 stale_evidence
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "admit_research_campaign",
            "payload": _admit_payload(
                camp_id, work_id, rev_id, spec_sha, policy_sha, manifest, "e" * 64
            ),
        },
    )
    assert status == 409, body
    assert body["error"]["code"] == "stale_evidence"
    assert any("compatibility manifest digest mismatch" in d for d in body["error"].get("diagnostics", []))

    # 6. Valid admission -> applied, HTTP readback matches
    worker_sha = _register_component(service, workspace_id, "worker", name="w-valid")
    valid_manifest, valid_manifest_sha = _manifest(
        workers=(worker_sha,), evaluators=(eval_sha,)
    )
    valid_admit = _admit_payload(
        camp_id, work_id, rev_id, spec_sha, policy_sha, valid_manifest, valid_manifest_sha
    )
    status, body = _command(
        service,
        workspace_id,
        {"type": "admit_research_campaign", "payload": valid_admit},
    )
    assert status == 200, body
    assert body["result"]["status"] == "applied"
    assert body["result"]["campaign"]["compatibility_sha256"] == valid_manifest_sha
    assert body["result"]["campaign"]["compatibility"] == valid_manifest

    # HTTP readback
    resp = service.client.get(
        f"/v1/work-items/{item['key']}/research", headers=_owner_headers(workspace_id)
    )
    assert resp.status_code == 200
    camp_read = next(c for c in resp.json()["campaigns"] if c["campaign_id"] == str(camp_id))
    assert camp_read["compatibility_sha256"] == valid_manifest_sha
    assert camp_read["compatibility"] == valid_manifest

    # 7. Identical replay -> replayed
    status_rep, body_rep = _command(
        service,
        workspace_id,
        {"type": "admit_research_campaign", "payload": valid_admit},
        operation_id=uuid4(),
    )
    assert status_rep == 200, body_rep
    assert body_rep["result"]["status"] == "replayed"

    # 8. Different manifest replay -> idempotency_conflict
    diff_manifest, diff_manifest_sha = _manifest(workers=(worker_sha,))
    diff_admit = _admit_payload(
        camp_id, work_id, rev_id, spec_sha, policy_sha, diff_manifest, diff_manifest_sha
    )
    status_diff, body_diff = _command(
        service,
        workspace_id,
        {"type": "admit_research_campaign", "payload": diff_admit},
    )
    assert status_diff == 409, body_diff
    assert body_diff["error"]["code"] == "idempotency_conflict"

    # 9. Direct SQL alteration of compatibility raises
    with (
        psycopg.connect(**service.config.connection_kwargs("omp_work_app")) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
            (str(workspace_id), str(OWNER)),
        )
        with pytest.raises(psycopg.errors.RaiseException, match="campaign compatibility is immutable once set"):
            cur.execute(
                "UPDATE omp_research.campaigns SET compatibility_sha256 = %s WHERE campaign_id = %s",
                ("7" * 64, camp_id),
            )
        conn.rollback()


def test_trial_proposal_fails_closed_on_unlisted_identity(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "unlisted identity target")
    work_id = UUID(item["work_id"])
    rev_id = UUID(item["revision_id"])

    camp_id = uuid4()
    spec, spec_sha = _sample_spec()

    policy_sha = _register_component(service, workspace_id, "policy", name="p-trial")
    eval_listed = _register_component(service, workspace_id, "evaluator", name="eval-listed")
    eval_unlisted = _register_component(service, workspace_id, "evaluator", name="eval-unlisted")
    env_listed = _register_component(service, workspace_id, "environment", name="env-listed")
    env_unlisted = _register_component(service, workspace_id, "environment", name="env-unlisted")

    manifest, manifest_sha = _manifest(
        evaluators=(eval_listed,), environments=(env_listed,)
    )

    _command(
        service,
        workspace_id,
        {
            "type": "create_research_campaign",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "revision_id": str(rev_id),
                "domain": "machine_learning",
                "spec": spec,
                "spec_sha256": spec_sha,
            },
        },
    )
    _command(
        service,
        workspace_id,
        {
            "type": "admit_research_campaign",
            "payload": _admit_payload(
                camp_id, work_id, rev_id, spec_sha, policy_sha, manifest, manifest_sha
            ),
        },
    )
    _command(
        service,
        workspace_id,
        {
            "type": "set_research_campaign_state",
            "payload": {
                "campaign_id": str(camp_id),
                "work_id": str(work_id),
                "expected_state": "admitted",
                "target_state": "running",
                "policy_sha256": policy_sha,
            },
        },
    )

    # 1. Registered-but-unlisted evaluator -> 409 stale_evidence
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "propose_research_trial",
            "payload": _trial_payload(
                camp_id, work_id, policy_sha, eval_unlisted, env_listed
            ),
        },
    )
    assert status == 409, body
    assert body["error"]["code"] == "stale_evidence"
    assert any("evaluator fingerprint is not declared compatible" in d for d in body["error"].get("diagnostics", []))

    # 2. Registered-but-unlisted environment -> 409 stale_evidence
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "propose_research_trial",
            "payload": _trial_payload(
                camp_id, work_id, policy_sha, eval_listed, env_unlisted
            ),
        },
    )
    assert status == 409, body
    assert body["error"]["code"] == "stale_evidence"
    assert any("environment fingerprint is not declared compatible" in d for d in body["error"].get("diagnostics", []))

    # 3. Listed pair -> 200 applied
    t_id = uuid4()
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "propose_research_trial",
            "payload": _trial_payload(
                camp_id, work_id, policy_sha, eval_listed, env_listed, trial_id=t_id
            ),
        },
    )
    assert status == 200 and body["result"]["status"] == "applied", body
    assert body["result"]["trial"]["evaluator_sha256"] == eval_listed
    assert body["result"]["trial"]["environment_sha256"] == env_listed

    # 4. HTTP readback: ResearchView.components contains exactly policy + listed components ordered by digest
    resp = service.client.get(
        f"/v1/work-items/{item['key']}/research", headers=_owner_headers(workspace_id)
    )
    assert resp.status_code == 200
    view = resp.json()
    comp_shas = [c["component_sha256"] for c in view["components"]]
    expected_shas = sorted([policy_sha, eval_listed, env_listed])
    assert comp_shas == expected_shas
    assert eval_unlisted not in comp_shas
    assert env_unlisted not in comp_shas


def test_migration_0027_preserves_legacy_campaigns_as_unmanaged(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OMP-R02-S3b migration 0027 rehearsal: pre-0027 campaigns remain readable and drivable, cannot gain manifest or run trials."""
    from types import SimpleNamespace

    import omp_work.operations.database as database_module
    from omp_work.operations.database import bootstrap
    from omp_work.v1.server import create_app
    from pg_native import seed_authority
    from starlette.testclient import TestClient
    from test_workflow_service import _config

    root = tmp_path_factory.mktemp("migration-0027-upgrade")
    config = _config(root)
    with native_postgres(root, config.port):
        original_migrate = database_module.migrate
        monkeypatch.setattr(database_module, "validate_bundle", lambda **kw: None)
        monkeypatch.setattr(
            database_module,
            "migrate",
            lambda cfg, **kw: original_migrate(cfg, target=26, **kw),
        )
        bootstrap(config)
        monkeypatch.setattr(database_module, "migrate", original_migrate)

        workspace_id = uuid4()
        work_id = uuid4()
        revision_id = uuid4()
        camp_id = uuid4()
        trial_id = uuid4()
        now = datetime.now(UTC)
        spec, spec_sha = _sample_spec()
        policy_sha = "a" * 64

        with psycopg.connect(
            **config.connection_kwargs("postgres"), autocommit=True
        ) as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO omp_control.workspaces(workspace_id) VALUES (%s)",
                (workspace_id,),
            )
            cur.execute(
                "INSERT INTO omp_work.work_items(work_id, workspace_id, state) VALUES (%s, %s, 'NOW')",
                (work_id, workspace_id),
            )
            cur.execute(
                "INSERT INTO omp_work.work_revisions(revision_id, work_id, workspace_id, revision_number, title, description, scope, content_sha256, created_by, supplied_at) "
                "VALUES (%s, %s, %s, 1, 'migration 0027 test item', '', '', %s, 'test', %s)",
                (revision_id, work_id, workspace_id, "a" * 64, now),
            )
            cur.execute(
                "UPDATE omp_work.work_items SET current_revision_id=%s WHERE work_id=%s",
                (revision_id, work_id),
            )
            cur.execute(
                "INSERT INTO omp_work.work_aliases(work_id, workspace_id, key, origin, primary_alias) VALUES (%s, %s, 'OMP-27', 'local', true)",
                (work_id, workspace_id),
            )
            # Insert admitted campaign under migration 0026 schema (has outcome, etc., but no compatibility columns)
            cur.execute(
                """
                INSERT INTO omp_research.campaigns(
                    campaign_id, workspace_id, work_id, revision_id,
                    domain, state, spec, spec_sha256, policy_sha256,
                    created_at, admitted_at
                ) VALUES (
                    %s, %s, %s, %s,
                    'machine_learning', 'admitted', %s, %s, %s,
                    %s, %s
                )
                """,
                (
                    camp_id, workspace_id, work_id, revision_id,
                    json.dumps(spec), spec_sha, policy_sha,
                    now, now,
                ),
            )
            # Insert proposed trial under migration 0026 schema
            cur.execute(
                """
                INSERT INTO omp_research.trials(
                    trial_id, workspace_id, campaign_id, work_id, decision_id,
                    candidate_digest, experiment_spec_sha256, evaluator_sha256,
                    environment_sha256, input_manifest_sha256, policy_sha256,
                    state, action, reason
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    'proposed', 'evaluate', 'initial trial'
                )
                """,
                (
                    trial_id, workspace_id, camp_id, work_id, uuid4(),
                    "c" * 64, "e" * 64, "b" * 64,
                    "c" * 64, "d" * 64, policy_sha,
                ),
            )

        # Run migration 0027
        original_migrate(config)

        # Set up capabilities and authority for HTTP tests
        capabilities = root / "capabilities"
        capabilities.mkdir(mode=0o700, exist_ok=True)
        owner = capabilities / "owner.json"
        owner.write_text(
            json.dumps(
                {
                    "token": "owner-token",
                    "actor_id": str(OWNER),
                    "actor_kind": "owner",
                    "workspaces": [str(workspace_id)],
                    "scopes": [
                        "work.read",
                        "work.mutate",
                        "work.approve",
                        "work.close",
                        "work.execute",
                    ],
                }
            )
        )
        owner.chmod(0o600)
        seed_authority(config.connection_kwargs("postgres"), workspace_id, OWNER)

        svc = SimpleNamespace(
            client=TestClient(create_app(config, capabilities_dir=capabilities)),
            capabilities=capabilities,
            config=config,
        )

        item_key = "OMP-27"

        # 1. Read back historical campaign over HTTP: compatibility is None
        resp = svc.client.get(
            f"/v1/work-items/{item_key}/research", headers=_owner_headers(workspace_id)
        )
        assert resp.status_code == 200, resp.json()
        research_data = resp.json()
        camp = research_data["campaigns"][0]
        assert camp["state"] == "admitted"
        assert camp["compatibility"] is None
        assert camp["compatibility_sha256"] is None
        assert camp["policy_sha256"] == policy_sha

        trial = research_data["trials"][0]
        assert trial["trial_id"] == str(trial_id)
        assert trial["action"] == "evaluate"

        # 2. Propose new trial on legacy campaign fails closed with 409 stale_evidence
        new_trial_id = uuid4()
        status, body = _command(
            svc,
            workspace_id,
            {
                "type": "propose_research_trial",
                "payload": {
                    "trial_id": str(new_trial_id),
                    "campaign_id": str(camp_id),
                    "work_id": str(work_id),
                    "decision_id": str(uuid4()),
                    "candidate_digest": "c" * 64,
                    "experiment_spec_sha256": "e" * 64,
                    "evaluator_sha256": "b" * 64,
                    "environment_sha256": "c" * 64,
                    "input_manifest_sha256": "d" * 64,
                    "policy_sha256": policy_sha,
                    "action": "replicate",
                    "reason": "attempt trial on legacy campaign",
                },
            },
        )
        assert status == 409, body
        assert body["error"]["code"] == "stale_evidence"
        assert any("legacy campaign has no compatibility manifest; new trials refused" in d for d in body["error"].get("diagnostics", []))

        # 3. set_research_campaign_state admitted -> running succeeds with original policy_sha
        status, body = _command(
            svc,
            workspace_id,
            {
                "type": "set_research_campaign_state",
                "payload": {
                    "campaign_id": str(camp_id),
                    "work_id": str(work_id),
                    "expected_state": "admitted",
                    "target_state": "running",
                    "policy_sha256": policy_sha,
                },
            },
        )
        assert status == 200 and body["result"]["status"] == "applied", body
        assert body["result"]["campaign"]["state"] == "running"

        # 4. Direct app-role UPDATE trying to add compatibility to legacy row raises
        dummy_manifest, dummy_manifest_sha = _manifest()
        with (
            psycopg.connect(**config.connection_kwargs("omp_work_app")) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
                (str(workspace_id), str(OWNER)),
            )
            with pytest.raises(psycopg.errors.RaiseException, match="legacy campaign cannot gain compatibility after admission"):
                cur.execute(
                    "UPDATE omp_research.campaigns SET compatibility_sha256 = %s, compatibility = %s WHERE campaign_id = %s",
                    (dummy_manifest_sha, json.dumps(dummy_manifest), camp_id),
                )
            conn.rollback()

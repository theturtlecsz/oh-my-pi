"""PostgreSQL custody tests for research artifacts, sources, and datasets (OMP-323)."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from omp_work.v1.canonical import sha256
from omp_work.v1.models import (
    ResearchArtifactManifest,
    ResearchDatasetManifest,
    ResearchSourceManifest,
)
from test_research_contract import _manifest, _sample_spec
from test_workflow_service import (
    OWNER,
    _command,
    _create,
    _grant,
    _owner_headers,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)


def _setup(service, workspace_id: UUID) -> None:
    with psycopg.connect(
        **service.config.connection_kwargs("postgres"), autocommit=True
    ) as conn:
        conn.execute(
            "INSERT INTO omp_control.workspaces(workspace_id) VALUES(%s) ON CONFLICT DO NOTHING",
            (workspace_id,),
        )
    _grant(service, workspace_id)


def _artifact_manifest(data: bytes, **overrides: object) -> tuple[dict[str, object], str]:
    body = {
        "contract_version": "research-artifact.v1",
        "artifact_sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "media_type": "text/plain",
        "name": "artifact.txt",
        "access_class": "workspace",
        "issuer_kind": "candidate_authored",
        "source_ref": "experiments/001/artifact.txt",
        "valid_until": None,
    }
    body.update(overrides)
    model = ResearchArtifactManifest.model_validate(body)
    dumped = model.model_dump(mode="json")
    return dumped, sha256(dumped)


def _register(service, workspace_id: UUID, data: bytes, **overrides: object):
    manifest, manifest_sha = _artifact_manifest(data, **overrides)
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "register_research_artifact",
            "payload": {
                "manifest_sha256": manifest_sha,
                "manifest": manifest,
                "content_base64": base64.b64encode(data).decode("ascii"),
            },
        },
    )
    return status, body, manifest["artifact_sha256"], manifest_sha


def _source_manifest(source_id: UUID, **overrides: object) -> tuple[dict[str, object], str]:
    body: dict[str, object] = {
        "contract_version": "research-source.v1",
        "source_id": str(source_id),
        "version": "1",
        "location": "experiments/001/sample.txt",
        "status": "ok",
        "retention_until": None,
        "access": {"access_class": "workspace", "project_id": None, "decision": None},
        "artifact_sha256": None,
    }
    body.update(overrides)
    model = ResearchSourceManifest.model_validate(body)
    dumped = model.model_dump(mode="json")
    return dumped, sha256(dumped)


def _dataset_manifest(dataset_id: UUID, source_id: UUID, **overrides: object):
    body: dict[str, object] = {
        "contract_version": "research-dataset.v1",
        "dataset_id": str(dataset_id),
        "snapshot_sha256": "b" * 64,
        "source_id": str(source_id),
        "version": "2026-09-26",
        "retention_until": None,
        "access": {"access_class": "workspace", "project_id": None, "decision": None},
        "artifact_sha256": None,
    }
    body.update(overrides)
    model = ResearchDatasetManifest.model_validate(body)
    dumped = model.model_dump(mode="json")
    return dumped, sha256(dumped)


def test_byte_registration_read_mismatch_and_immutability(service) -> None:
    workspace_id = uuid4()
    _setup(service, workspace_id)
    data = b"eval-weights"
    status, body, art_sha, manifest_sha = _register(
        service, workspace_id, data, name="weights.bin", media_type="application/octet-stream"
    )
    assert status == 200, body
    assert body["result"]["status"] == "applied"
    assert body["result"]["artifact"]["artifact_sha256"] == art_sha
    disk = (
        service.config.data_dir
        / "research-artifacts"
        / str(workspace_id)
        / art_sha[:2]
        / art_sha
    )
    assert disk.read_bytes() == data
    assert oct(disk.stat().st_mode & 0o777) == "0o400"

    status_rep, body_rep, _, _ = _register(
        service, workspace_id, data, name="weights.bin", media_type="application/octet-stream"
    )
    assert status_rep == 200, body_rep
    assert body_rep["result"]["status"] == "replayed"

    status_conf, body_conf, _, _ = _register(
        service, workspace_id, data, name="other.bin", media_type="application/octet-stream"
    )
    assert status_conf == 409, body_conf
    assert body_conf["error"]["code"] == "idempotency_conflict"

    bad_manifest, _ = _artifact_manifest(data, name="mismatch.bin")
    status_bad, body_bad = _command(
        service,
        workspace_id,
        {
            "type": "register_research_artifact",
            "payload": {
                "manifest_sha256": "0" * 64,
                "manifest": bad_manifest,
                "content_base64": base64.b64encode(data).decode("ascii"),
            },
        },
    )
    assert status_bad == 409, body_bad
    assert body_bad["error"]["code"] == "stale_evidence"

    other = b"hello"
    bad_manifest, bad_sha = _artifact_manifest(other, artifact_sha256="f" * 64)
    status_hash, body_hash = _command(
        service,
        workspace_id,
        {
            "type": "register_research_artifact",
            "payload": {
                "manifest_sha256": bad_sha,
                "manifest": bad_manifest,
                "content_base64": base64.b64encode(other).decode("ascii"),
            },
        },
    )
    assert status_hash == 409, body_hash
    assert body_hash["error"]["code"] == "stale_evidence"

    status_b64, body_b64 = _command(
        service,
        workspace_id,
        {
            "type": "register_research_artifact",
            "payload": {
                "manifest_sha256": manifest_sha,
                "manifest": body["result"]["artifact"]["manifest"],
                "content_base64": "not valid base64!!!",
            },
        },
    )
    assert status_b64 == 400, body_b64
    assert body_b64["error"]["code"] == "invalid_request"

    read = service.client.get(
        f"/v1/workspaces/{workspace_id}/research-artifacts/{art_sha}",
        headers=_owner_headers(workspace_id),
    )
    assert read.status_code == 200, read.text
    assert base64.b64decode(read.json()["content_base64"]) == data

    with psycopg.connect(**service.config.connection_kwargs("postgres")) as conn:
        conn.execute(
            "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
            (str(workspace_id), str(OWNER)),
        )
        with pytest.raises(psycopg.errors.RaiseException, match="immutable history"):
            conn.execute(
                "UPDATE omp_research.artifacts SET manifest_sha256=%s WHERE artifact_sha256=%s",
                ("1" * 64, art_sha),
            )
        conn.rollback()


def test_reads_refuse_missing_corrupt_foreign_and_expired_bytes(service) -> None:
    workspace_a = uuid4()
    workspace_b = uuid4()
    _setup(service, workspace_a)
    _setup(service, workspace_b)
    data = b"content to verify fail-closed semantics"
    status, _, art_sha, _ = _register(service, workspace_a, data)
    assert status == 200

    missing = service.client.get(
        f"/v1/workspaces/{workspace_a}/research-artifacts/{'0' * 64}",
        headers=_owner_headers(workspace_a),
    )
    assert missing.status_code == 400, missing.text
    assert any("not_found" in item for item in missing.json()["error"]["diagnostics"])

    foreign = service.client.get(
        f"/v1/workspaces/{workspace_b}/research-artifacts/{art_sha}",
        headers=_owner_headers(workspace_b),
    )
    assert foreign.status_code == 400, foreign.text

    corrupt = b"corruptible-bytes"
    _, _, corrupt_sha, _ = _register(service, workspace_a, corrupt, name="corrupt.txt")
    disk = (
        service.config.data_dir
        / "research-artifacts"
        / str(workspace_a)
        / corrupt_sha[:2]
        / corrupt_sha
    )
    disk.chmod(0o600)
    disk.write_bytes(b"tampered")
    disk.chmod(0o400)
    refused = service.client.get(
        f"/v1/workspaces/{workspace_a}/research-artifacts/{corrupt_sha}",
        headers=_owner_headers(workspace_a),
    )
    assert refused.status_code == 503, refused.text
    assert refused.json()["error"]["code"] == "artifact_unavailable"

    gone = b"bytes-to-be-deleted"
    _, _, gone_sha, _ = _register(service, workspace_a, gone, name="gone.txt")
    gone_path = (
        service.config.data_dir
        / "research-artifacts"
        / str(workspace_a)
        / gone_sha[:2]
        / gone_sha
    )
    gone_path.unlink()
    unavailable = service.client.get(
        f"/v1/workspaces/{workspace_a}/research-artifacts/{gone_sha}",
        headers=_owner_headers(workspace_a),
    )
    assert unavailable.status_code == 503, unavailable.text
    restored, restored_body, _, _ = _register(service, workspace_a, gone, name="gone.txt")
    assert restored == 200, restored_body
    assert restored_body["result"]["status"] == "replayed"
    back = service.client.get(
        f"/v1/workspaces/{workspace_a}/research-artifacts/{gone_sha}",
        headers=_owner_headers(workspace_a),
    )
    assert back.status_code == 200, back.text

    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    status_exp, _, exp_sha, _ = _register(
        service, workspace_a, b"expired", name="expired.txt", valid_until=past
    )
    assert status_exp == 200
    expired = service.client.get(
        f"/v1/workspaces/{workspace_a}/research-artifacts/{exp_sha}",
        headers=_owner_headers(workspace_a),
    )
    assert expired.status_code == 409, expired.text
    assert expired.json()["error"]["code"] == "stale_evidence"
    assert (
        service.config.data_dir
        / "research-artifacts"
        / str(workspace_a)
        / exp_sha[:2]
        / exp_sha
    ).is_file()


def test_database_rejects_manifest_digest_mismatch(service) -> None:
    workspace_id = uuid4()
    _setup(service, workspace_id)
    manifest = {
        "contract_version": "research-artifact.v1",
        "artifact_sha256": "b" * 64,
        "size_bytes": 0,
        "media_type": "application/octet-stream",
        "name": "mismatch.bin",
        "access_class": "workspace",
        "issuer_kind": "candidate_authored",
        "source_ref": "qualification/direct-sql",
        "valid_until": None,
    }
    with psycopg.connect(**service.config.connection_kwargs("postgres")) as conn:
        conn.execute(
            "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
            (str(workspace_id), str(OWNER)),
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO omp_research.artifacts "
                "(workspace_id, artifact_sha256, manifest_sha256, manifest) "
                "VALUES (%s, %s, %s, %s::jsonb)",
                (workspace_id, "a" * 64, sha256(manifest), json.dumps(manifest)),
            )
        conn.rollback()


def test_concurrent_registration_is_one_applied_and_one_replayed(service) -> None:
    workspace_id = uuid4()
    _setup(service, workspace_id)
    data = b"concurrent-" + os.urandom(16)
    manifest, manifest_sha = _artifact_manifest(data, name="concurrent.bin")
    command = {
        "type": "register_research_artifact",
        "payload": {
            "manifest_sha256": manifest_sha,
            "manifest": manifest,
            "content_base64": base64.b64encode(data).decode("ascii"),
        },
    }

    def once() -> tuple[int, dict[str, object]]:
        return _command(service, workspace_id, command, operation_id=uuid4())

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(once).result(), pool.submit(once).result()
    assert first[0] == 200, first[1]
    assert second[0] == 200, second[1]
    assert sorted(
        [first[1]["result"]["status"], second[1]["result"]["status"]]
    ) == ["applied", "replayed"]


def test_candidate_reader_cannot_retrieve_artifact_bytes(service) -> None:
    workspace_id = uuid4()
    _setup(service, workspace_id)
    status, body, art_sha, _ = _register(service, workspace_id, b"secret-bytes")
    assert status == 200, body
    reader = service.capabilities / "candidate.json"
    reader.write_text(
        json.dumps(
            {
                "token": "candidate-token",
                "actor_id": str(uuid4()),
                "actor_kind": "candidate",
                "workspaces": [str(workspace_id)],
                "scopes": ["work.candidate.read"],
                "candidate_ids": [str(uuid4())],
            }
        )
    )
    reader.chmod(0o600)
    response = service.client.get(
        f"/v1/workspaces/{workspace_id}/research-artifacts/{art_sha}",
        headers=_owner_headers(workspace_id)
        | {"Authorization": "Bearer candidate-token"},
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "forbidden"


def test_research_view_resolves_only_registered_workspace_artifacts(service) -> None:
    workspace_id = uuid4()
    _setup(service, workspace_id)
    item = _create(service, workspace_id, "artifact resolution")
    work_id = item["work_id"]
    held = b"candidate patch"
    _, _, held_sha, _ = _register(service, workspace_id, held, name="patch.diff")
    descriptor = {
        "contract_version": "research-component.v1",
        "kind": "evaluator",
        "name": "evaluator-a",
        "version": "1",
        "artifact_sha256": held_sha,
        "roles": [],
        "capabilities": [],
    }
    component_sha = sha256(descriptor)
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "register_research_component",
            "payload": {"component_sha256": component_sha, "descriptor": descriptor},
        },
    )
    assert status == 200, body
    policy = {
        "contract_version": "research-component.v1",
        "kind": "policy",
        "name": "policy-a",
        "version": "1",
        "artifact_sha256": "c" * 64,
        "roles": [],
        "capabilities": [],
    }
    policy_sha = sha256(policy)
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "register_research_component",
            "payload": {"component_sha256": policy_sha, "descriptor": policy},
        },
    )
    assert status == 200, body
    environment = {
        "contract_version": "research-component.v1",
        "kind": "environment",
        "name": "env-a",
        "version": "1",
        "artifact_sha256": "d" * 64,
        "roles": [],
        "capabilities": [],
    }
    environment_sha = sha256(environment)
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "register_research_component",
            "payload": {
                "component_sha256": environment_sha,
                "descriptor": environment,
            },
        },
    )
    assert status == 200, body
    spec, spec_sha = _sample_spec()
    campaign_id = uuid4()
    compatibility, compatibility_sha = _manifest(
        evaluators=(component_sha,), environments=(environment_sha,)
    )
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "create_research_campaign",
            "payload": {
                "campaign_id": str(campaign_id),
                "work_id": work_id,
                "revision_id": item["revision_id"],
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
            "payload": {
                "campaign_id": str(campaign_id),
                "work_id": work_id,
                "revision_id": item["revision_id"],
                "spec_sha256": spec_sha,
                "policy_sha256": policy_sha,
                "compatibility": compatibility,
                "compatibility_sha256": compatibility_sha,
            },
        },
    )
    assert status == 200, body
    unregistered = "e" * 64
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "propose_research_trial",
            "payload": {
                "trial_id": str(uuid4()),
                "campaign_id": str(campaign_id),
                "work_id": work_id,
                "decision_id": str(uuid4()),
                "candidate_digest": held_sha,
                "experiment_spec_sha256": unregistered,
                "evaluator_sha256": component_sha,
                "environment_sha256": environment_sha,
                "input_manifest_sha256": "2" * 64,
                "policy_sha256": policy_sha,
                "action": "evaluate",
                "reason": "resolve held bytes only",
            },
        },
    )
    assert status == 200, body
    view = service.client.get(
        f"/v1/work-items/{item['key']}/research",
        headers=_owner_headers(workspace_id),
    )
    assert view.status_code == 200, view.text
    resolved = {row["artifact_sha256"] for row in view.json()["artifacts"]}
    assert resolved == {held_sha}
    assert unregistered not in resolved


def test_source_and_dataset_retention_and_acl(service) -> None:
    workspace_id = uuid4()
    _setup(service, workspace_id)
    data = b"source-bytes"
    _, _, art_sha, _ = _register(service, workspace_id, data, name="source.txt")
    project_id = uuid4()
    denied = uuid4()
    source_id = uuid4()
    manifest, manifest_sha = _source_manifest(
        source_id,
        artifact_sha256=art_sha,
        access={
            "access_class": "project",
            "project_id": str(project_id),
            "decision": "deny",
        },
    )
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "register_research_source",
            "payload": {"manifest_sha256": manifest_sha, "manifest": manifest},
        },
    )
    assert status == 200, body
    denied_read = service.client.get(
        f"/v1/workspaces/{workspace_id}/research-sources/{source_id}/projects/{project_id}",
        headers=_owner_headers(workspace_id),
    )
    assert denied_read.status_code == 403, denied_read.text
    assert "permissions" in " ".join(denied_read.json()["error"]["diagnostics"]).lower() or "deny" in " ".join(
        denied_read.json()["error"]["diagnostics"]
    ).lower()
    bare = service.client.get(
        f"/v1/workspaces/{workspace_id}/research-sources/{source_id}",
        headers=_owner_headers(workspace_id),
    )
    assert bare.status_code == 403, bare.text

    allowed_id = uuid4()
    allowed_manifest, allowed_sha = _source_manifest(
        allowed_id,
        artifact_sha256=art_sha,
        location="experiments/001/allowed.txt",
        access={
            "access_class": "project",
            "project_id": str(project_id),
            "decision": "allow",
        },
    )
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "register_research_source",
            "payload": {"manifest_sha256": allowed_sha, "manifest": allowed_manifest},
        },
    )
    assert status == 200, body
    allowed = service.client.get(
        f"/v1/workspaces/{workspace_id}/research-sources/{allowed_id}/projects/{project_id}",
        headers=_owner_headers(workspace_id),
    )
    assert allowed.status_code == 200, allowed.text
    assert base64.b64decode(allowed.json()["content_base64"]) == data
    wrong_project = service.client.get(
        f"/v1/workspaces/{workspace_id}/research-sources/{allowed_id}/projects/{denied}",
        headers=_owner_headers(workspace_id),
    )
    assert wrong_project.status_code == 403, wrong_project.text

    escaped, escaped_sha = _source_manifest(uuid4(), location="../secret")
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "register_research_source",
            "payload": {"manifest_sha256": escaped_sha, "manifest": escaped},
        },
    )
    assert status == 400, body

    hidden_id = uuid4()
    hidden, hidden_sha = _source_manifest(
        hidden_id,
        status="inaccessible",
        location="source://weights@v1",
        artifact_sha256=art_sha,
    )
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "register_research_source",
            "payload": {"manifest_sha256": hidden_sha, "manifest": hidden},
        },
    )
    assert status == 200, body
    hidden_read = service.client.get(
        f"/v1/workspaces/{workspace_id}/research-sources/{hidden_id}",
        headers=_owner_headers(workspace_id),
    )
    assert hidden_read.status_code == 503, hidden_read.text
    assert any(
        "inaccessible" in item for item in hidden_read.json()["error"]["diagnostics"]
    )

    expired_id = uuid4()
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    expired, expired_sha = _source_manifest(
        expired_id,
        artifact_sha256=art_sha,
        location="experiments/001/old.txt",
        retention_until=past,
    )
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "register_research_source",
            "payload": {"manifest_sha256": expired_sha, "manifest": expired},
        },
    )
    assert status == 200, body
    expired_read = service.client.get(
        f"/v1/workspaces/{workspace_id}/research-sources/{expired_id}",
        headers=_owner_headers(workspace_id),
    )
    assert expired_read.status_code == 409, expired_read.text
    assert expired_read.json()["error"]["code"] == "stale_evidence"

    dataset_id = uuid4()
    dataset, dataset_sha = _dataset_manifest(
        dataset_id,
        allowed_id,
        snapshot_sha256=art_sha,
        artifact_sha256=art_sha,
        access={
            "access_class": "project",
            "project_id": str(project_id),
            "decision": "allow",
        },
    )
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "register_research_dataset",
            "payload": {"manifest_sha256": dataset_sha, "manifest": dataset},
        },
    )
    assert status == 200, body
    dataset_read = service.client.get(
        f"/v1/workspaces/{workspace_id}/research-datasets/{dataset_id}/projects/{project_id}",
        headers=_owner_headers(workspace_id),
    )
    assert dataset_read.status_code == 200, dataset_read.text
    assert base64.b64decode(dataset_read.json()["content_base64"]) == data
    mismatch, mismatch_sha = _dataset_manifest(
        uuid4(),
        allowed_id,
        snapshot_sha256="a" * 64,
        artifact_sha256=art_sha,
    )
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "register_research_dataset",
            "payload": {"manifest_sha256": mismatch_sha, "manifest": mismatch},
        },
    )
    assert status == 409, body
    assert body["error"]["code"] == "stale_evidence"

    blocked_id = uuid4()
    blocked, blocked_sha = _dataset_manifest(blocked_id, hidden_id, snapshot_sha256="b" * 64)
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "register_research_dataset",
            "payload": {"manifest_sha256": blocked_sha, "manifest": blocked},
        },
    )
    assert status == 200, body
    blocked_read = service.client.get(
        f"/v1/workspaces/{workspace_id}/research-datasets/{blocked_id}",
        headers=_owner_headers(workspace_id),
    )
    assert blocked_read.status_code == 503, blocked_read.text
    assert any(
        "inaccessible" in item for item in blocked_read.json()["error"]["diagnostics"]
    )


def test_collection_containment_cache_and_receipt_binding(service) -> None:
    workspace_id = uuid4()
    _setup(service, workspace_id)
    root = service.config.data_dir / "research-collections" / str(workspace_id)
    member = root / "runs" / "out.bin"
    member.parent.mkdir(parents=True)
    payload = b"collected-plain"
    member.write_bytes(payload)
    manifest, manifest_sha = _artifact_manifest(payload, name="out.bin", source_ref="runs/out.bin")
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "collect_research_artifact",
            "payload": {
                "relative_path": "runs/out.bin",
                "archive_member": None,
                "manifest_sha256": manifest_sha,
                "manifest": manifest,
            },
        },
    )
    assert status == 200, body
    assert body["result"]["status"] == "applied"
    art_sha = manifest["artifact_sha256"]

    malicious = io.BytesIO()
    with zipfile.ZipFile(malicious, "w") as archive:
        archive.writestr("../evil.txt", b"nope")
        archive.writestr("ok.txt", b"yes")
    (root / "bad.zip").write_bytes(malicious.getvalue())
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "collect_research_artifact",
            "payload": {
                "relative_path": "../bad.zip",
                "archive_member": "ok.txt",
                "manifest_sha256": manifest_sha,
                "manifest": manifest,
            },
        },
    )
    assert status == 400, body
    safe = io.BytesIO()
    with zipfile.ZipFile(safe, "w") as archive:
        archive.writestr("../evil.txt", b"nope")
    (root / "runs" / "bad.zip").write_bytes(safe.getvalue())
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "collect_research_artifact",
            "payload": {
                "relative_path": "runs/bad.zip",
                "archive_member": None,
                "manifest_sha256": manifest_sha,
                "manifest": manifest,
            },
        },
    )
    assert status == 400, body
    assert "archive" in " ".join(body["error"]["diagnostics"])

    status, body = _command(
        service,
        workspace_id,
        {
            "type": "record_research_cache",
            "payload": {"cache_key": "job-1", "artifact_sha256": art_sha},
        },
    )
    assert status == 200, body
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "claim_research_replicate",
            "payload": {"artifact_sha256": art_sha},
        },
    )
    assert status == 409, body
    assert body["error"]["code"] == "stale_evidence"
    fresh = b"fresh-replicate"
    _, _, fresh_sha, fresh_manifest = _register(service, workspace_id, fresh, name="fresh.txt")
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "claim_research_replicate",
            "payload": {"artifact_sha256": fresh_sha},
        },
    )
    assert status == 200, body
    assert body["result"]["status"] == "applied"
    receipt_id = uuid4()
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "bind_research_receipt_manifest",
            "payload": {
                "receipt_id": str(receipt_id),
                "artifact_sha256": fresh_sha,
                "manifest_sha256": fresh_manifest,
            },
        },
    )
    assert status == 200, body
    assert body["result"]["status"] == "applied"
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "bind_research_receipt_manifest",
            "payload": {
                "receipt_id": str(receipt_id),
                "artifact_sha256": art_sha,
                "manifest_sha256": manifest_sha,
            },
        },
    )
    assert status == 409, body
    assert body["error"]["code"] == "idempotency_conflict"

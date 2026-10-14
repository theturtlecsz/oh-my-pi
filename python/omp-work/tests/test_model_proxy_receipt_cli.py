"""``model-receipts`` exposes only settled receipts of a sealed job, within its workspace."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from omp_work.operations.config import OperationsConfig
from omp_work.orchestrator.service import load_config, main, model_receipts
from omp_work.orchestrator.service import OrchestratorError
from psycopg.types.json import Jsonb
from test_model_proxy import call, proxy, register  # noqa: F401
from test_model_proxy_budget import _admitting_job
from test_workflow_service import service  # noqa: F401

pytestmark = pytest.mark.skipif(os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1", reason="PostgreSQL integration required")


def _config(ctx, tmp_path: Path, *, workspace_id=None, scopes=("work.read",), name="orch"):
    workspace = workspace_id or ctx.workspace_id
    capability = tmp_path / f"{name}-automation.json"
    capability.write_text(json.dumps({"token": "x", "actor_kind": "automation", "scopes": list(scopes), "actor_id": str(ctx.actor_id), "workspaces": [str(ctx.workspace_id), str(workspace)]}))
    capability.chmod(0o600)
    body = {
        "workspace_id": str(workspace),
        "automation_capability_path": str(capability),
        "qualification_path": str(tmp_path / "qualification.json"),
        "lock_map_path": str(tmp_path / "lock-map.json"),
        "verifier_key_path": str(tmp_path / "verifier.key"),
        "allowed_signers": str(tmp_path / "signers"),
        "control_repo": str(tmp_path / "repo"),
        "worktrees_dir": str(tmp_path / "worktrees"),
        "live_checkout": str(tmp_path / "live"),
    }
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(body))
    return path, load_config(path, ctx.service.config)


def _grant(ctx):
    job_id = _admitting_job(ctx, lease_seconds=300)
    grant = register(ctx, job_id=job_id)
    assert call(ctx, grant).status_code == 200
    return job_id


def _sql(ctx, statement, params=()):
    with psycopg.connect(**ctx.service.config.connection_kwargs("postgres")) as conn:
        conn.execute(statement, params)


def _seal(ctx, job_id, *, event=True):
    _sql(ctx, "UPDATE omp_jobs.jobs SET status='sealed' WHERE workspace_id=%s AND job_id=%s", (ctx.workspace_id, job_id))
    if event:
        with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
            ctx.jobs.append_event(cur, workspace_id=ctx.workspace_id, job_id=job_id, kind="settled", actor="worker-1")


def _operation(ctx, kind, job_id, settlement):
    result = {"fingerprint": {"job_id": job_id, "mission_id": None}, "settlement": settlement}
    operation_id = f"op-{uuid4()}"
    _sql(ctx, "INSERT INTO omp_jobs.operations (operation_id, workspace_id, kind, request_sha256, result) VALUES (%s,%s,%s,%s,%s)", (operation_id, ctx.workspace_id, kind, hashlib.sha256(operation_id.encode()).hexdigest(), Jsonb(result)))
    return operation_id


def test_sealed_settled_job_exposes_only_its_model_call_receipts(proxy, tmp_path, capsys, monkeypatch):
    ctx = proxy
    monkeypatch.setattr(OperationsConfig, "defaults", classmethod(lambda cls: ctx.service.config))
    job_id, sibling = _grant(ctx), _grant(ctx)
    decoy_kind = _operation(ctx, "model_call_decoy", job_id, {"model": "decoy"})
    decoy_null = _operation(ctx, "model_call_receipt", job_id, None)
    _seal(ctx, job_id)
    _seal(ctx, sibling)
    path, config = _config(ctx, tmp_path)

    assert main(["model-receipts", "--config", str(path), "--job", job_id]) == 0
    printed = capsys.readouterr().out
    body = json.loads(printed)
    assert body["job_id"] == job_id and body["status"] == "sealed" and body["settled_events"] == 1
    (receipt,) = body["receipts"]
    assert set(receipt) == {"operation_id", "job_id", "mission_id", "model", "settlement"}
    assert receipt["job_id"] == job_id and receipt["model"] == receipt["settlement"]["model"]
    assert receipt["settlement"]["actual_usd"]
    assert decoy_kind not in printed and decoy_null not in printed
    assert "control-secret" not in printed and "provider-secret" not in printed
    assert model_receipts(config, sibling)["receipts"][0]["operation_id"] != receipt["operation_id"]


def test_unknown_and_unsettled_jobs_have_empty_receipts(proxy, tmp_path):
    ctx = proxy
    job_id = _grant(ctx)
    _, config = _config(ctx, tmp_path)
    assert model_receipts(config, "job-missing") == {"job_id": "job-missing", "status": None, "settled_events": 0, "receipts": []}
    unsettled = model_receipts(config, job_id)
    assert unsettled["status"] == "admitted" and unsettled["receipts"] == []
    _seal(ctx, job_id, event=False)
    assert model_receipts(config, job_id)["receipts"] == []


def test_other_workspace_sees_nothing_and_missing_scope_is_refused(proxy, tmp_path, capsys, monkeypatch):
    ctx = proxy
    monkeypatch.setattr(OperationsConfig, "defaults", classmethod(lambda cls: ctx.service.config))
    job_id = _grant(ctx)
    _seal(ctx, job_id)
    _, foreign = _config(ctx, tmp_path, workspace_id=uuid4(), name="foreign")
    assert model_receipts(foreign, job_id)["status"] is None
    path, denied = _config(ctx, tmp_path, scopes=("work.execute",), name="denied")
    with pytest.raises(OrchestratorError) as refused:
        model_receipts(denied, job_id)
    assert refused.value.code == "forbidden"
    assert main(["model-receipts", "--config", str(path), "--job", job_id]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "control-secret" not in captured.err and "provider-secret" not in captured.err

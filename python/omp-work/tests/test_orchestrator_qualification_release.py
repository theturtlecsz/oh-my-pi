"""Submit and dry-run qualification bind proofs to the deployed release."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql

from omp_work.jobs.admission import enqueue_job
from omp_work.jobs.store import NativeJobStore
from omp_work.orchestrator.service import OrchestratorHandler, dry_run, load_config, submit
from omp_work.orchestrator.stages import decide
from test_orchestrator_service import _checks, _decision_count, _orch_jobs, open_world
from test_workflow_service import OWNER

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)


@pytest.fixture(autouse=True)
def clear_release(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMP_ORCHESTRATOR_RELEASE", raising=False)


def _reload(service, world: dict, release: str | None) -> None:
    path = world["config_path"]
    document = json.loads(path.read_text())
    if release is None:
        document.pop("release", None)
    else:
        document["release"] = release
    path.write_text(json.dumps(document))
    world["config"] = load_config(path, service.config)


def _snapshot(service, world: dict) -> tuple:
    """All Work table counts and input bytes must survive dry-run unchanged."""
    with psycopg.connect(**service.config.connection_kwargs("postgres")) as conn:
        tables = conn.execute(
            "SELECT schemaname, tablename FROM pg_tables "
            "WHERE starts_with(schemaname, 'omp_') ORDER BY schemaname, tablename"
        ).fetchall()
        counts = tuple(
            (
                schema,
                table,
                conn.execute(
                    sql.SQL("SELECT count(*) FROM {}.{}").format(
                        sql.Identifier(schema), sql.Identifier(table)
                    )
                ).fetchone()[0],
            )
            for schema, table in tables
        )
    proof = world["config"].qualification_path
    return counts, world["config_path"].read_bytes(), proof.read_bytes() if proof.exists() else None


def _assert_gate(service, world: dict, *, match: bool) -> None:
    before = _snapshot(service, world)
    preview = dry_run(world["config"], world["request"])
    assert preview["rule_id"] == ("stage-dispatch" if match else "release-qualification")
    assert _snapshot(service, world) == before
    submitted = submit(world["config"], world["request"])
    jobs = _orch_jobs(service, world["workspace_id"])
    if match:
        assert submitted["status"] == "enqueued"
        assert submitted["job_id"] == f"orch:{world['mission_id']}:0"
        assert [job["job_id"] for job in jobs] == [submitted["job_id"]]
        assert _decision_count(service, world["workspace_id"]) == 0
    else:
        assert submitted["status"] == "refused"
        assert submitted["code"] == "release-qualification"
        assert submitted["job_id"] is None
        assert jobs == []
        # Refusal still persists the work item and owner decision.
        assert submitted["work_id"] is not None
        assert submitted["decision_id"] is not None
        assert _decision_count(service, world["workspace_id"]) == 1


@pytest.mark.parametrize(
    "case",
    [
        "wrong-release",
        "prior-release",
        "envelope-only",
        "entry-only",
        "missing-file",
        "absent-lock",
        "missing-identity-labelled",
        "missing-identity-empty",
        "match",
    ],
)
def test_unattended_release_gate(service, tmp_path: Path, case: str) -> None:
    proof = _checks(locks=True)
    identity: str | None = "r1"
    if case in ("wrong-release", "prior-release"):
        release = "other" if case == "wrong-release" else "r0"
        proof["release"] = release
        for entry in proof["results"].values():
            entry["release"] = release
    elif case == "envelope-only":
        proof["release"] = "r0"
    elif case == "entry-only":
        proof["results"]["lock-12"]["release"] = "r0"
    elif case == "absent-lock":
        del proof["results"]["lock-12"]
    elif case.startswith("missing-identity"):
        identity = None
        if case == "missing-identity-empty":
            proof["release"] = ""
            for entry in proof["results"].values():
                entry["release"] = ""
    world = open_world(service, tmp_path, unattended=True, qualification=proof)
    _reload(service, world, identity)
    if case == "missing-file":
        world["config"].qualification_path.unlink()
    _assert_gate(service, world, match=case == "match")


@pytest.mark.parametrize(
    ("json_release", "env_release", "expected", "match"),
    [
        ("r1", "r0", "r1", True),
        ("r0", "r1", "r0", False),
        ("", "r1", "r1", True),
        (None, "r1", "r1", True),
        ("", None, "", False),
        (None, None, "", False),
    ],
)
def test_release_config_precedence(
    service,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    json_release: str | None,
    env_release: str | None,
    expected: str,
    match: bool,
) -> None:
    world = open_world(service, tmp_path, unattended=True, qualification=_checks(locks=True))
    if env_release is not None:
        monkeypatch.setenv("OMP_ORCHESTRATOR_RELEASE", env_release)
    _reload(service, world, json_release)
    assert world["config"].release == expected
    _assert_gate(service, world, match=match)


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("shape", ["envelope", "inherited", "flat"])
def test_attended_proof_identity_fallback_and_decoding(
    service, tmp_path: Path, configured: bool, shape: str,
) -> None:
    proof = _checks()
    if shape == "inherited":
        del proof["results"]["jobs-worker-kill-restart"]["release"]
    elif shape == "flat":
        proof = proof["results"]
    world = open_world(service, tmp_path, qualification=proof)
    _reload(service, world, "r0" if configured else None)
    _assert_gate(service, world, match=not configured)


@pytest.mark.parametrize("case", ["prior-release", "envelope-only", "empty-identity"])
def test_running_unattended_mission_rechecks_release(service, tmp_path: Path, case: str) -> None:
    world = open_world(service, tmp_path, unattended=True, qualification=_checks(locks=True))
    _reload(service, world, "r1")
    assert submit(world["config"], world["request"])["status"] == "enqueued"
    handler = OrchestratorHandler(world["config"])
    handler._bind(world["mission_id"])

    proof = _checks(locks=True)
    proof["release"] = "" if case == "empty-identity" else "r0"
    if case != "envelope-only":
        for entry in proof["results"].values():
            entry["release"] = proof["release"]
    if case == "empty-identity":
        handler.config = replace(handler.config, release="")
    handler.config.qualification_path.write_text(json.dumps(proof))

    chosen = decide(handler._facts(handler.steps(), 0, "implement", None), handler._bounds("implement"))
    assert chosen.kind == "pause"
    assert chosen.rule_id == "release-qualification"

    handler.config = replace(handler.config, release="r1")
    handler.config.qualification_path.write_text(json.dumps(_checks(locks=True)))
    chosen = decide(handler._facts(handler.steps(), 0, "implement", None), handler._bounds("implement"))
    assert chosen.kind == "dispatch"
    assert chosen.rule_id == "stage-dispatch"


@pytest.mark.parametrize("unattended", [True, False])
def test_capacity_requires_current_release_rollout(service, tmp_path: Path, unattended: bool) -> None:
    proof = _checks(locks=unattended)
    proof["results"]["omp16-rollout"] = {"result": "pass", "release": "r0"}
    world = open_world(service, tmp_path, unattended=unattended, qualification=proof)
    _reload(service, world, "r1" if unattended else None)
    world["config"] = replace(world["config"], max_workers=4)
    submitted = submit(world["config"], world["request"])
    assert submitted["status"] == "enqueued"

    blocker = f"blocker-{uuid4()}"
    enqueue_job(
        NativeJobStore(service.config),
        operation_id=str(uuid4()),
        workspace_id=world["workspace_id"],
        actor_id=OWNER,
        job_id=blocker,
        work_id=UUID(submitted["work_id"]),
        kind="compute",
        required_capabilities=["omp.orchestrator"],
        resources={"cpu": 1, "memory_mib": 1, "gpu": 0, "model_calls": 0},
        lease_seconds=30,
    )
    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute(
            "UPDATE omp_jobs.jobs SET status='admitted' WHERE workspace_id=%s AND job_id=%s",
            (world["workspace_id"], blocker),
        )
    handler = OrchestratorHandler(world["config"])
    handler._bind(world["mission_id"])
    assert handler._capacity_free() is False

    proof["results"]["omp16-rollout"]["release"] = "r1"
    handler.config.qualification_path.write_text(json.dumps(proof))
    assert handler._capacity_free() is True
    if unattended:
        handler.config = replace(handler.config, release="")
        assert handler._capacity_free() is False


def test_running_attended_mission_uses_proof_release_without_identity(service, tmp_path: Path) -> None:
    world = open_world(service, tmp_path, qualification=_checks())
    _reload(service, world, None)
    assert submit(world["config"], world["request"])["status"] == "enqueued"
    handler = OrchestratorHandler(world["config"])
    handler._bind(world["mission_id"])
    chosen = decide(handler._facts(handler.steps(), 0, "implement", None), handler._bounds("implement"))
    assert chosen.kind == "dispatch"
    assert chosen.rule_id == "stage-dispatch"

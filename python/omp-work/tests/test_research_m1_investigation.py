"""M1 proof: run, evaluate, recover, and reproduce one engineering investigation.

The path is a client ``mission.submit`` of kind ``research.run``, then an
approved link, then managed trials on a real worker process. One worker is
killed during a trial. A new worker settles that trial once. A cancelled
trial records nothing. The broken trial does not bind. The winner binds
after a review root. A clean copy of the bundle reproduces only when the
confirmation bytes are supplied beside it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from omp_work import contract_sha256
from omp_work.jobs.admission import claim_job, enqueue_job
from omp_work.jobs.cancel import cancel_job
from omp_work.jobs.lease import settle_job
from omp_work.jobs.managed_trial import CAPABILITY, ManagedTrialRefused, dispatch_managed_trial
from omp_work.jobs.store import NativeJobStore
from omp_work.research.bundle import build_bundle, verify_bundle
from omp_work.research.engineering import freeze_candidate
from omp_work.research.receipt import (
    MANAGED_CAPABILITY,
    MANAGED_POLICY,
    EvaluationProtocol,
    managed_job_id,
)
from omp_work.v1.canonical import sha256
from test_jobs_worker_process import _config_dict
from test_mission_links_store import _BUDGET, _ITEM_A, _approve, _draft, _link, _seed_budget
from test_research_contract import (
    _admit_payload,
    _manifest,
    _plan_and_finalize,
    _register_component,
    _sample_spec,
    _trial_payload,
)
from test_research_runner import _unshare_probe_fails
from test_restart_where_are_we import _kill, _start_service, _start_worker
from test_workflow_service import OWNER, _command, _create, _seed_project

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
        reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
    ),
    pytest.mark.skipif(_unshare_probe_fails(), reason="unshare probe failed"),
]
pytest_plugins = ("test_workflow_service", "native_jobs_support")

_CLIENT_TOKEN = "m1-client-token"
_RESOURCES = {"cpu": 1, "memory_mib": 0, "gpu": 0, "model_calls": 0}
_CONFIRMATION = b"m1-confirmation-label\n"
_RESULT_KEYS = [
    "format",
    "trial_id",
    "campaign_id",
    "receipt_sha256",
    "candidate_digest",
    "verdict",
    "primary_metric",
    "direction",
    "metrics",
    "description",
]

_EVALUATOR = r"""
import json
import pathlib

handoff = json.loads(pathlib.Path("input.json").read_text(encoding="utf-8"))
confirmation = handoff.get("confirmation")
conf_readable = confirmation is not None and pathlib.Path(confirmation).is_file()
outputs = handoff.get("outputs") or {}
seconds = None
leaked = None
if outputs:
    data = json.loads(pathlib.Path(next(iter(outputs.values()))).read_text(encoding="utf-8"))
    seconds = data.get("seconds")
    leaked = data.get("leaked")
score = float(seconds) if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) else 0.0
valid = (
    bool(outputs)
    and isinstance(seconds, (int, float))
    and not isinstance(seconds, bool)
    and conf_readable
    and leaked == []
)
pathlib.Path("score.json").write_text(
    json.dumps({"valid": valid, "metrics": {"seconds": score}}),
    encoding="utf-8",
)
"""

_HEARTBEAT = r"""
import json
import os
import pathlib
import time

path = pathlib.Path("heartbeat")
deadline = time.monotonic() + 6.0
while time.monotonic() < deadline:
    with path.open("ab") as handle:
        handle.write(b"x")
        handle.flush()
        os.fsync(handle.fileno())
    time.sleep(0.05)
pathlib.Path("out").mkdir(parents=True, exist_ok=True)
pathlib.Path("out/result.json").write_text(
    json.dumps({"seconds": 1.5, "leaked": []}),
    encoding="utf-8",
)
"""

_SLEEPER = r"""
import os
import pathlib
import time

path = pathlib.Path("heartbeat")
while True:
    with path.open("ab") as handle:
        handle.write(b"x")
        handle.flush()
        os.fsync(handle.fileno())
    time.sleep(0.05)
"""


def _score_source(seconds: float) -> str:
    return (
        "import json\n"
        "import pathlib\n"
        "pathlib.Path('out').mkdir(parents=True, exist_ok=True)\n"
        "pathlib.Path('out/result.json').write_text(\n"
        f"    json.dumps({{'seconds': {seconds}, 'leaked': []}}),\n"
        "    encoding='utf-8',\n"
        ")\n"
    )


def _broken_source() -> str:
    return "import sys\nsys.exit(1)\n"


def _candidate_repo(tmp_path: Path, source: str) -> Path:
    repo = tmp_path / f"candidate-repo-{uuid4()}"
    repo.mkdir()
    (repo / "candidate.py").write_text(source, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "-m",
            "candidate",
        ],
        check=True,
    )
    return repo


def _freeze(tmp_path: Path, source: str) -> tuple[bytes, str]:
    return freeze_candidate(_candidate_repo(tmp_path, source), "HEAD")


def _register_descriptor(service, workspace_id: UUID, descriptor: dict[str, object]) -> str:
    component_sha256 = sha256(descriptor)
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "register_research_component",
            "payload": {
                "component_sha256": component_sha256,
                "descriptor": descriptor,
            },
        },
    )
    assert status == 200, body
    return component_sha256


def _register_artifact(service, workspace_id: UUID, data: bytes, name: str) -> str:
    digest = hashlib.sha256(data).hexdigest()
    manifest = {
        "contract_version": "research-artifact.v1",
        "artifact_sha256": digest,
        "size_bytes": len(data),
        "media_type": "application/json",
        "name": name,
        "access_class": "workspace",
        "issuer_kind": "candidate_authored",
        "source_ref": f"m1/{name}",
        "valid_until": None,
    }
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "register_research_artifact",
            "payload": {
                "manifest_sha256": sha256(manifest),
                "manifest": manifest,
                "content_base64": base64.b64encode(data).decode("ascii"),
            },
        },
    )
    assert status == 200, body
    return digest


def _write_client_capability(capabilities: Path, workspace_id: UUID) -> None:
    path = capabilities / "m1-client.json"
    path.write_text(
        json.dumps(
            {
                "token": _CLIENT_TOKEN,
                "actor_id": str(uuid4()),
                "actor_kind": "client",
                "workspaces": [str(workspace_id)],
                "scopes": ["work.client", "work.mutate"],
            }
        )
    )
    path.chmod(0o600)


def _store(native_jobs) -> NativeJobStore:
    return NativeJobStore(native_jobs.service.config)


def _job_row(store: NativeJobStore, workspace_id: UUID, job_id: str) -> dict[str, object]:
    with store.transaction(workspace_id, OWNER) as cur:
        cur.execute(
            "SELECT status FROM omp_jobs.jobs WHERE workspace_id=%s AND job_id=%s",
            (workspace_id, job_id),
        )
        row = cur.fetchone()
    assert row is not None, job_id
    return row


def _observations(store: NativeJobStore, workspace_id: UUID, trial_id: UUID) -> list[dict[str, object]]:
    with store.transaction(workspace_id, OWNER) as cur:
        cur.execute(
            """
            SELECT issuer_kind, source_ref, payload
            FROM omp_research.observations
            WHERE workspace_id=%s AND trial_id=%s
            ORDER BY recorded_at
            """,
            (workspace_id, trial_id),
        )
        return list(cur.fetchall())


def _artifact_count(store: NativeJobStore, workspace_id: UUID, job_id: str) -> int:
    with store.transaction(workspace_id, OWNER) as cur:
        cur.execute(
            "SELECT count(*) AS n FROM omp_research.artifacts"
            " WHERE workspace_id=%s AND manifest->>'source_ref'=%s",
            (workspace_id, f"job:{job_id}"),
        )
        row = cur.fetchone()
    assert row is not None
    return int(row["n"])


def _tail(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")[-4000:]


def _wait_terminal(
    store: NativeJobStore,
    workspace_id: UUID,
    job_id: str,
    proc: subprocess.Popen,
    log_path: Path,
    *,
    timeout: float,
) -> str:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        last = str(_job_row(store, workspace_id, job_id)["status"])
        if last in {"sealed", "failed", "cancelled"}:
            return last
        if proc.poll() is not None:
            raise AssertionError(
                f"worker exited {proc.returncode} while {job_id} was {last}\n{_tail(log_path)}"
            )
        time.sleep(0.1)
    raise AssertionError(f"{job_id} stayed {last}\n{_tail(log_path)}")


def _payload(row: dict[str, object]) -> dict[str, object]:
    value = row["payload"]
    if isinstance(value, str):
        parsed = json.loads(value)
        assert isinstance(parsed, dict)
        return parsed
    assert isinstance(value, dict)
    return value


def _worker_config(
    native_jobs,
    tmp_path: Path,
    *,
    worker_id: str,
    component: str,
    work_url: str,
    content_dir: Path,
    scratch_dir: Path,
) -> Path:
    path = tmp_path / f"{worker_id}.json"
    path.write_text(
        json.dumps(
            {
                "workspace_id": str(native_jobs.workspace_id),
                "actor_id": str(OWNER),
                "worker_id": worker_id,
                "component_sha256": component,
                "capacity": 4,
                "idle_sleep": 0.05,
                "operations": _config_dict(native_jobs.service.config),
                "handlers": {
                    CAPABILITY: {
                        "factory": "omp_work.jobs.managed_trial:factory",
                        "options": {
                            "content_dir": str(content_dir),
                            "scratch_dir": str(scratch_dir),
                            "work_url": work_url,
                            "bearer_file": str(native_jobs.service.capabilities / "owner.json"),
                            "workspace_id": str(native_jobs.workspace_id),
                        },
                    }
                },
            }
        )
    )
    return path


def _growing_heartbeat(scratch: Path, proc: subprocess.Popen, log_path: Path) -> Path:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"worker exited before the heartbeat\n{_tail(log_path)}")
        for path in scratch.rglob("heartbeat"):
            if not path.is_file():
                continue
            before = path.stat().st_size
            time.sleep(0.15)
            if path.stat().st_size > before:
                return path
        time.sleep(0.05)
    raise AssertionError(f"no growing heartbeat\n{_tail(log_path)}")


def _stopped_heartbeat(path: Path) -> None:
    killed = time.monotonic()
    last = path.stat().st_size
    last_change = killed
    while time.monotonic() - killed < 2:
        time.sleep(0.05)
        size = path.stat().st_size
        if size != last:
            last = size
            last_change = time.monotonic()
    assert last_change - killed < 2
    time.sleep(0.4)
    assert path.stat().st_size == last


def _dispatch(store: NativeJobStore, workspace_id: UUID, work_id: UUID, trial_id: UUID, lease: int) -> str:
    dispatch_managed_trial(
        store,
        operation_id=str(uuid4()),
        workspace_id=workspace_id,
        actor_id=OWNER,
        work_id=work_id,
        trial_id=trial_id,
        resources=_RESOURCES,
        lease_seconds=lease,
    )
    return managed_job_id(trial_id)


def _evidence() -> dict[str, object]:
    return {
        "id": f"ev-{uuid4()}",
        "kind": "receipt",
        "content_digest": "abcd1234ef",
        "trust": "trusted",
    }


def _bind(service, workspace_id: UUID, item: dict[str, object], campaign_id: UUID, trial_id: UUID, digest: str, candidate_id: UUID):
    identity = {
        "candidate_digest": digest,
        "campaign_id": str(campaign_id),
        "native_candidate_id": str(candidate_id),
        "revision_id": str(item["revision_id"]),
        "trial_id": str(trial_id),
        "work_id": str(item["work_id"]),
        "workspace_id": str(workspace_id),
    }
    return _command(
        service,
        workspace_id,
        {
            "type": "bind_research_deliverable",
            "payload": {
                "trial_id": str(trial_id),
                "campaign_id": str(campaign_id),
                "work_id": str(item["work_id"]),
                "revision_id": str(item["revision_id"]),
                "candidate_digest": digest,
                "native_candidate_id": str(candidate_id),
                "binding_sha256": sha256(identity),
            },
        },
    )


def _clean_env(home: Path) -> dict[str, str]:
    return {"PATH": os.environ["PATH"], "HOME": str(home)}


def test_m1_investigation_runs_evaluates_recovers_and_reproduces(native_jobs, tmp_path: Path) -> None:
    service = native_jobs.service
    workspace_id = native_jobs.workspace_id
    store = _store(native_jobs)
    item = _create(service, workspace_id, f"m1-investigation-{uuid4()}")
    work_id = UUID(item["work_id"])
    _seed_budget(
        service,
        workspace_id,
        work_id=item["work_id"],
        revision_id=item["revision_id"],
        budget=_ITEM_A,
    )
    project_id = uuid4()
    _seed_project(service, workspace_id, project_id, "M1 investigation")
    _write_client_capability(service.capabilities, workspace_id)

    mission_id = uuid4()
    draft = _draft(
        project_id,
        kind="research.run",
        requested_capabilities=[MANAGED_CAPABILITY],
        objective="Run one engineering investigation",
        budget_policy=_BUDGET,
    )
    response = service.client.post(
        f"/v1/workspaces/{workspace_id}/client/missions",
        headers={
            "Authorization": f"Bearer {_CLIENT_TOKEN}",
            "X-OMP-Contract-SHA256": contract_sha256(),
        },
        json={
            "request_id": str(uuid4()),
            "payload": {"mission_id": str(mission_id), "draft": draft},
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["operation"] == "mission.submit"
    assert body["result"]["mission"]["kind"] == "research.run"
    assert MANAGED_CAPABILITY in body["result"]["mission"]["requested_capabilities"]

    with pytest.raises(ManagedTrialRefused) as refused:
        dispatch_managed_trial(
            store,
            operation_id=str(uuid4()),
            workspace_id=workspace_id,
            actor_id=OWNER,
            work_id=work_id,
            trial_id=uuid4(),
            resources=_RESOURCES,
            lease_seconds=30,
        )
    assert refused.value.code == "mission_not_authorized"
    with store.transaction(workspace_id, OWNER) as cur:
        cur.execute(
            "SELECT count(*) AS n FROM omp_jobs.jobs WHERE workspace_id=%s AND work_id=%s",
            (workspace_id, work_id),
        )
        row = cur.fetchone()
    assert row is not None
    assert int(row["n"]) == 0

    approved = _approve(service, workspace_id, mission_id, revision=1)
    assert approved["status"] == "approved"
    assert approved["kind"] == "research.run"
    status, linked = _link(service, workspace_id, mission_id, item["work_id"])
    assert status == 200, linked

    confirmation_sha = hashlib.sha256(_CONFIRMATION).hexdigest()
    protocol = EvaluationProtocol.model_validate(
        {
            "contract_version": "research-protocol.v1",
            "metrics": [{"name": "seconds", "direction": "lower"}],
            "primary": "seconds",
            "tolerance": {"seconds": 0.0},
            "candidate_command": ["python3", "candidate.py"],
            "evaluator_command": ["python3", "evaluate"],
            "outputs": ["out/result.json"],
            "requires": ["python3"],
            "confirmation_sha256": confirmation_sha,
            "timeout_seconds": 60,
        }
    )
    protocol_bytes = protocol.protocol_bytes()
    protocol_sha = hashlib.sha256(protocol_bytes).hexdigest()
    assert _register_artifact(service, workspace_id, protocol_bytes, "protocol.json") == protocol_sha

    evaluator_script = _EVALUATOR.encode("utf-8")
    evaluator_artifact = hashlib.sha256(evaluator_script).hexdigest()
    sources = {
        "baseline": (_score_source(2.0), "baseline"),
        "faster": (_score_source(1.0), "faster candidate"),
        "broken": (_broken_source(), "broken candidate"),
        "restart": (_HEARTBEAT, None),
        "sleeper": (_SLEEPER, "sleep until cancel"),
    }
    frozen = {
        name: _freeze(tmp_path, source) for name, (source, _reason) in sources.items()
    }
    content_dir = tmp_path / "content"
    scratch_dir = tmp_path / "scratch"
    content_dir.mkdir(mode=0o700)
    scratch_dir.mkdir(mode=0o700)
    (content_dir / protocol_sha).write_bytes(protocol_bytes)
    (content_dir / evaluator_artifact).write_bytes(evaluator_script)
    (content_dir / confirmation_sha).write_bytes(_CONFIRMATION)
    for tar, digest in frozen.values():
        (content_dir / digest).write_bytes(tar)

    spec, _spec_sha = _sample_spec()
    spec["evaluation_protocol_sha256"] = protocol_sha
    spec_sha = sha256(spec)
    worker_component = _register_component(
        service,
        workspace_id,
        "worker",
        name=f"m1-worker-{uuid4()}",
        capabilities=(CAPABILITY,),
    )
    evaluator_component = _register_descriptor(
        service,
        workspace_id,
        {
            "contract_version": "research-component.v1",
            "kind": "evaluator",
            "name": f"m1-evaluator-{uuid4()}",
            "version": "1",
            "artifact_sha256": evaluator_artifact,
            "roles": [],
            "capabilities": [],
        },
    )
    policy_component = _register_descriptor(
        service,
        workspace_id,
        {
            "contract_version": "research-component.v1",
            "kind": "policy",
            "name": MANAGED_POLICY,
            "version": "1",
            "artifact_sha256": sha256({"managed-policy": uuid4().hex}),
            "roles": [],
            "capabilities": [],
        },
    )
    manifest, manifest_sha = _manifest(
        workers=(worker_component,),
        evaluators=(evaluator_component,),
        audits=(native_jobs.components["audit"],),
        releases=(native_jobs.components["release"],),
        environments=(native_jobs.components["environment"],),
    )
    campaign_id = uuid4()
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "create_research_campaign",
            "payload": {
                "campaign_id": str(campaign_id),
                "work_id": item["work_id"],
                "revision_id": item["revision_id"],
                "domain": "engineering",
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
                campaign_id,
                item["work_id"],
                item["revision_id"],
                spec_sha,
                policy_component,
                manifest,
                manifest_sha,
            ),
        },
    )
    assert status == 200, body

    trial_ids: dict[str, UUID] = {}
    for name, (_source, reason) in sources.items():
        trial_id = uuid4()
        trial_ids[name] = trial_id
        payload = _trial_payload(
            campaign_id,
            item["work_id"],
            policy_component,
            evaluator_component,
            native_jobs.components["environment"],
            trial_id=trial_id,
            candidate_digest=frozen[name][1],
        )
        if reason is not None:
            payload["reason"] = reason
        status, body = _command(
            service,
            workspace_id,
            {"type": "propose_research_trial", "payload": payload},
        )
        assert status == 200, body

    procs: list[subprocess.Popen] = []
    logs: list[object] = []
    try:
        service_log = tmp_path / "service.log"
        service_proc, base = _start_service(
            json.dumps(_config_dict(service.config)),
            service.capabilities,
            service_log,
            procs,
            logs,
        )
        worker_1_id = f"m1-worker-a-{uuid4()}"
        worker_log = tmp_path / "worker-a.log"
        worker = _start_worker(
            _worker_config(
                native_jobs,
                tmp_path,
                worker_id=worker_1_id,
                component=worker_component,
                work_url=base,
                content_dir=content_dir,
                scratch_dir=scratch_dir,
            ),
            worker_log,
            procs,
            logs,
        )
        assert service_proc.poll() is None

        jobs = {
            name: _dispatch(store, workspace_id, work_id, trial_ids[name], 60)
            for name in ("baseline", "faster", "broken")
        }
        for name in ("baseline", "faster", "broken"):
            _wait_terminal(store, workspace_id, jobs[name], worker, worker_log, timeout=90)

        verdicts = {}
        for name in ("baseline", "faster", "broken"):
            rows = _observations(store, workspace_id, trial_ids[name])
            assert len(rows) == 1, (name, rows)
            assert rows[0]["issuer_kind"] == "legacy_autoresearch"
            assert rows[0]["source_ref"] == f"job:{jobs[name]}"
            verdicts[name] = _payload(rows[0])["verdict"]
        assert verdicts == {
            "baseline": "qualified",
            "faster": "qualified",
            "broken": "candidate_failed",
        }

        restart_job = _dispatch(store, workspace_id, work_id, trial_ids["restart"], 8)
        heartbeat = _growing_heartbeat(scratch_dir, worker, worker_log)
        assert str(_job_row(store, workspace_id, restart_job)["status"]) == "admitted"
        _kill(worker)
        _stopped_heartbeat(heartbeat)

        worker_2_id = f"m1-worker-b-{uuid4()}"
        worker_2_log = tmp_path / "worker-b.log"
        worker_2 = _start_worker(
            _worker_config(
                native_jobs,
                tmp_path,
                worker_id=worker_2_id,
                component=worker_component,
                work_url=base,
                content_dir=content_dir,
                scratch_dir=scratch_dir,
            ),
            worker_2_log,
            procs,
            logs,
        )
        assert _wait_terminal(
            store, workspace_id, restart_job, worker_2, worker_2_log, timeout=90
        ) == "sealed"
        restart_rows = _observations(store, workspace_id, trial_ids["restart"])
        assert len(restart_rows) == 1
        assert _payload(restart_rows[0])["verdict"] == "qualified"
        assert restart_rows[0]["source_ref"] == f"job:{restart_job}"

        sleeper_job = _dispatch(store, workspace_id, work_id, trial_ids["sleeper"], 9)
        _growing_heartbeat(scratch_dir, worker_2, worker_2_log)
        cancelled = cancel_job(
            store,
            operation_id=str(uuid4()),
            workspace_id=workspace_id,
            actor_id=OWNER,
            job_id=sleeper_job,
            reason="stop the sleeping trial",
        )
        assert cancelled["status"] == "applied"
        assert str(_job_row(store, workspace_id, sleeper_job)["status"]) == "cancelled"
        quiet = time.monotonic() + 15
        while time.monotonic() < quiet:
            if worker_2.poll() is not None:
                raise AssertionError(f"worker exited after cancel\n{_tail(worker_2_log)}")
            time.sleep(0.2)
        assert _observations(store, workspace_id, trial_ids["sleeper"]) == []
        assert _artifact_count(store, workspace_id, sleeper_job) == 0
        assert str(_job_row(store, workspace_id, sleeper_job)["status"]) == "cancelled"
        _kill(worker_2)

        winner_digest = frozen["faster"][1]
        final_id = _plan_and_finalize(
            service,
            workspace_id,
            item,
            candidate_hash=winner_digest,
            commit_sha=secrets.token_hex(20),
        )
        blocked_status, blocked = _bind(
            service,
            workspace_id,
            item,
            campaign_id,
            trial_ids["broken"],
            frozen["broken"][1],
            uuid4(),
        )
        assert blocked_status == 409, blocked
        assert blocked["error"]["code"] == "completion_blocked"
        assert blocked["error"]["diagnostics"] == ["failed"]

        review_id = f"review-{uuid4()}"
        enqueued = enqueue_job(
            store,
            operation_id=str(uuid4()),
            workspace_id=workspace_id,
            actor_id=OWNER,
            job_id=review_id,
            work_id=work_id,
            kind="compute",
            required_capabilities=[MANAGED_CAPABILITY],
            resources=_RESOURCES,
            lease_seconds=60,
            trial_id=trial_ids["faster"],
        )
        assert enqueued["status"] == "applied", enqueued
        claimed = claim_job(
            store,
            operation_id=str(uuid4()),
            workspace_id=workspace_id,
            actor_id=OWNER,
            worker_id=worker_2_id,
        )
        assert claimed["status"] == "applied", claimed
        claimed_job = claimed["job"]
        assert claimed_job is not None
        assert claimed_job["job_id"] == review_id
        settled = settle_job(
            store,
            operation_id=str(uuid4()),
            workspace_id=workspace_id,
            actor_id=OWNER,
            job_id=review_id,
            worker_id=worker_2_id,
            fence=int(claimed_job["fence"]),
            outcome="succeeded",
            receipts=[
                {
                    "role": "audit",
                    "issuer_component_sha256": native_jobs.components["audit"],
                    "evidence": _evidence(),
                },
                {
                    "role": "release",
                    "issuer_component_sha256": native_jobs.components["release"],
                    "evidence": _evidence(),
                },
            ],
        )
        assert settled["status"] == "applied", settled
        bound_status, bound = _bind(
            service,
            workspace_id,
            item,
            campaign_id,
            trial_ids["faster"],
            winner_digest,
            final_id,
        )
        assert bound_status == 200, bound
        assert bound["result"]["status"] == "applied"

        bundle_dir = tmp_path / "bundle"
        with store.transaction(workspace_id, OWNER) as cur:
            build_bundle(
                cur,
                workspace_id,
                work_id,
                content_dir=content_dir,
                data_dir=service.config.data_dir,
                out=bundle_dir,
            )
        assert verify_bundle(bundle_dir) == []
        manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["format"] == "omp-repro-bundle.v1"
        assert manifest["private_dependencies"] == [confirmation_sha]
        bundled_ids = [trial["trial_id"] for trial in manifest["trials"]]
        assert str(trial_ids["sleeper"]) not in bundled_ids
        assert confirmation_sha not in {path.name for path in (bundle_dir / "artifacts").iterdir()}
        lines = (bundle_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 4
        parsed = [json.loads(line) for line in lines]
        for record in parsed:
            assert list(record) == _RESULT_KEYS
            assert record["format"] == "omp-managed-result.v1"
            assert record["primary_metric"] == "seconds"
            assert record["direction"] == "lower"
        by_trial = {record["trial_id"]: record for record in parsed}
        assert by_trial[str(trial_ids["baseline"])]["description"] == "baseline"
        assert by_trial[str(trial_ids["faster"])]["description"] == "faster candidate"
        assert by_trial[str(trial_ids["broken"])]["verdict"] == "candidate_failed"
        assert by_trial[str(trial_ids["broken"])]["metrics"] == {}
        assert by_trial[str(trial_ids["restart"])]["description"] == f"trial {str(trial_ids['restart'])[:8]}"

        copy_dir = tmp_path / "bundle-copy"
        shutil.copytree(bundle_dir, copy_dir)
        home = tmp_path / "home"
        home.mkdir()
        private_dir = tmp_path / "private"
        private_dir.mkdir()
        (private_dir / confirmation_sha).write_bytes(_CONFIRMATION)
        env = _clean_env(home)
        replay = subprocess.run(
            [
                sys.executable,
                "-m",
                "omp_work.research.bundle",
                "reproduce",
                str(copy_dir),
                "--private-dir",
                str(private_dir),
            ],
            cwd=home,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        assert replay.returncode == 0, replay.stderr
        statuses = json.loads(replay.stdout)
        for name in ("baseline", "faster", "restart"):
            assert statuses[str(trial_ids[name])] == "reproduced", (name, statuses)
        assert str(trial_ids["broken"]) not in statuses
        assert str(trial_ids["sleeper"]) not in statuses

        withheld = subprocess.run(
            [sys.executable, "-m", "omp_work.research.bundle", "reproduce", str(copy_dir)],
            cwd=home,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert withheld.returncode == 1, withheld.stderr
        withheld_status = json.loads(withheld.stdout)
        assert (
            withheld_status[str(trial_ids["faster"])]
            == f"missing_private_dependency:{confirmation_sha}"
        )

        results = bundle_dir / "results.md"
        results.write_text(results.read_text(encoding="utf-8") + "edited\n", encoding="utf-8")
        assert verify_bundle(bundle_dir) == ["report_altered"]
        checked = subprocess.run(
            [sys.executable, "-m", "omp_work.research.bundle", "verify", str(bundle_dir)],
            cwd=home,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert checked.returncode == 1
        assert checked.stdout.splitlines() == ["report_altered"]
    finally:
        for proc in procs:
            _kill(proc)
        for handle in logs:
            handle.close()

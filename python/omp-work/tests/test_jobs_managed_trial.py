"""Managed-trial evaluation worker acceptance tests (R06, OMP-315-s04).

Proves the managed experiment path on the shared omp_jobs substrate:

- a mission-authorized managed trial runs its frozen candidate and then the
  sealed evaluator, records the typed receipt as custody bytes and a matching
  ``legacy_autoresearch`` observation, and seals the job succeeded with a
  receipt that ``verify_receipt`` accepts;
- the candidate never sees the protected confirmation file;
- a candidate that produces no declared output is ``candidate_failed``;
- an evaluator that exits non-zero, or that accepts the empty negative
  control, is ``evaluator_failed``;
- cancelling the job mid-candidate records nothing and settles nothing;
- a lease that expires after the observation is rebuilt by ``observe()``
  without evaluating a second time;
- a mission of another kind, or one that did not request the capability, is
  refused with ``mission_not_authorized``.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
import pytest
from omp_work.jobs.admission import claim_job
from omp_work.jobs.cancel import cancel_job
from omp_work.jobs.lease import reconcile_jobs
from omp_work.jobs.managed_trial import (
    CAPABILITY,
    ManagedTrialHandler,
    ManagedTrialRefused,
    dispatch_managed_trial,
)
from omp_work.jobs.runner import RunResult
from omp_work.jobs.store import NativeJobStore
from omp_work.jobs.worker import JobWorker, Settlement, WorkerConfig
from omp_work.operations.artifacts import bytes_sha256, read_verified_bytes
from omp_work.research.custody import artifact_path
from omp_work.research.engineering import freeze_candidate
from omp_work.research.receipt import (
    MANAGED_CAPABILITY,
    MANAGED_POLICY,
    EvaluationProtocol,
    ReceiptExpectation,
    load_protocol,
    managed_job_id,
    verify_receipt,
)
from omp_work.v1.canonical import sha256
from omp_work.v1.client import WorkClient
from psycopg.rows import dict_row
from test_mission_links_store import (
    _approve,
    _link,
    _seed_budget,
    _submit,
)
from test_research_contract import (
    _admit_payload,
    _manifest,
    _register_component,
    _sample_spec,
    _trial_payload,
)
from test_research_runner import _unshare_probe_fails
from test_workflow_service import OWNER, _command, _create

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
        reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
    ),
    pytest.mark.skipif(_unshare_probe_fails(), reason="unshare probe failed"),
]
pytest_plugins = ("test_workflow_service", "native_jobs_support")

AUTOMATION_TOKEN = "managed-trial-automation-token"
AUTOMATION_SCOPES = ("work.read", "work.mutate", "work.execute")
RESOURCES = {"cpu": 1, "memory_mib": 0, "gpu": 0, "model_calls": 0}
CONFIRMATION = b"confirmation-label-do-not-leak"
_ITEM_BUDGET = {
    "usd": "6",
    "tokens": 600,
    "wall_clock_seconds": 120,
    "max_subagents": 2,
}

_CANDIDATE = r"""
import json
import os
import pathlib

pathlib.Path("out").mkdir(parents=True, exist_ok=True)
skip = {"usr", "proc", "sys", "dev", "bin", "lib", "lib64", "sbin", "etc"}
leaked = []
for base, dirs, files in os.walk("/", topdown=True):
    dirs[:] = [d for d in dirs if d not in skip and not os.path.islink(os.path.join(base, d))]
    for name in files:
        if name == "confirmation":
            leaked.append(os.path.join(base, name))
pathlib.Path("out/result.json").write_text(
    json.dumps({"metric": 0.75, "leaked": leaked}), encoding="utf-8"
)
print("METRIC x=0")
"""

_CANDIDATE_NO_OUTPUT = r"""
print("METRIC x=0")
"""

_EVALUATOR = r"""
import json
import pathlib

handoff = json.loads(pathlib.Path("input.json").read_text(encoding="utf-8"))
confirmation = handoff.get("confirmation")
conf_readable = confirmation is not None and pathlib.Path(confirmation).is_file()
outputs = handoff.get("outputs") or {}
metric = None
leaked = None
if outputs:
    data = json.loads(pathlib.Path(next(iter(outputs.values()))).read_text(encoding="utf-8"))
    metric = data.get("metric")
    leaked = data.get("leaked")
score = float(metric) if isinstance(metric, (int, float)) else 0.0
valid = bool(outputs) and isinstance(metric, (int, float)) and conf_readable and leaked == []
pathlib.Path("score.json").write_text(
    json.dumps({"valid": valid, "metrics": {"score": score}}), encoding="utf-8"
)
"""

_EVALUATOR_EXIT_TWO = r"""
import sys

sys.exit(2)
"""

_EVALUATOR_ALWAYS_VALID = r"""
import json
import pathlib

pathlib.Path("score.json").write_text(
    json.dumps({"valid": True, "metrics": {"score": 1.0}}), encoding="utf-8"
)
"""


class _BlockingBackend:
    """A backend whose first run returns ``canceled`` once the event is set."""

    name = "test-blocking.v1"
    capabilities = frozenset({"cpu"})

    def __init__(self) -> None:
        self.started = threading.Event()

    def run(self, request, cancel=None) -> RunResult:
        self.started.set()
        while cancel is None or not cancel.is_set():
            time.sleep(0.01)
        return RunResult(
            status="canceled",
            exit_code=-9,
            output=b"",
            duration=0.0,
            manifest={"backend": self.name},
            manifest_sha256=sha256({"backend": self.name}),
        )


def _store(native_jobs) -> NativeJobStore:
    return NativeJobStore(native_jobs.service.config)


def _project_in(service, workspace_id) -> UUID:
    """A project row of an existing workspace, for missions to live in."""
    project_id = uuid4()
    with (
        psycopg.connect(
            **service.config.connection_kwargs("omp_work_app"), autocommit=True
        ) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT set_config('omp.workspace_id', %s, false),"
            " set_config('omp.actor_id', %s, false)",
            (str(workspace_id), str(OWNER)),
        )
        cur.execute(
            "INSERT INTO omp_work.projects"
            "(project_id, workspace_id, key, name, kind, provenance)"
            " VALUES (%s, %s, %s, %s, 'surface', %s)",
            (
                project_id,
                workspace_id,
                f"p-{project_id.hex[:8]}",
                "Managed project",
                "{}",
            ),
        )
    return project_id


def _register_evaluator(service, workspace_id: UUID, artifact_sha256: str) -> str:
    """Register an evaluator whose descriptor names ``artifact_sha256`` as its artifact."""
    descriptor = {
        "contract_version": "research-component.v1",
        "kind": "evaluator",
        "name": f"managed-evaluator-{uuid4()}",
        "version": "1",
        "artifact_sha256": artifact_sha256,
        "roles": [],
        "capabilities": [],
    }
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


def _register_managed_policy(service, workspace_id: UUID) -> str:
    descriptor = {
        "contract_version": "research-component.v1",
        "kind": "policy",
        "name": MANAGED_POLICY,
        "version": "1",
        "artifact_sha256": sha256({"managed-policy": uuid4().hex}),
        "roles": [],
        "capabilities": [],
    }
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


def _automation_bearer(native_jobs, directory: Path) -> Path:
    """A non-owner capability file that still holds ``work.execute``."""
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / "managed-trial-automation.json"
    path.write_text(
        json.dumps(
            {
                "token": AUTOMATION_TOKEN,
                "actor_id": str(uuid4()),
                "actor_kind": "automation",
                "workspaces": [str(native_jobs.workspace_id)],
                "scopes": list(AUTOMATION_SCOPES),
            }
        )
    )
    path.chmod(0o600)
    return path


def _automation_client(native_jobs, bearer: Path) -> WorkClient:
    return WorkClient(
        "http://testserver",
        native_jobs.workspace_id,
        bearer,
        transport=native_jobs.service.client._transport,
    )


def _protocol(
    *, evaluator_command: tuple[str, ...], confirmation_sha256: str | None
) -> EvaluationProtocol:
    return EvaluationProtocol.model_validate(
        {
            "contract_version": "research-protocol.v1",
            "metrics": [{"name": "score", "direction": "higher"}],
            "primary": "score",
            "tolerance": {},
            "candidate_command": ["python3", "candidate.py"],
            "evaluator_command": list(evaluator_command),
            "outputs": ["out/result.json"],
            "requires": ["python3"],
            "confirmation_sha256": confirmation_sha256,
            "timeout_seconds": 60,
        }
    )


def _make_fixture(
    native_jobs,
    tmp_path: Path,
    *,
    candidate_source: str = _CANDIDATE,
    evaluator_source: str = _EVALUATOR,
    evaluator_command: tuple[str, ...] = ("python3", "evaluate"),
    confirmation: bytes | None = CONFIRMATION,
    mission_kind: str = "research.run",
    requested_capabilities: tuple[str, ...] = (MANAGED_CAPABILITY,),
    link: bool = True,
) -> SimpleNamespace:
    """One authorized managed campaign whose inputs live in a content directory."""
    service = native_jobs.service
    workspace_id = native_jobs.workspace_id
    item = _create(service, workspace_id, f"managed-trial-{uuid4()}")
    _seed_budget(
        service,
        workspace_id,
        work_id=item["work_id"],
        revision_id=item["revision_id"],
        budget=_ITEM_BUDGET,
    )

    project_id = _project_in(service, workspace_id)
    mission_id, _draft_view = _submit(
        service,
        workspace_id,
        project_id,
        kind=mission_kind,
        requested_capabilities=requested_capabilities,
    )
    _approve(service, workspace_id, mission_id)
    if link:
        status, body = _link(service, workspace_id, mission_id, item["work_id"])
        assert status == 200, body

    evaluator_script = evaluator_source.encode("utf-8")
    evaluator_artifact = bytes_sha256(evaluator_script).hexdigest()
    protocol = _protocol(
        evaluator_command=evaluator_command,
        confirmation_sha256=(
            None if confirmation is None else bytes_sha256(confirmation).hexdigest()
        ),
    )
    protocol_bytes = protocol.protocol_bytes()
    protocol_sha256 = bytes_sha256(protocol_bytes).hexdigest()

    candidate_tar, candidate_digest = freeze_candidate(
        _candidate_repo(tmp_path, candidate_source), "HEAD"
    )

    content_dir = tmp_path / "content"
    content_dir.mkdir(mode=0o700, exist_ok=True)
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir(mode=0o700, exist_ok=True)
    inputs: list[tuple[str, bytes]] = [
        (candidate_digest, candidate_tar),
        (evaluator_artifact, evaluator_script),
        (protocol_sha256, protocol_bytes),
    ]
    if confirmation is not None:
        inputs.append((bytes_sha256(confirmation).hexdigest(), confirmation))
    for digest, data in inputs:
        (content_dir / digest).write_bytes(data)

    spec, _ = _sample_spec()
    spec["evaluation_protocol_sha256"] = protocol_sha256
    spec_sha = sha256(spec)

    worker_component = _register_component(
        service,
        workspace_id,
        "worker",
        name=f"managed-worker-{uuid4()}",
        capabilities=(CAPABILITY,),
    )
    evaluator_component = _register_evaluator(service, workspace_id, evaluator_artifact)
    policy_component = _register_managed_policy(service, workspace_id)
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

    trial_id = uuid4()
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "propose_research_trial",
            "payload": _trial_payload(
                campaign_id,
                item["work_id"],
                policy_component,
                evaluator_component,
                native_jobs.components["environment"],
                trial_id=trial_id,
                candidate_digest=candidate_digest,
            ),
        },
    )
    assert status == 200, body

    return SimpleNamespace(
        service=service,
        workspace_id=workspace_id,
        actor_id=OWNER,
        item=item,
        campaign_id=campaign_id,
        trial_id=trial_id,
        worker_component=worker_component,
        evaluator_component=evaluator_component,
        evaluator_artifact=evaluator_artifact,
        protocol=protocol,
        protocol_sha256=protocol_sha256,
        candidate_digest=candidate_digest,
        content_dir=content_dir,
        scratch_dir=scratch_dir,
    )


def _candidate_repo(tmp_path: Path, source: str) -> Path:
    import subprocess

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


def _handler(
    fix: SimpleNamespace, client: WorkClient | None = None
) -> ManagedTrialHandler:
    return ManagedTrialHandler(
        content_dir=fix.content_dir,
        execute=None if client is None else client.execute,
        scratch_dir=fix.scratch_dir,
    )


def _worker(fix: SimpleNamespace, store: NativeJobStore, handler) -> JobWorker:
    worker = JobWorker(
        store,
        WorkerConfig(
            workspace_id=fix.workspace_id,
            actor_id=fix.actor_id,
            worker_id=f"managed-trial-{uuid4()}",
            component_sha256=fix.worker_component,
            capabilities=[CAPABILITY],
            capacity=1,
        ),
        handler,
    )
    worker.start()
    return worker


def _select(fix: SimpleNamespace, sql: str, params: tuple[object, ...] = ()):
    store = _store(fix)
    with store.transaction(fix.workspace_id, fix.actor_id) as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def _observation(fix: SimpleNamespace, job_id: str):
    return _select(
        fix,
        """
        SELECT observation_id, campaign_id, trial_id, issuer_kind, source_ref,
               execution_status, payload, payload_sha256, observed_at
        FROM omp_research.observations
        WHERE workspace_id=%s AND issuer_kind='legacy_autoresearch' AND source_ref=%s
        """,
        (fix.workspace_id, f"job:{job_id}"),
    )


def _artifact_count(fix: SimpleNamespace, job_id: str) -> int:
    """Artifacts this job registered — the workspace is shared module-wide."""
    row = _select(
        fix,
        "SELECT count(*) AS n FROM omp_research.artifacts"
        " WHERE workspace_id=%s AND manifest->>'source_ref'=%s",
        (fix.workspace_id, f"job:{job_id}"),
    )
    return 0 if row is None else int(row["n"])


def _custody_bytes(fix: SimpleNamespace, digest: str) -> bytes:
    row = _select(
        fix,
        "SELECT manifest FROM omp_research.artifacts WHERE workspace_id=%s AND artifact_sha256=%s",
        (fix.workspace_id, digest),
    )
    assert row is not None
    manifest = (
        json.loads(row["manifest"])
        if isinstance(row["manifest"], str)
        else row["manifest"]
    )
    return read_verified_bytes(
        artifact_path(fix.service.config.data_dir, fix.workspace_id, digest),
        digest,
        int(manifest["size_bytes"]),
    )


def _expire_lease(fix: SimpleNamespace, job_id: str) -> None:
    with psycopg.connect(
        **fix.service.config.connection_kwargs("postgres"), row_factory=dict_row
    ) as conn:
        conn.execute(
            "UPDATE omp_jobs.jobs SET lease_expires_at = clock_timestamp() - interval '1 second' WHERE job_id=%s",
            (job_id,),
        )


def _dispatch(fix: SimpleNamespace, store: NativeJobStore) -> str:
    dispatch_managed_trial(
        store,
        operation_id=str(uuid4()),
        workspace_id=fix.workspace_id,
        actor_id=fix.actor_id,
        work_id=UUID(fix.item["work_id"]),
        trial_id=fix.trial_id,
        resources=RESOURCES,
        lease_seconds=60,
    )
    return managed_job_id(fix.trial_id)


def test_qualified_trial_seals_with_trusted_receipt_and_hides_confirmation(
    native_jobs, tmp_path: Path
) -> None:
    """A qualified trial records its receipt and observation; the candidate never leaks it."""
    fix = _make_fixture(native_jobs, tmp_path)
    bearer = _automation_bearer(native_jobs, native_jobs.service.capabilities)
    client = _automation_client(native_jobs, bearer)
    store = _store(native_jobs)
    handler = _handler(fix, client)

    job_id = _dispatch(fix, store)
    worker = _worker(fix, store, handler)

    assert worker.tick() == job_id
    assert handler.run_count == 1

    assert handler.last_evaluation is not None
    assert handler.last_evaluation.verdict == "qualified"
    assert handler.last_evaluation.negative_control == "rejected"
    assert handler.last_evaluation.metrics == {"score": 0.75}

    job = _select(
        fix,
        "SELECT status, settlement FROM omp_jobs.jobs WHERE job_id=%s",
        (job_id,),
    )
    assert job["status"] == "sealed"
    settlement = job["settlement"]
    assert settlement["outcome"] == "succeeded"
    assert len(settlement["receipts"]) == 1
    receipt_entry = settlement["receipts"][0]
    assert receipt_entry["role"] == "evaluator"
    assert receipt_entry["issuer_component_sha256"] == fix.evaluator_component
    assert receipt_entry["evidence"]["trust"] == "trusted"

    digest = receipt_entry["evidence"]["content_digest"]
    receipt_bytes = _custody_bytes(fix, digest)
    assert bytes_sha256(receipt_bytes).hexdigest() == digest
    reasons = verify_receipt(
        receipt_bytes,
        expected_sha256=digest,
        expected=ReceiptExpectation(
            trial_id=str(fix.trial_id),
            campaign_id=str(fix.campaign_id),
            job_id=job_id,
            candidate_digest=fix.candidate_digest,
            evaluator_component_sha256=fix.evaluator_component,
            evaluator_artifact_sha256=fix.evaluator_artifact,
        ),
        protocol=load_protocol(fix.protocol.protocol_bytes()),
        protocol_sha256=fix.protocol_sha256,
    )
    assert reasons == []

    observation = _observation(fix, job_id)
    assert observation is not None
    assert observation["execution_status"] == "completed"
    assert observation["payload"]["verdict"] == "qualified"
    assert observation["payload"]["receipt_sha256"] == digest
    assert observation["payload"]["metrics"] == {"score": 0.75}

    # The candidate's own output records what it could see: the metric it
    # computed, and that no file named ``confirmation`` was reachable.
    runs = sorted(fix.scratch_dir.glob("omp-managed-trial-*/candidate/out/result.json"))
    assert len(runs) == 1
    result = json.loads(runs[0].read_text(encoding="utf-8"))
    assert result["metric"] == 0.75
    assert result["leaked"] == []


def test_candidate_without_declared_output_is_candidate_failed(
    native_jobs, tmp_path: Path
) -> None:
    """A candidate that prints only a metric fails the job, but still settles untrusted."""
    fix = _make_fixture(native_jobs, tmp_path, candidate_source=_CANDIDATE_NO_OUTPUT)
    bearer = _automation_bearer(native_jobs, native_jobs.service.capabilities)
    store = _store(native_jobs)
    handler = _handler(fix, _automation_client(native_jobs, bearer))

    job_id = _dispatch(fix, store)
    worker = _worker(fix, store, handler)

    assert worker.tick() == job_id
    assert handler.last_evaluation is not None
    assert handler.last_evaluation.verdict == "candidate_failed"

    job = _select(
        fix,
        "SELECT status, settlement FROM omp_jobs.jobs WHERE job_id=%s",
        (job_id,),
    )
    assert job["status"] == "failed"
    assert job["settlement"]["outcome"] == "failed"
    assert job["settlement"]["receipts"][0]["evidence"]["trust"] == "untrusted"
    observation = _observation(fix, job_id)
    assert observation is not None
    assert observation["execution_status"] == "crashed"
    assert observation["payload"]["verdict"] == "candidate_failed"


def test_missing_candidate_input_fails_without_receipt_or_observation(
    native_jobs, tmp_path: Path
) -> None:
    """A candidate whose content bytes are absent settles failed and records nothing."""
    fix = _make_fixture(native_jobs, tmp_path)
    bearer = _automation_bearer(native_jobs, native_jobs.service.capabilities)
    store = _store(native_jobs)
    handler = _handler(fix, _automation_client(native_jobs, bearer))
    # Remove the frozen candidate's bytes: the digest can no longer be read.
    (fix.content_dir / fix.candidate_digest).unlink()

    job_id = _dispatch(fix, store)
    worker = _worker(fix, store, handler)

    assert worker.tick() == job_id
    assert handler.last_evaluation is None

    job = _select(
        fix,
        "SELECT status, settlement FROM omp_jobs.jobs WHERE job_id=%s",
        (job_id,),
    )
    assert job["status"] == "failed"
    assert job["settlement"]["outcome"] == "failed"
    assert job["settlement"]["receipts"] == []
    assert _observation(fix, job_id) is None
    assert _artifact_count(fix, job_id) == 0


def test_evaluator_exit_two_is_evaluator_failed(native_jobs, tmp_path: Path) -> None:
    """An evaluator that exits non-zero fails the job and marks the receipt untrusted."""
    fix = _make_fixture(native_jobs, tmp_path, evaluator_source=_EVALUATOR_EXIT_TWO)
    bearer = _automation_bearer(native_jobs, native_jobs.service.capabilities)
    store = _store(native_jobs)
    handler = _handler(fix, _automation_client(native_jobs, bearer))

    job_id = _dispatch(fix, store)
    worker = _worker(fix, store, handler)

    assert worker.tick() == job_id
    assert handler.last_evaluation is not None
    assert handler.last_evaluation.verdict == "evaluator_failed"

    job = _select(
        fix,
        "SELECT status, settlement FROM omp_jobs.jobs WHERE job_id=%s",
        (job_id,),
    )
    assert job["status"] == "failed"
    receipt = job["settlement"]["receipts"][0]
    assert receipt["evidence"]["trust"] == "untrusted"
    observation = _observation(fix, job_id)
    assert observation is not None
    assert observation["payload"]["verdict"] == "evaluator_failed"


def test_evaluator_accepting_empty_control_is_evaluator_failed(
    native_jobs, tmp_path: Path
) -> None:
    """An evaluator that scores the empty negative control valid fails the job."""
    fix = _make_fixture(native_jobs, tmp_path, evaluator_source=_EVALUATOR_ALWAYS_VALID)
    bearer = _automation_bearer(native_jobs, native_jobs.service.capabilities)
    store = _store(native_jobs)
    handler = _handler(fix, _automation_client(native_jobs, bearer))

    job_id = _dispatch(fix, store)
    worker = _worker(fix, store, handler)

    assert worker.tick() == job_id
    assert handler.last_evaluation is not None
    assert handler.last_evaluation.verdict == "evaluator_failed"
    assert handler.last_evaluation.failure_reason == "negative_control_accepted"
    assert handler.last_evaluation.negative_control == "accepted"

    job = _select(
        fix,
        "SELECT status, settlement FROM omp_jobs.jobs WHERE job_id=%s",
        (job_id,),
    )
    assert job["status"] == "failed"
    assert job["settlement"]["receipts"][0]["evidence"]["trust"] == "untrusted"


def test_cancel_job_mid_candidate_records_nothing(native_jobs, tmp_path: Path) -> None:
    """Cancelling the job while the candidate runs leaves no observation or receipt."""
    fix = _make_fixture(native_jobs, tmp_path)
    store = _store(native_jobs)
    handler = _handler(fix)
    backend = _BlockingBackend()
    handler.backend = backend

    job_id = _dispatch(fix, store)
    worker = _worker(fix, store, handler)
    claimed = claim_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=fix.workspace_id,
        actor_id=fix.actor_id,
        worker_id=worker.worker_id,
    )
    assert claimed["job"]["job_id"] == job_id
    job = claimed["job"]

    outcome: dict[str, object] = {}

    def _drive() -> None:
        outcome["settlement"] = handler.run(worker, job)

    thread = threading.Thread(target=_drive)
    thread.start()
    assert backend.started.wait(timeout=30)

    cancelled = cancel_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=fix.workspace_id,
        actor_id=fix.actor_id,
        job_id=job_id,
        reason="owner cancelled the managed trial",
    )
    assert cancelled["status"] == "applied"
    worker.cancel_requested.set()
    thread.join(timeout=30)
    assert not thread.is_alive()

    settlement = outcome["settlement"]
    assert isinstance(settlement, Settlement)
    assert settlement.outcome == "failed"
    assert settlement.receipts == []
    assert handler.last_evaluation is not None
    assert handler.last_evaluation.canceled is True

    assert _observation(fix, job_id) is None
    assert _artifact_count(fix, job_id) == 0
    row = _select(fix, "SELECT status FROM omp_jobs.jobs WHERE job_id=%s", (job_id,))
    assert row["status"] == "cancelled"

    # A tick after the cancel neither reclaims nor settles the cancelled job.
    assert worker.tick() is None


def test_lease_expiry_after_observation_observe_rebuilds_without_rerun(
    native_jobs, tmp_path: Path
) -> None:
    """A lease expired after the observation is settled from it; the candidate runs once."""
    fix = _make_fixture(native_jobs, tmp_path)
    bearer = _automation_bearer(native_jobs, native_jobs.service.capabilities)
    client = _automation_client(native_jobs, bearer)
    store = _store(native_jobs)
    handler = _handler(fix, client)

    job_id = _dispatch(fix, store)
    worker = _worker(fix, store, handler)

    claimed = claim_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=fix.workspace_id,
        actor_id=fix.actor_id,
        worker_id=worker.worker_id,
    )
    assert claimed["job"]["job_id"] == job_id
    first = handler.run(worker, claimed["job"])
    assert first.outcome == "succeeded"
    assert handler.run_count == 1
    recorded = _observation(fix, job_id)
    assert recorded is not None

    _expire_lease(fix, job_id)
    reconciled = reconcile_jobs(
        store,
        operation_id=str(uuid4()),
        workspace_id=fix.workspace_id,
        actor_id=fix.actor_id,
    )
    assert job_id in reconciled["job_ids"]

    assert worker.tick() == job_id
    assert handler.observe_count >= 1
    assert handler.run_count == 1

    job = _select(
        fix,
        "SELECT status, settlement FROM omp_jobs.jobs WHERE job_id=%s",
        (job_id,),
    )
    assert job["status"] == "sealed"
    assert job["settlement"]["outcome"] == "succeeded"
    # The rebuilt settlement points at the receipt the observation recorded.
    digest = job["settlement"]["receipts"][0]["evidence"]["content_digest"]
    assert digest == recorded["payload"]["receipt_sha256"]
    assert bytes_sha256(_custody_bytes(fix, digest)).hexdigest() == digest


@pytest.mark.parametrize(
    ("mission_kind", "requested_capabilities", "link"),
    [
        ("engineering.execute", (MANAGED_CAPABILITY,), True),
        ("research.run", (), True),
        ("research.run", (MANAGED_CAPABILITY,), False),
    ],
    ids=("other_kind", "capability_not_requested", "not_linked"),
)
def test_unauthorized_mission_is_refused(
    native_jobs,
    tmp_path: Path,
    mission_kind: str,
    requested_capabilities: tuple[str, ...],
    link: bool,
) -> None:
    """A mission of another kind, without the capability, or absent refuses the dispatch."""
    fix = _make_fixture(
        native_jobs,
        tmp_path,
        mission_kind=mission_kind,
        requested_capabilities=requested_capabilities,
        link=link,
    )
    store = _store(native_jobs)

    with pytest.raises(ManagedTrialRefused) as exc:
        dispatch_managed_trial(
            store,
            operation_id=str(uuid4()),
            workspace_id=fix.workspace_id,
            actor_id=fix.actor_id,
            work_id=UUID(fix.item["work_id"]),
            trial_id=fix.trial_id,
            resources=RESOURCES,
            lease_seconds=60,
        )
    assert exc.value.code == "mission_not_authorized"

    job = _select(
        fix,
        "SELECT status FROM omp_jobs.jobs WHERE job_id=%s",
        (managed_job_id(fix.trial_id),),
    )
    assert job is None

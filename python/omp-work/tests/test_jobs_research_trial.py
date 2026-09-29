"""Trial binding acceptance tests on the s02 worker (R03, OMP-400).

Proves the research-trial handler end to end on the shared omp_jobs substrate:

- an evaluator whose registered artifact is the harness runs once in a temp cwd,
  its ``METRIC`` lines are recorded as a ``legacy_autoresearch`` observation keyed
  ``job:<job_id>`` with the evaluator receipt sealing the job (and the WorkService
  call carries a non-owner actor);
- a harness whose bytes do not hash to the evaluator artifact fails the job and
  records no observation;
- a lease that expires after the observation is rebuilt by ``observe()`` without
  running the harness a second time;
- a harness that overruns the campaign ``max_wall_seconds`` is recorded as
  ``timed_out`` and settles the job failed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
import pytest
from omp_work.jobs.admission import claim_job
from omp_work.jobs.lease import reconcile_jobs
from omp_work.jobs.projection import trial_execution
from omp_work.jobs.research_trial import (
    CAPABILITY,
    ResearchTrialHandler,
    dispatch_trial,
    trial_job_id,
)
from omp_work.jobs.store import NativeJobStore
from omp_work.jobs.worker import JobWorker, WorkerConfig
from omp_work.operations.artifacts import bytes_sha256
from omp_work.v1.canonical import sha256
from omp_work.v1.client import WorkClient
from psycopg.rows import dict_row
from test_research_contract import (
    _admit_payload,
    _manifest,
    _register_component,
    _sample_spec,
    _trial_payload,
)
from test_workflow_service import OWNER, _command, _create

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service", "native_jobs_support")

AUTOMATION_TOKEN = "research-trial-automation-token"
AUTOMATION_SCOPES = ("work.read", "work.mutate", "work.execute")
RESOURCES = {"cpu": 1, "memory_mib": 0, "gpu": 0, "model_calls": 0}


def _register_evaluator(service, workspace_id: UUID, *, artifact_sha256: str) -> str:
    """Register an evaluator whose descriptor names ``artifact_sha256`` as its artifact.

    ``test_research_contract._register_component`` derives the artifact from the
    component name, so the harness digest cannot be selected through it.
    """
    name = f"trial-evaluator-{uuid4()}"
    descriptor = {
        "contract_version": "research-component.v1",
        "kind": "evaluator",
        "name": name,
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


def _automation_bearer(native_jobs, directory: Path) -> Path:
    """A non-owner capability file that still holds ``work.execute``."""
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / "research-trial-automation.json"
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


def _harness(
    marker: Path, *, metrics: tuple[str, ...], sleep_seconds: int = 0
) -> bytes:
    lines = ["#!/usr/bin/env bash", f"printf 'run\\n' >> '{marker}'"]
    if sleep_seconds:
        lines.append(f"sleep {sleep_seconds}")
    lines.extend(metrics)
    lines.append("exit 0")
    return ("\n".join(lines) + "\n").encode()


def _make_fixture(
    native_jobs,
    tmp_path: Path,
    *,
    harness_metrics: tuple[str, ...] = (
        'echo "METRIC score=0.75"',
        'echo "METRIC loss=1.5"',
        'echo "METRIC __proto__=9"',
        'echo "METRIC broken=abc"',
        'echo "METRIC nanval=nan"',
        'echo "METRIC infval=inf"',
    ),
    sleep_seconds: int = 0,
    max_wall_seconds: int | None = None,
    tamper_harness: bool = False,
) -> SimpleNamespace:
    """One workspace-local work item, worker component, campaign, trial, harness."""
    service = native_jobs.service
    workspace_id = native_jobs.workspace_id
    marker = tmp_path / "harness-runs.txt"
    harness = _harness(marker, metrics=harness_metrics, sleep_seconds=sleep_seconds)
    artifact_sha256 = bytes_sha256(harness).hexdigest()

    harness_dir = tmp_path / "harness"
    harness_dir.mkdir(mode=0o700, exist_ok=True)
    stored = b"#!/usr/bin/env bash\nexit 0\n" if tamper_harness else harness
    (harness_dir / artifact_sha256).write_bytes(stored)

    item = _create(service, workspace_id, f"research-trial-{uuid4()}")
    spec, _ = _sample_spec()
    if max_wall_seconds is not None:
        spec["resource_vector"]["max_wall_seconds"] = max_wall_seconds
    spec_sha = sha256(spec)

    worker_component = _register_component(
        service,
        workspace_id,
        "worker",
        name=f"trial-worker-{uuid4()}",
        capabilities=(CAPABILITY,),
    )
    evaluator_component = _register_evaluator(
        service, workspace_id, artifact_sha256=artifact_sha256
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
                native_jobs.components["policy"],
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
                native_jobs.components["policy"],
                evaluator_component,
                native_jobs.components["environment"],
                trial_id=trial_id,
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
        artifact_sha256=artifact_sha256,
        harness_dir=harness_dir,
        marker=marker,
    )


def _store(native_jobs) -> NativeJobStore:
    return NativeJobStore(native_jobs.service.config)


def _automation_client(native_jobs, bearer: Path) -> WorkClient:
    return WorkClient(
        "http://testserver",
        native_jobs.workspace_id,
        bearer,
        transport=native_jobs.service.client._transport,
    )


def _worker(
    fix: SimpleNamespace, store: NativeJobStore, handler: ResearchTrialHandler
) -> JobWorker:
    worker = JobWorker(
        store,
        WorkerConfig(
            workspace_id=fix.workspace_id,
            actor_id=fix.actor_id,
            worker_id=f"research-trial-{uuid4()}",
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


def _runs(fix: SimpleNamespace) -> int:
    if not fix.marker.is_file():
        return 0
    return len([line for line in fix.marker.read_text().splitlines() if line.strip()])


def _expire_lease(fix: SimpleNamespace, job_id: str) -> None:
    with psycopg.connect(
        **fix.service.config.connection_kwargs("postgres"), row_factory=dict_row
    ) as conn:
        conn.execute(
            "UPDATE omp_jobs.jobs SET lease_expires_at = clock_timestamp() - interval '1 second' WHERE job_id=%s",
            (job_id,),
        )


def test_dispatch_records_harness_metrics_and_seals_with_evaluator_receipt(
    native_jobs, tmp_path: Path
) -> None:
    """One tick runs the harness, records the observation, and seals the job."""
    fix = _make_fixture(native_jobs, tmp_path)
    bearer = _automation_bearer(native_jobs, native_jobs.service.capabilities)
    client = _automation_client(native_jobs, bearer)
    store = _store(native_jobs)
    handler = ResearchTrialHandler(harness_dir=fix.harness_dir, execute=client.execute)

    dispatch_trial(
        store,
        operation_id=str(uuid4()),
        workspace_id=fix.workspace_id,
        actor_id=fix.actor_id,
        work_id=UUID(fix.item["work_id"]),
        trial_id=fix.trial_id,
        resources=RESOURCES,
        lease_seconds=60,
    )
    job_id = trial_job_id(fix.trial_id)
    worker = _worker(fix, store, handler)

    assert worker.tick() == job_id
    assert handler.run_count == 1
    assert _runs(fix) == 1

    job = _select(
        fix,
        "SELECT status, settlement FROM omp_jobs.jobs WHERE job_id=%s",
        (job_id,),
    )
    assert job["status"] == "sealed"
    settlement = job["settlement"]
    assert settlement["outcome"] == "succeeded"
    receipts = settlement["receipts"]
    assert len(receipts) == 1
    assert receipts[0]["role"] == "evaluator"
    assert receipts[0]["issuer_component_sha256"] == fix.evaluator_component

    observation = _observation(fix, job_id)
    assert observation is not None
    assert observation["execution_status"] == "completed"
    assert observation["campaign_id"] == fix.campaign_id
    assert observation["trial_id"] == fix.trial_id
    assert observation["payload"]["metrics"] == {"score": 0.75, "loss": 1.5}
    assert observation["payload"]["exit_code"] == 0
    assert observation["payload"]["harness_sha256"] == fix.artifact_sha256
    assert observation["payload_sha256"] == receipts[0]["evidence"]["content_digest"]

    projection = _store(fix)
    with projection.transaction(fix.workspace_id, fix.actor_id) as cur:
        proj = trial_execution(cur, fix.workspace_id, fix.trial_id)
    assert proj["status"] == "succeeded"

    event = _select(
        fix,
        """
        SELECT actor_kind FROM omp_audit.domain_events
        WHERE workspace_id=%s AND event_type='record_research_observation'
        ORDER BY sequence DESC LIMIT 1
        """,
        (fix.workspace_id,),
    )
    assert event is not None
    assert event["actor_kind"] == "automation"


def test_tampered_harness_fails_job_without_observation(
    native_jobs, tmp_path: Path
) -> None:
    """Harness bytes that do not hash to the evaluator artifact fail without running."""
    fix = _make_fixture(native_jobs, tmp_path, tamper_harness=True)
    bearer = _automation_bearer(native_jobs, native_jobs.service.capabilities)
    client = _automation_client(native_jobs, bearer)
    store = _store(native_jobs)
    handler = ResearchTrialHandler(harness_dir=fix.harness_dir, execute=client.execute)

    dispatch_trial(
        store,
        operation_id=str(uuid4()),
        workspace_id=fix.workspace_id,
        actor_id=fix.actor_id,
        work_id=UUID(fix.item["work_id"]),
        trial_id=fix.trial_id,
        resources=RESOURCES,
        lease_seconds=60,
    )
    job_id = trial_job_id(fix.trial_id)
    worker = _worker(fix, store, handler)

    assert worker.tick() == job_id
    assert _runs(fix) == 0

    job = _select(
        fix,
        "SELECT status, settlement FROM omp_jobs.jobs WHERE job_id=%s",
        (job_id,),
    )
    assert job["status"] == "failed"
    assert job["settlement"]["outcome"] == "failed"
    assert job["settlement"]["receipts"] == []
    assert _observation(fix, job_id) is None

    with _store(fix).transaction(fix.workspace_id, fix.actor_id) as cur:
        proj = trial_execution(cur, fix.workspace_id, fix.trial_id)
    assert proj["status"] == "failed"
    assert proj["eligible"] is False


def test_lease_expiry_after_observation_observe_rebuilds_without_rerun(
    native_jobs, tmp_path: Path
) -> None:
    """A lease expired after the observation is settled from it; the harness runs once."""
    fix = _make_fixture(native_jobs, tmp_path)
    bearer = _automation_bearer(native_jobs, native_jobs.service.capabilities)
    client = _automation_client(native_jobs, bearer)
    store = _store(native_jobs)
    handler = ResearchTrialHandler(harness_dir=fix.harness_dir, execute=client.execute)

    dispatch_trial(
        store,
        operation_id=str(uuid4()),
        workspace_id=fix.workspace_id,
        actor_id=fix.actor_id,
        work_id=UUID(fix.item["work_id"]),
        trial_id=fix.trial_id,
        resources=RESOURCES,
        lease_seconds=60,
    )
    job_id = trial_job_id(fix.trial_id)
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
    assert _runs(fix) == 1
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
    assert _runs(fix) == 1

    job = _select(
        fix,
        "SELECT status, settlement FROM omp_jobs.jobs WHERE job_id=%s",
        (job_id,),
    )
    assert job["status"] == "sealed"
    assert job["settlement"]["outcome"] == "succeeded"
    assert (
        job["settlement"]["receipts"][0]["evidence"]["content_digest"]
        == recorded["payload_sha256"]
    )
    assert _observation(fix, job_id)["observation_id"] == recorded["observation_id"]


def test_harness_overrun_records_timed_out_and_settles_failed(
    native_jobs, tmp_path: Path
) -> None:
    """A harness past the campaign wall clock is recorded timed_out and failed."""
    fix = _make_fixture(
        native_jobs,
        tmp_path,
        harness_metrics=('echo "METRIC score=1.0"',),
        sleep_seconds=5,
        max_wall_seconds=1,
    )
    bearer = _automation_bearer(native_jobs, native_jobs.service.capabilities)
    client = _automation_client(native_jobs, bearer)
    store = _store(native_jobs)
    handler = ResearchTrialHandler(harness_dir=fix.harness_dir, execute=client.execute)

    dispatch_trial(
        store,
        operation_id=str(uuid4()),
        workspace_id=fix.workspace_id,
        actor_id=fix.actor_id,
        work_id=UUID(fix.item["work_id"]),
        trial_id=fix.trial_id,
        resources=RESOURCES,
        lease_seconds=60,
    )
    job_id = trial_job_id(fix.trial_id)
    worker = _worker(fix, store, handler)

    assert worker.tick() == job_id

    observation = _observation(fix, job_id)
    assert observation is not None
    assert observation["execution_status"] == "timed_out"
    assert observation["payload"]["exit_code"] is None
    assert observation["payload"]["duration_seconds"] < 5

    job = _select(
        fix,
        "SELECT status, settlement FROM omp_jobs.jobs WHERE job_id=%s",
        (job_id,),
    )
    assert job["status"] == "failed"
    assert job["settlement"]["outcome"] == "failed"

    with _store(fix).transaction(fix.workspace_id, fix.actor_id) as cur:
        proj = trial_execution(cur, fix.workspace_id, fix.trial_id)
    assert proj["status"] == "failed"
    assert proj["eligible"] is False

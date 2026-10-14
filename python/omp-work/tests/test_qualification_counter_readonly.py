"""End-to-end write proof for the read-only qualification count (OMP-620).

Semantics come from ``docs/qualification-counting-rules.md`` and the r2 rule it
quotes: an existing snapshot with no frozen protocol reports every gate as not
started with a null count, missing proof is named, and no count grants
acceptance. This test drives the real ``omp_work.qualification count`` dispatch
(``omp_work.__main__.main``) against disposable PostgreSQL holding seeded
ledger, evidence, job, operation and event rows and proves it only reads.

Two independent arms, both of which deny every ledger-command and native-job
mutation entrypoint with test-scoped instrumentation:

* the in-process arm resolves the real ``OperationsConfig.defaults()`` from a
  private XDG root, runs the command, and fingerprints ledger/jobs/evidence/
  operations/events rows before and after;
* the isolated-child arm re-enters the real CLI under ``-B`` /
  ``PYTHONDONTWRITEBYTECODE=1`` with a process-scoped audit hook that rejects
  every write open, create, rename, unlink or mkdir. Unchanged final bytes are
  not enough: the hook is armed before the count, a deliberate probe proves it
  refuses a write, and any transient write the code under test attempted would
  be refused and recorded, not merely cleaned up afterwards.

Setup runs before any hook is armed and only seeds rows. No production hook is
added and no validation is weakened. The count never starts a trial, freezes a
protocol, preregisters a stratum or mutates a record.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest

from native_jobs_support import native_jobs  # noqa: F401  (fixture)
from omp_work import qualification_snapshot
from omp_work.__main__ import main
from omp_work.jobs import admission, lease
from omp_work.jobs import store as job_store
from omp_work.qualification_counter import build_report
from omp_work.v1 import store as work_store
from omp_work.v1.canonical import sha256
from test_native_jobs_fencing import _claim, _enqueue, _settle, _worker
from test_workflow_service import OWNER, _command, _plan

pytest_plugins = ("test_workflow_service",)
pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)

GATES = ("consecutive", "sustained", "attempts")
FREEZE_REASON = "O9 freeze and release baseline unavailable"
STALE_REASON = "missing O9 freeze or release baseline"

# Whole rows plus explicit counts: a write to any ledger, evidence, job,
# operation or event row changes a fingerprint.
_TABLE_STATE = (
    "omp_work.work_items",
    "omp_work.work_revisions",
    "omp_evidence.receipts",
    "omp_audit.domain_events",
    "omp_research.campaigns",
    "omp_research.trials",
    "omp_research.observations",
    "omp_research.components",
    "omp_jobs.jobs",
    "omp_jobs.job_events",
    "omp_jobs.operations",
    "omp_jobs.outbox",
    "omp_jobs.reservations",
    "omp_control.idempotent_commands",
    "omp_control.operations_evidence",
)

_ENTRYPOINTS = (
    (work_store.PostgresWorkStore, "execute"),
    (work_store.PostgresWorkStore, "record_refused_attempt"),
    (job_store.NativeJobStore, "run_operation"),
    (job_store.NativeJobStore, "append_event"),
    (job_store, "register_worker"),
    (job_store, "drain_worker"),
    (job_store, "record_usage"),
    (admission, "enqueue_job"),
    (admission, "claim_job"),
    (lease, "require_live_holder"),
    (lease, "renew_lease"),
    (lease, "settle_job"),
    (lease, "reconcile_jobs"),
)


def _fingerprint(config) -> dict[str, tuple[int, str]]:
    state: dict[str, tuple[int, str]] = {}
    with psycopg.connect(**config.connection_kwargs("postgres")) as conn:
        for table in _TABLE_STATE:
            count, rows = conn.execute(
                psycopg.sql.SQL(
                    "SELECT count(*), coalesce("
                    "jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text), '[]') FROM {} t"
                ).format(psycopg.sql.Identifier(*table.split(".")))
            ).fetchone()
            state[table] = (count, json.dumps(rows, sort_keys=True))
    return state


def _private_xdg(config, root: Path) -> Path:
    """Real ``defaults()`` layout: ``XDG_CONFIG_HOME/omp/work-ledger/credentials``."""
    credentials = root / "omp" / "work-ledger" / "credentials"
    credentials.mkdir(parents=True, mode=0o700)
    for name in ("workspace-id", "operator-actor-id", "omp_work_readonly"):
        source = config.secret_path(name)
        target = credentials / name
        target.write_text(source.read_text(encoding="utf-8"))
        target.chmod(0o600)
    return root


@pytest.fixture(scope="module")
def seeded(native_jobs, tmp_path_factory):
    """Ledger, evidence, job, operation and event rows; never a live trial/freeze."""
    service = native_jobs.service
    config = service.config
    ws = native_jobs.workspace_id
    # The service fixture does not create these; the count's real defaults() reads them.
    for name, value in (("workspace-id", ws), ("operator-actor-id", OWNER)):
        path = config.secret_path(name)
        path.write_text(str(value))
        path.chmod(0o600)
    xdg = _private_xdg(config, tmp_path_factory.mktemp("qualification-readonly-xdg"))

    plan = _plan(service, ws, native_jobs.item)
    observation_id = uuid4()
    payload = {"result": "negative science", "path": "/not/a/readable/evidence/path"}
    status, body = _command(service, ws, {
        "type": "record_research_observation", "payload": {
            "observation_id": str(observation_id),
            "campaign_id": str(native_jobs.campaign_id), "trial_id": None,
            "issuer_kind": "candidate_authored", "source_ref": "/missing/observation.json",
            "execution_status": "completed", "payload": payload,
            "payload_sha256": sha256(payload), "observed_at": datetime.now(UTC).isoformat(),
        },
    })
    assert status == 200, body
    revision_id = uuid4()
    status, body = _command(service, ws, {
        "type": "revise_work", "payload": {
            "work_id": native_jobs.item["work_id"],
            "expected_revision_id": native_jobs.item["revision_id"],
            "revision": {
                "revision_id": str(revision_id), "work_id": native_jobs.item["work_id"],
                "revision_number": 2, "title": "read-only count revision",
                "description": "history", "scope": "", "acceptance_criteria": [],
                "content_sha256": sha256(
                    {"title": "read-only count revision", "description": "history"}
                ),
                "created_by": "owner", "created_at": datetime.now(UTC).isoformat(),
            },
        },
    })
    assert status == 200, body
    store = job_store.NativeJobStore(config)
    job_id = f"readonly-{uuid4()}"
    _enqueue(native_jobs, store, job_id=job_id)
    worker_id = _worker(native_jobs, store, ["compute.cpu"])
    claimed = _claim(native_jobs, store, worker_id)
    assert claimed["job"]["job_id"] == job_id
    _settle(
        native_jobs, store, job_id=job_id, worker_id=worker_id,
        fence=claimed["job"]["fence"],
    )
    queued_id = f"queued-{uuid4()}"
    _enqueue(native_jobs, store, job_id=queued_id)
    with psycopg.connect(**config.connection_kwargs("postgres")) as conn:
        # > typical page sizes, no annotation: every event must survive the read.
        conn.execute(
            "INSERT INTO omp_jobs.job_events(job_id,seq,kind,at) "
            "SELECT %s, 1000+n, 'poll', "
            "'2020-01-01'::timestamptz + n*interval '1 second' "
            "FROM generate_series(1,125) n",
            (job_id,),
        )
        events = conn.execute(
            "SELECT event_id::text FROM omp_audit.domain_events WHERE workspace_id=%s",
            (ws,),
        ).fetchall()
    ids = [
        native_jobs.item["work_id"], native_jobs.item["revision_id"], str(revision_id),
        str(native_jobs.campaign_id), str(native_jobs.trial_id), str(observation_id),
        plan["receipt_id"], job_id, queued_id,
        *(component for component in native_jobs.components.values()),
        *(event[0] for event in events),
        *(f"{job_id}:{1000 + n}" for n in range(1, 126)),
    ]
    return SimpleNamespace(
        config=config, workspace_id=ws, xdg=xdg, ids=tuple(dict.fromkeys(ids)),
    )


def _use_private_xdg(monkeypatch, seeded) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(seeded.xdg))
    monkeypatch.setenv("OMP_WORK_POSTGRES_PORT", str(seeded.config.port))


def _deny_mutations(monkeypatch) -> list[str]:
    """Refuse every ledger-command and native-job mutation entrypoint."""
    called: list[str] = []

    def deny(label):
        def denied(*_args, **_kwargs):
            called.append(label)
            raise AssertionError(f"qualification count called mutation entrypoint {label}")
        return denied

    for owner, name in _ENTRYPOINTS:
        monkeypatch.setattr(owner, name, deny(f"{owner.__name__}.{name}"), raising=True)
    return called


def _assert_unannotated_report(document: str, seeded) -> None:
    report = json.loads(document)
    assert report["grants_acceptance"] is False and report["frozen"] is None
    assert report["observations"] == []
    assert set(report["gates"]) == set(GATES)
    for gate in report["gates"].values():
        assert gate["status"] == "not started"
        assert gate["count"] is None
        assert gate["strata"] == {"status": "not preregistered", "allocations": []}
        assert gate["missing_proof"]
    # Excluded source ids and reasons survive: nothing is dropped or repaired.
    consecutive = {
        row["record_id"]: row["reason"]
        for row in report["gates"]["consecutive"]["exclusions"]
    }
    assert set(seeded.ids) <= set(consecutive)
    assert set(consecutive.values()) <= {FREEZE_REASON, STALE_REASON}
    attempts = report["gates"]["attempts"]
    assert attempts["queued"] == {"record_ids": [], "job_ids": [], "count": 0}
    assert attempts["execution_attempt_ids"] == [] and attempts["target_reached"] is False
    assert "invalid receipt:" not in document and "invalid observation:" not in document
    # No qualification is initiated: no stratum allocation or numeric target invented.
    assert report["gates"]["sustained"]["strata"]["allocations"] == []


def test_real_count_against_database_writes_nothing(seeded, monkeypatch, capsys):
    config = seeded.config
    before = _fingerprint(config)
    called = _deny_mutations(monkeypatch)
    _use_private_xdg(monkeypatch, seeded)

    assert main(["qualification", "count"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    _assert_unannotated_report(captured.out, seeded)

    # The printed document is exactly the projection of the stored rows.
    expected = json.dumps(
        build_report(qualification_snapshot.load_snapshot(config)), sort_keys=True,
    ) + "\n"
    assert captured.out == expected

    assert called == []
    assert _fingerprint(config) == before


_CHILD_SOURCE = r'''
import json
import os
import sys

MODE = sys.argv[1]
REPORT_FD = int(sys.argv[2])
CWD = sys.argv[3]

BLOCKED = []
ARMED = []
DENIED = []
FATAL = None
PROBING = [False]

_WRITE_CHARS = "wax+"
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
_MUTATORS = frozenset((
    "os.mkdir", "os.rmdir", "os.remove", "os.unlink", "os.rename", "os.replace",
    "os.link", "os.symlink", "os.truncate", "os.chmod", "os.chown", "os.lchown",
    "os.utime", "os.mkfifo", "os.mknod",
    "shutil.copyfile", "shutil.copymode", "shutil.copystat", "shutil.copy",
    "shutil.copy2", "shutil.move", "shutil.rmtree",
    "tempfile.mkstemp", "tempfile.mkdtemp",
))


def _audit(event, args):
    if event == "open":
        path, mode, flags = args
        bad = isinstance(mode, str) and any(char in mode for char in _WRITE_CHARS)
        if flags and flags & _WRITE_FLAGS:
            bad = True
        if bad:
            # The deliberate probe below is refused too, but is not a violation.
            if not PROBING[0]:
                BLOCKED.append(("open", str(path), mode, flags))
            raise PermissionError("qualification count attempted a filesystem write")
    elif event in _MUTATORS:
        if not PROBING[0]:
            BLOCKED.append((event, repr(args)))
        raise PermissionError("qualification count attempted a filesystem mutation")


def _arm():
    """Prove the hook refuses a write; the deliberate probe is not a violation."""
    PROBING[0] = True
    try:
        open(os.path.join(CWD, "write-probe"), "w")
    except PermissionError:
        return True
    finally:
        PROBING[0] = False
    return False


def _deny(label):
    def denied(*_args, **_kwargs):
        DENIED.append(label)
        raise AssertionError("mutation entrypoint called: " + label)
    return denied


sys.addaudithook(_audit)
ARMED.append(_arm())

try:
    from omp_work.jobs import admission, lease
    from omp_work.jobs import store as job_store
    from omp_work.v1 import store as work_store

    for owner, name in (
        (work_store.PostgresWorkStore, "execute"),
        (work_store.PostgresWorkStore, "record_refused_attempt"),
        (job_store.NativeJobStore, "run_operation"),
        (job_store.NativeJobStore, "append_event"),
        (job_store, "register_worker"),
        (job_store, "drain_worker"),
        (job_store, "record_usage"),
        (admission, "enqueue_job"),
        (admission, "claim_job"),
        (lease, "require_live_holder"),
        (lease, "renew_lease"),
        (lease, "settle_job"),
        (lease, "reconcile_jobs"),
    ):
        setattr(owner, name, _deny(owner.__name__ + "." + name))

    if MODE == "source-failure":
        import omp_work.qualification_snapshot as snapshot
        snapshot._SOURCES = (
            ("omp_work.qualification_missing_table", "work_id", "created_at"),
            *snapshot._SOURCES,
        )

    from omp_work.__main__ import main

    code = main(["qualification", "count"])
except BaseException as error:  # noqa: BLE001 - reported, never mixed into stdout
    FATAL = f"{type(error).__name__}: {error}"
    code = 2

ARMED.append(_arm())
payload = {
    "mode": MODE, "code": code, "blocked": BLOCKED, "armed": ARMED,
    "denied": DENIED, "fatal": FATAL,
}
os.write(REPORT_FD, json.dumps(payload).encode())
raise SystemExit(code or 0)
'''


def _package_src() -> str:
    # tests/ -> omp-work/
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


def _child_env(seeded, cwd: Path) -> dict[str, str]:
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("PG", "POSTGRES")) and key not in ("XDG_CONFIG_HOME", "HOME")
    }
    env["HOME"] = str(cwd)
    env["XDG_CONFIG_HOME"] = str(seeded.xdg)
    env["XDG_STATE_HOME"] = str(cwd / "state")
    env["XDG_DATA_HOME"] = str(cwd / "data")
    env["OMP_WORK_POSTGRES_PORT"] = str(seeded.config.port)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(
        [_package_src(), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    return env


def _run_child(seeded, cwd: Path, mode: str):
    read_fd, write_fd = os.pipe()
    try:
        proc = subprocess.run(
            [sys.executable, "-B", "-c", _CHILD_SOURCE, mode, str(write_fd), str(cwd)],
            cwd=str(cwd),
            env=_child_env(seeded, cwd),
            pass_fds=(write_fd,),
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        os.close(write_fd)
    with closing(os.fdopen(read_fd, "rb")) as handle:
        raw = handle.read()
    return proc, (json.loads(raw.decode()) if raw else None)


@pytest.mark.parametrize("mode", ["count", "source-failure"])
def test_isolated_child_count_and_source_failure_write_nothing(seeded, tmp_path, mode):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    before = _fingerprint(seeded.config)

    proc, report = _run_child(seeded, cwd, mode)

    context = f"stdout={proc.stdout!r} stderr={proc.stderr!r} report={report!r}"
    assert report is not None, context
    assert report["fatal"] is None, context
    # Armed before the count and still armed at exit: a transient write attempt
    # would have been refused and recorded, not merely reverted.
    assert report["armed"] == [True, True], context
    assert report["blocked"] == [], context
    assert report["denied"] == [], context
    assert list(cwd.iterdir()) == [] and not (cwd / "write-probe").exists()

    if mode == "count":
        assert proc.returncode == 0, context
        assert proc.stderr == "", context
        _assert_unannotated_report(proc.stdout, seeded)
    else:
        assert proc.returncode == 1, context
        assert proc.stdout == "", context
        assert (
            'relation "omp_work.qualification_missing_table" does not exist' in proc.stderr
        ), context

    assert _fingerprint(seeded.config) == before

"""Reusable world for the orchestrator slices (OMP-417-s07).

``open_e2e_world`` builds one disposable world: a bare remote that counts each
push per ref, a control clone, the worktrees directory named by
``OMP_WORKTREES_DIR``, a live checkout, an owner signing key with its allowed
signers, a verifier HMAC key, a merge credential, and a seeded project with a
repository record, a ``push_branch`` standing policy, a standing mandate, and a
project spend budget. It writes ``orchestrator.json`` and a mission request and
returns a :class:`E2EWorld` of those handles.

The world also carries the route and verdict evidence the slices assert on: the
mission draft the route reasons over, the mandate and ceiling ``mission_scope``
reads, the repository record ``classify`` reads, and the worker/verifier script
modes whose artifacts ``accept_candidate`` judges.

No ``owner.json`` is written, so the orchestrator pre-approves nothing: intake
drafts and the confirm stage waits for the owner.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import shutil
import signal
import subprocess
import sys
from typing import Any
from uuid import UUID, uuid4

import psycopg

from omp_work.candidate_git import add_worktree  # type: ignore[attr-defined]
from omp_work.mission_intake_rules import draft_from_intake
from omp_work.orchestrator import worker_sandbox
from omp_work.orchestrator.service import (
    MissionRequest,
    OrchestratorConfig,
    OrchestratorHandler,
    StageContext,
    load_config,
    load_request,
    status,
)
from omp_work.project_store import ChangeAuthority
from omp_work.spend_budget import SpendBudget
from omp_work.standing_mandate import StandingMandate
from omp_work.standing_policy import StandingPolicy
from omp_work.v1.canonical import text_sha256
from omp_work.v1.models import CommandEnvelope, MissionDraft
from omp_work.v1.store import PostgresWorkStore
from test_research_contract import _register_component
from test_workflow_service import OWNER, _grant, _seed_project

__all__ = [
    "CANDIDATE_SHA256",
    "FORGED_ATTEMPT",
    "FORGED_CANDIDATE",
    "OTHER_ATTEMPT",
    "E2EWorld",
    "SIMULATED_CRASH",
    "SimulatedCrash",
    "answer_intake",
    "crash_after_command",
    "crash_on_step",
    "draft_intake",
    "kill_first_run",
    "open_e2e_world",
    "open_worker",
    "push_ref",
    "remote_updates",
    "run_verifier_script",
    "run_worker_script",
    "seed_published_budget",
    "set_verifier_mode",
    "sign_merge_answer",
    "tick_until",
]

_FUTURE = datetime(2099, 1, 1, tzinfo=timezone.utc)
_RELEASE = "r1"
_SCOPES = ["work.read", "work.mutate", "work.approve", "work.close", "work.execute"]
_REPO_KEY = "test-repo"
_OBJECTIVE = "Ship the orchestrated change"
_BASE_CAPABILITIES = ("git_read", "git_write")
_APPROVAL_CLASSES = ("merge_protected_branch",)
_SCOPE_BUDGET = {"usd": "20", "tokens": 5000, "wall_clock_seconds": 3600, "max_subagents": 2}
_PROJECT_CEILING = Decimal("100")
_DEFAULT_TEST_COMMAND = ("/usr/bin/python3", "-c", "import sys; sys.exit(0)")
_PYTHON = "/usr/bin/python3"

CANDIDATE_SHA256 = "c" * 64
FORGED_CANDIDATE = "0" * 64
FORGED_ATTEMPT = "a-forged"
OTHER_ATTEMPT = "a-other"
SIMULATED_CRASH = "simulated-crash"

# The verifier script reads .omp/verify-task.json and writes the signed verdict
# and the auditor report the way the audit stage later expects them.
_VERIFIER_SCRIPT = r'''
import hashlib, hmac, json, subprocess, sys
from pathlib import Path
mode = sys.argv[1]
task = json.loads(Path(".omp/verify-task.json").read_text())
attempt = task["attempt_id"]
if mode == "skip":
    sys.exit(0)
if mode == "crash":
    sys.exit(1)
if mode == "other_attempt":
    attempt = "a-other"
command = task["test_command"]
criteria = task.get("criteria") or []
completed = subprocess.run(command, capture_output=True)
exit_code = completed.returncode
passed = mode == "pass" and exit_code == 0
verdict = "pass" if passed else "fail"
payload = {
    "candidate_sha256": task["candidate_sha256"],
    "attempt_id": attempt,
    "verdict": verdict,
    "verifier_id": task["verifier_id"],
}
raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
signature = hmac.new(Path(task["key_path"]).read_bytes(), raw, hashlib.sha256).hexdigest()
out = Path(".omp")
out.mkdir(exist_ok=True)
(out / "verdict.json").write_text(json.dumps({**payload, "signature": signature}))
lines = ["VERDICT: " + ("PASS" if passed else "NEEDS_FIX"), "FINDINGS", "(none)"]
lines.append("ACCEPTANCE COVERAGE")
for criterion in criteria:
    lines.append("- " + str(criterion.get("observable_outcome") or criterion.get("statement")))
lines += ["OUT OF SCOPE", "none", "CHECKS RUN", " ".join(command) + " -> exit " + str(exit_code)]
lines += ["REMAINING QUESTIONS", "none"]
(out / "audit-report.txt").write_text("\n".join(lines) + "\n")
sys.exit(0 if passed else 1)
'''

# The worker script writes the artifact each worker mode stands for.
_WORKER_SCRIPT = r'''
import hashlib, hmac, json, sys
from pathlib import Path
mode = sys.argv[1]
if mode == "ok":
    Path("feature.txt").write_text("feature implemented\n")
elif mode == "outside":
    Path("outside.txt").write_text("out of envelope\n")
elif mode == "new_scope":
    Path(".omp").mkdir(exist_ok=True)
    Path(".omp/new-scope.json").write_text(json.dumps({"reason": "needs a wider scope"}))
elif mode == "forge":
    payload = {
        "candidate_sha256": "0" * 64,
        "attempt_id": "a-forged",
        "verdict": "pass",
        "verifier_id": "verifier",
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(b"wrong-key", raw, hashlib.sha256).hexdigest()
    Path(".omp").mkdir(exist_ok=True)
    Path(".omp/verdict.json").write_text(json.dumps({**payload, "signature": signature}))
sys.exit(0)
'''


class SimulatedCrash(BaseException):
    """One injected crash. ``Exception`` handlers must not swallow it."""


@dataclass
class E2EWorld:
    """Every handle one e2e world exposes to the slices."""

    service: Any
    workspace_id: UUID
    project_id: UUID
    mission_id: UUID
    repo_key: str
    objective: str
    extra_capability: str | None
    remote_repo: Path
    control_repo: Path
    worktrees_dir: Path
    live_checkout: Path
    worktree: Path
    base_commit: str
    candidate_ref: str
    target_ref: str
    owner_key_path: Path
    merge_key_path: Path
    allowed_signers_path: Path
    verifier_key_path: Path
    verifier_key: bytes
    worker_mode: str
    worker_argv: tuple[str, ...]
    verifier_mode: str
    verifier_argv: tuple[str, ...]
    mandate: StandingMandate
    ceiling: Decimal
    draft: MissionDraft
    config: OrchestratorConfig
    config_path: Path
    request: MissionRequest
    request_path: Path
    operations: tuple[str, ...]
    store: PostgresWorkStore
    intake_decision_id: UUID | None = None
    mission_revision: int = 0
    _count_path: Path = field(default_factory=Path)

    def repository_record(self):
        from omp_work.standing_policy import RepositoryRecord

        return RepositoryRecord(
            key=self.repo_key,
            default_branch="main",
            protected_branches=("main", "refs/heads/main"),
            automation_ci_secret_free=True,
        )


def _owner(decision_id: UUID | str) -> ChangeAuthority:
    return ChangeAuthority(
        requested_by_kind="owner",
        decision_id=decision_id,
        answered_by_kind="owner",
    )


def _run_git(*args: str, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd is not None else None,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _seed_bare_remote(root: Path) -> tuple[Path, Path]:
    """Create the bare remote (with a per-ref push counter) and the seeded main."""
    remote = root / "remote.git"
    _run_git("init", "--bare", "-b", "main", str(remote))
    _run_git("-C", str(remote), "config", "core.logAllRefUpdates", "always")
    count_path = remote / "push-count"
    count_path.write_text("", encoding="utf-8")
    hooks = remote / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    update = hooks / "update"
    update.write_text(f"#!/bin/sh\necho \"$1\" >> '{count_path}'\nexit 0\n", encoding="utf-8")
    update.chmod(0o755)

    seed = root / "seed"
    seed.mkdir()
    _run_git("init", "-b", "main", str(seed))
    _run_git("-C", str(seed), "config", "user.name", "Test Author")
    _run_git("-C", str(seed), "config", "user.email", "author@example.com")
    _run_git("-C", str(seed), "config", "commit.gpgsign", "false")
    (seed / "README.md").write_text("# Project\n", encoding="utf-8")
    _run_git("-C", str(seed), "add", "README.md")
    _run_git("-C", str(seed), "commit", "-m", "Initial commit")
    _run_git("-C", str(seed), "remote", "add", "origin", str(remote))
    _run_git("-C", str(seed), "push", "origin", "main")
    base = _run_git("-C", str(seed), "rev-parse", "HEAD")
    return remote, Path(base)


def set_verifier_mode(world: E2EWorld, mode: str) -> None:
    """Point the verifier argv at the script mode the next run uses."""
    world.verifier_mode = mode
    world.verifier_argv = (_PYTHON, "-c", _VERIFIER_SCRIPT, mode)


def run_verifier_script(
    world: E2EWorld,
    *,
    attempt_id: str = "a-1",
    candidate_sha256: str = CANDIDATE_SHA256,
    cwd: Path | None = None,
) -> tuple[int, dict[str, Any] | None]:
    """Write the verify task, run the verifier argv, return ``(exit, verdict)``."""
    target = world.worktree if cwd is None else Path(cwd)
    omp = target / ".omp"
    omp.mkdir(parents=True, exist_ok=True)
    for name in ("verdict.json", "audit-report.txt"):
        (omp / name).unlink(missing_ok=True)
    task = {
        "candidate_sha256": candidate_sha256,
        "attempt_id": attempt_id,
        "verifier_id": "verifier",
        "key_path": str(world.verifier_key_path),
        "test_command": list(world.request.test_command),
        "criteria": [
            item.model_dump(mode="json") for item in world.request.intake.acceptance_criteria
        ],
    }
    (omp / "verify-task.json").write_text(json.dumps(task), encoding="utf-8")
    completed = subprocess.run(
        list(world.verifier_argv), cwd=str(target), capture_output=True, text=True
    )
    verdict_path = omp / "verdict.json"
    record = json.loads(verdict_path.read_text()) if verdict_path.is_file() else None
    return completed.returncode, record


def run_worker_script(world: E2EWorld, *, cwd: Path | None = None) -> int:
    """Run the worker argv in its mode. Returns the exit status."""
    target = world.worktree if cwd is None else Path(cwd)
    completed = subprocess.run(
        list(world.worker_argv), cwd=str(target), capture_output=True, text=True
    )
    return completed.returncode


def run_verifier_report(world: E2EWorld) -> str:
    """The auditor report the last verifier run wrote, or ``""``."""
    path = world.worktree / ".omp" / "audit-report.txt"
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def sign_merge_answer(
    key: Path,
    *,
    workspace_id: UUID,
    decision_id: UUID,
    action_class: str = "merge_protected_branch",
    answer: str = "approve",
    target_sha256: str | None = None,
    expires_at: datetime | None = None,
) -> str:
    """Sign one tier-3 decision answer with the owner key. Returns the signature."""
    from omp_work.v1.owner_signature import NAMESPACE, decision_signature_message

    message = decision_signature_message(
        workspace_id=workspace_id,
        decision_id=decision_id,
        action_class=action_class,
        answer=answer,
        target_sha256=target_sha256,
        expires_at=expires_at if expires_at is not None else _FUTURE,
    )
    completed = subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", NAMESPACE],
        input=message,
        capture_output=True,
        check=True,
    )
    return completed.stdout.decode("ascii")


def remote_updates(world: E2EWorld, ref: str | None = None) -> int:
    """Push updates recorded by the bare remote, total or for one ref."""
    if not world._count_path.is_file():
        return 0
    lines = [line for line in world._count_path.read_text(encoding="utf-8").split() if line]
    if ref is None:
        return len(lines)
    return sum(1 for line in lines if line == ref)


def push_ref(world: E2EWorld, ref: str, *, commit: str | None = None) -> None:
    """Push ``commit`` (default the base commit) to ``ref`` on the bare remote."""
    _run_git(
        "-C",
        str(world.control_repo),
        "push",
        str(world.remote_repo),
        f"{commit or world.base_commit}:{ref}",
    )


def seed_published_budget(
    service: Any,
    workspace_id: UUID,
    work_id: UUID,
    revision_id: UUID,
) -> None:
    """Seed the candidate and intake receipt a work item's budget check reads.

    Mirrors ``test_mission_intake_e2e._seed_budget``: one ``planned`` candidate
    and one ``intake_publication`` receipt carrying the item budget.
    """
    from omp_work.v1.canonical import sha256

    budget = {"usd": "2.00", "tokens": 100, "wall_clock_seconds": 60, "max_subagents": 1}
    candidate_id, receipt_id = uuid4(), uuid4()
    payload = {
        "draft": {"budget": budget},
        "semantic_sha256": "0" * 64,
        "rule_bundle_sha256": "0" * 64,
        "ratified_by": str(uuid4()),
        "assessment_operation_id": str(uuid4()),
        "admission_receipt_id": str(uuid4()),
    }
    now = datetime.now(timezone.utc)
    with psycopg.connect(**service.config.connection_kwargs("postgres")) as conn:
        conn.execute(
            "INSERT INTO omp_work.candidates(candidate_id,workspace_id,work_id,revision_id,"
            "candidate_sha256,commit_sha,kind,allocated_at)"
            " VALUES(%s,%s,%s,%s,%s,NULL,'planned',%s)",
            (candidate_id, workspace_id, work_id, revision_id, "e" * 64, now),
        )
        conn.execute(
            "INSERT INTO omp_evidence.receipts(receipt_id,workspace_id,work_id,revision_id,"
            "candidate_id,kind,payload,payload_sha256,issuer,issued_at,candidate_sha256,"
            "candidate_commit)"
            " VALUES(%s,%s,%s,%s,%s,'intake_publication',%s,%s,"
            "'work-service/bounded-intake',%s,%s,NULL)",
            (
                receipt_id,
                workspace_id,
                work_id,
                revision_id,
                candidate_id,
                json.dumps(payload),
                sha256(payload),
                now,
                "0" * 64,
            ),
        )


def _command_envelope(world: E2EWorld, command_type: str, payload: dict[str, Any]) -> CommandEnvelope:
    return CommandEnvelope.model_validate(
        {
            "api_version": "work.omp.dev/v1",
            "workspace_id": str(world.workspace_id),
            "operation_id": str(uuid4()),
            "request_id": str(uuid4()),
            "correlation_id": str(uuid4()),
            "command": {"type": command_type, "payload": payload},
        }
    )


def draft_intake(world: E2EWorld) -> dict[str, Any]:
    """Draft the mission intake through the store and remember its decision."""
    request = world.request
    envelope = _command_envelope(
        world,
        "draft_mission_intake",
        {
            "mission_id": str(world.mission_id),
            "base_revision": None,
            "intake": request.intake.model_dump(mode="json"),
            "scope": request.scope.model_dump(mode="json"),
            "instruction": request.instruction.model_dump(mode="json"),
        },
    )
    _receipt, result = world.store.execute(
        envelope, actor_id=OWNER, actor_kind="automation", required_scope="work.mutate"
    )
    decision_id = result.get("decision_id")
    world.intake_decision_id = UUID(str(decision_id)) if decision_id else None
    mission = result.get("mission") or {}
    world.mission_revision = int(mission.get("revision") or 0)
    return result


def _edited_draft(world: E2EWorld) -> MissionDraft:
    """The draft with the extra capability dropped, as the owner would edit it."""
    if world.extra_capability is None:
        return world.draft
    return world.draft.model_copy(
        update={
            "requested_capabilities": tuple(
                item
                for item in world.draft.requested_capabilities
                if item != world.extra_capability
            )
        }
    )


def answer_intake(world: E2EWorld, answer: str = "confirm") -> dict[str, Any]:
    """Answer the pending intake decision: ``confirm``, ``edited_draft`` or ``reject``."""
    if world.intake_decision_id is None:
        raise AssertionError("draft_intake has not run")
    if answer == "confirm":
        body: dict[str, Any] = {"kind": "option", "option": "confirm"}
    elif answer == "reject":
        body = {"kind": "option", "option": "reject"}
    elif answer == "edited_draft":
        body = {"kind": "edited_draft", "draft": _edited_draft(world).model_dump(mode="json")}
    else:
        raise ValueError(f"unknown answer {answer!r}")
    envelope = _command_envelope(
        world,
        "answer_mission_draft",
        {
            "decision_id": str(world.intake_decision_id),
            "mission_id": str(world.mission_id),
            "revision": world.mission_revision,
            "answer": body,
            "instruction": world.request.instruction.model_dump(mode="json"),
        },
    )
    _receipt, result = world.store.execute(
        envelope, actor_id=OWNER, actor_kind="owner", required_scope="work.approve"
    )
    return result


def open_worker(world: E2EWorld) -> Any:
    """Start one orchestrator jobs worker bound to the world's config."""
    from omp_work.jobs.store import NativeJobStore
    from omp_work.jobs.worker import JobWorker, WorkerConfig

    component = _register_component(
        world.service,
        world.workspace_id,
        "worker",
        name=f"orch-{uuid4().hex[:8]}",
        capabilities=("omp.orchestrator",),
    )
    worker = JobWorker(
        NativeJobStore(world.service.config),
        WorkerConfig(
            workspace_id=world.workspace_id,
            actor_id=OWNER,
            worker_id=f"orch-{uuid4().hex[:8]}",
            component_sha256=component,
            capabilities=["omp.orchestrator"],
            capacity=1024,
        ),
        OrchestratorHandler(world.config),
    )
    worker.start()
    return worker


def tick_until(worker: Any, world: E2EWorld, predicate, limit: int = 120) -> dict[str, Any]:
    """Tick the worker until ``predicate(status)`` holds."""
    view = status(world.config, world.mission_id)
    for _ in range(limit):
        if predicate(view):
            return view
        worker.tick()
        view = status(world.config, world.mission_id)
    raise AssertionError(
        json.dumps({"view": view, "steps": OrchestratorHandler(world.config).steps()}, default=str)[:4000]
    )


def crash_on_step(monkeypatch, kind: str) -> dict[str, bool]:
    """Raise one :class:`SimulatedCrash` when a step of ``kind`` is recorded."""
    holder = {"done": False}
    original = OrchestratorHandler.record

    def record(self, step_index: int, body: dict[str, Any]) -> dict[str, Any]:
        if not holder["done"] and body.get("kind") == kind:
            holder["done"] = True
            raise SimulatedCrash(kind)
        return original(self, step_index, body)

    monkeypatch.setattr(OrchestratorHandler, "record", record)
    return holder


def crash_after_command(monkeypatch, command_type: str) -> dict[str, bool]:
    """Raise one :class:`SimulatedCrash` after the named command completes."""
    holder = {"done": False}
    original = StageContext.command

    def command(self, type: str, payload, *, key: str | None = None):  # noqa: A002
        result = original(self, type, payload, key=key)
        if not holder["done"] and type == command_type:
            holder["done"] = True
            raise SimulatedCrash(command_type)
        return result

    monkeypatch.setattr(StageContext, "command", command)
    return holder


def kill_first_run(monkeypatch, identity: str) -> dict[str, bool]:
    """Make the first ``run_worker`` call for ``identity`` die by self-SIGKILL."""
    holder = {"done": False}
    original = worker_sandbox.run_worker
    suicide = "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"

    def run_worker(argv, **kwargs):
        if not holder["done"] and kwargs.get("identity") == identity:
            holder["done"] = True
            argv = [_PYTHON, "-c", suicide]
        return original(argv, **kwargs)

    monkeypatch.setattr(worker_sandbox, "run_worker", run_worker)
    return holder


def open_e2e_world(
    service: Any,
    tmp_path: Path,
    monkeypatch,
    *,
    verifier_mode: str,
    worker_mode: str,
    extra_capability: str | None = "egress",
    test_command: tuple[str, ...] | None = None,
    operations: tuple[str, ...],
) -> E2EWorld:
    """Build the disposable orchestrator world described in the module docstring."""
    root = Path(tmp_path)
    root.mkdir(parents=True, exist_ok=True)

    workspace_id, project_id, mission_id = uuid4(), uuid4(), uuid4()
    _grant(service, workspace_id)
    with psycopg.connect(
        **service.config.connection_kwargs("postgres"), autocommit=True
    ) as conn:
        conn.execute(
            "INSERT INTO omp_control.workspaces(workspace_id) VALUES (%s)"
            " ON CONFLICT DO NOTHING",
            (workspace_id,),
        )
    _seed_project(service, workspace_id, project_id)

    remote_repo, base_commit = _seed_bare_remote(root)
    control_repo = root / "control.git"
    _run_git("clone", "--bare", str(remote_repo), str(control_repo))

    worktrees_dir = root / "worktrees"
    worktrees_dir.mkdir()
    monkeypatch.setenv("OMP_WORKTREES_DIR", str(worktrees_dir))
    live_checkout = root / "live"
    _run_git("clone", str(remote_repo), str(live_checkout))

    owner_key_path = root / "owner_key"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(owner_key_path)],
        check=True,
        capture_output=True,
    )
    owner_pub = Path(f"{owner_key_path}.pub").read_text(encoding="utf-8").strip()
    allowed_signers_path = root / "allowed_signers"
    allowed_signers_path.write_text(f"owner {owner_pub}\n", encoding="utf-8")
    service.config.config_dir.mkdir(parents=True, exist_ok=True)
    (service.config.config_dir / "owner_allowed_signers").write_text(
        f"owner {owner_pub}\n", encoding="utf-8"
    )
    merge_key_path = root / "merge_key"
    shutil.copyfile(owner_key_path, merge_key_path)
    merge_key_path.chmod(0o600)

    verifier_key_path = root / "verifier.key"
    verifier_key = b"0123456789abcdef0123456789abcdef"
    verifier_key_path.write_bytes(verifier_key)

    store = PostgresWorkStore(service.config)
    store.update_profile(
        workspace_id,
        OWNER,
        project_id,
        repositories=(
            {
                "key": _REPO_KEY,
                "name": _REPO_KEY,
                "url": str(remote_repo),
                "default_branch": "main",
                "protected_branches": ["main", "refs/heads/main"],
                "automation_ci_secret_free": True,
            },
        ),
    )

    push_decision = uuid4()
    store.put_standing_policy(
        workspace_id,
        OWNER,
        project_id,
        StandingPolicy(
            policy_id=uuid4(),
            action_class="push_branch",
            repositories=(_REPO_KEY,),
            branch_patterns=("refs/heads/omp/*",),
            decision_id=push_decision,
        ),
        _owner(push_decision),
    )

    capabilities = list(_BASE_CAPABILITIES)
    if extra_capability is not None:
        capabilities.append(extra_capability)
    mandate_decision = uuid4()
    mandate = StandingMandate(
        mandate_id=uuid4(),
        goals=frozenset({_OBJECTIVE}),
        repositories=frozenset({_REPO_KEY}),
        capabilities=frozenset(_BASE_CAPABILITIES),
        tier3_classes=frozenset(_APPROVAL_CLASSES),
        decision_id=mandate_decision,
    )
    store.set_standing_mandate(
        workspace_id, OWNER, project_id, mandate, _owner(mandate_decision)
    )
    store.set_spend_budget(
        workspace_id,
        OWNER,
        project_id,
        None,
        SpendBudget(str(project_id), _PROJECT_CEILING, Decimal("40")),
        _owner(uuid4()),
    )

    candidate_ref = f"refs/heads/omp/{mission_id.hex[:12]}"
    allowed_paths = ["feature.txt"]
    if worker_mode == "forge":
        allowed_paths += [".omp/verdict.json", ".omp/audit-report.txt"]
    command = tuple(test_command) if test_command is not None else _DEFAULT_TEST_COMMAND
    prompt = _OBJECTIVE

    request_document = {
        "mission_id": str(mission_id),
        "project_id": str(project_id),
        "unattended": False,
        "intake": {
            "archetype": "small_code_change",
            "source": {"text": prompt, "sha256": text_sha256(prompt), "spans": []},
            "goal": {"id": "goal-1", "statement": _OBJECTIVE, "source_span_ids": []},
            "acceptance_criteria": [
                {
                    "id": "ac-1",
                    "statement": "tests pass",
                    "source_span_ids": [],
                    "observable_outcome": "tests pass",
                    "oracle": "automated_test",
                }
            ],
        },
        "scope": {
            "project_id": str(project_id),
            "repositories": [_REPO_KEY],
            "requested_capabilities": capabilities,
            "approval_classes": list(_APPROVAL_CLASSES),
            "risk_policy": "standard",
            "approval_policy": "standard",
            "effort_policy": "standard",
            "budget_policy": dict(_SCOPE_BUDGET),
            "kind": "engineering.execute",
        },
        "instruction": {
            "text": "Please proceed with the implementation.",
            "provenance": {
                "channel": "cli",
                "message_ref": "msg-417",
                "received_at": "2026-10-01T00:00:00+00:00",
            },
        },
        "work": {
            "client_ref": f"m-{mission_id.hex[:12]}",
            "title": "Orchestrated feature",
            "project_id": str(project_id),
            "description": prompt,
            "acceptance_criteria": ["tests pass"],
        },
        "repository": _REPO_KEY,
        "remote": str(remote_repo),
        "candidate_ref": candidate_ref,
        "target_ref": "main",
        "base_commit": base_commit,
        "allowed_paths": allowed_paths,
        "test_command": list(command),
        "worker_argv": [_PYTHON, "-c", _WORKER_SCRIPT, worker_mode],
        "controls": {},
    }
    request_path = root / "request.json"
    request_path.write_text(json.dumps(request_document, indent=2), encoding="utf-8")
    request = load_request(request_path)

    config_path = root / "orchestrator.json"
    config_path.write_text(
        json.dumps(
            {
                "workspace_id": str(workspace_id),
                "automation_capability_path": str(root / "automation.json"),
                "qualification_path": str(root / "qualification.json"),
                "lock_map_path": str(root / "lock-map.json"),
                "verifier_key_path": str(verifier_key_path),
                "allowed_signers": str(allowed_signers_path),
                "control_repo": str(control_repo),
                "worktrees_dir": str(worktrees_dir),
                "live_checkout": str(live_checkout),
                "repair_rounds": 3,
                "max_workers": 1,
                "lease_seconds": 60,
                "wait_seconds": 0,
                "verifier_argv": [_PYTHON, "-c", _VERIFIER_SCRIPT, verifier_mode],
                "merge_credential_path": str(merge_key_path),
                "operations": list(operations),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (root / "automation.json").write_text(
        json.dumps(
            {
                "actor_id": str(OWNER),
                "actor_kind": "automation",
                "workspaces": [str(workspace_id)],
                "scopes": _SCOPES,
            }
        ),
        encoding="utf-8",
    )
    (root / "qualification.json").write_text(
        json.dumps(
            {
                "release": _RELEASE,
                "results": {
                    "jobs-worker-kill-restart": {
                        "item": "jobs-worker-kill-restart",
                        "test": "t",
                        "result": "pass",
                        "release": _RELEASE,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (root / "lock-map.json").write_text("{}", encoding="utf-8")
    config = load_config(config_path, service.config)

    worktree = add_worktree(control_repo, worktrees_dir, f"{mission_id.hex[:12]}-verify", base_commit)
    draft = draft_from_intake(request.intake, request.scope.model_dump())

    return E2EWorld(
        service=service,
        workspace_id=workspace_id,
        project_id=project_id,
        mission_id=mission_id,
        repo_key=_REPO_KEY,
        objective=_OBJECTIVE,
        extra_capability=extra_capability,
        remote_repo=remote_repo,
        control_repo=control_repo,
        worktrees_dir=worktrees_dir,
        live_checkout=live_checkout,
        worktree=worktree,
        base_commit=base_commit,
        candidate_ref=candidate_ref,
        target_ref="main",
        owner_key_path=owner_key_path,
        merge_key_path=merge_key_path,
        allowed_signers_path=allowed_signers_path,
        verifier_key_path=verifier_key_path,
        verifier_key=verifier_key,
        worker_mode=worker_mode,
        worker_argv=(_PYTHON, "-c", _WORKER_SCRIPT, worker_mode),
        verifier_mode=verifier_mode,
        verifier_argv=(_PYTHON, "-c", _VERIFIER_SCRIPT, verifier_mode),
        mandate=mandate,
        ceiling=_PROJECT_CEILING,
        draft=draft,
        config=config,
        config_path=config_path,
        request=request,
        request_path=request_path,
        operations=tuple(operations),
        store=store,
        _count_path=remote_repo / "push-count",
    )

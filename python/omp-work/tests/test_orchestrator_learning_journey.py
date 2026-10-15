"""Native PASS admission through a real orchestrator worker launch.

Learning stays off in the world's orchestrator file. The test turns it on only
in a temporary config. The worker is the compiled orchestrator-worker binary,
jailed, talking to the model proxy. Provider system prompts are the upstream
Gemini requests.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from omp_work.jobs.cancel import cancel_job
from omp_work.jobs.store import NativeJobStore
from omp_work.model_proxy import ProxyConfig, create_app
from omp_work.orchestrator import service as orchestrator
from omp_work.orchestrator.service import OrchestratorHandler, submit
from omp_work.v1.api_models import DomainEventView, DomainEventsPage
from omp_work.v1.canonical import canonical_json, sha256, text_sha256
from omp_work.v1.client import WorkClient
from omp_work.v1.server import create_app as work_app
from orchestrator_e2e_support import open_e2e_world, open_worker, tick_until
from test_item_budget_alerts import _seed_budget
from test_model_proxy import serving
from test_model_proxy_upstream import gemini_route, ollama_route
from test_orchestrator_production_worker import (
    CONTENT,
    CONTROL_TOKEN,
    PROVIDER_KEY,
    binary as production_binary,
    upstream,
)
from test_workflow_service import (
    OWNER,
    _command,
    _create,
    _owner_headers,
    _plan,
    _receipt,
)
from typed_intake_support import seed_typed_intake

_KNOWLEDGE_SRC = Path(__file__).resolve().parents[2] / "omp-knowledge" / "src"
_WORK_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_KNOWLEDGE_SRC) not in sys.path:
    sys.path.insert(0, str(_KNOWLEDGE_SRC))

from omp_knowledge.context.stage import compile_stage
from omp_knowledge.learning import capture, lifecycle, promotion
from omp_knowledge.learning.generation import GenerationResult
from omp_knowledge.learning.models import Claim, Lesson, SourceIdentity, SourceSpan
from omp_knowledge.learning.store import LearningStore
from omp_knowledge.learning.uses import record_use
from omp_work.orchestrator import lesson_context, operations
from omp_work.research.lessons import LessonDraft, stage

pytest_plugins = (
    "test_workflow_service",
    "test_orchestrator_production_worker",
)

LESSON_TITLE = "Journey bound PASS procedure"
LESSON_STEP = "Repeat the cited PASS before the edit"
CLAIM_TEXT = "Journey PASS evidence"
INSTRUCTION = "Please proceed with the implementation."


class _BoundPassGenerator:
    """One lesson whose claim is a slice of the trace's PASS verification."""

    model = "journey-deterministic"
    profile = "journey-profile"

    def generate(self, trace: dict) -> GenerationResult:
        work_record = trace.get("work_record")
        if not isinstance(work_record, dict):
            raise RuntimeError(f"work record missing: {sorted(trace)}")
        match = next(
            (
                row
                for row in work_record.get("receipts") or []
                if row.get("kind") == "verification"
                and row.get("verdict") == "PASS"
                and row.get("payload_truncated") is False
            ),
            None,
        )
        if match is None:
            raise RuntimeError(f"PASS verification missing: {work_record.get('receipts')}")
        payload_text = match["payload_text"]
        start = payload_text.index(CLAIM_TEXT)
        lesson = Lesson(
            title=LESSON_TITLE,
            steps=(LESSON_STEP,),
            claims=(
                Claim(
                    text=CLAIM_TEXT,
                    receipt_ids=(match["receipt_id"],),
                    source_span=SourceSpan(start=start, end=start + len(CLAIM_TEXT)),
                    evaluator=match["issuer"],
                    outcome="PASS",
                ),
            ),
        )
        body = lesson.model_dump_json()
        return GenerationResult(
            model=self.model,
            profile=self.profile,
            request_sha256=text_sha256(canonical_json(trace)),
            response_sha256=text_sha256(body),
            lessons=(lesson,),
        )


class _OneEvent:
    def __init__(self, event: DomainEventView) -> None:
        self._event = event

    def events(self, after_sequence: int, limit: int) -> DomainEventsPage:
        del limit
        sequence = int(self._event.sequence)
        if int(after_sequence) >= sequence:
            return DomainEventsPage(
                events=(),
                watermark_sequence=sequence,
                next_after_sequence=sequence,
                has_more=False,
            )
        return DomainEventsPage(
            events=(self._event,),
            watermark_sequence=sequence,
            next_after_sequence=sequence,
            has_more=False,
        )


def test_unknown_citation_is_rejected(tmp_path: Path) -> None:
    draft = LessonDraft(
        lesson_id="unknown-citation",
        project_id="journey-project",
        kind="execution",
        title="Unresolved citation",
        steps=["Do not stage"],
        claim="missing",
        receipt_ids=["receipt-does-not-resolve"],
    )

    def resolve(receipt_id: str) -> dict:
        raise LookupError(receipt_id)

    record = stage(draft, tmp_path, resolve=resolve)
    assert record["status"] == "rejected"
    assert record["revision"] == 0
    assert record["fk6_lesson"] is None
    assert not (tmp_path / draft.lesson_id).exists()


def _workflow(service, workspace_id, key: str) -> dict:
    response = service.client.get(
        f"/v1/work-items/{key}/workflow", headers=_owner_headers(workspace_id)
    )
    assert response.status_code == 200, response.text
    return response.json()


def _append_pass(service, workspace_id, item: dict, candidate: dict) -> dict:
    extra = {
        "candidate_sha256": candidate["candidate_sha256"],
        "verdict": "PASS",
        "issuer": "journey-evaluator",
    }
    if candidate.get("commit_sha"):
        extra["candidate_commit"] = candidate["commit_sha"]
    receipt = _receipt(
        item["work_id"],
        item["revision_id"],
        candidate["candidate_id"],
        "verification",
        body={"summary": CLAIM_TEXT},
        **extra,
    )
    status, body = _command(
        service,
        workspace_id,
        {"type": "append_evidence", "payload": {"receipt": receipt}},
    )
    assert status == 200, body
    return body["result"]["receipt"]


def _rows(learning_dir: Path, table: str) -> list[sqlite3.Row]:
    with sqlite3.connect(learning_dir / "learning.sqlite") as conn:
        conn.row_factory = sqlite3.Row
        return list(conn.execute(f"SELECT * FROM {table}").fetchall())


def _procedure(learning_dir: Path) -> sqlite3.Row:
    rows = _rows(learning_dir, "procedures")
    assert len(rows) == 1, [dict(row) for row in rows]
    return rows[0]


def _bundle(state_dir: Path, bundle_id: str) -> sqlite3.Row:
    with sqlite3.connect(state_dir / "context-bundles.sqlite") as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM context_bundles WHERE bundle_id = ?", (bundle_id,)
        ).fetchone()
    assert row is not None, bundle_id
    return row


def _use_config(world, worker, config):
    world.config = config
    if worker is not None:
        worker.handlers["*"].config = config
    return config


def _worker_prompts(calls) -> list[str]:
    texts: list[str] = []
    for path, body, *_rest in calls:
        if not str(path).startswith("/v1beta/"):
            continue
        instruction = body.get("systemInstruction") or body.get("system_instruction") or {}
        parts = instruction.get("parts") if isinstance(instruction, dict) else None
        for part in parts or []:
            text = part.get("text") if isinstance(part, dict) else None
            if isinstance(text, str) and "You are the orchestrator worker" in text:
                texts.append(text)
    return texts


def _prepare_mission(world, service, *, fresh: bool) -> UUID:
    if fresh:
        mission_id = uuid4()
        request = replace(
            world.request,
            mission_id=mission_id,
            candidate_ref=f"refs/heads/omp/{mission_id.hex[:12]}",
            work=world.request.work.model_copy(
                update={
                    "client_ref": f"m-{mission_id.hex[:12]}",
                    "title": f"Orchestrated feature {mission_id.hex[:12]}",
                }
            ),
        )
        seed_typed_intake(
            world.store,
            workspace_id=world.workspace_id,
            actor_id=OWNER,
            mission_id=mission_id,
            run_id=f"orchestrator:{mission_id}",
            draft=request.intake,
        )
        world.mission_id = mission_id
        world.request = request
    submitted = submit(world.config, world.request)
    assert submitted["status"] == "enqueued", submitted
    work_id = UUID(submitted["work_id"])
    tree = world.store.read(world.workspace_id, OWNER, "tree", "")
    item = next(row for row in tree["items"] if str(row.get("work_id")) == str(work_id))
    _seed_budget(
        SimpleNamespace(service=service, workspace_id=world.workspace_id, actor_id=OWNER),
        work_id=work_id,
        revision_id=UUID(str(item["revision"]["revision_id"])),
        budget={
            "usd": "2.00",
            "tokens": 200_000,
            "wall_clock_seconds": 180,
            "max_subagents": 0,
        },
    )
    return work_id


def _outcome(world) -> dict:
    handler = OrchestratorHandler(world.config)
    handler.mission_id = world.mission_id
    return next(
        step
        for step in handler.steps()
        if step.get("stage") == "implement" and step.get("kind") == "outcome"
    )


def _release_successor(world) -> None:
    handler = OrchestratorHandler(world.config)
    handler.mission_id = world.mission_id
    successors = [step.get("next_job") for step in handler.steps() if step.get("next_job")]
    if not successors:
        return
    cancel_job(
        NativeJobStore(world.service.config),
        operation_id=str(uuid4()),
        workspace_id=world.workspace_id,
        actor_id=OWNER,
        job_id=str(successors[-1]),
        reason="stop after implement",
    )


def _release_grant(service, world) -> None:
    """One workspace admits a single active execution grant."""
    grant_id = orchestrator._ids(world.mission_id, "grant")
    response = service.client.get(
        f"/v1/workspaces/{world.workspace_id}/execution/{grant_id}",
        headers=_owner_headers(world.workspace_id),
    )
    assert response.status_code == 200, response.text
    grant = response.json()["grant"]
    if grant["state"] not in ("active", "paused"):
        return
    status, body = _command(
        service,
        world.workspace_id,
        {
            "type": "set_execution_state",
            "payload": {
                "grant_id": str(grant_id),
                "expected_grant_version": grant["grant_version"],
                "target_state": "canceled",
                "reason": "release the workspace grant for the next launch",
                "judge_sha256": grant["judge_sha256"],
            },
        },
    )
    assert status == 200, body


def _launch(worker, world, service, calls, *, fresh: bool) -> tuple[dict, list[str]]:
    _prepare_mission(world, service, fresh=fresh)
    start = len(calls)

    def implemented(view) -> bool:
        return any(
            step.get("stage") == "implement" and step.get("kind") == "outcome"
            for step in view["steps"]
        )

    tick_until(worker, world, implemented, limit=40)
    outcome = _outcome(world)
    prompts = _worker_prompts(calls[start:])
    _release_successor(world)
    _release_grant(service, world)
    return outcome, prompts


def _succeeded(outcome: dict, prompts: list[str], commands: list) -> None:
    stderr = ""
    if commands:
        stderr = commands[-1][2].stderr or ""
    assert outcome["outcome"] == "succeeded", (outcome, stderr[-2000:])
    assert (Path(outcome["data"]["worktree"]) / "feature.txt").read_text() == CONTENT
    assert prompts, outcome


def _sweep_calls(commands: list) -> int:
    return sum(1 for argv, _kwargs, _result in commands if "sweep" in argv)


@pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
def test_orchestrator_learning_journey(service, tmp_path, monkeypatch, binary) -> None:
    """Approved lesson is supplied once; unapproved, disabled, and revised stays out."""
    binary_path, shim, native = binary
    assert production_binary.__name__ == "binary"
    assert callable(compile_stage)
    assert callable(lifecycle.sweep)
    assert callable(lesson_context.compile_context)
    assert callable(lesson_context.record_delivery)
    assert callable(lesson_context.sweep_lessons)
    monkeypatch.setattr(orchestrator, "_REGISTRY", dict(orchestrator._REGISTRY))
    monkeypatch.syspath_prepend(str(_KNOWLEDGE_SRC))
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join((str(_KNOWLEDGE_SRC), str(_WORK_SRC)))
    )
    for key in list(os.environ):
        if key.startswith("OMP_KNOWLEDGE_"):
            monkeypatch.delenv(key)
    counter = tmp_path / "counter.py"
    counter.write_text(
        "import json\nimport sys\n"
        "encoding = sys.argv[3]\n"
        'print(json.dumps({"api": "countTokens", "encoding": encoding}), flush=True)\n'
        "for line in sys.stdin:\n"
        "    item = json.loads(line)\n"
        '    print(json.dumps({"id": item["id"], "tokens": len(item["text"].split())}), flush=True)\n'
    )
    settings = tmp_path / "context.json"
    state_dir = tmp_path / "context-state"
    context_command = (sys.executable, "-m", "omp_knowledge.context")
    settings.write_text(
        json.dumps(
            {
                "command": list(context_command),
                "token_cmd": [sys.executable, str(counter)],
                "state_dir": str(state_dir),
                "encoding": "test-words",
                "token_budget": 100000,
            }
        )
    )
    commands: list = []
    original_run = lesson_context.subprocess.run

    def record_run(*args, **kwargs):
        result = original_run(*args, **kwargs)
        argv = args[0] if args else ()
        text = " ".join(str(part) for part in argv)
        if "omp_knowledge.context" in text or "omp_knowledge.learning" in text:
            commands.append((argv, kwargs, result))
        return result

    monkeypatch.setattr(lesson_context.subprocess, "run", record_run)
    monkeypatch.setenv("GEMINI_API_KEY", PROVIDER_KEY)
    monkeypatch.setenv("OMP_CONTROL_TOKEN", CONTROL_TOKEN)
    world = open_e2e_world(
        service,
        tmp_path / "world",
        monkeypatch,
        verifier_mode="pass",
        worker_mode="ok",
        extra_capability=None,
        operations=("omp_work.orchestrator.operations:register",),
    )
    assert world.config.learning_state_dir is None
    assert world.config.learning_command == ()
    assert world.config.learning_bearer_file is None
    on_disk = json.loads(world.config_path.read_text())
    assert "learning_state_dir" not in on_disk
    assert "learning_command" not in on_disk
    world.request = replace(
        world.request,
        worker_argv=(str(binary_path), "orchestrator-worker"),
        scope=world.request.scope.model_copy(
            update={
                "budget_policy": world.request.scope.budget_policy.model_copy(
                    update={"tokens": 200_000}
                )
            }
        ),
    )
    assert world.request.instruction.text == INSTRUCTION
    capability = world.config.automation_capability_path
    data = json.loads(capability.read_text())
    data["token"] = CONTROL_TOKEN
    capability.write_text(json.dumps(data))
    capability.chmod(0o600)
    for name in ("gemini-key", "ollama-key"):
        key_path = service.config.credentials_dir / name
        key_path.write_text(PROVIDER_KEY)
        key_path.chmod(0o600)
    from omp_work import egress_sandbox
    from omp_work.orchestrator import worker_sandbox

    real_sandbox = worker_sandbox.run_sandboxed
    real_pump = egress_sandbox._pump

    def observe_sandbox(argv, identity, recorder, workdir, env, timeout, **kwargs):
        kwargs["ro_binds"] = (*kwargs.get("ro_binds", ()), str(shim), str(native))
        return real_sandbox(argv, identity, recorder, workdir, env, timeout, **kwargs)

    def observe_peer(left, right):
        return real_pump(left, right)

    monkeypatch.setattr(worker_sandbox, "run_sandboxed", observe_sandbox)
    monkeypatch.setattr(egress_sandbox, "_pump", observe_peer)

    learning_dir = tmp_path / "learning"
    bearer = tmp_path / "learning-bearer.json"
    bearer.write_text(json.dumps({"token": "owner-token"}))
    bearer.chmod(0o600)

    with upstream() as (transport, calls, _entered, release):
        proxy = create_app(
            ProxyConfig(
                service.config,
                capability,
                {
                    "gemini_flash": gemini_route(model="gemini-3.8-flash-high"),
                    "ollama_cloud": ollama_route(model="deepseek-v4.1-flash:cloud"),
                },
            ),
            transport=transport,
        )
        with (
            serving(proxy) as proxy_url,
            serving(work_app(service.config, capabilities_dir=service.capabilities)) as work_url,
        ):
            try:
                learning_config = replace(
                    world.config,
                    model_proxy_url=proxy_url,
                    workservice_url=work_url,
                    lease_seconds=180,
                    context_command=context_command,
                    context_settings_path=settings,
                    learning_state_dir=learning_dir,
                    learning_command=(sys.executable, "-m", "omp_knowledge.learning"),
                    learning_bearer_file=bearer,
                )
                disabled_config = replace(
                    learning_config,
                    learning_state_dir=None,
                    learning_command=(),
                    learning_bearer_file=None,
                )
                source = _create(
                    service,
                    world.workspace_id,
                    "Journey source",
                    description="bound pass",
                )
                _plan(service, world.workspace_id, source)
                source_workflow = _workflow(service, world.workspace_id, source["key"])
                candidate = source_workflow["item"]["candidate"]
                assert candidate is not None
                verification = _append_pass(
                    service, world.workspace_id, source, candidate
                )
                payload = {"work_id": str(source["work_id"]), "state": "DONE"}
                event = DomainEventView(
                    event_id=uuid4(),
                    sequence=1,
                    workspace_id=world.workspace_id,
                    aggregate_type="work_item",
                    aggregate_id=UUID(str(source["work_id"])),
                    aggregate_version=1,
                    actor_id=OWNER,
                    actor_kind="owner",
                    capability_id=uuid4(),
                    request_id=uuid4(),
                    correlation_id=uuid4(),
                    operation_id=uuid4(),
                    causation_id=uuid4(),
                    event_type="complete_work",
                    outcome="applied",
                    payload=payload,
                    payload_sha256=sha256(payload),
                    previous_event_sha256=None,
                    event_sha256=sha256({"event": "complete_work", "work_id": payload["work_id"]}),
                    occurred_at=datetime.now(timezone.utc),
                )
                client = WorkClient(work_url, world.workspace_id, bearer)
                store = LearningStore(learning_dir)
                try:
                    run = capture.drain(
                        store,
                        _OneEvent(event),
                        client,
                        _BoundPassGenerator(),
                        workspace_id=world.workspace_id,
                        records=client,
                    )
                    proposals = store.execute(
                        "SELECT status, reason, source_json FROM proposals"
                    ).fetchall()
                    assert run.proposals_accepted == 1, (
                        run.model_dump(),
                        [dict(row) for row in proposals],
                    )
                    assert run.proposals_rejected == 0
                    source_identity = SourceIdentity.model_validate_json(
                        proposals[0]["source_json"]
                    )
                    assert source_identity.aggregate_id == str(source["work_id"])
                    assert source_identity.work_id == str(source["work_id"])
                    assert source_identity.candidate_id == str(candidate["candidate_id"])
                    assert source_identity.revision_id == str(source["revision_id"])
                    assert lifecycle.workspace_has_resolvable_lessons(
                        store, world.workspace_id
                    )
                finally:
                    store.close()
                procedure = _procedure(learning_dir)
                procedure_id = procedure["procedure_id"]
                assert procedure["status"] == "active"
                assert procedure["title"] == LESSON_TITLE
                support = _rows(learning_dir, "procedure_support")
                assert [row["receipt_id"] for row in support] == [verification["receipt_id"]]
                assert _rows(learning_dir, "supplies") == []
                assert _rows(learning_dir, "uses") == []
                assert _rows(learning_dir, "outcomes") == []

                _use_config(world, None, learning_config)
                worker = open_worker(world)
                assert operations.run_implement is orchestrator._REGISTRY["implement"]
                sweeps_before = _sweep_calls(commands)
                unapproved, unapproved_prompts = _launch(
                    worker, world, service, calls, fresh=False
                )
                _succeeded(unapproved, unapproved_prompts, commands)
                assert _sweep_calls(commands) > sweeps_before
                assert all(LESSON_TITLE not in text for text in unapproved_prompts)
                assert all(procedure_id not in text for text in unapproved_prompts)
                assert unapproved["data"].get("lesson_supply_ids") in (None, [])
                assert _rows(learning_dir, "supplies") == []
                assert _procedure(learning_dir)["status"] == "active"

                store = LearningStore(learning_dir)
                try:
                    promotion.record_journey_pass(
                        store,
                        workspace_id=world.workspace_id,
                        work_id=source_identity.aggregate_id,
                    )
                    decision = promotion.request_promotion(
                        store,
                        world.workspace_id,
                        project_id=world.project_id,
                    )
                    assert decision is not None, source_identity.model_dump()
                    promotion.answer_promotion(
                        store,
                        decision["decision_id"],
                        approve=True,
                        answer_ref="owner-journey-approval",
                    )
                finally:
                    store.close()

                sweeps_before = _sweep_calls(commands)
                _use_config(world, worker, disabled_config)
                disabled, disabled_prompts = _launch(
                    worker, world, service, calls, fresh=True
                )
                _succeeded(disabled, disabled_prompts, commands)
                assert _sweep_calls(commands) == sweeps_before
                assert all(LESSON_TITLE not in text for text in disabled_prompts)
                assert all(procedure_id not in text for text in disabled_prompts)
                assert "lesson_supply_ids" not in disabled["data"]
                assert _rows(learning_dir, "supplies") == []

                _use_config(world, worker, learning_config)
                approved, approved_prompts = _launch(
                    worker, world, service, calls, fresh=True
                )
                _succeeded(approved, approved_prompts, commands)
                assert all(LESSON_TITLE in text for text in approved_prompts)
                assert all(LESSON_STEP in text for text in approved_prompts)
                assert all(f"{procedure_id}@v1" in text for text in approved_prompts)
                bundle = _bundle(state_dir, approved["data"]["bundle_id"])
                compiled_text = bundle["bundle_text"]
                compiled_hash = sha256(compiled_text)
                assert compiled_hash == bundle["bundle_sha256"]
                assert compiled_hash == approved["data"]["compiled_context_hash"]
                assert approved["data"]["lesson_versions"] == [
                    {"id": procedure_id, "version": 1}
                ]
                assert all(compiled_text in text for text in approved_prompts)
                supplies = _rows(learning_dir, "supplies")
                assert len(supplies) == 1
                supply = supplies[0]
                assert supply["procedure_id"] == procedure_id
                assert int(supply["version"]) == 1
                assert supply["supply_id"] in compiled_text
                assert all(supply["supply_id"] in text for text in approved_prompts)
                assert supply["worker_job_id"] == f"orch:{world.mission_id}:{approved['step_index']}"
                assert supply["compiled_context_hash"] == compiled_hash
                prompt_hash = sha256([approved_prompts[0], INSTRUCTION])
                assert supply["prompt_hash"] == prompt_hash == approved["data"]["prompt_hash"]
                assert len(supply["prompt_hash"]) == 64
                mission_key = bundle["work_key"]
                mission_workflow = _workflow(service, world.workspace_id, mission_key)
                manifest_candidate = mission_workflow["item"]["candidate"]["candidate_id"]
                assert bundle["candidate_id"] == str(manifest_candidate)
                assert _rows(learning_dir, "uses") == []
                assert _rows(learning_dir, "outcomes") == []

                reviewer = _receipt(
                    mission_workflow["item"]["work_id"],
                    mission_workflow["item"]["revision"]["revision_id"],
                    manifest_candidate,
                    "handoff",
                    body={"note": "reviewer linked the supplied lesson to this candidate"},
                    issuer="reviewer",
                )
                status, body = _command(
                    service,
                    world.workspace_id,
                    {"type": "append_evidence", "payload": {"receipt": reviewer}},
                )
                assert status == 200, body
                reviewer_id = body["result"]["receipt"]["receipt_id"]
                store = LearningStore(learning_dir)
                try:
                    use = record_use(
                        store,
                        client,
                        supply_id=supply["supply_id"],
                        candidate_id=manifest_candidate,
                        receipt_ids=(reviewer_id,),
                    )
                finally:
                    store.close()
                uses = _rows(learning_dir, "uses")
                assert len(uses) == 1
                assert uses[0]["use_id"] == use.use_id
                assert uses[0]["supply_id"] == supply["supply_id"]
                assert uses[0]["candidate_id"] == str(manifest_candidate)
                assert json.loads(uses[0]["receipt_ids_json"]) == [reviewer_id]
                assert _rows(learning_dir, "outcomes") == []
                assert len(_rows(learning_dir, "supplies")) == 1

                revised = {
                    "revision_id": str(uuid4()),
                    "work_id": source["work_id"],
                    "revision_number": 2,
                    "title": "Journey source",
                    "description": "revised",
                    "scope": "",
                    "acceptance_criteria": [],
                    "content_sha256": sha256(
                        {"title": "Journey source", "description": "revised"}
                    ),
                    "created_by": "owner",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
                status, body = _command(
                    service,
                    world.workspace_id,
                    {
                        "type": "revise_work",
                        "payload": {
                            "work_id": source["work_id"],
                            "expected_revision_id": source["revision_id"],
                            "revision": revised,
                        },
                    },
                )
                assert status == 200 and body["result"]["changed"] is True, body
                cleared = _workflow(service, world.workspace_id, source["key"])
                assert cleared["item"]["candidate"] is None
                assert _procedure(learning_dir)["status"] == "active"

                _prepare_mission(world, service, fresh=True)
                sweeps_before = _sweep_calls(commands)
                recorded_before = len(commands)

                worker.tick()
                assert _procedure(learning_dir)["status"] == "suspended"
                assert _sweep_calls(commands) > sweeps_before
                sweep_results = [
                    result.returncode
                    for argv, _kwargs, result in commands[recorded_before:]
                    if "sweep" in argv
                ]
                assert sweep_results and sweep_results[-1] == 0, [
                    (argv, result.returncode, result.stderr[-500:])
                    for argv, _kwargs, result in commands
                    if "sweep" in argv
                ]

                def implemented(view) -> bool:
                    return any(
                        step.get("stage") == "implement" and step.get("kind") == "outcome"
                        for step in view["steps"]
                    )

                start = len(calls)
                tick_until(worker, world, implemented, limit=40)
                excluded, excluded_prompts = _outcome(world), _worker_prompts(calls[start:])
                _release_successor(world)
                _release_grant(service, world)
                _succeeded(excluded, excluded_prompts, commands)
                assert all(LESSON_TITLE not in text for text in excluded_prompts)
                assert all(procedure_id not in text for text in excluded_prompts)
                assert len(_rows(learning_dir, "supplies")) == 1
                assert len(_rows(learning_dir, "uses")) == 1
                assert _rows(learning_dir, "outcomes") == []
                assert _procedure(learning_dir)["status"] == "suspended"

                _use_config(world, None, learning_config)
                restarted = open_worker(world)
                restarted_outcome, restarted_prompts = _launch(
                    restarted, world, service, calls, fresh=True
                )
                _succeeded(restarted_outcome, restarted_prompts, commands)
                assert all(LESSON_TITLE not in text for text in restarted_prompts)
                assert all(procedure_id not in text for text in restarted_prompts)
                assert len(_rows(learning_dir, "supplies")) == 1
                assert len(_rows(learning_dir, "uses")) == 1
                assert _rows(learning_dir, "outcomes") == []
                assert _procedure(learning_dir)["status"] == "suspended"
            finally:
                release.set()

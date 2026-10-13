"""Real context CLI compiles workflow and approved lessons before sandbox launch."""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
import sqlite3
import sys

import pytest

from omp_work.knowledge_source import normalize_remote_url
from omp_work.orchestrator import operations
from omp_work.orchestrator.service import OrchestratorError, load_config
from test_orchestrator_worker_wiring import wiring, run

pytest_plugins = ("test_workflow_service",)
pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="PostgreSQL integration required",
)


@pytest.fixture
def token_counter(tmp_path):
    counter = tmp_path / "counter.py"
    counter.write_text('''import json
import sys
encoding = sys.argv[3]
print(json.dumps({"api": "countTokens", "encoding": encoding}), flush=True)
for line in sys.stdin:
    item = json.loads(line)
    print(json.dumps({"id": item["id"], "tokens": len(item["text"].split())}), flush=True)
''')
    return counter


@pytest.fixture
def context(wiring, tmp_path, monkeypatch, token_counter):
    knowledge = Path(__file__).resolve().parents[2] / "omp-knowledge" / "src"
    monkeypatch.syspath_prepend(str(knowledge))
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join((str(knowledge), str(Path(__file__).resolve().parents[1] / "src"))))
    for key in list(os.environ):
        if key.startswith("OMP_KNOWLEDGE_"):
            monkeypatch.delenv(key)
    settings = tmp_path / "context.json"
    state_dir = tmp_path / "context-state"
    command = (sys.executable, "-m", "omp_knowledge.context")
    settings.write_text(json.dumps({
        "command": list(command), "token_cmd": [sys.executable, str(token_counter)],
        "state_dir": str(state_dir), "encoding": "test-words", "token_budget": 100000,
    }))
    config = dataclasses.replace(wiring.world.config, context_command=command, context_settings_path=settings)
    wiring.worker.handlers["*"].config = config
    from omp_work.orchestrator import lesson_context
    original = lesson_context.subprocess.run
    wiring.compiles = []
    def record_compile(*args, **kwargs):
        result = original(*args, **kwargs)
        if "omp_knowledge.context" in args[0]:
            wiring.compiles.append((args[0], kwargs, result))
        return result
    monkeypatch.setattr(lesson_context.subprocess, "run", record_compile)
    wiring.settings, wiring.state_dir = settings, state_dir
    return wiring


def enable_learning(state, tmp_path):
    from omp_knowledge.learning.store import LearningStore
    from omp_knowledge.learning.promotion import record_journey_pass, request_promotion, answer_promotion

    learning_dir = tmp_path / "learning"
    store = LearningStore(learning_dir)
    with store.transaction() as conn:
        conn.execute("INSERT INTO procedures VALUES (?, ?, ?, ?, ?, ?, ?)",
                     ("context-lesson", "fp-context", "active", 1, "Verified worker lesson", "now", "now"))
        conn.execute(
            "INSERT INTO procedure_versions (procedure_id, version, title, steps_json, preconditions_json, "
            "model, profile, source_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("context-lesson", 1, "Verified worker lesson", '["Inspect actual worktree"]', "[]", "m", "p", "{}", "now"),
        )
    record_journey_pass(store, workspace_id=state.world.workspace_id, work_id=None)
    decision = request_promotion(store, workspace_id=state.world.workspace_id, project_id=state.world.project_id)
    answer_promotion(store, decision["decision_id"], approve=True, answer_ref="owner-test-approval")
    store.close()
    bearer = tmp_path / "learning-bearer"
    bearer.write_text("learning-test-token")
    handler = state.worker.handlers["*"]
    handler.config = dataclasses.replace(handler.config, learning_state_dir=learning_dir,
        learning_command=(sys.executable, "-m", "omp_knowledge.learning"), learning_bearer_file=bearer)
    return learning_dir


@pytest.mark.parametrize("exit_code", [0, 1, None])
def test_compiled_context_and_supply_survive_worker_outcomes(context, tmp_path, exit_code):
    state = context
    learning_dir = enable_learning(state, tmp_path)
    state.exit_code = exit_code
    outcome = run(state)
    assert outcome.outcome == ("succeeded" if exit_code == 0 else "crashed"), [outcome.data, state.compiles[0][2].stderr]
    if exit_code is None:
        assert outcome.data["code"] == "lease_dead"
    ctx = state.contexts[0]
    manifest = state.runs[0][3]
    _, invocation, _ = state.compiles[0]
    body = json.loads(invocation["input"])
    assert invocation["cwd"] == Path(outcome.data["worktree"])
    assert body["cwd"] == outcome.data["worktree"]
    assert body["stage"] == "implement"
    assert body["attempt_id"] == str(ctx.lease.job_id)
    repository = normalize_remote_url(str(state.world.remote_repo))
    assert f'{repository}\n{ctx.request["base_commit"]}\nHEAD\nclean' in manifest["context"]
    assert "Verified worker lesson" in manifest["context"]
    assert "Inspect actual worktree" in manifest["context"]
    with sqlite3.connect(state.state_dir / "context-bundles.sqlite") as conn:
        conn.row_factory = sqlite3.Row
        bundle = conn.execute("SELECT * FROM context_bundles WHERE bundle_id=?", (outcome.data["bundle_id"],)).fetchone()
    assert bundle["bundle_text"] == manifest["context"]
    assert bundle["bundle_sha256"] == outcome.data["bundle_sha256"]
    assert bundle["work_id"] == str(ctx.work_id)
    assert bundle["work_key"] == operations._work_key(ctx)
    assert bundle["stage"] == "implement"
    assert bundle["attempt_id"] == str(ctx.lease.job_id)
    workflow = ctx.service.read(ctx.principal, ctx.workspace_id, "workflow", operations._work_key(ctx))
    # Publication receipts have no independence claim in direct store reads.
    # The compiler receives absence, which its typed receipt defaults to false.
    assert any(receipt["independent"] is None for receipt in workflow["receipts"])
    source_receipts = {str(receipt["receipt_id"]): receipt for receipt in workflow["receipts"]}
    for receipt in body["workflow"]["receipts"]:
        source = source_receipts[receipt["receipt_id"]]
        if source["independent"] is None:
            assert "independent" not in receipt
        else:
            assert receipt["independent"] is source["independent"]
    assert manifest["candidate_id"] == bundle["candidate_id"] == str(workflow["item"]["candidate"]["candidate_id"])
    with sqlite3.connect(learning_dir / "learning.sqlite") as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM supplies").fetchall()
    assert [row["supply_id"] for row in rows] == outcome.data["lesson_supply_ids"]
    assert rows[0]["procedure_id"] == "context-lesson"
    assert rows[0]["work_key"] == operations._work_key(ctx)
    assert json.loads(rows[0]["context_json"]) == {"project_id": str(ctx.project_id), "repository": repository}
    assert rows[0]["supply_id"] in manifest["context"]
    recorded = next(step for step in state.worker.handlers["*"].steps()
                    if step["kind"] == "outcome" and step["stage"] == "implement")
    assert recorded["data"]["bundle_id"] == bundle["bundle_id"]
    assert recorded["data"]["lesson_supply_ids"] == outcome.data["lesson_supply_ids"]


def test_disabled_learning_has_no_lesson(context, tmp_path):
    learning_dir = enable_learning(context, tmp_path)
    handler = context.worker.handlers["*"]
    handler.config = dataclasses.replace(handler.config, learning_state_dir=None, learning_command=(), learning_bearer_file=None)
    outcome = run(context)
    assert outcome.outcome == "succeeded", [outcome.data, context.compiles[0][2].stderr]
    assert "Verified worker lesson" not in context.runs[0][3]["context"]
    assert "lesson_supply_ids" not in outcome.data
    with sqlite3.connect(learning_dir / "learning.sqlite") as conn:
        assert conn.execute("SELECT count(*) FROM supplies").fetchone()[0] == 0


@pytest.mark.parametrize("failure", ["settings", "budget", "output"])
def test_compile_errors_fail_before_launch(context, monkeypatch, failure):
    if failure == "settings":
        context.settings.write_text("{}")
    elif failure == "budget":
        settings = json.loads(context.settings.read_text())
        settings["token_budget"] = 1
        context.settings.write_text(json.dumps(settings))
    else:
        # Real compiler still runs; transport corruption must reject the entire stdout.
        from omp_work.orchestrator import lesson_context
        original = lesson_context.subprocess.run
        def corrupt(*args, **kwargs):
            result = original(*args, **kwargs)
            if "omp_knowledge.context" in args[0]:
                result.stdout += '\n{"extra": true}\n'
            return result
        monkeypatch.setattr(lesson_context.subprocess, "run", corrupt)
    outcome = run(context)
    assert outcome.outcome == "failed"
    assert outcome.data["code"] == "context_compile_failed"
    assert context.runs == []
    assert context.posts == []


def test_config_loads_commands_and_rejects_partial_learning(context, tmp_path):
    config = context.worker.handlers["*"].config
    keys = ("workspace_id", "automation_capability_path", "qualification_path", "lock_map_path",
            "verifier_key_path", "allowed_signers", "control_repo", "worktrees_dir", "live_checkout")
    doc = {key: str(getattr(config, key)) for key in keys}
    path = tmp_path / "orchestrator.json"
    path.write_text(json.dumps(doc))
    assert load_config(path).context_command == ()
    doc.update(context_command=list(config.context_command), context_settings_path=str(config.context_settings_path))
    path.write_text(json.dumps(doc))
    assert load_config(path).context_command == config.context_command
    doc["learning_state_dir"] = str(tmp_path / "learning")
    path.write_text(json.dumps(doc))
    with pytest.raises(OrchestratorError, match="invalid_request"):
        load_config(path)
    doc.update(learning_command=[sys.executable, "-m", "omp_knowledge.learning"], learning_bearer_file=str(tmp_path / "bearer"))
    path.write_text(json.dumps(doc))
    assert load_config(path).learning_command == tuple(doc["learning_command"])

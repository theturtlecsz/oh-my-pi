"""Compiler-output validation and lesson delivery through the context CLI (OMP-607)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from omp_work.orchestrator import lesson_context
from omp_work.v1.canonical import sha256
from omp_work.v1.service import WorkError

TEXT = "PROCEDURE lesson-a@v2 do the thing"
FAKE = """\
import json, sys
from pathlib import Path
spec = json.loads(Path(sys.argv[1]).read_text())
with Path(sys.argv[1]).with_suffix(".log").open("a") as log:
    log.write(sys.argv[2] + "\\n")
if sys.argv[2] == "deliver":
    sys.stdin.read()
    sys.exit(spec.get("deliver_exit", 0))
sys.stdout.write(spec["stdout"])
sys.exit(spec.get("exit", 0))
"""


def supply(sid: str) -> dict:
    return {
        "supply_id": sid, "procedure_id": "lesson-a", "version": 2, "workspace_id": "ws",
        "work_key": "wk", "context": {}, "supplied_at": "2026-01-01T00:00:00+00:00",
    }


def compiled_doc(**over) -> dict:
    doc = {
        "text": TEXT, "bundle_id": "b1", "bundle_sha256": sha256(TEXT), "stage": "implement",
        "lesson_supply_ids": ["s1", "s2"], "lesson_supplies": [supply("s1"), supply("s2")],
    }
    doc.update(over)
    return {key: value for key, value in doc.items() if value is not None}


def make_ctx(tmp_path: Path, spec: dict, learning: Path | None = None):
    script = tmp_path / "fake_cli.py"
    script.write_text(FAKE)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec))
    workflow = {"item": {"candidate": {"candidate_id": "cand"}}, "receipts": []}
    return SimpleNamespace(
        config=SimpleNamespace(
            context_command=(sys.executable, str(script), str(spec_path)),
            context_settings_path=tmp_path / "settings.json",
            learning_state_dir=learning or tmp_path / "learning",
        ),
        service=SimpleNamespace(read=lambda *_a: workflow),
        principal=None, workspace_id="ws", lease=SimpleNamespace(job_id=uuid4()),
    ), spec_path.with_suffix(".log")


def compile_with(tmp_path: Path, doc: object, **spec):
    ctx, log = make_ctx(tmp_path, {"stdout": json.dumps(doc), **spec})
    return lesson_context.compile_context(ctx, tmp_path, "wk")


def event(compiled: dict, **over) -> str:
    line = {"type": "context-delivery", "compiled_context_hash": sha256(compiled["text"]), "prompt_hash": "a" * 64}
    line.update(over)
    return json.dumps(line)


@pytest.mark.parametrize("doc", [
    compiled_doc(text=TEXT + " tampered"),
    compiled_doc(bundle_sha256="0" * 64),
    compiled_doc(error="boom"),
    compiled_doc(lesson_supplies=[supply("s1")]),
    compiled_doc(lesson_supplies=[supply("s1"), supply("x")]),
    compiled_doc(lesson_supply_ids=["s1", "s1"], lesson_supplies=[supply("s1"), supply("s1")]),
    compiled_doc(lesson_supplies=[supply("s1"), "s2"]),
    compiled_doc(lesson_supplies=[supply("s1"), {"procedure_id": "p"}]),
    compiled_doc(lesson_supplies={"s1": supply("s1")}),
    compiled_doc(lesson_supplies=None),
    compiled_doc(lesson_supply_ids=None),
    compiled_doc() | {"lesson_supplies": None},
    compiled_doc() | {"lesson_supply_ids": None},
    compiled_doc() | {"lesson_supplies": None, "lesson_supply_ids": None},
    compiled_doc(lesson_supply_ids=None, lesson_supplies=None) | {"lesson_supplies": None},
    compiled_doc(lesson_supply_ids=None, lesson_supplies=None) | {"lesson_supply_ids": None},
], ids=["text", "hash", "error", "missing", "extra", "duplicate", "non-object", "no-id", "not-list",
        "ids-only", "supplies-only", "null-supplies", "null-ids", "both-null",
        "null-supplies-only", "null-ids-only"])
def test_compile_refuses_inconsistent_output(tmp_path, doc):
    ctx, log = make_ctx(tmp_path, {"stdout": json.dumps(doc)})
    data: dict = {}
    with pytest.raises(WorkError) as raised:
        _, compiled = lesson_context.compile_context(ctx, tmp_path, "wk")
        lesson_context.record_delivery(ctx, compiled, event(compiled), data)
    assert raised.value.code == "context_compile_failed"
    assert log.read_text() == "stage\n"
    assert not ctx.config.learning_state_dir.exists()
    assert "prompt_hash" not in data


@pytest.mark.parametrize("field", list(supply("s1")))
def test_compile_refuses_supply_missing_required_field(tmp_path, field):
    record = supply("s1")
    del record[field]
    test_compile_refuses_inconsistent_output(tmp_path, compiled_doc(lesson_supplies=[record, supply("s2")]))


@pytest.mark.parametrize(("field", "value"), [
    ("supply_id", 1),
    ("procedure_id", []),
    ("version", "2"),
    ("version", True),
    ("workspace_id", None),
    ("work_key", {}),
    ("context", []),
    ("supplied_at", 123),
    ("worker_job_id", []),
    ("compiled_context_hash", 123),
    ("prompt_hash", False),
    ("unknown", "value"),
])
def test_compile_refuses_supply_with_invalid_field(tmp_path, field, value):
    record = supply("s1") | {field: value}
    test_compile_refuses_inconsistent_output(tmp_path, compiled_doc(lesson_supplies=[record, supply("s2")]))


def test_compile_refuses_nonzero_exit(tmp_path):
    with pytest.raises(WorkError) as raised:
        compile_with(tmp_path, compiled_doc(), exit=2)
    assert raised.value.code == "context_compile_failed"


def test_compile_without_lesson_fields_means_no_lessons(tmp_path):
    candidate, compiled = compile_with(tmp_path, compiled_doc(lesson_supply_ids=None, lesson_supplies=None))
    assert candidate == "cand"
    assert "lesson_versions" in compiled and "lesson_supplies" not in compiled


@pytest.mark.parametrize("output", [
    "",
    "noise\n{\"type\":\"other\"}\n",
    event(compiled_doc(), compiled_context_hash="b" * 64) + "\n",
    event(compiled_doc(), prompt_hash="a" * 63) + "\n",
    event(compiled_doc(), prompt_hash="g" * 64) + "\n",
    event(compiled_doc(), prompt_hash="A" * 64) + "\n",
    event(compiled_doc(), prompt_hash=None) + "\n",
], ids=["empty", "absent", "wrong-context-hash", "short", "nonhex", "uppercase", "null"])
def test_refused_evidence_writes_no_supply(tmp_path, output):
    ctx, log = make_ctx(tmp_path, {})
    data: dict = {}
    with pytest.raises(WorkError) as raised:
        lesson_context.record_delivery(ctx, compiled_doc(), output, data)
    assert raised.value.code == "context_delivery_failed"
    assert not log.exists()
    assert "prompt_hash" not in data


def test_deliver_failure_leaves_prompt_hash_unset(tmp_path):
    ctx, log = make_ctx(tmp_path, {"deliver_exit": 2})
    data: dict = {}
    with pytest.raises(WorkError) as raised:
        lesson_context.record_delivery(ctx, compiled_doc(), event(compiled_doc()), data)
    assert raised.value.code == "context_delivery_failed"
    assert log.read_text() == "deliver\n"
    assert "prompt_hash" not in data


def test_missing_command_is_delivery_failure(tmp_path):
    ctx, _ = make_ctx(tmp_path, {})
    ctx.config.context_command = (str(tmp_path / "absent"),)
    data: dict = {}
    with pytest.raises(WorkError) as raised:
        lesson_context.record_delivery(ctx, compiled_doc(), event(compiled_doc()), data)
    assert raised.value.code == "context_delivery_failed"
    assert "prompt_hash" not in data


def test_delivery_through_real_cli_records_supply_only(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(root / "omp-knowledge" / "src"))
    from omp_knowledge.learning.store import LearningStore

    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(
        (str(root / "omp-knowledge" / "src"), str(root / "omp-work" / "src"))))
    learning = tmp_path / "learning"
    ctx, _ = make_ctx(tmp_path, {}, learning)
    ctx.config.context_command = (sys.executable, "-m", "omp_knowledge.context")
    _, compiled = compile_with(tmp_path, compiled_doc())
    noisy = f"booting\n{{not json\n{json.dumps({'type': 'other'})}\n{event(compiled, prompt_hash='c' * 64)}\nbye\n"
    data: dict = {}
    lesson_context.record_delivery(ctx, compiled, noisy, data)
    assert data["prompt_hash"] == "c" * 64
    store = LearningStore(learning)
    try:
        rows = store.execute(
            "SELECT supply_id, worker_job_id, compiled_context_hash, prompt_hash FROM supplies ORDER BY supply_id"
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            (sid, str(ctx.lease.job_id), sha256(TEXT), "c" * 64) for sid in ("s1", "s2")
        ]
        assert store.execute("SELECT COUNT(*) FROM uses").fetchone()[0] == 0
        assert store.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 0
    finally:
        store.close()

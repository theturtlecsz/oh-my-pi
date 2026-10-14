"""Generated lesson tests exercise live admission and catch unresolved citations."""

import os
from pathlib import Path
import subprocess
import sys
import textwrap

from omp_work.research.lessons import LessonDraft, stage


def test_generated_regression_detects_unresolved_citation_bug(tmp_path: Path) -> None:
    claim = "Measured behaviour meets threshold"
    draft = LessonDraft(
        lesson_id="metric-lesson",
        project_id="project-1",
        kind="scientific",
        title="Measured behaviour",
        claim=claim,
        receipt_ids=["receipt-1"],
        binding={
            "source_span": f"1:{1 + len(claim)}",
            "revision": "revision-1",
            "work_item": "work-item-1",
            "candidate": "candidate-1",
            "evaluator": "evaluator-1",
        },
        outcome="PASS",
        behaviour={"metric": "metric", "op": "ge", "value": 0.7},
    )
    receipt = {
        "claim": draft.claim,
        "binding": dict(draft.binding),
        "outcome": draft.outcome,
        "project_id": draft.project_id,
        "metrics": {"metric": 0.7},
    }
    record = stage(draft, tmp_path / "lessons", resolve={"receipt-1": receipt}.get)
    assert record["status"] == "staged"
    generated = tmp_path / "lessons" / draft.lesson_id / "regression_test.py"
    cwd = tmp_path / "child-cwd"
    cwd.mkdir()
    env = {key: value for key, value in os.environ.items() if key != "PYTEST_ADDOPTS"}
    argv = [
        sys.executable, "-m", "pytest", "-q", "-p", "no:xdist",
        "--basetemp", str(tmp_path / "child-basetemp"), str(generated),
    ]
    clean = subprocess.run(
        argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=60,
    )
    assert clean.returncode == 0, clean.stdout + clean.stderr

    conftest = generated.with_name("conftest.py")
    conftest.write_text(
        textwrap.dedent("""\
            from omp_work.research import lessons

            original_stage = lessons.stage

            def stage_with_unresolved_citation_bug(draft, directory, *, resolve, **kwargs):
                def resolve_with_fallback(receipt_id):
                    receipt = resolve(receipt_id)
                    if receipt is None:
                        return resolve(draft.receipt_ids[0])
                    return receipt

                return original_stage(draft, directory, resolve=resolve_with_fallback, **kwargs)

            lessons.stage = stage_with_unresolved_citation_bug
            """),
        encoding="utf-8",
    )
    try:
        broken = subprocess.run(
            argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=60,
        )
    finally:
        conftest.unlink()
    output = broken.stdout + broken.stderr
    assert broken.returncode != 0, output
    assert "FAILED" in output and "test_unresolved_citation_is_refused" in output, output

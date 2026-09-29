"""Stamped plan files and sealed paths are real before stamp_execution_plan.

A stamp reads its plan_file and records each paths entry. Every fixture
scenario must write that plan file and each path in an earlier write step,
or the path must already exist relative to the repository root.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
FIXTURES = REPO / "evals" / "harbor" / "fixtures"


def _exists(target: str) -> bool:
    if target.startswith("/") or ".." in Path(target).parts:
        return False
    candidate = (REPO / target).resolve()
    try:
        candidate.relative_to(REPO.resolve())
    except ValueError:
        return False
    return candidate.is_file()


def _calls(step: object) -> list[dict]:
    if not isinstance(step, dict):
        return []
    calls = step.get("tool_calls")
    if not isinstance(calls, list):
        return []
    return [call for call in calls if isinstance(call, dict)]


def _arguments(call: dict) -> dict:
    arguments = call.get("arguments")
    return arguments if isinstance(arguments, dict) else {}


def test_stamped_files_are_written_or_exist() -> None:
    scenarios = sorted(FIXTURES.glob("*/scenario.json"))
    assert {path.parent.name for path in scenarios} >= {"f1", "f2"}

    for scenario_path in scenarios:
        document = json.loads(scenario_path.read_text(encoding="utf-8"))
        written: set[str] = set()
        for step in document["model_script"]:
            for call in _calls(step):
                arguments = _arguments(call)
                if call.get("name") == "stamp_execution_plan":
                    targets = [arguments["plan_file"], *arguments.get("paths", [])]
                    for target in targets:
                        assert isinstance(target, str) and target
                        assert target in written or _exists(target), (
                            f"{scenario_path.parent.name}: {target} is not written "
                            "before stamp_execution_plan and does not exist at the repo root"
                        )
                elif call.get("name") == "write":
                    path = arguments.get("path")
                    if isinstance(path, str) and path:
                        written.add(path)

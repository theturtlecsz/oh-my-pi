"""Load ``fixtures/<id>/fixture.json`` and its scenario file."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FILE_RE = _ID_RE
_EXECUTION_REF_RE = re.compile(r"^refs/heads/execution/([a-z0-9]+-[0-9]+)-[0-9a-f]+$")

_FIXTURE_KEYS = {
    "id",
    "scored_experiment",
    "seed_patch",
    "solution_patch",
    "independent_tests",
    "scenario",
    "rules",
}
_TEST_KEYS = {"runner", "target", "requires", "files"}
_SCENARIO_KEYS = {"command", "terminal", "model_script", "ui_script", "kill_at", "timeout_s"}
_TERMINAL_KEYS = {"pointer", "in"}
_RULE_KEYS = {
    "readback_equals": {"type", "pointer", "value"},
    "transcript_count": {"type", "count"},
    "outcome_is": {"type", "outcome"},
    "independent_tests_pass": {"type"},
}


class IndependentTest:
    def __init__(self, runner: str, target: str, requires: tuple[str, ...] = (), files: tuple[str, ...] = ()) -> None:
        self.runner = runner
        self.target = target
        self.requires = requires
        self.files = files

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, IndependentTest)
            and self.runner == other.runner
            and self.target == other.target
            and self.requires == other.requires
            and self.files == other.files
        )

    def __repr__(self) -> str:
        return (
            f"IndependentTest(runner={self.runner!r}, target={self.target!r}, "
            f"requires={self.requires!r}, files={self.files!r})"
        )


class Terminal:
    """``accepted`` is the scenario JSON key ``in``."""

    def __init__(
        self,
        pointer: str = "",
        accepted: tuple[Any, ...] = (),
        *,
        agent_end: bool = False,
    ) -> None:
        self.pointer = pointer
        self.accepted = accepted
        self.agent_end = agent_end

    @property
    def in_(self) -> tuple[Any, ...]:
        return self.accepted


class KillAt:
    """Subset of an outbound RPC frame.

    ``boundary`` is ``None`` when a match ends the RPC process immediately.
    ``enqueue`` arms on that frame and ends the process only after the
    continuation has been queued.
    """

    def __init__(self, match: dict[str, Any], boundary: str | None = None) -> None:
        self.match = match
        self.boundary = boundary


class Scenario:
    def __init__(
        self,
        command: str,
        terminal: Terminal,
        model_script: tuple[Any, ...],
        ui_script: tuple[Any, ...],
        kill_at: KillAt | None,
        timeout_s: int | float | None,
        filename: str,
    ) -> None:
        self.command = command
        self.terminal = terminal
        self.model_script = model_script
        self.ui_script = ui_script
        self.kill_at = kill_at
        self.timeout_s = timeout_s
        self.filename = filename


class Fixture:
    def __init__(
        self,
        fixture_id: str,
        scored_experiment: str,
        seed_patch: str,
        solution_patch: str,
        independent_tests: tuple[IndependentTest, ...],
        scenario: Scenario,
        rules: tuple[dict[str, Any], ...],
        digest: str,
        directory: Path,
    ) -> None:
        self.id = fixture_id
        self.scored_experiment = scored_experiment
        self.seed_patch = seed_patch
        self.solution_patch = solution_patch
        self.independent_tests = independent_tests
        self.scenario = scenario
        self.rules = rules
        self.digest = digest
        self.directory = directory


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {path.name}: {exc}") from exc


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _exact_keys(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    missing = sorted(allowed - set(value))
    if unknown or missing:
        raise ValueError(f"{label} keys must be {sorted(allowed)}, got {sorted(value)}")


def _string(value: object, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (value == "" and not empty):
        raise ValueError(f"{label} must be a {'string' if empty else 'non-empty string'}")
    return value


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or item == "" for item in value):
        raise ValueError(f"{label} must be a list of non-empty strings")
    return tuple(value)


def _scenario_name(value: object) -> str:
    if not isinstance(value, str) or _FILE_RE.fullmatch(value) is None or value == "fixture.json":
        raise ValueError("scenario must name a file in the fixture directory")
    return value


def _load_test(raw: object, index: int) -> IndependentTest:
    document = _mapping(raw, f"independent_tests[{index}]")
    _exact_keys_optional(document, _TEST_KEYS, {"runner", "target"}, f"independent_tests[{index}]")
    return IndependentTest(
        runner=_string(document["runner"], f"independent_tests[{index}].runner"),
        target=_string(document["target"], f"independent_tests[{index}].target"),
        requires=_string_tuple(document["requires"], f"independent_tests[{index}].requires")
        if "requires" in document
        else (),
        files=_string_tuple(document["files"], f"independent_tests[{index}].files") if "files" in document else (),
    )


def _exact_keys_optional(value: Mapping[str, Any], allowed: set[str], required: set[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    missing = sorted(required - set(value))
    if unknown or missing:
        raise ValueError(f"{label} has missing {missing} or unknown {unknown}")


def _json_pointer(value: object, label: str) -> str:
    if not isinstance(value, str) or (value != "" and not value.startswith("/")):
        raise ValueError(f"{label} must be a JSON pointer")
    return value


def _load_rule(raw: object, index: int) -> dict[str, Any]:
    document = _mapping(raw, f"rules[{index}]")
    rule_type = document.get("type")
    if not isinstance(rule_type, str) or rule_type not in _RULE_KEYS:
        raise ValueError(f"rules[{index}] has unknown type {rule_type!r}")
    _exact_keys(document, _RULE_KEYS[rule_type], f"rules[{index}]")
    if rule_type == "readback_equals":
        _json_pointer(document["pointer"], f"rules[{index}].pointer")
    elif rule_type == "transcript_count":
        count = document["count"]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(f"rules[{index}].count must be a non-negative integer")
    elif rule_type == "outcome_is":
        _string(document["outcome"], f"rules[{index}].outcome")
    return json.loads(json.dumps(document))


def parse_terminal(raw: object, label: str = "scenario.terminal") -> Terminal:
    document = _mapping(raw, label)
    if "agent_end" in document:
        _exact_keys(document, {"agent_end"}, label)
        if document["agent_end"] is not True:
            raise ValueError(f"{label}.agent_end must be true")
        return Terminal(agent_end=True)
    _exact_keys(document, _TERMINAL_KEYS, label)
    accepted = document["in"]
    if not isinstance(accepted, list) or accepted == []:
        raise ValueError(f"{label}.in must be a non-empty list")
    return Terminal(
        pointer=_json_pointer(document["pointer"], f"{label}.pointer"),
        accepted=tuple(accepted),
    )


def _load_scenario(document: dict[str, Any], filename: str) -> Scenario:
    _exact_keys_optional(document, _SCENARIO_KEYS, {"command", "terminal", "model_script", "ui_script"}, "scenario")
    terminal = parse_terminal(document["terminal"], "scenario.terminal")
    model_script = document["model_script"]
    ui_script = document["ui_script"]
    if not isinstance(model_script, list) or not isinstance(ui_script, list):
        raise ValueError("scenario model_script and ui_script must be lists")
    kill_at: KillAt | None = None
    if "kill_at" in document and document["kill_at"] is not None:
        raw_kill = _mapping(document["kill_at"], "scenario.kill_at")
        _exact_keys_optional(raw_kill, {"match", "boundary"}, {"match"}, "scenario.kill_at")
        boundary: str | None = None
        if "boundary" in raw_kill:
            boundary = _string(raw_kill["boundary"], "scenario.kill_at.boundary")
            if boundary != "enqueue":
                raise ValueError('scenario.kill_at.boundary must be "enqueue"')
        kill_at = KillAt(_mapping(raw_kill["match"], "scenario.kill_at.match"), boundary)
    timeout_s: int | float | None = None
    if "timeout_s" in document and document["timeout_s"] is not None:
        raw_timeout = document["timeout_s"]
        if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (int, float)) or raw_timeout <= 0:
            raise ValueError("scenario.timeout_s must be a positive number")
        timeout_s = raw_timeout
    return Scenario(
        command=_string(document["command"], "scenario.command"),
        terminal=terminal,
        model_script=tuple(model_script),
        ui_script=tuple(ui_script),
        kill_at=kill_at,
        timeout_s=timeout_s,
        filename=filename,
    )


def execution_work_item_key(execution: Mapping[str, Any]) -> str | None:
    """Primary alias key carried by an execution view, or ``None`` when it carries none.

    ``GET /v1/work-items/{key}`` resolves only a primary alias, and the grant's
    ``remote_ref`` (``refs/heads/execution/<key>-<grant>``) is the view's only
    field that names one: the grant items carry ``work_id`` alone.
    """

    if not isinstance(execution, Mapping):
        return None
    grant = execution.get("grant")
    remote_ref = grant.get("remote_ref") if isinstance(grant, Mapping) else None
    if not isinstance(remote_ref, str):
        return None
    match = _EXECUTION_REF_RE.fullmatch(remote_ref)
    if match is None:
        return None
    return match.group(1).upper()


def fixture_digest(fixture_dir: str | Path) -> str:
    """sha256 over ``fixture.json`` and the scenario file it names.

    Each file contributes its name, a NUL, its length as 8 big-endian bytes,
    then its raw bytes, in that order: fixture file first, scenario file second.
    """

    directory = Path(fixture_dir)
    fixture_path = directory / "fixture.json"
    if not fixture_path.is_file():
        raise ValueError(f"missing fixture.json in {directory}")
    document = _mapping(_read_json(fixture_path), "fixture")
    if "scenario" not in document:
        raise ValueError("fixture.scenario must name a file in the fixture directory")
    scenario_name = _scenario_name(document["scenario"])
    scenario_path = directory / scenario_name
    if not scenario_path.is_file():
        raise ValueError(f"missing scenario file {scenario_name}")
    hasher = hashlib.sha256()
    for path in (fixture_path, scenario_path):
        data = path.read_bytes()
        hasher.update(path.name.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(len(data).to_bytes(8, "big"))
        hasher.update(data)
    return hasher.hexdigest()


def load_fixture(fixtures_root: str | Path, fixture_id: str) -> Fixture:
    """Load ``<fixtures_root>/<fixture_id>/fixture.json``."""

    if not isinstance(fixture_id, str) or _ID_RE.fullmatch(fixture_id) is None:
        raise ValueError(f"unsafe fixture id: {fixture_id!r}")
    directory = Path(fixtures_root) / fixture_id
    fixture_path = directory / "fixture.json"
    if not fixture_path.is_file():
        raise ValueError(f"missing fixture.json in {directory}")
    document = _mapping(_read_json(fixture_path), "fixture")
    _exact_keys(document, _FIXTURE_KEYS, "fixture")
    file_id = _string(document["id"], "fixture.id")
    if file_id != fixture_id:
        raise ValueError(f"fixture id {file_id!r} does not match directory {fixture_id!r}")
    tests_raw = document["independent_tests"]
    if not isinstance(tests_raw, list):
        raise ValueError("fixture.independent_tests must be a list")
    tests = tuple(_load_test(item, index) for index, item in enumerate(tests_raw))
    seen = {(item.runner, item.target) for item in tests}
    if len(seen) != len(tests):
        raise ValueError("fixture.independent_tests has a duplicate runner and target")
    rules_raw = document["rules"]
    if not isinstance(rules_raw, list):
        raise ValueError("fixture.rules must be a list")
    rules = tuple(_load_rule(item, index) for index, item in enumerate(rules_raw))
    scenario_name = _scenario_name(document["scenario"])
    scenario_path = directory / scenario_name
    if not scenario_path.is_file():
        raise ValueError(f"missing scenario file {scenario_name}")
    scenario = _load_scenario(_mapping(_read_json(scenario_path), "scenario"), scenario_name)
    return Fixture(
        fixture_id=file_id,
        scored_experiment=_string(document["scored_experiment"], "fixture.scored_experiment"),
        seed_patch=_string(document["seed_patch"], "fixture.seed_patch", empty=True),
        solution_patch=_string(document["solution_patch"], "fixture.solution_patch", empty=True),
        independent_tests=tests,
        scenario=scenario,
        rules=rules,
        digest=fixture_digest(directory),
        directory=directory,
    )

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Literal

CLOSED_CHECKS: frozenset[str] = frozenset({"exact_text", "contains_all", "verdict_equals"})

KNOWN_ROLES: frozenset[str] = frozenset({
    "plan",
    "implement",
    "verify",
    "research",
    "intake_support_judge",
    "judge",
    "review",
    "audit",
    "operator_session",
    "operator",
})

ROLE_ALIASES: dict[str, str] = {
    "plan": "plan",
    "implement": "implement",
    "verify": "verify",
    "research": "research",
    "intake_support_judge": "intake_support_judge",
    "judge": "intake_support_judge",
    "review": "review",
    "audit": "review",
    "operator_session": "operator_session",
    "operator": "operator_session",
}

DEFAULT_TASK_SET_PATH = Path(__file__).with_name("qualification-tasks.v1.json")


def normalize_role(role: str) -> str:
    """Normalize role string to canonical form."""
    return ROLE_ALIASES.get(role, role)


class TaskSet:
    """Validated in-memory qualification task set."""

    def __init__(self, version: str, tasks: list[dict[str, Any]]) -> None:
        self.version = version
        self.tasks = tasks
        self.tasks_by_id: dict[str, dict[str, Any]] = {t["id"]: t for t in tasks}

    def tasks_for_role(self, role: str) -> list[dict[str, Any]]:
        norm = normalize_role(role)
        return [t for t in self.tasks if normalize_role(t["role"]) == norm]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "tasks": self.tasks,
        }

    def __getitem__(self, task_id: str) -> dict[str, Any]:
        return self.tasks_by_id[task_id]

    def __contains__(self, task_id: str) -> bool:
        return task_id in self.tasks_by_id

    def __iter__(self):
        return iter(self.tasks)

    def __len__(self) -> int:
        return len(self.tasks)


class QualificationResult:
    """Outcome and recorded evidence of a role qualification run."""

    def __init__(
        self,
        digest: str,
        passed_ids: list[str],
        failed_ids: list[str],
        outcome: Literal["pass", "fail"],
        role: str,
        caller: str | None = None,
        model: str | None = None,
        provider: str | None = None,
        rubric_evidence: dict[str, Any] | None = None,
        candidate_outputs: dict[str, Any] | None = None,
        recorded_at: str | None = None,
    ) -> None:
        self.digest = digest
        self.passed_ids = passed_ids
        self.failed_ids = failed_ids
        self.outcome = outcome
        self.role = role
        self.caller = caller
        self.model = model
        self.provider = provider
        self.rubric_evidence = rubric_evidence or {}
        self.candidate_outputs = candidate_outputs or {}
        self.recorded_at = recorded_at

    @property
    def passed(self) -> bool:
        return self.outcome == "pass"

    def __iter__(self):
        return iter((self.digest, self.passed_ids, self.failed_ids, self.outcome))

    def __getitem__(self, item: str) -> Any:
        if hasattr(self, item):
            return getattr(self, item)
        raise KeyError(item)

    def to_dict(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "role": self.role,
            "outcome": self.outcome,
            "passed_ids": list(self.passed_ids),
            "failed_ids": list(self.failed_ids),
            "caller": self.caller,
            "model": self.model,
            "provider": self.provider,
            "rubric_evidence": self.rubric_evidence,
            "recorded_at": self.recorded_at,
        }

    def __repr__(self) -> str:
        return (
            f"QualificationResult(digest={self.digest[:12]}..., role={self.role!r}, "
            f"outcome={self.outcome!r}, passed={len(self.passed_ids)}, failed={len(self.failed_ids)})"
        )


def _validate_task_set_dict(data: Any) -> TaskSet:
    if not isinstance(data, dict):
        raise ValueError("task set root must be a JSON object")

    version = data.get("version")
    if not version or not isinstance(version, str):
        raise ValueError("task set must contain a valid 'version' string")

    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        raise ValueError("task set must contain a 'tasks' list")
    if not tasks:
        raise ValueError("task set 'tasks' list cannot be empty")

    seen_ids: set[str] = set()
    for task in tasks:
        if not isinstance(task, dict):
            raise ValueError("each task in task set must be a JSON object")

        tid = task.get("id")
        if not tid or not isinstance(tid, str):
            raise ValueError("each task must have a non-empty string 'id'")
        if tid in seen_ids:
            raise ValueError(f"duplicate task id in task set: {tid}")
        seen_ids.add(tid)

        role = task.get("role")
        if not role or not isinstance(role, str):
            raise ValueError(f"task {tid} missing valid 'role'")
        if role not in KNOWN_ROLES:
            raise ValueError(f"unknown role: {role!r} in task {tid}")

        if "input" not in task:
            raise ValueError(f"task {tid} missing required 'input' field")

        checks = task.get("checks")
        if checks is None and "check" in task:
            single_check = task["check"]
            checks = [single_check] if isinstance(single_check, dict) else [{"check": single_check}]

        if not isinstance(checks, list) or len(checks) == 0:
            raise ValueError(f"task {tid} must define at least one check in 'checks'")

        for check in checks:
            if not isinstance(check, dict):
                raise ValueError(f"check in task {tid} must be an object")
            check_type = check.get("check") or check.get("type")
            if check_type not in CLOSED_CHECKS:
                raise ValueError(f"unknown check type: {check_type!r} in task {tid}")
            if check_type == "exact_text":
                if "expected" not in check and "expected_text" not in check:
                    raise ValueError(f"exact_text check in task {tid} missing 'expected'")
            elif check_type == "contains_all":
                expected = check.get("expected") or check.get("substrings") or check.get("values")
                if expected is None:
                    raise ValueError(f"contains_all check in task {tid} missing 'expected'")
            elif check_type == "verdict_equals":
                expected = check.get("expected") or check.get("verdict")
                if expected is None:
                    raise ValueError(f"verdict_equals check in task {tid} missing 'expected'")

    return TaskSet(version=version, tasks=tasks)


def load_task_set(path: str | Path | None = None) -> TaskSet:
    """Load and validate the qualification task set from disk or default package."""
    target_path = Path(path) if path is not None else DEFAULT_TASK_SET_PATH
    try:
        raw_text = target_path.read_text(encoding="utf-8")
        data = json.loads(raw_text)
    except Exception as exc:
        if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError):
            raise
        raise ValueError(f"failed to read task set from {target_path}: {exc}") from exc

    return _validate_task_set_dict(data)


def canonical_task_set_bytes(data: TaskSet | dict[str, Any]) -> bytes:
    """Produce deterministic, sorted canonical JSON bytes for a task set."""
    raw = data.to_dict() if isinstance(data, TaskSet) else data
    return json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def task_set_sha256(task_set: TaskSet | dict[str, Any] | Path | str | bytes | None = None) -> str:
    """Calculate canonical SHA256 digest of a qualification task set."""
    if task_set is None or isinstance(task_set, (str, Path)):
        ts = load_task_set(task_set)
        raw_bytes = canonical_task_set_bytes(ts)
    elif isinstance(task_set, TaskSet):
        raw_bytes = canonical_task_set_bytes(task_set)
    elif isinstance(task_set, dict):
        # Validate task set structure to ensure canonical representation
        ts = _validate_task_set_dict(task_set)
        raw_bytes = canonical_task_set_bytes(ts)
    elif isinstance(task_set, bytes):
        raw_bytes = task_set
    else:
        raise ValueError(f"unsupported task_set type for digest: {type(task_set)}")

    return hashlib.sha256(raw_bytes).hexdigest()


def _grade_single_check(check: dict[str, Any], output: Any) -> bool:
    check_type = check.get("check") or check.get("type")
    if check_type not in CLOSED_CHECKS:
        raise ValueError(f"unknown check type: {check_type!r}")

    if check_type == "exact_text":
        expected = check.get("expected")
        if expected is None:
            expected = check.get("expected_text", "")
        if not isinstance(output, (str, int, float)):
            return False
        return str(output).strip() == str(expected).strip()

    if check_type == "contains_all":
        expected = check.get("expected") or check.get("substrings") or check.get("values")
        if isinstance(expected, str):
            expected = [expected]
        if not isinstance(expected, (list, tuple, set)):
            return False
        out_str = str(output) if output is not None else ""
        case_sensitive = check.get("case_sensitive", True)
        if case_sensitive:
            return all(str(sub) in out_str for sub in expected)
        out_lower = out_str.lower()
        return all(str(sub).lower() in out_lower for sub in expected)

    if check_type == "verdict_equals":
        expected = check.get("expected") or check.get("verdict")
        if output is None or expected is None:
            return False
        if isinstance(output, dict) and "verdict" in output:
            val = output["verdict"]
        elif hasattr(output, "verdict"):
            val = output.verdict
        else:
            val = str(output)
        return str(val).strip().lower() == str(expected).strip().lower()

    return False


def grade(check_or_task: dict[str, Any], output: Any) -> bool:
    """Grade candidate output against a single check or all checks of a task.

    Returns True if all checks pass, False otherwise.
    """
    if not isinstance(check_or_task, dict):
        raise ValueError("check_or_task must be a dictionary")

    checks = check_or_task.get("checks")
    if checks is not None and isinstance(checks, list):
        if not checks:
            return False
        return all(_grade_single_check(c, output) for c in checks)

    return _grade_single_check(check_or_task, output)


def qualify(
    role: str,
    outputs: dict[str, Any],
    *,
    task_set: TaskSet | dict[str, Any] | Path | str | None = None,
    caller: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    rubric_evidence: dict[str, Any] | None = None,
) -> QualificationResult:
    """Qualify candidate model outputs against tasks for a specific role.

    Requires every task of the role to be present and pass; extra IDs or wrong
    answers cause failure.
    """
    if task_set is None or isinstance(task_set, (str, Path)):
        ts = load_task_set(task_set)
    elif isinstance(task_set, TaskSet):
        ts = task_set
    elif isinstance(task_set, dict):
        ts = _validate_task_set_dict(task_set)
    else:
        raise ValueError(f"unsupported task_set type: {type(task_set)}")

    norm_role = normalize_role(role)
    valid_roles = {normalize_role(r) for r in KNOWN_ROLES}
    if norm_role not in valid_roles:
        raise ValueError(f"unknown role: {role!r}")

    role_tasks = ts.tasks_for_role(role)
    if not role_tasks:
        raise ValueError(f"no tasks found in task set for role: {role!r}")

    role_task_ids = {t["id"] for t in role_tasks}
    provided_ids = set(outputs.keys())
    extra_ids = sorted(provided_ids - role_task_ids)

    passed_ids: list[str] = []
    failed_ids: list[str] = []

    for task in role_tasks:
        tid = task["id"]
        if tid not in outputs:
            failed_ids.append(tid)
        else:
            candidate_out = outputs[tid]
            if grade(task, candidate_out):
                passed_ids.append(tid)
            else:
                failed_ids.append(tid)

    # Reject extra task IDs by including them in failed_ids
    failed_ids.extend(extra_ids)

    outcome: Literal["pass", "fail"] = (
        "pass" if (not failed_ids and len(passed_ids) == len(role_tasks)) else "fail"
    )

    digest = task_set_sha256(ts)
    recorded_at = datetime.now(timezone.utc).isoformat()

    return QualificationResult(
        digest=digest,
        passed_ids=passed_ids,
        failed_ids=failed_ids,
        outcome=outcome,
        role=role,
        caller=caller,
        model=model,
        provider=provider,
        rubric_evidence=rubric_evidence or {},
        candidate_outputs=outputs,
        recorded_at=recorded_at,
    )

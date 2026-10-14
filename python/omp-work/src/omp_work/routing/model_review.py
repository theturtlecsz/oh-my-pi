from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from .policy import RoutingPolicy

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
DEFAULT_BASELINE_PATH = Path(__file__).with_name("model-review-baseline.v1.json")
# Canonical SHA256 of the frozen baseline snapshot. Changing the baseline file
# without a reviewed change to this pin fails the gate.
BASELINE_SHA256 = "fae318c62cdd2c44ea5b9fe78838d2592a1d0e2bd979ed57fa103a05a20e5cff"


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

    def candidate_input(self, task_id: str) -> Any:
        """Only this input view may be sent to a candidate; checks remain local."""
        return deepcopy(self.tasks_by_id[task_id]["input"])

    def to_dict(self) -> dict[str, Any]:
        return {"version": self.version, "tasks": self.tasks}

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


def _validate_check(check: Any) -> None:
    if not isinstance(check, dict):
        raise ValueError("check must be an object")
    kind = check.get("check")
    if not isinstance(kind, str) or kind not in CLOSED_CHECKS:
        raise ValueError(f"unknown check type: {kind!r}")
    if set(check) != {"check", "expected"}:
        raise ValueError("check must contain only 'check' and 'expected'")
    expected = check["expected"]
    if kind == "contains_all":
        if not isinstance(expected, list) or not expected or not all(
            isinstance(text, str) and text for text in expected
        ):
            raise ValueError("contains_all expected must be a non-empty list of non-empty strings")
    elif not isinstance(expected, str):
        raise ValueError(f"{kind} expected must be a string")
    elif kind == "verdict_equals" and expected not in {"supports", "contradicts", "neither"}:
        raise ValueError("verdict_equals expected must be a corpus verdict")


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
        if not isinstance(checks, list) or not checks:
            raise ValueError(f"task {tid} must define at least one check in 'checks'")
        for check in checks:
            _validate_check(check)


    return TaskSet(version=version, tasks=tasks)


def load_task_set(path: str | Path | None = None) -> TaskSet:
    """Load and validate the qualification task set from disk or default package."""
    target_path = Path(path) if path is not None else DEFAULT_TASK_SET_PATH
    try:
        raw_text = target_path.read_text(encoding="utf-8")
        data = json.loads(raw_text)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
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
        raw_bytes = canonical_task_set_bytes(_validate_task_set_dict(json.loads(task_set)))
    else:
        raise ValueError(f"unsupported task_set type for digest: {type(task_set)}")

    return hashlib.sha256(raw_bytes).hexdigest()


def _grade_single_check(check: dict[str, Any], output: Any) -> bool:
    kind, expected = check["check"], check["expected"]
    if kind == "exact_text":
        return isinstance(output, str) and output == expected
    if kind == "contains_all":
        return isinstance(output, str) and all(text in output for text in expected)
    verdict = output.get("verdict") if isinstance(output, dict) else getattr(output, "verdict", output)
    return isinstance(verdict, str) and verdict == expected


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
        for check in checks:
            _validate_check(check)
        return all(_grade_single_check(c, output) for c in checks)

    _validate_check(check_or_task)
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


# --- governing-policy review check --------------------------------------------------

RUBRIC_PATH = "docs/model-review.md"
SCHEDULE_KEYS: tuple[str, ...] = ("daily_check", "weekly_qualification", "urgent_review")
RUBRIC_AXES: tuple[str, ...] = ("capability", "cost", "quota", "latency", "role_fit")
LAST_REVIEW_OUTCOMES: frozenset[str] = frozenset({"baseline_retained", "pass", "fail"})


class PolicyReviewError(ValueError):
    """Governing policy holds a recipe that is neither baseline nor qualified."""

    def __init__(self, findings: list[str]) -> None:
        self.findings = findings
        super().__init__("; ".join(findings))


def baseline_sha256(baseline: dict[str, Any]) -> str:
    raw = json.dumps(baseline, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load_baseline(path: str | Path | None = None) -> dict[str, Any]:
    target = Path(path) if path is not None else DEFAULT_BASELINE_PATH
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"failed to read model review baseline from {target}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("recipes"), dict) or not isinstance(
        data.get("pending"), list
    ):
        raise ValueError("model review baseline must contain 'recipes' object and 'pending' list")
    return data


def _stage_roles(governance: dict[str, Any]) -> tuple[dict[str, str], dict[tuple[str, int], str]]:
    stage_role: dict[str, str] = {}
    reviewer_role: dict[tuple[str, int], str] = {}
    for name, role in (governance.get("roles") or {}).items():
        for selector in role.get("selectors", []):
            if "reviewer_index" in selector:
                reviewer_role[(selector["stage"], selector["reviewer_index"])] = name
            else:
                stage_role[selector["stage"]] = name
    return stage_role, reviewer_role


def policy_recipes(policy: RoutingPolicy) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Derive (recipes, pending bindings) from the policy, keyed by stable recipe id.

    Recipes cover stages, tiers, alternates, every reviewer identity (verbatim,
    suffix included) and concrete governed execution bindings. Bindings with no
    resolved wire or unverified caller are pending, never recipes.
    """
    governance = policy.raw.get("governance") or {}
    stage_role, reviewer_role = _stage_roles(governance)
    recipes: dict[str, dict[str, Any]] = {}
    pending: dict[str, dict[str, Any]] = {}
    for name, stage in policy.stages.items():
        recipes[f"stage:{name}"] = {
            "effort": stage.effort, "provider": stage.provider, "model": stage.model,
            "role": stage_role.get(name),
        }
        for index, identity in enumerate(stage.reviewers):
            recipes[f"reviewer:{name}:{index}"] = {
                "stage": name, "identity": identity, "role": reviewer_role.get((name, index)),
            }
    for effort, tier in policy.tiers.items():
        recipes[f"tier:{effort}"] = {"effort": effort, "provider": tier.provider, "model": tier.model}
    for alt in policy.alternates:
        recipes[f"alternate:{alt.rule}"] = {
            "from": alt.from_provider, "provider": alt.to.provider, "model": alt.to.model,
        }
    for lane, record in (governance.get("lanes") or {}).items():
        binding = {"wire": record.get("wire"), "status": record.get("status")}
        if record.get("wire") and record.get("status") == "test_verified":
            recipes[f"binding:lane:{lane}"] = binding
        else:
            pending[f"binding:lane:{lane}"] = binding
    for name, role in (governance.get("roles") or {}).items():
        invocation = role.get("invocation") or {}
        binding = {"caller": invocation.get("caller"), "status": invocation.get("status")}
        if invocation.get("caller") and invocation.get("status") == "test_verified":
            recipes[f"binding:role:{name}"] = binding
        else:
            pending[f"binding:role:{name}"] = binding
    return recipes, pending


def _required_roles(policy: RoutingPolicy, key: str, recipe: dict[str, Any]) -> set[str]:
    """Roles whose outputs must be graded before this recipe may enter routing."""
    stage_role, _ = _stage_roles(policy.raw.get("governance") or {})
    if recipe.get("role"):
        return {normalize_role(recipe["role"])}
    kind, _, rest = key.partition(":")
    if kind == "binding" and rest.startswith("role:"):
        return {normalize_role(rest.removeprefix("role:"))}
    if kind == "tier":
        stages = [n for n, s in policy.stages.items() if s.effort == recipe["effort"]]
    elif kind == "alternate":
        stages = [n for n, s in policy.stages.items() if s.provider == recipe["from"]]
    else:
        lane = rest.removeprefix("lane:")
        stages = [n for n, s in policy.stages.items() if s.provider == lane]
    return {normalize_role(stage_role[n]) for n in stages if n in stage_role}


def _is_date(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _check_qualification(
    policy: RoutingPolicy,
    key: str,
    recipe: dict[str, Any],
    entry: dict[str, Any],
    task_set: TaskSet,
    digest: str,
    last_review: str,
) -> list[str]:
    found: list[str] = []
    qual = entry.get("qualification")
    if not isinstance(qual, dict):
        return [f"missing_qualification:{key}"]
    if not _is_date(qual.get("date")):
        found.append(f"qualification_date:{key}")
    elif _is_date(last_review) and qual["date"] > last_review:
        found.append(f"qualification_after_last_review:{key}")
    if qual.get("recipe") != recipe:
        found.append(f"qualification_recipe_mismatch:{key}")
    if qual.get("task_digest") != digest:
        found.append(f"qualification_stale_digest:{key}")
    evidence = qual.get("evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    for axis in RUBRIC_AXES:
        item = evidence.get(axis)
        if not (isinstance(item, dict) and _text(item.get("source")) and _text(item.get("finding"))
                and _is_date(item.get("observed"))):
            found.append(f"missing_rubric_evidence:{key}:{axis}")
    rubric = qual.get("rubric")
    if not (isinstance(rubric, dict) and rubric.get("outcome") == "pass" and _text(rubric.get("reviewer"))
            and _text(rubric.get("reason"))):
        found.append(f"missing_rubric_verdict:{key}")
    required = _required_roles(policy, key, recipe)
    outputs = qual.get("outputs")
    outputs = outputs if isinstance(outputs, dict) else {}
    graded = {normalize_role(r): o for r, o in outputs.items() if isinstance(r, str)}
    if not required:
        found.append(f"no_role_to_qualify:{key}")
    claimed = qual.get("results")
    claimed = claimed if isinstance(claimed, dict) else {}
    for role in sorted(required):
        if not isinstance(graded.get(role), dict):
            found.append(f"missing_role_outputs:{key}:{role}")
            continue
        result = qualify(role, graded[role], task_set=task_set)
        if not result.passed:
            found.append(f"qualification_outputs_fail:{key}:{role}")
        record = claimed.get(role)
        if not (isinstance(record, dict) and record.get("outcome") == result.outcome
                and record.get("digest") == result.digest):
            found.append(f"forged_result:{key}:{role}")
    return found


def check_policy_review(
    policy: RoutingPolicy,
    *,
    baseline: dict[str, Any] | None = None,
    task_set: TaskSet | None = None,
    repo_root: Path | None = None,
) -> dict[str, list[str]]:
    """Refuse a governing policy holding recipes that are neither baseline nor qualified.

    A ``baseline`` entry is valid only while the derived recipe equals the frozen
    snapshot exactly; additions or changes need a dated qualification whose
    outputs are re-graded here. Unresolved bindings stay ``pending`` and can
    never count as execution verified. Raises PolicyReviewError listing findings;
    returns recipe keys grouped by status otherwise.
    """
    found: list[str] = []
    frozen = load_baseline() if baseline is None else baseline
    if baseline_sha256(frozen) != BASELINE_SHA256:
        found.append("baseline_snapshot_tampered")
    ts = task_set if task_set is not None else load_task_set()
    digest = task_set_sha256(ts)

    review = policy.raw.get("review")
    if not isinstance(review, dict):
        raise PolicyReviewError([*found, "missing_review_metadata"])
    if review.get("rubric") != RUBRIC_PATH:
        found.append("rubric_path")
    elif repo_root is not None and not (repo_root / RUBRIC_PATH).is_file():
        found.append("rubric_missing")
    schedule = review.get("schedule")
    for name in SCHEDULE_KEYS:
        if not (isinstance(schedule, dict) and _text(schedule.get(name))):
            found.append(f"schedule_missing:{name}")
    if review.get("task_set_digest") != digest:
        found.append("stale_task_set_digest")
    last = review.get("last_review")
    last_date = last.get("date") if isinstance(last, dict) else None
    if not (isinstance(last, dict) and _is_date(last_date) and last.get("outcome") in LAST_REVIEW_OUTCOMES):
        found.append("last_review_missing")
        last_date = ""

    entries = review.get("recipes")
    entries = entries if isinstance(entries, dict) else {}
    recipes, pending = policy_recipes(policy)
    summary: dict[str, list[str]] = {"baseline": [], "qualified": [], "pending": []}
    frozen_recipes = frozen["recipes"]
    for key in sorted(recipes.keys() | pending.keys() | entries.keys()):
        entry = entries.get(key)
        if key not in recipes and key not in pending:
            found.append(f"unknown_recipe_entry:{key}")
            continue
        if not isinstance(entry, dict):
            found.append(f"unreviewed_recipe:{key}")
            continue
        status = entry.get("status")
        if key in pending:
            if status != "pending":
                found.append(f"pending_counted_verified:{key}")
            elif key not in frozen["pending"]:
                found.append(f"unlisted_pending:{key}")
            else:
                summary["pending"].append(key)
            continue
        if status == "pending":
            found.append(f"resolved_but_marked_pending:{key}")
        elif status == "baseline":
            if not _is_date(entry.get("since")):
                found.append(f"baseline_since:{key}")
            if frozen_recipes.get(key) != recipes[key]:
                found.append(f"baseline_recipe_mismatch:{key}")
            else:
                summary["baseline"].append(key)
        elif status == "qualified":
            if not _is_date(entry.get("since")):
                found.append(f"qualified_since:{key}")
            problems = _check_qualification(policy, key, recipes[key], entry, ts, digest, last_date)
            found.extend(problems)
            if not problems:
                summary["qualified"].append(key)
        else:
            found.append(f"unknown_status:{key}")
    if found:
        raise PolicyReviewError(found)
    return summary

"""Load one campaign definition and score its trials (OMP-590).

The definition is a JSON object. Harness paths are resolved against the
definition file's directory, and each harness is read once at load so a later
edit does not change the cached bytes or the candidate digest. Scoring is a
pure comparison of observed metrics to those criteria: it does not create a
campaign, change a contract, or touch an actor.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, TypeGuard

from omp_work.v1.models import ResearchDomain

__all__ = [
    "CampaignDefinition",
    "Criterion",
    "PreparedTrial",
    "SearchConfig",
    "load_definition",
    "score_campaign",
    "score_metrics",
]

_DEFINITION_KEYS = frozenset(
    {"objective", "domain", "trials", "criteria", "timeout_seconds", "search"}
)
_TRIAL_KEYS = frozenset({"harness", "id", "parent", "cost"})
_SEARCH_KEYS = frozenset({"policy", "budget", "width", "seed", "metric"})
_TRIAL_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
_CRITERION_KEYS = frozenset({"metric", "op", "threshold"})
_DEFAULT_TIMEOUT_SECONDS = 60
_MAX_METRIC_LENGTH = 128
_COMPLETED = "completed"

# (predicate, symbol shown in a failed-comparison reason)
_COMPARISONS: dict[str, tuple[Any, str]] = {
    "gte": (lambda value, threshold: value >= threshold, ">="),
    "gt": (lambda value, threshold: value > threshold, ">"),
    "lte": (lambda value, threshold: value <= threshold, "<="),
    "lt": (lambda value, threshold: value < threshold, "<"),
    "eq": (lambda value, threshold: value == threshold, "=="),
}

Outcome = Literal["supported", "refuted"]


@dataclass(frozen=True, slots=True)
class Criterion:
    """One numeric acceptance check, applied in definition order."""

    metric: str
    op: str
    threshold: int | float


@dataclass(frozen=True, slots=True)
class PreparedTrial:
    """One harness snapshotted at load.

    ``candidate_digest`` is the SHA-256 hex digest of ``harness_bytes``.
    """

    harness: Path
    harness_bytes: bytes
    candidate_digest: str
    id: str | None = None
    parent: str | None = None
    cost: int | float = 1


@dataclass(frozen=True, slots=True)
class SearchConfig:
    """Resolved search options; greedy maximizes the declared metric."""

    policy: Literal["fixed", "greedy"]
    budget: int | float
    width: int
    seed: int
    metric: str


@dataclass(frozen=True, slots=True)
class CampaignDefinition:
    """A loaded definition: objective, domain, criteria, and prepared trials."""

    objective: str
    domain: ResearchDomain
    timeout_seconds: int
    criteria: tuple[Criterion, ...]
    trials: tuple[PreparedTrial, ...]
    search: SearchConfig | None = None


def _reject_constant(token: str) -> None:
    raise ValueError(f"non-finite number {token}")


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError(f"duplicate key {key}")
        document[key] = value
    return document


def _read_json(path: Path) -> Any:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read definition {path}") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"definition {path} is not utf-8") from exc
    try:
        return json.loads(
            text, object_pairs_hook=_object_pairs, parse_constant=_reject_constant
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f"definition {path} is not valid JSON") from exc


def _object(value: object, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{where} must be an object")
    return value


def _check_keys(value: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    extra = sorted(key for key in value if key not in allowed)
    if not extra:
        return
    noun = "key" if len(extra) == 1 else "keys"
    raise ValueError(f"{where} has unknown {noun} {', '.join(extra)}")


def _require(value: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in value:
        raise ValueError(f"{where} missing {key}")
    return value[key]


def _nonempty_str(value: object, label: str, *, max_length: int | None = None) -> str:
    if not isinstance(value, str) or value == "":
        raise ValueError(f"{label} must be a nonempty string")
    if max_length is not None and len(value) > max_length:
        raise ValueError(f"{label} must be at most {max_length} characters")
    return value


def _is_finite_number(value: object) -> TypeGuard[int | float]:
    return not isinstance(value, bool) and (
        isinstance(value, int) or (isinstance(value, float) and math.isfinite(value))
    )


def _finite_number(value: object, label: str) -> int | float:
    if not _is_finite_number(value):
        raise ValueError(f"{label} must be a finite number")
    return value


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _positive_number(value: object, label: str) -> int | float:
    number = _finite_number(value, label)
    try:
        finite = math.isfinite(number)
    except OverflowError:
        finite = False
    if not finite or number <= 0:
        raise ValueError(f"{label} must be a finite positive number")
    return number


def _trial_id(value: object, label: str) -> str:
    if not isinstance(value, str) or _TRIAL_ID.fullmatch(value) is None:
        raise ValueError(f"{label} must match [A-Za-z0-9._-]{{1,64}}")
    return value


def _nonempty_list(value: object, label: str) -> list[Any]:
    if not isinstance(value, list) or len(value) == 0:
        raise ValueError(f"{label} must be a nonempty list")
    return value


def _domain(value: object) -> ResearchDomain:
    if isinstance(value, str):
        try:
            return ResearchDomain(value)
        except ValueError:
            pass
    raise ValueError(f"domain is not a research domain: {value!r}")


def _comparison(value: object) -> str:
    if isinstance(value, str) and value in _COMPARISONS:
        return value
    allowed = ", ".join(_COMPARISONS)
    raise ValueError(f"op must be one of {allowed}")


def _read_harness(definition_path: Path, raw: object) -> PreparedTrial:
    text = _nonempty_str(raw, "harness")
    if "\x00" in text:
        raise ValueError("harness path contains NUL")
    candidate = Path(text)
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (definition_path.parent / candidate).resolve()
    try:
        harness_bytes = resolved.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read harness {text}") from exc
    digest = hashlib.sha256(harness_bytes).hexdigest()
    return PreparedTrial(
        harness=resolved, harness_bytes=harness_bytes, candidate_digest=digest
    )


def _parse_trial(
    definition_path: Path, value: object, index: int, earlier_ids: set[str]
) -> PreparedTrial:
    where = f"trials[{index}]"
    trial = _object(value, where)
    _check_keys(trial, _TRIAL_KEYS, where)
    trial_id = _trial_id(trial.get("id", f"t{index:03d}"), f"{where}.id")
    if trial_id in earlier_ids:
        raise ValueError(f"{where} duplicate id {trial_id}")
    parent = trial.get("parent")
    if parent is not None:
        parent = _trial_id(parent, f"{where}.parent")
        if parent not in earlier_ids:
            raise ValueError(f"{where}.parent must name an earlier trial id")
    cost = _positive_number(trial.get("cost", 1), f"{where}.cost")
    prepared = _read_harness(definition_path, _require(trial, "harness", where))
    earlier_ids.add(trial_id)
    return replace(prepared, id=trial_id, parent=parent, cost=cost)


def _parse_search(
    value: object, trials: Sequence[PreparedTrial], criteria: Sequence[Criterion]
) -> SearchConfig:
    search = _object(value, "search")
    _check_keys(search, _SEARCH_KEYS, "search")
    policy = search.get("policy", "fixed")
    if policy not in ("fixed", "greedy"):
        raise ValueError("search.policy must be fixed or greedy")
    width = _positive_int(search.get("width", 1), "search.width")
    seed = search.get("seed", 0)
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("search.seed must be a nonnegative integer")
    metric = _nonempty_str(search.get("metric", criteria[0].metric), "search.metric")
    if metric not in {criterion.metric for criterion in criteria}:
        raise ValueError("search.metric must name a declared criterion")
    if "budget" in search:
        budget = _positive_number(search["budget"], "search.budget")
    else:
        try:
            total = math.fsum(trial.cost for trial in trials)
        except OverflowError as exc:
            raise ValueError("search.budget total must be finite") from exc
        budget = _positive_number(total, "search.budget")
    return SearchConfig(
        policy=policy, budget=budget, width=width, seed=seed, metric=metric
    )


def _parse_criterion(value: object, index: int) -> Criterion:
    where = f"criteria[{index}]"
    criterion = _object(value, where)
    _check_keys(criterion, _CRITERION_KEYS, where)
    metric = _nonempty_str(
        _require(criterion, "metric", where),
        "metric",
        max_length=_MAX_METRIC_LENGTH,
    )
    op = _comparison(_require(criterion, "op", where))
    threshold = _finite_number(_require(criterion, "threshold", where), "threshold")
    return Criterion(metric=metric, op=op, threshold=threshold)


def load_definition(path: Path) -> CampaignDefinition:
    """Read ``path`` and snapshot every harness against that file's directory.

    ``timeout_seconds`` defaults to 60 when the key is absent. Unknown keys,
    duplicate keys, booleans used as numbers, and non-finite numbers are
    refused. Trial ids default to t000, t001, etc.; parents must name earlier
    trials. An explicit ``search`` object resolves policy, metric, and budget
    defaults once. The returned trials keep the bytes read during this call.
    """
    definition_path = Path(path)
    document = _object(_read_json(definition_path), "definition")
    _check_keys(document, _DEFINITION_KEYS, "definition")
    objective = _nonempty_str(
        _require(document, "objective", "definition"), "objective"
    )
    domain = _domain(_require(document, "domain", "definition"))
    if "timeout_seconds" in document:
        timeout_seconds = _positive_int(document["timeout_seconds"], "timeout_seconds")
    else:
        timeout_seconds = _DEFAULT_TIMEOUT_SECONDS
    earlier_ids: set[str] = set()
    trials = tuple(
        _parse_trial(definition_path, trial, index, earlier_ids)
        for index, trial in enumerate(
            _nonempty_list(_require(document, "trials", "definition"), "trials")
        )
    )
    criteria = tuple(
        _parse_criterion(criterion, index)
        for index, criterion in enumerate(
            _nonempty_list(_require(document, "criteria", "definition"), "criteria")
        )
    )
    search = (
        _parse_search(document["search"], trials, criteria)
        if "search" in document
        else None
    )
    return CampaignDefinition(
        objective=objective,
        domain=domain,
        timeout_seconds=timeout_seconds,
        criteria=criteria,
        trials=trials,
        search=search,
    )


def _format_number(value: float) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    return format(value, "g")


def _passed(count: int, singular: str, plural: str) -> str:
    noun = singular if count == 1 else plural
    return f"passed {count} {noun}"


def _observed_number(metrics: Mapping[str, object], metric: str) -> int | float | str:
    """Return the finite observation, or a refutation reason."""
    if metric not in metrics:
        return f"missing metric {metric}"
    value = metrics[metric]
    if not _is_finite_number(value):
        return f"metric {metric} is not a finite number"
    return value


def score_metrics(
    metrics: Mapping[str, object],
    criteria: Sequence[Criterion],
    execution_status: object,
) -> tuple[Outcome, str]:
    """Score one trial's metrics against ``criteria``.

    A status other than ``completed``, a missing metric, or a non-finite
    metric refutes before any later criterion is considered. Comparisons are
    ``>=``, ``>``, ``<=``, ``<``, and ``==`` for ``gte``, ``gt``, ``lte``,
    ``lt``, and ``eq``. The first failing criterion refutes. When every
    criterion passes, the reason names how many passed.
    """
    if not isinstance(metrics, Mapping):
        raise TypeError("metrics must be a mapping")
    if execution_status != _COMPLETED:
        return "refuted", f"execution status {execution_status} is not completed"
    for criterion in criteria:
        if criterion.op not in _COMPARISONS:
            raise ValueError(f"op must be one of {', '.join(_COMPARISONS)}")
        observed = _observed_number(metrics, criterion.metric)
        if isinstance(observed, str):
            return "refuted", observed
        compare, symbol = _COMPARISONS[criterion.op]
        if not compare(observed, criterion.threshold):
            actual = _format_number(observed)
            limit = _format_number(criterion.threshold)
            detail = (
                f"metric {criterion.metric} failed comparison {criterion.op} "
                f"({actual} {symbol} {limit})"
            )
            return "refuted", detail
    return "supported", _passed(len(criteria), "criterion", "criteria")


def _verdict_parts(verdict: object) -> tuple[str, str]:
    if isinstance(verdict, tuple) and len(verdict) == 2:
        outcome, reason = verdict
        if isinstance(outcome, str) and isinstance(reason, str):
            return outcome, reason
    if isinstance(verdict, str):
        return verdict, ""
    raise ValueError("verdict must be an outcome or a (outcome, reason) pair")


def score_campaign(verdicts: Sequence[object]) -> tuple[Outcome, str]:
    """Combine per-trial verdicts into one campaign outcome.

    Every verdict must be ``supported`` for the campaign to be supported, and
    the success reason names the trial count. Otherwise the reason names the
    first failure with a 1-based trial index.
    """
    for index, verdict in enumerate(verdicts, start=1):
        outcome, detail = _verdict_parts(verdict)
        if outcome != "supported":
            if detail:
                return "refuted", f"trial {index} refuted: {detail}"
            return "refuted", f"trial {index} refuted"
    return "supported", _passed(len(verdicts), "trial", "trials")

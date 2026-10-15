"""Pure mechanism exploration, critique, specification, and generation (R13)."""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from omp_work.research import hypothesis, mechanism_recipe
from omp_work.v1 import canonical

ADAPTIVE_RESTART = mechanism_recipe.ADAPTIVE_RESTART
ROOT_FIRST = mechanism_recipe.ROOT_FIRST
FAMILIES = mechanism_recipe.FAMILIES
THRESHOLD_GRID = mechanism_recipe.THRESHOLD_GRID
canonical_recipe = mechanism_recipe.canonical_recipe

__all__ = [
    "MechanismDraft",
    "classify_trace",
    "critique",
    "explore",
    "failure_pattern",
    "generation_job",
    "generation_population",
    "specify",
    "trace_digest",
    "trace_hypothesis",
]


def classify_trace(trace: Mapping[str, Any]) -> Literal["instrumentation", "mechanism"]:
    """Classify a trace as instrumentation when infra_error is present, else mechanism."""
    if trace.get("infra_error") is not None:
        return "instrumentation"
    return "mechanism"


def failure_pattern(traces: Iterable[Mapping[str, Any]]) -> str | None:
    """Identify failure patterns over mechanism traces only, in order."""
    mech_traces = [t for t in traces if classify_trace(t) == "mechanism"]

    # repeated_failure:<action> when two or more share one action with outcome failed
    failed_counts: dict[str, int] = {}
    for t in mech_traces:
        if t.get("outcome") == "failed":
            action = str(t.get("action", ""))
            failed_counts[action] = failed_counts.get(action, 0) + 1
            if failed_counts[action] >= 2:
                return f"repeated_failure:{action}"

    # Else plateau:<policy> when three or more consecutive traces under one policy
    # each fail to beat the best metric seen before them (metric None never beats).
    current_policy: str | None = None
    streak = 0
    best_metric: float | None = None

    for t in mech_traces:
        policy = str(t.get("policy", ""))
        if policy != current_policy:
            current_policy = policy
            streak = 0

        metric = t.get("metric")
        beats = (
            best_metric is not None
            and metric is not None
            and float(metric) > best_metric
        )
        if beats:
            streak = 0
        else:
            streak += 1

        if metric is not None:
            val = float(metric)
            if best_metric is None or val > best_metric:
                best_metric = val

        if streak >= 3:
            return f"plateau:{current_policy}"

    return None


def trace_digest(traces: Iterable[Mapping[str, Any]]) -> str:
    """Canonical SHA-256 of the trace list."""
    return canonical.sha256(list(traces))


def trace_hypothesis(
    traces: Iterable[Mapping[str, Any]], question: str
) -> hypothesis.Hypothesis:
    """Build an R11 hypothesis naming the detected pattern or raise ValueError("no_pattern")."""
    trace_list = list(traces)
    pattern = failure_pattern(trace_list)
    if pattern is None:
        raise ValueError("no_pattern")
    ctx = hypothesis.generation_context(question, trace_digest(trace_list))
    drafts = hypothesis.generate(ctx, [pattern])
    return drafts[0]


@dataclass(frozen=True)
class MechanismDraft:
    """Structured candidate mechanism modification."""

    id: str
    name: str
    kind: Literal["code", "config"] | str
    parent: str | None
    addresses: tuple[str, ...]
    components: tuple[str, ...]
    description: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "addresses", tuple(self.addresses))
        object.__setattr__(self, "components", tuple(self.components))


def explore(
    pattern: str, proposals: Iterable[Mapping[str, Any]]
) -> list[MechanismDraft]:
    """Map caller-supplied proposal mappings to numbered mechanism drafts mech-1, mech-2, ..."""
    drafts: list[MechanismDraft] = []
    for i, proposal in enumerate(proposals, start=1):
        draft_id = f"mech-{i}"
        name = str(proposal.get("name", draft_id))
        kind = str(proposal.get("kind", "code"))
        parent = proposal.get("parent")
        if parent is not None:
            parent = str(parent)
        if "addresses" in proposal:
            raw_addr = proposal["addresses"]
            addresses = tuple(raw_addr) if raw_addr is not None else ()
        else:
            addresses = (pattern,)
        components = tuple(proposal.get("components") or ())
        description = str(proposal.get("description", ""))
        drafts.append(
            MechanismDraft(
                id=draft_id,
                name=name,
                kind=kind,
                parent=parent,
                addresses=addresses,
                components=components,
                description=description,
            )
        )
    return drafts


def critique(
    draft: MechanismDraft, pattern: str, *, complexity_budget: int
) -> dict[str, Any]:
    """Critique a mechanism draft against the failure pattern and complexity budget."""
    reasons: list[str] = []
    if pattern not in draft.addresses:
        reasons.append("unaddressed_pattern")
    if len(draft.components) > complexity_budget:
        reasons.append("complexity_exceeded")
    status = "accepted" if not reasons else "rejected"
    return {
        "draft_id": draft.id,
        "status": status,
        "reasons": reasons,
    }


def specify(draft: MechanismDraft) -> dict[str, Any]:
    """Return the candidate specification dictionary for a mechanism draft."""
    return {
        "name": draft.name,
        "interface_version": 1,
        "input_keys": ["steps", "opened", "seed", "width"],
        "output": "step_id | None",
        "state_fields": list(draft.components),
        "expected_behavior": draft.description,
        "candidate_kind": draft.kind,
    }


def generation_job(spec: Mapping[str, Any], parameters: Mapping[str, Any]) -> str:
    """Generate deterministic strategy source defining propose(steps, opened, *, seed=0, width=1).

    ``parameters`` is validated as a recipe; a missing ``family`` means adaptive-restart.v1.
    """
    recipe = canonical_recipe(parameters)
    threshold_str = repr(recipe["min_metric"])
    if recipe["family"] == ROOT_FIRST:
        return _root_first_source(threshold_str)
    return _adaptive_restart_source(threshold_str)


def _adaptive_restart_source(threshold_str: str) -> str:
    return f'''def propose(steps, opened, *, seed=0, width=1):
    threshold = {threshold_str}
    eligible_parents = []
    for step_id in opened:
        node = steps[step_id]
        if node.get("infra_error") is not None:
            continue
        metric = node.get("metric")
        if metric is not None and metric >= threshold:
            eligible_parents.append(step_id)

    def sort_key(sid):
        metric = steps[sid].get("metric")
        if metric is None:
            return (1, 0.0, sid)
        return (0, -float(metric), sid)

    eligible_parents = sorted(eligible_parents, key=sort_key)

    for parent_id in eligible_parents[:width]:
        children = sorted(c for c in (steps[parent_id].get("children") or ()) if c not in opened)
        if children:
            return children[0], seed

    unopened_roots = sorted(sid for sid, node in steps.items() if node.get("parent_id") is None and sid not in opened)
    if unopened_roots:
        return unopened_roots[0], seed

    active_nodes = []
    for step_id in opened:
        node = steps[step_id]
        if node.get("infra_error") is None:
            active_nodes.append(step_id)

    active_nodes = sorted(active_nodes, key=sort_key)

    for parent_id in active_nodes:
        children = sorted(c for c in (steps[parent_id].get("children") or ()) if c not in opened)
        if children:
            return children[0], seed

    return None, seed
'''


def _root_first_source(threshold_str: str) -> str:
    return f'''def propose(steps, opened, *, seed=0, width=1):
    threshold = {threshold_str}
    unopened_roots = sorted(sid for sid, node in steps.items() if node.get("parent_id") is None and sid not in opened)
    if unopened_roots:
        return unopened_roots[0], seed

    def sort_key(sid):
        metric = steps[sid].get("metric")
        if metric is None:
            return (1, 0.0, sid)
        return (0, -float(metric), sid)

    active_nodes = sorted(
        (sid for sid in opened if steps[sid].get("infra_error") is None),
        key=sort_key,
    )
    eligible = [
        sid for sid in active_nodes
        if steps[sid].get("metric") is not None and steps[sid]["metric"] >= threshold
    ]
    for parent_id in eligible[:width] + active_nodes:
        children = sorted(c for c in (steps[parent_id].get("children") or ()) if c not in opened)
        if children:
            return children[0], seed

    return None, seed
'''


_PRODUCTIONS = {
    ADAPTIVE_RESTART: _adaptive_restart_source,
    ROOT_FIRST: _root_first_source,
}


def generation_population(spec: Mapping[str, Any], family: str) -> list[dict[str, Any]]:
    """Generate one executable policy per frozen threshold for one production rule."""
    if family not in FAMILIES:
        raise ValueError("unknown recipe family")
    implementation = hashlib.sha256(
        inspect.getsource(_PRODUCTIONS[family]).encode("utf-8")
    ).hexdigest()
    population: list[dict[str, Any]] = []
    for threshold in THRESHOLD_GRID:
        recipe = canonical_recipe({"family": family, "min_metric": threshold})
        source = generation_job(spec, recipe)
        population.append(
            {
                "family": family,
                "recipe": recipe,
                "recipe_sha256": canonical.sha256(recipe),
                "spec_sha256": canonical.sha256(spec),
                "source": source,
                "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                "implementation_sha256": implementation,
            }
        )
    return population

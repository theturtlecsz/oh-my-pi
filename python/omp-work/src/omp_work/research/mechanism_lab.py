"""M3 proof: mechanism lab and harness promotion (R13, R14, OMP-317).

``run_lab`` chains trace pattern, hypothesis, exploration, critique,
specification, and generation (s01), then the isolated strategy protocol (s02),
then development comparison and fresh-cohort confirmation (s03).
``run_promotion`` packages that source (s04), then records canary, rollback,
and evaluator-change decisions (s05) plus the stable greedy run.

Each function writes one JSON file and nothing else.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from omp_work.research import mechanism, search
from omp_work.research.harness_release import (
    bind_evaluated_bytes,
    gate_status,
    new_manifest,
    package_candidate,
    propose_release,
    record_gate,
)
from omp_work.research.mechanism_eval import (
    Candidate,
    ConfirmationCohort,
    advance,
    confirm,
    development_comparison,
)
from omp_work.research.promotion import (
    CanaryObservation,
    EvaluatorChange,
    canary_verdict,
    check_promotion,
    confirmation_valid,
    plan_rollback,
    requires_fresh_confirmation,
)
from omp_work.research.strategy_protocol import (
    config_candidate,
    load_strategy,
    run_candidate,
    run_with_fallback,
)
from omp_work.v1.canonical import canonical_json, sha256

__all__ = ["run_lab", "run_promotion"]

DEV_IDS = ("A1", "A2", "A3", "A4")
CONF1_IDS = ("C1", "C2", "C3", "C4")
CONF2_IDS = ("D1", "D2", "D3", "D4")
QUESTION = "Why did greedy stop improving?"
MIN_METRIC = 0.85
RESEARCH_COST = 1.0
COMPARISON_COST = 1.0
RESOURCE_CAPS = {"cpu_seconds": 120}
ALLOWED_ACTIONS = ("open",)
EVALUATOR = {"component_sha256": "11" * 32, "artifact_sha256": "22" * 32}
NEW_EVALUATOR = {"component_sha256": "33" * 32, "artifact_sha256": "44" * 32}
VERSIONS = {"policy": "adaptive-restart.v1", "model": "m1", "skill": "s1"}
CANDIDATE_RELEASE = "candidate-1"
STABLE_RELEASE = "stable-1"
STRATEGY_PATH = "research/strategies/adaptive_restart.py"
CONFIG_PATH = "research/config/lab.json"
STABLE_PATH = "research/strategies/greedy.py"
CORE_PATH = "python/omp-work/src/omp_work/contracts/v1/authority.py"
GATES = (
    ("code_review", "reviewer"),
    ("audit", "auditor"),
    ("ci", "ci"),
    ("native_acceptance", "owner"),
)

BROKEN_SOURCE = "def propose("
OVERSIZED_SOURCE = (
    'RESOURCES = {"cpu_seconds": 1000}\n'
    "def propose(steps, opened, *, seed=0, width=1):\n"
    "    for step_id in sorted(opened):\n"
    "        children = sorted(\n"
    "            child for child in (steps[step_id].get('children') or []) if child not in opened\n"
    "        )\n"
    "        if children:\n"
    "            return children[0], seed\n"
    "    return None, seed\n"
)
WRONG_INTERFACE_SOURCE = (
    "def propose(steps, opened, *, seed=0, width):\n    return None, seed\n"
)
TAMPER_SOURCE = (
    "def propose(steps, opened, *, seed=0, width=1):\n"
    "    verify_receipt()\n"
    "    return None, seed\n"
)
CRASH_SOURCE = "def propose(steps, opened, *, seed=0, width=1):\n    return 1 / 0\n"
_REJECTIONS = (
    ("broken", BROKEN_SOURCE, "broken_code"),
    ("oversized", OVERSIZED_SOURCE, "resource_exceeded"),
    ("wrong_interface", WRONG_INTERFACE_SOURCE, "wrong_interface"),
    ("tampering", TAMPER_SOURCE, "evaluator_tampering"),
)


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _steps(ids: tuple[str, str, str, str]) -> dict[str, dict[str, Any]]:
    root_a, child_a, root_b, child_b = ids
    return {
        root_a: {
            "parent_id": None,
            "metric": 0.9,
            "cost": 1,
            "children": [child_a],
            "infra_error": None,
        },
        child_a: {
            "parent_id": root_a,
            "metric": 0.1,
            "cost": 1,
            "children": [],
            "infra_error": None,
        },
        root_b: {
            "parent_id": None,
            "metric": 0.8,
            "cost": 1,
            "children": [child_b],
            "infra_error": None,
        },
        child_b: {
            "parent_id": root_b,
            "metric": 0.95,
            "cost": 1,
            "children": [],
            "infra_error": None,
        },
    }


def _task(ids: tuple[str, str, str, str]) -> dict[str, Any]:
    return {"steps": _steps(ids), "budget": 4, "width": 1}


def _traces() -> list[dict[str, Any]]:
    metrics = (0.5, 0.4, 0.45)
    return [
        {
            "step_id": f"t{index}",
            "policy": "greedy",
            "action": "open",
            "metric": metric,
            "cost": 1.0,
            "infra_error": None,
            "outcome": "completed",
        }
        for index, metric in enumerate(metrics, start=1)
    ]


def _fingerprint(steps: dict[str, Any], opened: set[str]) -> dict[str, str]:
    return {
        "steps": canonical_json(steps),
        "opened": canonical_json(sorted(opened)),
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(f"not jsonable: {type(value).__name__}")


def _write_json(path: Path, record: dict[str, Any]) -> dict[str, Any]:
    payload = _jsonable(record)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return payload


def _mechanism() -> dict[str, Any]:
    traces = _traces()
    pattern = mechanism.failure_pattern(traces)
    _check(pattern == "plateau:greedy", f"unexpected_pattern:{pattern}")
    hypothesis = mechanism.trace_hypothesis(traces, QUESTION)
    drafts = mechanism.explore(
        pattern,
        [
            {
                "name": "adaptive_restart",
                "kind": "code",
                "parent": "greedy",
                "addresses": (pattern,),
                "components": ("threshold",),
                "description": (
                    "Open another root when no open parent meets min_metric; "
                    "otherwise extend that parent."
                ),
            }
        ],
    )
    draft = drafts[0]
    reviewed = mechanism.critique(draft, pattern, complexity_budget=2)
    _check(reviewed["status"] == "accepted", f"critique_rejected:{reviewed['reasons']}")
    spec = mechanism.specify(draft)
    source = mechanism.generation_job(spec, {"min_metric": MIN_METRIC})
    fn = load_strategy(source)
    return {
        "pattern": pattern,
        "hypothesis_id": hypothesis.id,
        "spec": spec,
        "source": source,
        "fn": fn,
        "candidate_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
    }


def _run_keywords() -> dict[str, Any]:
    return {
        "seed": 0,
        "width": 1,
        "allowed_actions": ALLOWED_ACTIONS,
        "resource_caps": RESOURCE_CAPS,
    }


def _rejection_row(
    name: str,
    source: str,
    steps: dict[str, Any],
    opened: set[str],
    original: dict[str, str],
) -> dict[str, Any]:
    result = run_candidate(source, steps, opened, **_run_keywords())
    after = _fingerprint(steps, opened)
    _check(after == original, f"fixture_mutated:{name}")
    reason = result.get("reason") or (result["reasons"][0] if result["reasons"] else "")
    return {
        "name": name,
        "source": source,
        "status": result["status"],
        "reason": reason,
        "reasons": list(result["reasons"]),
        "steps_before": original["steps"],
        "steps_after": after["steps"],
        "opened_before": original["opened"],
        "opened_after": after["opened"],
    }


def _opened_winner(
    policy: Any, steps: dict[str, Any], *, budget: float
) -> dict[str, Any]:
    tree = json.loads(canonical_json(steps))
    opened = search.run_policy(policy, tree, budget=budget, width=1)
    winner_id = search.winner({step_id: tree[step_id] for step_id in opened})
    metric = None if winner_id is None else tree[winner_id]["metric"]
    return {"opened": opened, "winner": winner_id, "winner_metric": metric}


def run_lab(directory: str | Path) -> dict[str, Any]:
    """Run the mechanism lab and write ``lab.json``."""
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    built = _mechanism()
    fn = built.pop("fn")

    steps = _steps(DEV_IDS)
    opened: set[str] = {"A1"}
    original = _fingerprint(steps, opened)
    loaded = run_candidate(built["source"], steps, opened, **_run_keywords())
    _check(_fingerprint(steps, opened) == original, "fixture_mutated:load")
    _check(loaded["status"] == "ok", f"load_failed:{loaded}")

    refusal_reasons = [
        _rejection_row(name, source, steps, opened, original)
        for name, source, _expected in _REJECTIONS
    ]
    for row, (_name, _source, expected) in zip(
        refusal_reasons, _REJECTIONS, strict=True
    ):
        _check(row["reason"] == expected, f"unexpected_refusal:{row['reason']}")

    greedy_pick = search.greedy(steps, opened, seed=0, width=1)
    fallback = run_with_fallback(
        CRASH_SOURCE,
        steps,
        opened,
        fallback_fn=search.greedy,
        **_run_keywords(),
    )
    after_fallback = _fingerprint(steps, opened)
    _check(after_fallback == original, "fixture_mutated:fallback")
    _check(fallback["status"] == "fallback", f"fallback_status:{fallback['status']}")
    _check(tuple(fallback["proposal"]) == tuple(greedy_pick), "fallback_mismatch")

    candidate = Candidate(
        id="cand-1",
        parent="greedy",
        source_sha256=built["candidate_sha256"],
        interface_version=1,
        declared_capabilities=("search",),
        contract_passed=False,
        state="proposed",
    )
    lifecycle = [candidate.state]
    for event in ("inspect", "contract_pass", "development"):
        candidate = advance(candidate, event)
        lifecycle.append(candidate.state)
    _check(candidate.state == "development_compared", candidate.state)

    dev_task = _task(DEV_IDS)
    comparison = development_comparison(
        fn,
        search.greedy,
        {"dev-1": dev_task},
        research_cost=RESEARCH_COST,
        comparison_cost=COMPARISON_COST,
    )
    dev_candidate = _opened_winner(fn, dev_task["steps"], budget=4)
    dev_baseline = _opened_winner("greedy", dev_task["steps"], budget=4)
    _check(dev_candidate["opened"] == ["A1", "A2", "A3", "A4"], "candidate_opened")
    _check(dev_candidate["winner_metric"] == 0.95, "candidate_metric")
    _check(dev_baseline["opened"] == ["A1", "A2"], "baseline_opened")
    _check(dev_baseline["winner_metric"] == 0.9, "baseline_metric")
    _check(comparison["cost"]["total"] == 9.0, "dev_total")
    _check(comparison["cost"]["research"] == 2.0, "dev_research")
    _check(comparison["cost"]["comparison"] == 1.0, "dev_comparison")

    cohort = ConfirmationCohort(
        dev_task_ids=("dev-1",),
        task_ids=("conf-1", "conf-2"),
        required_wins=2,
    )
    confirmation = confirm(
        candidate,
        cohort,
        fn,
        search.greedy,
        {"conf-1": _task(CONF1_IDS), "conf-2": _task(CONF2_IDS)},
        frozen_source=built["source"],
        research_cost=RESEARCH_COST,
        comparison_cost=COMPARISON_COST,
    )
    _check(confirmation["status"] == "confirmed", confirmation["status"])
    _check(confirmation["wins"] == 2, "confirmation_wins")
    candidate = advance(candidate, "confirm")
    lifecycle.append(candidate.state)
    _check(candidate.state == "confirmed", candidate.state)

    record = {
        "pattern": built["pattern"],
        "hypothesis_id": built["hypothesis_id"],
        "spec": built["spec"],
        "source": built["source"],
        "candidate_sha256": built["candidate_sha256"],
        "candidate_state": candidate.state,
        "lifecycle": lifecycle,
        "loaded": {
            "status": loaded["status"],
            "step_id": loaded["proposal"]["step_id"],
        },
        "comparison": comparison,
        "dev_search": {"candidate": dev_candidate, "baseline": dev_baseline},
        "confirmation": confirmation,
        "refusal_reasons": refusal_reasons,
        "fallback": {
            "status": fallback["status"],
            "proposal": list(fallback["proposal"]),
            "greedy": list(greedy_pick),
            "steps_before": original["steps"],
            "steps_after": after_fallback["steps"],
            "opened_before": original["opened"],
            "opened_after": after_fallback["opened"],
        },
    }
    return _write_json(root / "lab.json", record)


def _refuse(call: Any) -> str:
    try:
        call()
    except ValueError as exc:
        return str(exc)
    raise ValueError("expected_refusal")


def _config_bytes() -> bytes:
    config = config_candidate({"min_metric": MIN_METRIC, "width": 1}, "greedy")
    return canonical_json(config).encode("utf-8")


def _confirmed_source(root: Path) -> str:
    lab_path = root / "lab.json"
    if lab_path.is_file():
        lab = json.loads(lab_path.read_text(encoding="utf-8"))
        source = lab["source"]
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        _check(digest == lab["candidate_sha256"], "changed_candidate")
        _check(lab["candidate_state"] == "confirmed", "not_confirmed")
        return source
    return _mechanism()["source"]


def run_promotion(directory: str | Path) -> dict[str, Any]:
    """Package the confirmed mechanism and write ``promotion.json``."""
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    research_records_present = (root / "lab.json").is_file()
    source = _confirmed_source(root)
    source_bytes = source.encode("utf-8")
    config_bytes = _config_bytes()
    bundle = package_candidate(
        {STRATEGY_PATH: source_bytes, CONFIG_PATH: config_bytes},
        parent_release=STABLE_RELEASE,
    )
    _check(bundle["touches_core_authority"] is False, "candidate_touches_core")
    manifest = new_manifest(
        CANDIDATE_RELEASE,
        bundle,
        evaluator=EVALUATOR,
        versions=VERSIONS,
        schema_read=(1, 2),
        schema_write=2,
    )
    manifest = bind_evaluated_bytes(manifest, bundle["bundle_sha256"])
    for gate, actor in GATES:
        manifest = record_gate(
            manifest,
            bundle,
            gate,
            actor=actor,
            evidence_sha256=sha256(gate),
        )
    activation_refusal = _refuse(
        lambda: record_gate(
            manifest,
            bundle,
            "live_activation",
            actor="lab",
            evidence_sha256=sha256("live_activation"),
        )
    )
    _check(activation_refusal == "activation_requires_owner", activation_refusal)
    _check("live_activation" not in manifest["gates"], "activation_recorded")

    core_bundle = package_candidate({CORE_PATH: b"core\n"})
    _check(core_bundle["touches_core_authority"] is True, "core_prefix")
    core_manifest = new_manifest(
        "core-probe",
        core_bundle,
        evaluator=EVALUATOR,
        versions=VERSIONS,
        schema_read=(1, 2),
        schema_write=2,
    )
    core_refusal = _refuse(
        lambda: record_gate(
            core_manifest,
            core_bundle,
            "native_acceptance",
            actor="lab",
            evidence_sha256=sha256("native_acceptance"),
        )
    )
    _check(core_refusal == "core_authority_requires_owner", core_refusal)
    proposed = propose_release(manifest)
    _check(proposed["status"] == "proposed_release", proposed["status"])
    _check(proposed["evaluated_bundle_sha256"] == bundle["bundle_sha256"], "unbound")

    stable_bundle = package_candidate({STABLE_PATH: b"greedy\n"})
    stable_manifest = new_manifest(
        STABLE_RELEASE,
        stable_bundle,
        evaluator=dict(EVALUATOR),
        versions={"policy": "greedy", "model": "m1", "skill": "s1"},
        schema_read=(1, 2),
        schema_write=2,
    )
    _check(stable_bundle["bundle_sha256"] != bundle["bundle_sha256"], "same_bundle")
    rollback_plan = plan_rollback(proposed, stable_manifest, (1, 2))
    _check(rollback_plan["to"] == STABLE_RELEASE, "rollback_target")
    _check(
        rollback_plan["restores_bundle"] == stable_bundle["bundle_sha256"],
        "rollback_bundle",
    )
    narrow = dict(stable_manifest)
    narrow["schema_read"] = (1,)
    rollback_refusal = _refuse(lambda: plan_rollback(proposed, narrow, (1, 2)))
    _check(rollback_refusal == "rollback_incompatible:2", rollback_refusal)

    observation = CanaryObservation(
        release_id=CANDIDATE_RELEASE,
        task_id="canary-1",
        metric=0.8,
        baseline_metric=0.9,
        direction="higher",
    )
    verdict = canary_verdict([observation])
    _check(verdict == "rollback", verdict)

    change = EvaluatorChange(
        version=2,
        old_component_sha256=EVALUATOR["component_sha256"],
        old_artifact_sha256=EVALUATOR["artifact_sha256"],
        new_component_sha256=NEW_EVALUATOR["component_sha256"],
        new_artifact_sha256=NEW_EVALUATOR["artifact_sha256"],
        reason="recalibrated scorer",
    )
    earlier = {
        "evaluator_component_sha256": EVALUATOR["component_sha256"],
        "evaluator_artifact_sha256": EVALUATOR["artifact_sha256"],
    }
    _check(
        requires_fresh_confirmation(change, earlier) is True, "confirmation_still_valid"
    )
    evaluator_manifest = dict(proposed)
    evaluator_manifest["evaluator"] = dict(NEW_EVALUATOR)
    invalidation = confirmation_valid(earlier, evaluator_manifest)
    _check(invalidation == ["changed_evaluator"], str(invalidation))

    other_bundle = package_candidate(
        {"research/strategies/recalibrated.py": b"recalibrated\n"}
    )
    mixed = dict(proposed)
    mixed["bundle_sha256"] = other_bundle["bundle_sha256"]
    mixed["evaluator"] = dict(NEW_EVALUATOR)
    mixed_refusal = _refuse(lambda: check_promotion(proposed, mixed))
    _check(mixed_refusal == "mixed_change", mixed_refusal)

    stable_steps = _steps(DEV_IDS)
    stable_before = canonical_json(stable_steps)
    stable_opened = search.run_policy("greedy", stable_steps, budget=3)
    stable_after = canonical_json(stable_steps)
    _check(stable_before == stable_after, "stable_tree_mutated")
    _check(stable_opened == ["A1", "A2"], str(stable_opened))
    stable_winner = search.winner(
        {step_id: stable_steps[step_id] for step_id in stable_opened}
    )
    _check(stable_winner == "A1", str(stable_winner))
    _check(stable_steps[stable_winner]["metric"] == 0.9, "stable_metric")

    record = {
        "bundle": bundle,
        "manifest": proposed,
        "gates": proposed["gates"],
        "gate_status": gate_status(proposed),
        "refusals": [
            {"gate": "live_activation", "actor": "lab", "error": activation_refusal},
            {
                "gate": "native_acceptance",
                "actor": "lab",
                "error": core_refusal,
                "core_path": CORE_PATH,
            },
            {"kind": "rollback", "error": rollback_refusal},
            {"kind": "mixed", "error": mixed_refusal},
        ],
        "stable_run": {
            "policy": "greedy",
            "budget": 3,
            "opened": stable_opened,
            "winner": stable_winner,
            "winner_metric": stable_steps[stable_winner]["metric"],
            "tree_before": stable_before,
            "tree_after": stable_after,
            "research_records_present": research_records_present,
        },
        "stable_manifest": stable_manifest,
        "canary_verdict": verdict,
        "canary": {
            "release_id": observation.release_id,
            "task_id": observation.task_id,
            "metric": observation.metric,
            "baseline_metric": observation.baseline_metric,
            "direction": observation.direction,
        },
        "rollback_plan": rollback_plan,
        "evaluator_change": {
            "version": change.version,
            "old_component_sha256": change.old_component_sha256,
            "old_artifact_sha256": change.old_artifact_sha256,
            "new_component_sha256": change.new_component_sha256,
            "new_artifact_sha256": change.new_artifact_sha256,
            "reason": change.reason,
            "requires_fresh_confirmation": True,
            "confirmation_valid": invalidation,
        },
        "mixed_manifest": {
            "bundle_sha256": mixed["bundle_sha256"],
            "evaluator": mixed["evaluator"],
        },
    }
    return _write_json(root / "promotion.json", record)

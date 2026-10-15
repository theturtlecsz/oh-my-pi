"""Receipt-backed L1 / L2 / L3 report for a mechanism study (OMP-606-s06).

Deltas come from verified managed receipts read back through custody. Tree
metrics (``steps[*].metric``), the runner's best, candidate-reported scores and
caller summaries are not measurements. Failed and missing attempts stay in the
record. Five complete paired seeds can support a local Student-t statement.
Fewer seeds, or any incomplete pair, do not. A source-domain win does not
transfer, and a negative or zero engineering difference stays visible.
"""

from __future__ import annotations

import hashlib
import math
import tempfile
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from omp_work.research.accounting import ResourceLimits
from omp_work.research.campaign_comparison import compute_student_t
from omp_work.research.custody import artifact_path
from omp_work.research.dossier import verify_dossier, write_dossier
from omp_work.research.managed import verify_managed_trial
from omp_work.research.mechanism_recipe import THRESHOLD_GRID
from omp_work.research.mechanism_tasks import MechanismTask
from omp_work.research.mechanism_study import (
    PASS_RULE,
    Admission,
    FrozenStudy,
    MemberRun,
    StudyExecution,
    population_average,
    validate_study,
)
from omp_work.research.recursive import CallerGrant, RecursionState
from omp_work.research.recursive_transfer import assemble_results
from omp_work.research.recursive_transfer_evidence import ConsumptionHistory, NativeEvidence
from omp_work.research.recursive_transfer_execution import ArmExecution, TaskExecution
from omp_work.research.recursive_transfer_freshness import CohortTask, StepArms, prepare_cohorts
from omp_work.v1.canonical import canonical_json, sha256

__all__ = [
    "MechanismReport",
    "build_report",
    "next_trial_cost",
]

_OWNER_OPTIONS = ("revise", "defer", "retire")
_REPLAY = frozenset({"confirmation_reused", "duplicate_confirmation_content", "replay"})
_IGNORED = ("best", "steps", "trials", "summary", "candidate_score", "candidate_scores")


def next_trial_cost(caps: Mapping[str, Any]) -> dict[str, Any]:
    """Upper bound of one more local five-seed population comparison."""
    seeds = 5
    members = len(THRESHOLD_GRID) * 2
    return {
        "seeds": seeds,
        "population_members": members,
        "node_reservations": seeds * members * float(caps["node_budget"]),
        "selection_attempt_cap": seeds * members * int(caps["selection_attempts"]),
        "note": "Local comparison only. It does not count toward programme qualification.",
    }


@dataclass
class MechanismReport:
    """Three claims, kept apart: L1 selections, L2 paired populations, L3 admission."""

    completed: bool
    outcome: str
    ok: bool
    programme_qualification: bool
    l1: dict[str, Any]
    l2: dict[str, Any]
    l3: dict[str, Any]
    transfer: dict[str, Any]
    source: dict[str, Any]
    pairs: list[dict[str, Any]]
    groups: list[dict[str, Any]]
    failures: list[str]
    costs: list[dict[str, Any]]
    measurements: list[dict[str, Any]]
    owner_options: tuple[str, ...]
    next_trial: dict[str, Any]
    history: ConsumptionHistory
    dossier_reasons: list[str] = field(default_factory=list)
    goal_retained: bool = True

    @property
    def default_activated(self) -> bool:
        return False


def _carrier() -> RecursionState:
    """Arm records need a state object. This one admits nothing and grants nothing."""
    start = datetime.now(timezone.utc)
    return RecursionState(grant=CallerGrant(
        caller_id="mechanism-report", policy_id="mechanism-report", decision_id="mechanism-report",
        signature_sha256="0" * 64, window_start=start, window_end=start + timedelta(hours=1),
        max_depth=0, max_descendants=0, limits=ResourceLimits(1, 1, Decimal(1)),
    ))


def _sign(values: Sequence[float]) -> str:
    mean = math.fsum(values) / len(values)
    if mean > 0:
        return "win"
    if mean < 0:
        return "loss"
    return "null result"


def _links(run: MemberRun) -> list[dict[str, Any]]:
    raw = run.result.get("receipt_links") if isinstance(run.result, Mapping) else None
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, Mapping) and item.get("trial_id")]


def _receipt_digest(run: MemberRun) -> str:
    for link in _links(run):
        digest = link.get("receipt_sha256")
        if isinstance(digest, str) and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest):
            return digest
    return ""


def _selections(run: MemberRun) -> list[str]:
    steps: list[str] = []
    for decision in run.result.get("decisions") or []:
        if isinstance(decision, Mapping) and isinstance(decision.get("step_id"), str):
            steps.append(decision["step_id"])
    return steps


def _costs(run: MemberRun) -> dict[str, Any]:
    selections = [row for row in (run.result.get("selection_attempts") or []) if isinstance(row, Mapping)]
    nodes = [row for row in (run.result.get("node_attempts") or []) if isinstance(row, Mapping)]
    return {
        "role": run.role,
        "family": run.family,
        "threshold": run.threshold,
        "task_id": run.task_id,
        "seed": run.seed,
        "domain": run.domain,
        "spent": float(run.result.get("spent") or 0),
        "selection_seconds": math.fsum(float(row.get("duration") or 0) for row in selections),
        "node_seconds": math.fsum(float(row.get("duration") or 0) for row in nodes),
        "selection_attempts": len(selections),
        "node_attempts": len(nodes),
        "failure": run.failure,
    }


def _identity_ok(run: MemberRun, study: FrozenStudy | None) -> bool:
    if hashlib.sha256(run.source.encode("utf-8")).hexdigest() != run.source_sha256:
        return False
    for row in list(run.result.get("decisions") or []) + list(run.result.get("selection_attempts") or []):
        if not isinstance(row, Mapping):
            continue
        if "source_sha256" in row and row.get("source_sha256") != run.source_sha256:
            return False
        if "recipe_sha256" in row and row.get("recipe_sha256") != run.recipe_sha256:
            return False
    if study is not None:
        task = next((task for task in study.tasks if task.task_id == run.task_id), None)
        if task is None or (
            run.content_sha256 != task.content_sha256
            or run.manifest_sha256 != task.manifest_sha256
            or run.domain != task.domain or run.cohort != task.cohort or run.seed != task.seed
            or run.protocol_sha256 != hashlib.sha256(task.protocol_bytes).hexdigest()
            or run.evaluator_sha256 != hashlib.sha256(task.evaluator_script).hexdigest()
            or run.phase != ("development" if task.cohort == "development" else "confirmation")
            or run.metric != ("score" if task.domain == "simulation" else "ratio")
            or run.direction != "higher"
        ):
            return False
    if study is not None:
        if run.threshold is None:
            return False
        policy = study.policy(run.family, run.threshold)
        if (
            policy is None
            or policy.role != run.role
            or policy.source_sha256 != run.source_sha256
            or policy.recipe_sha256 != run.recipe_sha256
            or policy.spec_sha256 != run.spec_sha256
        ):
            return False
    return True


def _pair_runs(
    runs: Sequence[MemberRun], study: FrozenStudy | None,
) -> list[tuple[float | None, MemberRun | None, MemberRun | None, MechanismTask | None]]:
    grouped: dict[str, list[MemberRun]] = defaultdict(list)
    tasks = {} if study is None else {task.task_id: task for task in study.tasks}
    for task_id in tasks:
        grouped[task_id] = []
    for run in runs:
        if study is not None and run.task_id not in tasks:
            raise ValueError("task_not_frozen")
        if run.role not in {"baseline", "candidate"} or (
            study is not None and run.threshold not in THRESHOLD_GRID
        ):
            raise ValueError("unknown_member")
        grouped[run.task_id].append(run)
    pairs: list[tuple[float | None, MemberRun | None, MemberRun | None, MechanismTask | None]] = []
    for task_id, members in grouped.items():
        task = tasks.get(task_id)
        if study is not None or any(member.threshold is not None for member in members):
            for threshold in THRESHOLD_GRID:
                pairs.append((
                    threshold,
                    _one(members, "baseline", threshold),
                    _one(members, "candidate", threshold),
                    task,
                ))
        else:
            pairs.append((None, _one(members, "baseline", None), _one(members, "candidate", None), task))
    return pairs


def _one(members: Sequence[MemberRun], role: str, threshold: float | None) -> MemberRun | None:
    found = [
        member for member in members
        if member.role == role and member.threshold == threshold
    ]
    if len(found) > 1:
        raise ValueError("duplicate_member")
    return found[0] if found else None


def _verify_link(native_store, workspace_id, actor_id, data_dir, link: Mapping[str, Any]):
    trial_id = link.get("trial_id")
    with native_store.transaction(workspace_id, actor_id) as cur:
        receipt, reasons = verify_managed_trial(cur, workspace_id, trial_id, data_dir=data_dir)
    digest = link.get("receipt_sha256")
    if not isinstance(digest, str) or receipt is None:
        return None, list(reasons) or ["missing_receipt"]
    try:
        blob = artifact_path(Path(data_dir), workspace_id, digest).read_bytes()
    except OSError:
        return None, ["custody_mismatch", *reasons]
    if hashlib.sha256(blob).hexdigest() != digest or blob != receipt.receipt_bytes():
        return None, ["custody_mismatch", *reasons]
    if reasons:
        return receipt, list(reasons)
    return receipt, []


def _arm_value(run: MemberRun, store, workspace_id, actor_id, data_dir) -> tuple[float | None, list[str], list[str]]:
    """Mean of qualified receipt metrics. Runner best and step metrics are not read."""
    reasons: list[str] = []
    values: list[float] = []
    trials: list[str] = []
    links = _links(run)
    if not links:
        return None, ["missing_receipt"], []
    for link in links:
        receipt, problems = _verify_link(store, workspace_id, actor_id, data_dir, link)
        trial_id = str(link.get("trial_id"))
        trials.append(trial_id)
        if problems or receipt is None:
            reasons.extend(problems or ["missing_receipt"])
            continue
        if receipt.protocol_sha256 != run.protocol_sha256 or receipt.evaluator_artifact_sha256 != run.evaluator_sha256:
            reasons.append("judge_swap")
            continue
        if receipt.verdict != "qualified" or run.metric not in receipt.metrics:
            reasons.append("unqualified_receipt")
            continue
        metric = receipt.metrics[run.metric]
        if isinstance(metric, bool) or not isinstance(metric, (int, float)) or not math.isfinite(float(metric)):
            reasons.append("nonfinite_metric")
            continue
        values.append(float(metric))
    if reasons or len(values) != len(links):
        return None, list(dict.fromkeys(reasons)), trials
    return math.fsum(values) / len(values), [], trials


def _slice_claim(seed_rows: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(seed_rows, key=lambda row: row["seed"])
    deltas: list[float | None] = [row["difference"] for row in ordered]
    complete = [delta for delta in deltas if delta is not None]
    incomplete = any(delta is None for delta in deltas)
    stats = None
    inferred = False
    if not ordered or not complete and incomplete:
        outcome = "inconclusive" if not complete else "incomplete comparison"
    elif incomplete or len(deltas) != 5:
        outcome = "incomplete comparison" if incomplete else _sign(complete)
    else:
        stats = compute_student_t(complete)
        inferred = True
        outcome = str(stats["outcome"])
    return {
        "seeds": ordered,
        "deltas": deltas,
        "outcome": outcome,
        "inferred": inferred,
        "stats": stats,
        "programme_qualification": False,
    }


def _domain_claim(claims: list[dict[str, Any]], domain: str) -> dict[str, Any]:
    """Preserve every protocol slice; never infer across different evaluators."""
    selected = [claim for claim in claims if claim["domain"] == domain]
    if len(selected) == 1:
        return selected[0]
    outcomes = {claim["outcome"] for claim in selected}
    deltas = [delta for claim in selected for delta in claim["deltas"]]
    outcome = "inconclusive"
    if any(delta is not None for delta in deltas):
        outcome = ("incomplete comparison" if any(delta is None for delta in deltas)
                   else next(iter(outcomes)) if len(outcomes) == 1 else "mixed results")
    return {"outcome": outcome, "deltas": deltas, "inferred": False, "stats": None,
            "seeds": [row for claim in selected for row in claim["seeds"]], "slices": selected}


def _dataset_content(run: MemberRun) -> str:
    return sha256({"content_sha256": run.content_sha256, "domain": run.domain, "cohort": run.cohort})


def _l3(admission: Admission | None) -> dict[str, Any]:
    if admission is None:
        return {
            "admitted": False,
            "reason": "not_attempted",
            "structural": False,
            "pass_rule": PASS_RULE,
            "parent_family": None,
            "proposed_family": None,
            "production_rule_change": None,
            "cohort_count": 0,
            "cohort_proof": None,
        }
    change = None
    if admission.structural:
        change = f"{admission.parent_family} -> {admission.proposed_family}"
    return {
        "admitted": admission.admitted,
        "reason": admission.reason,
        "structural": admission.structural,
        "pass_rule": admission.pass_rule,
        "parent_family": admission.parent_family,
        "proposed_family": admission.proposed_family,
        "production_rule_change": change,
        "cohort_count": admission.cohort_count,
        "cohort_proof": {
            "pass_rule": admission.pass_rule,
            "reason": admission.reason,
            "admitted": admission.admitted,
            "cohort_count": admission.cohort_count,
        },
    }


def _groups(report) -> list[dict[str, Any]]:
    out = []
    for group in report.domains:
        out.append({
            "phase": group.phase,
            "domain": group.domain,
            "protocol_sha256": group.protocol_sha256,
            "metric": group.metric,
            "direction": group.direction,
            "status": group.status,
            "deltas": list(group.deltas),
            "failures": list(group.failures),
            "keep_both": group.keep_both,
            "denominator": group.denominator,
        })
    return out


def build_report(
    execution: StudyExecution | Sequence[MemberRun] | None = None,
    *,
    runs: Sequence[MemberRun] | None = None,
    study: FrozenStudy | None = None,
    admission: Admission | None = None,
    store,
    workspace_id,
    actor_id,
    work_id,
    data_dir,
    history: ConsumptionHistory | None = None,
    dossier_dir: Path | str | None = None,
    summary: Mapping[str, Any] | None = None,
) -> MechanismReport:
    """Build L1, L2 and L3 from custody-verified receipts.

    ``summary`` is accepted so a caller can try to supply a conclusion. Its
    values are not read. ``steps``, ``best`` and trial metric maps on a campaign
    result are not read either.
    """
    del summary  # caller summaries are not measurements
    if isinstance(execution, StudyExecution):
        runs = execution.runs
        study = execution.study
        admission = execution.admission
        completed = execution.completed
    else:
        if runs is None and isinstance(execution, Sequence):
            runs = execution
        completed = True
    if runs is None:
        raise ValueError("runs are required")
    if study is not None:
        validate_study(study)
    history = history if history is not None else ConsumptionHistory()
    failures: list[str] = []
    pairs: list[dict[str, Any]] = []
    measurements: list[dict[str, Any]] = []
    executions: list[TaskExecution] = []
    execution_pairs: list[dict[str, Any]] = []
    cohorts: list[CohortTask] = []
    cohort_task_ids: list[str] = []
    # One confirmation document per task. Separate cohorts per threshold share
    # that document and are rejected as duplicate confirmation content.
    pending_steps: dict[tuple, list[StepArms]] = {}
    state = _carrier()
    seen: set[str] = set()
    any_links = False
    verified_any = False

    for threshold, baseline, candidate, frozen_task in _pair_runs(tuple(runs), study):
        pair_failures: list[str] = []
        if baseline is None or candidate is None:
            pair_failures.append("missing_member")
            pair = _empty_pair(threshold, baseline, candidate, pair_failures, frozen_task)
            pairs.append(pair)
            execution_pairs.append(pair)
            empty_arm = ArmExecution(None, 0, (), None, tuple(pair_failures), state, None, None)
            executions.append(TaskExecution(
                pair["phase"], pair["domain"], pair["protocol_sha256"], pair["metric"], pair["direction"],
                pair["task_id"], None, "ineligible", None, {"baseline": None, "candidate": None},
                0, tuple(pair_failures), empty_arm, empty_arm, False,
            ))
            failures.extend(pair_failures)
            continue
        if baseline.failure or candidate.failure:
            pair_failures.append("failed_attempt")
        if (baseline.task_id, baseline.content_sha256, baseline.manifest_sha256, baseline.seed,
            baseline.domain, baseline.phase, baseline.cohort, baseline.dataset_id) != (
            candidate.task_id, candidate.content_sha256, candidate.manifest_sha256, candidate.seed,
            candidate.domain, candidate.phase, candidate.cohort, candidate.dataset_id):
            pair_failures.append("task_mismatch")
        if baseline.caps != candidate.caps or (study is not None and baseline.caps != study.caps):
            pair_failures.append("unequal_caps")
        if baseline.protocol_sha256 != candidate.protocol_sha256 or baseline.evaluator_sha256 != candidate.evaluator_sha256:
            pair_failures.append("judge_swap")
        if baseline.direction != candidate.direction or baseline.metric != candidate.metric:
            pair_failures.append("judge_swap")
        if not _identity_ok(baseline, study):
            pair_failures.append("baseline_tamper")
        if not _identity_ok(candidate, study):
            pair_failures.append("baseline_tamper")
        base_value, base_reasons, base_trials = _arm_value(baseline, store, workspace_id, actor_id, data_dir)
        cand_value, cand_reasons, cand_trials = _arm_value(candidate, store, workspace_id, actor_id, data_dir)
        any_links = any_links or bool(_links(baseline) or _links(candidate))
        for reason in base_reasons + cand_reasons:
            if reason == "judge_swap":
                pair_failures.append("judge_swap")
            elif reason == "custody_mismatch":
                pair_failures.append("custody_mismatch")
            elif reason == "missing_receipt":
                pair_failures.append("missing_receipt")
            elif reason not in {"unqualified_receipt", "nonfinite_metric"}:
                pair_failures.append(reason)
            else:
                pair_failures.append(reason)
        for trial_id in base_trials + cand_trials:
            if trial_id in seen or ("trial", trial_id) in history.consumed:
                pair_failures.append("replay")
            seen.add(trial_id)
        if len(base_trials) != len(cand_trials):
            pair_failures.append("unequal_attempts")
        difference = None
        if not pair_failures and base_value is not None and cand_value is not None:
            difference = cand_value - base_value if baseline.direction == "higher" else base_value - cand_value
            if not math.isfinite(difference):
                pair_failures.append("nonfinite_metric")
                difference = None
            else:
                verified_any = True
        pair = {
            "threshold": threshold,
            "task_id": baseline.task_id,
            "seed": baseline.seed,
            "phase": baseline.phase,
            "domain": baseline.domain,
            "protocol_sha256": baseline.protocol_sha256,
            "metric": baseline.metric,
            "direction": baseline.direction,
            "difference": difference,
            "baseline": {"value": base_value, "trials": base_trials, "selections": _selections(baseline)},
            "candidate": {"value": cand_value, "trials": cand_trials, "selections": _selections(candidate)},
            "failures": list(dict.fromkeys(pair_failures)),
            "costs": [_costs(baseline), _costs(candidate)],
            **_task_metadata(frozen_task),
        }
        pairs.append(pair)
        failures.extend(pair["failures"])
        for run, value, trials in ((baseline, base_value, base_trials), (candidate, cand_value, cand_trials)):
            measurements.append({
                "role": run.role, "family": run.family, "threshold": run.threshold,
                "task_id": run.task_id, "seed": run.seed, "domain": run.domain,
                "value": value, "trials": trials, "receipt_sha256": _receipt_digest(run),
            })
        if (
            difference is not None and not pair["failures"]
            and base_trials and len(base_trials) == len(cand_trials)
        ):
            key = (
                baseline.phase, baseline.domain, baseline.metric, baseline.direction,
                baseline.protocol_sha256, baseline.seed, baseline.dataset_id, baseline.task_id,
                int(baseline.caps["width"]), int(float(baseline.caps["node_budget"])),
            )
            pending_steps.setdefault(key, []).extend(
                StepArms(f"{threshold}:{index}", base_id, cand_id)
                for index, (base_id, cand_id) in enumerate(zip(base_trials, cand_trials))
            )
        execution_pairs.append(pair)
        executions.append(_execution(baseline, candidate, difference, pair["failures"], base_trials, cand_trials, state))

    for key, steps in pending_steps.items():
        phase, domain, metric, direction, protocol, seed, dataset_id, _task_id, width, budget = key
        cohort_task_ids.append(_task_id)
        cohorts.append(CohortTask(
            phase=phase, domain=domain, metric=metric, direction=direction,
            protocol_sha256=protocol, seed=seed, width=width, budget=budget,
            dataset_id=dataset_id, steps=tuple(steps),
        ))

    if cohorts:
        native = NativeEvidence(
            store, workspace_id, actor_id, work_id, data_dir, history=history,
        )
        prepared = prepare_cohorts(tuple(cohorts), native, ())
        for cohort, task, task_id in zip(cohorts, prepared, cohort_task_ids, strict=True):
            if not task.eligible:
                for reason in task.reasons:
                    failures.append("replay" if reason in _REPLAY else reason)
                for pair in pairs:
                    if (pair["task_id"] == task_id
                        and pair["phase"] == cohort.phase and pair["domain"] == cohort.domain
                        and pair["protocol_sha256"] == cohort.protocol_sha256
                        and pair["metric"] == cohort.metric and pair["direction"] == cohort.direction):
                        pair["failures"].extend("replay" if reason in _REPLAY else reason for reason in task.reasons)
                        pair["difference"] = None

    for run in runs:
        for trial_id in [link.get("trial_id") for link in _links(run)]:
            if trial_id:
                history.record_attempt(("trial", str(trial_id)))
        if run.phase == "confirmation":
            history.note_confirmation((_dataset_content(run), run.task_id))
        elif run.phase == "development":
            history.record_attempt(("development", run.task_id, run.content_sha256))

    # Freshness and custody eligibility applies to every downstream claim.
    executions = [replace(item, delta=pair["difference"], ok=pair["difference"] is not None,
                          status="compared" if pair["difference"] is not None else "ineligible",
                          failures=tuple(pair["failures"]))
                  for item, pair in zip(executions, execution_pairs, strict=True)]
    for pair in pairs:
        if pair["failures"]:
            for role in ("baseline", "candidate"):
                if pair[role] is not None:
                    pair[role]["value"] = None
    for measurement in measurements:
        pair = next(pair for pair in pairs if pair["task_id"] == measurement["task_id"]
                    and pair["threshold"] == measurement["threshold"])
        if pair["failures"]:
            measurement["value"] = None
        measurement["failures"] = list(pair["failures"])
    comparison = assemble_results(executions)
    seed_groups: dict[tuple, dict[int, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for pair in pairs:
        if pair["task_id"] is None:
            continue
        key = (pair["phase"], pair["domain"], pair["protocol_sha256"], pair["metric"], pair["direction"])
        seed_groups[key][pair["seed"]].append(pair)

    claims: list[dict[str, Any]] = []
    for key, seeds in seed_groups.items():
        phase, domain, protocol, metric, direction = key
        seed_rows = []
        for seed, members in seeds.items():
            thresholded = [member for member in members if member["threshold"] is not None]
            if thresholded:
                by_threshold = {member["threshold"]: member["difference"] for member in thresholded}
                if len(thresholded) != len(THRESHOLD_GRID) or len(by_threshold) != len(THRESHOLD_GRID) or any(value is None for value in by_threshold.values()):
                    difference = None
                else:
                    ordered = [by_threshold[threshold] for threshold in THRESHOLD_GRID]
                    difference = population_average([float(value) for value in ordered])
                per_threshold = [
                    {"threshold": member["threshold"], "difference": member["difference"]}
                    for member in sorted(thresholded, key=lambda item: item["threshold"])
                ]
            else:
                difference = members[0]["difference"] if len(members) == 1 else None
                per_threshold = [{"threshold": None, "difference": difference}]
            seed_rows.append({
                "seed": seed, "difference": difference, "per_threshold": per_threshold,
                "failures": list(dict.fromkeys(reason for member in members for reason in member["failures"])),
            })
        claim = _slice_claim(seed_rows)
        claim.update({"phase": phase, "domain": domain, "protocol_sha256": protocol, "metric": metric, "direction": direction})
        claims.append(claim)
    source = _domain_claim(claims, "simulation")
    transfer = _domain_claim(claims, "engineering")
    # Source results are reported above and are not an input to transfer.
    integrity = [reason for reason in dict.fromkeys(failures) if reason in {
        "replay", "baseline_tamper", "judge_swap", "unequal_caps", "custody_mismatch", "task_mismatch",
    }]
    if integrity:
        outcome = "failed"
        ok = False
    elif not any_links or not verified_any and transfer["outcome"] == "inconclusive":
        outcome = "inconclusive"
        ok = True
        transfer = {**transfer, "outcome": "inconclusive", "inferred": False}
    else:
        outcome = transfer["outcome"]
        ok = True

    l1 = {"selections": [
        {
            "role": run.role, "family": run.family, "threshold": run.threshold,
            "task_id": run.task_id, "seed": run.seed, "domain": run.domain,
            "steps": _selections(run), "failure": run.failure,
        }
        for run in runs
    ]}
    l2 = {
        "population_average": True,
        "best_member": False,
        "slices": claims,
        "source": source,
        "transfer": transfer,
    }
    l3 = _l3(admission)
    if l3["admitted"]:
        # Campaign deltas are not admission. A true flag still requires the gate's state.
        l3["admitted"] = bool(admission and admission.admitted and admission.reason is None)
    owner = () if outcome == "win" and transfer.get("inferred") else _OWNER_OPTIONS
    caps = study.caps if study is not None else (runs[0].caps if runs else {"node_budget": 0, "selection_attempts": 0})
    dossier_reasons = _write_dossier(
        dossier_dir, study, measurements, transfer, source, l3, outcome, owner, caps,
    )
    if dossier_reasons:
        integrity = [*integrity, "dossier"]
        outcome = "failed"
        ok = False
    return MechanismReport(
        completed=completed,
        outcome=outcome,
        ok=ok and not integrity,
        programme_qualification=False,
        l1=l1,
        l2=l2,
        l3=l3,
        transfer=transfer,
        source=source,
        pairs=pairs,
        groups=_groups(comparison),
        failures=list(dict.fromkeys([*failures, *integrity])),
        costs=[_costs(run) for run in runs],
        measurements=measurements,
        owner_options=owner,
        next_trial=next_trial_cost(caps),
        history=history,
        dossier_reasons=dossier_reasons,
    )


def _task_metadata(task: MechanismTask | None) -> dict[str, Any]:
    return {} if task is None else {
        "task_id": task.task_id, "seed": task.seed, "domain": task.domain,
        "phase": "development" if task.cohort == "development" else "confirmation",
        "protocol_sha256": hashlib.sha256(task.protocol_bytes).hexdigest(),
        "metric": "score" if task.domain == "simulation" else "ratio", "direction": "higher",
    }


def _empty_pair(threshold, baseline, candidate, reasons, task=None) -> dict[str, Any]:
    present = baseline or candidate
    return {
        "threshold": threshold,
        "task_id": None if present is None else present.task_id,
        "seed": None if present is None else present.seed,
        "phase": None if present is None else present.phase,
        "domain": None if present is None else present.domain,
        "protocol_sha256": None if present is None else present.protocol_sha256,
        "metric": None if present is None else present.metric,
        "direction": None if present is None else present.direction,
        "difference": None,
        "baseline": None,
        "candidate": None,
        "failures": reasons,
        "costs": [_costs(run) for run in (baseline, candidate) if run is not None],
        **_task_metadata(task),
    }


def _execution(baseline, candidate, difference, failures, base_trials, cand_trials, state) -> TaskExecution:
    status = "compared" if difference is not None and not failures else "inconclusive" if failures or base_trials or cand_trials else "ineligible"
    return TaskExecution(
        baseline.phase, baseline.domain, baseline.protocol_sha256, baseline.metric, baseline.direction,
        baseline.task_id, baseline.content_sha256, status, difference,
        {"baseline": (_selections(baseline) or [None])[-1], "candidate": (_selections(candidate) or [None])[-1]},
        len(base_trials) + len(cand_trials), tuple(dict.fromkeys(failures)),
        ArmExecution((_selections(baseline) or [None])[-1], len(base_trials), tuple(base_trials), None, tuple(failures), state, baseline.source_sha256, None),
        ArmExecution((_selections(candidate) or [None])[-1], len(cand_trials), tuple(cand_trials), None, tuple(failures), state, candidate.source_sha256, None),
        difference is not None and not failures,
    )


def _write_dossier(directory, study, measurements, transfer, source, l3, outcome, owner, caps) -> list[str]:
    experiments = []
    for index, measurement in enumerate(measurements):
        if measurement["value"] is None:
            continue
        experiments.append({
            "name": f"{measurement['domain']}-{measurement['role']}-s{measurement['seed']}-{measurement['threshold']}-{index}",
            "metrics": {"value": measurement["value"]},
            "receipt_sha256": measurement.get("receipt_sha256") or "",
        })
    negatives = []
    for delta in transfer.get("deltas") or []:
        if delta is not None and delta <= 0:
            negatives.append(f"engineering paired difference {delta}")
    identity = {
        "digest": None if study is None else study.digest,
        "spec_sha256": None if study is None else study.spec_sha256,
    }
    body = canonical_json(identity).encode()
    record = {
        "objective": "OMP-606 mechanism study",
        "scope": "L1 selections, L2 population-average paired differences, L3 admission",
        "protocol": None if study is None else study.digest,
        "decision": outcome,
        "limits": "Five paired seeds are a local decision only and do not qualify the programme.",
        "sources": [{"id": "frozen.json", "sha256": hashlib.sha256(body).hexdigest(), "coverage": "frozen identities"}],
        "experiments": experiments,
        "negatives": negatives,
        "contradictions": [
            "steps[*].metric, runner best, candidate scores and caller summaries are not measurements",
        ],
        "questions": list(owner),
    }
    root_cm = Path(directory) if directory is not None else None
    temporary = None
    if root_cm is None:
        temporary = tempfile.TemporaryDirectory(prefix="mechanism-report-")
        root_cm = Path(temporary.name)
    try:
        write_dossier(root_cm, record, source_bytes={"frozen.json": body})
        reasons = verify_dossier(root_cm)
    finally:
        if temporary is not None:
            temporary.cleanup()
    return list(reasons)

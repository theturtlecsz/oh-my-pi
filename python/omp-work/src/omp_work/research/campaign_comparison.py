"""Compare adaptive and fixed campaign search policies on source compression (OMP-605-s07)."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from omp_work.jobs.campaign_definition import load_definition, search_steps
from omp_work.research.zlib_source_task import create_seed_task

__all__ = [
    "SEEDS",
    "T_CRITICAL_95_DF4",
    "compare",
    "compute_student_t",
    "extract_arm_summary",
]

SEEDS = (0, 1, 2, 3, 4)
T_CRITICAL_95_DF4 = 2.776445105


def extract_arm_summary(result: dict[str, Any]) -> dict[str, Any]:
    """Extract consumed campaign fields and compute actual trial/cost accounting."""
    trials = result.get("trials") or []
    selected_ids = [t["step_id"] for t in trials if isinstance(t, dict) and "step_id" in t]
    trusted_metrics = [
        t.get("metrics", {}) for t in trials if isinstance(t, dict)
    ]
    decisions = result.get("decisions") or []
    best = result.get("best")
    spent = float(result.get("spent", 0.0))
    best_metric = (
        float(best["metric"])
        if isinstance(best, dict) and best.get("metric") is not None
        else None
    )
    return {
        "campaign_id": str(result.get("campaign_id", "")),
        "selected_ids": selected_ids,
        "trusted_metrics": trusted_metrics,
        "decisions": decisions,
        "best": best,
        "spent": spent,
        "actual_trials": len(trials),
        "actual_cost": spent,
        "best_metric": best_metric,
        "trials": trials,
    }


def compute_student_t(differences: Sequence[float]) -> dict[str, Any]:
    """Compute mean and 95% Student-t interval over paired differences.

    Interval: mean +/- 2.776445105 * sample_stdev / sqrt(N).
    Mark 'null result' iff interval contains 0; never drop seeds or relabel null as win.
    """
    n = len(differences)
    if n == 0:
        raise ValueError("differences sequence must not be empty")

    mean = math.fsum(differences) / n
    if n > 1:
        variance = sum((d - mean) ** 2 for d in differences) / (n - 1)
        sample_stdev = math.sqrt(variance)
    else:
        sample_stdev = 0.0

    margin = T_CRITICAL_95_DF4 * sample_stdev / math.sqrt(n)
    ci_lower = mean - margin
    ci_upper = mean + margin
    null_result = (ci_lower <= 0.0 <= ci_upper)
    outcome = "null result" if null_result else ("win" if mean > 0 else "loss")

    return {
        "mean": mean,
        "sample_stdev": sample_stdev,
        "margin": margin,
        "confidence_interval_95": [ci_lower, ci_upper],
        "ci_lower": ci_lower,
        "ci_upper": ci_upper,
        "null_result": null_result,
        "outcome": outcome,
        "conclusion": outcome,
    }


def compare(
    run_campaign: Callable[[Path | str], dict[str, Any]],
    repo_root: Path | str,
    out_dir: Path | str,
) -> dict[str, Any]:
    """Run adaptive vs fixed search comparison across fixed seeds 0..4.

    Consumes research_campaign results, reports actual per-seed trials/cost,
    paired metric differences, and 95% Student-t confidence interval.
    """
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    root = Path(repo_root)

    corpus_hashes_by_seed: dict[str, dict[str, str]] = {}
    corpus_trees_by_seed: dict[str, str] = {}
    seeds_report: list[dict[str, Any]] = []
    differences: list[float | None] = []
    missing_scores: list[dict[str, Any]] = []
    reference_steps: dict[str, Any] | None = None

    for seed in SEEDS:
        greedy_path, fixed_path, hashes, tree_sha256 = create_seed_task(root, seed, out_path)
        corpus_hashes_by_seed[str(seed)] = hashes
        corpus_trees_by_seed[str(seed)] = tree_sha256

        if reference_steps is None:
            reference_def = load_definition(greedy_path)
            reference_steps = search_steps(reference_def)

        greedy_raw = run_campaign(greedy_path)
        fixed_raw = run_campaign(fixed_path)

        greedy_summary = extract_arm_summary(greedy_raw)
        fixed_summary = extract_arm_summary(fixed_raw)

        for arm, summary in (("greedy", greedy_summary), ("fixed", fixed_summary)):
            if summary["best_metric"] is None:
                missing_scores.append({"seed": seed, "arm": arm})
        diff = (
            greedy_summary["best_metric"] - fixed_summary["best_metric"]
            if greedy_summary["best_metric"] is not None and fixed_summary["best_metric"] is not None
            else None
        )
        differences.append(diff)

        seeds_report.append({
            "seed": seed,
            "greedy": greedy_summary,
            "fixed": fixed_summary,
            "difference": diff,
            "actual_trials_greedy": greedy_summary["actual_trials"],
            "actual_cost_greedy": greedy_summary["actual_cost"],
            "actual_trials_fixed": fixed_summary["actual_trials"],
            "actual_cost_fixed": fixed_summary["actual_cost"],
        })

    # An unscored arm is missing evidence, never a measured zero. Keep all five
    # seeds and their actual costs, but refuse inference on an incomplete pair.
    if missing_scores:
        stats = {
            "mean": None,
            "sample_stdev": None,
            "margin": None,
            "confidence_interval_95": None,
            "ci_lower": None,
            "ci_upper": None,
            "null_result": None,
            "outcome": "incomplete comparison",
            "conclusion": "incomplete comparison",
        }
    else:
        stats = compute_student_t([d for d in differences if d is not None])

    return {
        "corpus_hashes": corpus_hashes_by_seed,
        "corpus_tree": corpus_trees_by_seed,
        "tree": reference_steps or {},
        "corpus": {
            "hashes": corpus_hashes_by_seed,
            "tree_sha256": corpus_trees_by_seed,
            "tree": reference_steps or {},
        },
        "seeds": seeds_report,
        "per_seed": {str(s["seed"]): s for s in seeds_report},
        "paired_differences": differences,
        "differences": differences,
        "missing_scores": missing_scores,
        **stats,
    }

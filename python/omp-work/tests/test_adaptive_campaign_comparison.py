"""Tests for adaptive campaign comparison on real zlib task (OMP-605-s07)."""

from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import omp_work.__main__ as cli
from omp_work.__main__ import main
from omp_work.jobs import research_campaign
from omp_work.jobs.campaign_definition import load_definition
from omp_work.jobs.research_trial import parse_metric_lines
from omp_work.research import campaign_comparison
from omp_work.research.campaign_comparison import (
    SEEDS,
    T_CRITICAL_95_DF4,
    compare,
    compute_student_t,
)
from omp_work.research.zlib_source_task import (
    POOL_NODES,
    build_corpus,
    create_seed_task,
    production_files,
    sample_production_files,
)
from test_research_campaign_cli import _assert_unbuilt, _layout, _spies


# --- 1. Known paired values verify CI / null rule ---


def test_known_paired_values_student_t_exact_formula() -> None:
    """Verify Student-t 95% interval formula with known paired differences."""
    # diffs: 1.0, 2.0, 3.0, 4.0, 5.0
    diffs = [1.0, 2.0, 3.0, 4.0, 5.0]
    stats = compute_student_t(diffs)

    expected_mean = 3.0
    # Sample variance: sum((x - 3)^2) / 4 = (4 + 1 + 0 + 1 + 4) / 4 = 10 / 4 = 2.5
    expected_stdev = math.sqrt(2.5)
    expected_margin = T_CRITICAL_95_DF4 * expected_stdev / math.sqrt(5)
    expected_ci = [expected_mean - expected_margin, expected_mean + expected_margin]

    assert stats["mean"] == pytest.approx(expected_mean)
    assert stats["sample_stdev"] == pytest.approx(expected_stdev)
    assert stats["margin"] == pytest.approx(expected_margin)
    assert stats["confidence_interval_95"][0] == pytest.approx(expected_ci[0])
    assert stats["confidence_interval_95"][1] == pytest.approx(expected_ci[1])
    assert stats["ci_lower"] == pytest.approx(expected_ci[0])
    assert stats["ci_upper"] == pytest.approx(expected_ci[1])
    # Interval [1.0368, 4.9632] does not contain 0 -> win, not null result
    assert stats["null_result"] is False
    assert stats["outcome"] == "win"
    assert stats["conclusion"] == "win"


@pytest.mark.parametrize(
    ("diffs", "expected_null", "expected_outcome"),
    [
        # All zero differences: mean=0, stdev=0, interval=[0, 0] contains 0 -> null result
        ([0.0, 0.0, 0.0, 0.0, 0.0], True, "null result"),
        # Symmetric around 0: mean=0, interval spans [-margin, margin] -> null result
        ([-0.1, 0.1, -0.05, 0.05, 0.0], True, "null result"),
        # Positive mean (0.09) but wide margin (0.119) spans 0 -> MUST mark null result, NOT win
        ([0.05, 0.1, 0.15, 0.2, -0.05], True, "null result"),
        # Negative mean (-0.09) but wide margin spans 0 -> null result
        ([-0.05, -0.1, -0.15, -0.2, 0.05], True, "null result"),
        # Decisively positive differences: CI > 0 -> win
        ([1.0, 1.2, 0.9, 1.1, 1.0], False, "win"),
        # Decisively negative differences: CI < 0 -> loss
        ([-1.0, -1.2, -0.9, -1.1, -1.0], False, "loss"),
    ],
    ids=[
        "all-zero",
        "symmetric-zero-mean",
        "positive-mean-spans-zero-null-not-win",
        "negative-mean-spans-zero-null",
        "decisive-positive-win",
        "decisive-negative-loss",
    ],
)
def test_known_paired_values_null_rule(
    diffs: list[float], expected_null: bool, expected_outcome: str
) -> None:
    """Mark 'null result' iff interval contains 0; never drop seeds or relabel null as win."""
    stats = compute_student_t(diffs)
    assert stats["null_result"] is expected_null
    assert stats["outcome"] == expected_outcome
    assert stats["conclusion"] == expected_outcome
    ci_lower = stats["ci_lower"]
    ci_upper = stats["ci_upper"]
    contains_zero = ci_lower <= 0.0 <= ci_upper
    assert stats["null_result"] == contains_zero


def test_student_t_refuses_empty() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        compute_student_t([])


# --- 2. Generated harness computes real compression metric ---


def test_generated_harness_computes_real_compression_metric(tmp_path: Path) -> None:
    """Generated self-contained Python/zlib bash harness computes real ratio metric on execution."""
    repo_root = Path(__file__).resolve().parents[3]
    files = production_files(repo_root)
    assert len(files) >= 40

    corpus_bytes, hashes, tree_sha = build_corpus(repo_root, seed=0, count=40)
    assert len(corpus_bytes) > 0
    assert len(hashes) == 40
    assert len(tree_sha) == 64

    greedy_path, fixed_path, hashes2, tree_sha2 = create_seed_task(repo_root, 0, tmp_path)
    assert hashes == hashes2
    assert tree_sha == tree_sha2

    # Verify both definitions load and contain the 7 pool nodes
    greedy_def = load_definition(greedy_path)
    fixed_def = load_definition(fixed_path)
    assert len(greedy_def.trials) == 7
    assert len(fixed_def.trials) == 7
    assert greedy_def.search is not None and greedy_def.search.policy == "greedy"
    assert fixed_def.search is not None and fixed_def.search.policy == "fixed"

    # Execute all 7 generated harnesses directly with bash
    harnesses_dir = tmp_path / "seed_0" / "harnesses"
    for node in POOL_NODES:
        script = harnesses_dir / f"{node.id}.sh"
        assert script.is_file()
        completed = subprocess.run(["bash", str(script)], capture_output=True, text=True, check=False)
        assert completed.returncode == 0, f"harness {node.id} failed: {completed.stderr}"
        metrics = parse_metric_lines(completed.stdout)
        assert "ratio" in metrics, f"no ratio metric in stdout for node {node.id}: {completed.stdout}"
        ratio = metrics["ratio"]
        # Python source text compresses with ratio > 1.5
        assert ratio > 1.5, f"expected real compression ratio > 1.5, got {ratio}"


# --- 3. CLI callback spies: 10 runs, matched seeds/caps/pools, early-stop accounting ---


def test_cli_callback_spies_and_output_accounting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Spies verify 10 runs, matched seeds/caps/pools, early stop accounting, and cleaned harnesses."""
    config, _definition, workspace_id, actor_id, _bearer_bytes = _layout(tmp_path)
    repo_root = Path(__file__).resolve().parents[3]
    out_json = tmp_path / "report.json"

    recorded_runs: list[dict[str, Any]] = []

    def spy_run_campaign(
        client: Any,
        store: Any,
        key: str,
        definition: Any,
        *,
        harness_dir: Path,
        actor_id: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        # Track that harness_dir exists while callback is running
        assert harness_dir.is_dir()
        call_record = {
            "key": key,
            "definition": definition,
            "harness_dir": harness_dir,
            "actor_id": actor_id,
            "seed": definition.search.seed,
            "policy": definition.search.policy,
            "budget": definition.search.budget,
        }
        recorded_runs.append(call_record)

        run_idx = len(recorded_runs)
        seed = definition.search.seed
        policy = definition.search.policy

        # Seed 2 greedy: simulate early stop (only 2 trials executed, spent 2.0)
        if seed == 2 and policy == "greedy":
            trials = [
                {"step_id": "a", "execution_status": "completed", "metrics": {"ratio": 3.4}},
                {"step_id": "a1", "execution_status": "completed", "metrics": {"ratio": 4.2}},
            ]
            return {
                "campaign_id": f"camp-s{seed}-{policy}",
                "trials": trials,
                "decisions": [{"index": 0, "step_id": "a"}, {"index": 1, "step_id": "a1"}],
                "best": {"step_id": "a1", "metric": 4.2},
                "spent": 2.0,
            }

        # Normal run: 5 trials executed, spent 5.0
        trials = [
            {"step_id": f"t{i}", "execution_status": "completed", "metrics": {"ratio": 3.0 + i * 0.2}}
            for i in range(5)
        ]
        return {
            "campaign_id": f"camp-s{seed}-{policy}",
            "trials": trials,
            "decisions": [{"index": i, "step_id": f"t{i}"} for i in range(5)],
            "best": {"step_id": "t4", "metric": 3.8},
            "spent": 5.0,
        }

    with _spies(monkeypatch) as calls:
        monkeypatch.setattr(research_campaign, "run_research_campaign", spy_run_campaign)
        exit_code = main(
            [
                "research",
                "compare",
                "--key",
                "OMP-1",
                "--repo-root",
                str(repo_root),
                "--output",
                str(out_json),
                "--client-config",
                str(config),
            ]
        )

    captured = capsys.readouterr()
    assert exit_code == 0, f"CLI exited nonzero: {captured.err}"
    assert captured.err == ""

    # Verify exactly 10 runs (5 seeds x 2 policies)
    assert len(recorded_runs) == 10

    # Group runs by seed
    runs_by_seed: dict[int, dict[str, Any]] = {}
    for run in recorded_runs:
        s = run["seed"]
        pol = run["policy"]
        runs_by_seed.setdefault(s, {})[pol] = run

    assert sorted(runs_by_seed.keys()) == [0, 1, 2, 3, 4]

    for s in range(5):
        greedy_run = runs_by_seed[s]["greedy"]
        fixed_run = runs_by_seed[s]["fixed"]

        # Matched seeds
        assert greedy_run["definition"].search.seed == s
        assert fixed_run["definition"].search.seed == s

        # Matched caps
        assert greedy_run["definition"].search.budget == 5
        assert fixed_run["definition"].search.budget == 5

        # Matched pools: identical trial IDs, costs, parents, and candidate digests
        g_trials = greedy_run["definition"].trials
        f_trials = fixed_run["definition"].trials
        assert [t.id for t in g_trials] == [t.id for t in f_trials]
        assert [t.cost for t in g_trials] == [t.cost for t in f_trials]
        assert [t.parent for t in g_trials] == [t.parent for t in f_trials]
        assert [t.candidate_digest for t in g_trials] == [t.candidate_digest for t in f_trials]

        # Verify temporary harness dirs inside callback are cleaned up
        assert not greedy_run["harness_dir"].exists()
        assert not fixed_run["harness_dir"].exists()

    # Verify output JSON written and printed to stdout
    assert out_json.is_file()
    file_report = json.loads(out_json.read_text(encoding="utf-8"))
    stdout_report = json.loads(captured.out)
    assert file_report == stdout_report

    # Verify report structure
    assert "corpus_hashes" in file_report
    assert "tree" in file_report
    assert "seeds" in file_report
    assert len(file_report["seeds"]) == 5
    assert len(file_report["paired_differences"]) == 5

    # Verify actual early-stop accounting for seed 2 greedy arm
    seed2_report = file_report["seeds"][2]
    assert seed2_report["greedy"]["actual_trials"] == 2
    assert seed2_report["greedy"]["actual_cost"] == 2.0
    assert seed2_report["greedy"]["spent"] == 2.0
    assert len(seed2_report["greedy"]["selected_ids"]) == 2

    # Normal runs accounted 5 trials and 5.0 cost
    seed0_report = file_report["seeds"][0]
    assert seed0_report["greedy"]["actual_trials"] == 5
    assert seed0_report["greedy"]["actual_cost"] == 5.0
    assert seed0_report["fixed"]["actual_trials"] == 5
    assert seed0_report["fixed"]["actual_cost"] == 5.0


def test_cli_missing_args_and_partial_db_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI invalid input exits 2 before constructing client or database."""
    config, _definition, _workspace, _actor, _bearer_bytes = _layout(tmp_path)

    # Missing required --output exits 2
    with _spies(monkeypatch) as calls:
        with pytest.raises(SystemExit) as exc:
            main(["research", "compare", "--key", "OMP-1", "--repo-root", str(tmp_path)])
        assert exc.value.code == 2
        captured = capsys.readouterr()
        _assert_unbuilt(calls, captured.out)

    # Partial DB flags exit 2
    with _spies(monkeypatch) as calls:
        code = main(
            [
                "research",
                "compare",
                "--key",
                "OMP-1",
                "--repo-root",
                str(tmp_path),
                "--output",
                str(tmp_path / "out.json"),
                "--client-config",
                str(config),
                "--db-port",
                "5432",
            ]
        )
        assert code == 2
        captured = capsys.readouterr()
        assert "must all be set or all be omitted" in captured.err
        _assert_unbuilt(calls, captured.out)

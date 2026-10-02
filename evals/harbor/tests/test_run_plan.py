"""Tests for Harbor run plan: arms, price estimate and budget gate."""

from __future__ import annotations

from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from omp_harbor_eval.corpus import task_sha256
from omp_harbor_eval.evidence import canonical_json
from omp_harbor_eval.fixtures import Terminal
from omp_harbor_eval.run_plan import (
    Arm,
    Estimate,
    Experiment,
    ExperimentModel,
    GateResult,
    ModelPrice,
    Plan,
    estimate,
    gate,
    load_plan,
    main,
    model_price,
    schedule,
    usd,
)


def _make_corpus_manifest(
    root: Path,
    tasks_spec: list[dict[str, Any]],
) -> tuple[Path, str]:
    tasks_dict: dict[str, Any] = {}
    for spec in tasks_spec:
        tid = spec["task_id"]
        tdir = root / "evals" / "harbor" / "corpus" / "tasks" / tid
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / "seed.patch").write_text("patch content", encoding="utf-8")
        sha = task_sha256(tdir)
        tasks_dict[tid] = {
            "category": spec.get("category", "localized_bug"),
            "fixture": str(tdir),
            "long_horizon": bool(spec.get("long_horizon", False)),
            "source_paths": spec.get("source_paths", ["file.ts"]),
            "task_sha256": sha,
        }

    body = {
        "base_commit": "abc1234",
        "batches": ["batch_0"],
        "tasks": tasks_dict,
    }
    corpus_sha = hashlib.sha256(canonical_json(body)).hexdigest()
    manifest_doc = {**body, "corpus_sha256": corpus_sha}

    manifest_file = root / "manifest.json"
    manifest_file.write_text(json.dumps(manifest_doc, indent=2), encoding="utf-8")
    return manifest_file, corpus_sha


def _create_test_plan_doc(
    manifest_file: Path,
    corpus_sha: str,
    *,
    arms: list[dict[str, Any]] | None = None,
    experiments: list[dict[str, Any]] | None = None,
    plan_id: str = "plan_test",
    max_input_tokens: int = 100_000,
    max_output_tokens: int = 10_000,
    trial_timeout_s: int = 300,
) -> dict[str, Any]:
    if arms is None:
        arms = [
            {
                "id": "plain_arm",
                "command": "omp {instruction}",
                "terminal": {"agent_end": True},
                "status": "available",
            },
            {
                "id": "exec_arm",
                "command": "/execute {key}",
                "terminal": {"pointer": "/state", "in": ["done"]},
                "status": "available",
            },
        ]
    if experiments is None:
        experiments = [
            {
                "id": "exp1",
                "arms": ["plain_arm"],
                "models": [{"provider": "testprov", "id": "m_base"}],
                "tasks": "all",
                "repetitions": 1,
            }
        ]
    return {
        "plan_id": plan_id,
        "corpus_manifest": str(manifest_file),
        "corpus_sha256": corpus_sha,
        "max_input_tokens": max_input_tokens,
        "max_output_tokens": max_output_tokens,
        "trial_timeout_s": trial_timeout_s,
        "arms": arms,
        "experiments": experiments,
    }


def test_refuses_identical_arms() -> None:
    # 1. Duplicate arm IDs
    term = Terminal(agent_end=True)
    arm1 = Arm(id="arm_a", command="omp {instruction}", terminal=term)
    arm2 = Arm(id="arm_a", command="omp-other {instruction}", terminal=term)
    with pytest.raises(ValueError, match="duplicate arm id"):
        Plan(
            plan_id="p1",
            corpus_manifest="m.json",
            corpus_sha256="a" * 64,
            max_input_tokens=1000,
            max_output_tokens=1000,
            trial_timeout_s=60,
            arms=[arm1, arm2],
            experiments=[],
        )

    # 2. Different IDs but identical configuration
    arm3 = Arm(id="arm_b", command="omp {instruction}", terminal=term)
    with pytest.raises(ValueError, match="arms must differ"):
        Plan(
            plan_id="p1",
            corpus_manifest="m.json",
            corpus_sha256="a" * 64,
            max_input_tokens=1000,
            max_output_tokens=1000,
            trial_timeout_s=60,
            arms=[arm1, arm3],
            experiments=[],
        )


def test_refuses_unpriced_model() -> None:
    cat = {
        "prov": {
            "m_valid": {"cost": {"input": 2, "output": 10}},
            "m_zero_in": {"cost": {"input": 0, "output": 10}},
            "m_neg_out": {"cost": {"input": 2, "output": -5}},
            "m_no_cost": {},
        }
    }

    # Model not found in catalog
    with pytest.raises(ValueError, match="model not found"):
        model_price(cat, "prov", "m_unknown")

    # Base input is 0
    with pytest.raises(ValueError, match="base input price must be positive"):
        model_price(cat, "prov", "m_zero_in")

    # Base output is negative
    with pytest.raises(ValueError, match="base output price must be positive"):
        model_price(cat, "prov", "m_neg_out")

    # Cost object missing
    with pytest.raises(ValueError, match="has no cost object"):
        model_price(cat, "prov", "m_no_cost")


def test_plain_command_and_pointer_terminal() -> None:
    ptr_terminal = {"pointer": "/status", "in": ["completed"]}
    end_terminal = {"agent_end": True}

    # Plain command with pointer terminal is refused
    with pytest.raises(ValueError, match="plain command requires agent_end terminal"):
        Arm(id="arm1", command="omp {instruction}", terminal=ptr_terminal)

    # /execute command with agent_end terminal is refused
    with pytest.raises(ValueError, match="requires a pointer terminal"):
        Arm(id="arm2", command="/execute {key}", terminal=end_terminal)

    # Invalid placeholder is refused
    with pytest.raises(ValueError, match="invalid placeholder"):
        Arm(id="arm3", command="omp {unknown_var}", terminal=end_terminal)

    # Unavailable arm without reason is refused
    with pytest.raises(ValueError, match="unavailable arm requires a reason"):
        Arm(id="arm4", command="omp {instruction}", terminal=end_terminal, status="unavailable", reason="")


def test_refuses_drift_and_sha_mismatch(tmp_path: Path) -> None:
    manifest_file, corpus_sha = _make_corpus_manifest(
        tmp_path,
        [{"task_id": "t1", "long_horizon": False}],
    )
    cat = {"testprov": {"m1": {"cost": {"input": 2, "output": 10}}}}

    # 1. SHA mismatch
    bad_sha_doc = _create_test_plan_doc(manifest_file, "f" * 64)
    plan_file = tmp_path / "plan_bad_sha.json"
    plan_file.write_text(json.dumps(bad_sha_doc), encoding="utf-8")
    with pytest.raises(ValueError, match="corpus_sha256 mismatch"):
        load_plan(plan_file, catalog=cat)

    # 2. Corpus drift: mutate task file after manifest was written
    (tmp_path / "evals" / "harbor" / "corpus" / "tasks" / "t1" / "mutated.txt").write_text("drift")
    valid_doc = _create_test_plan_doc(manifest_file, corpus_sha)
    plan_valid_file = tmp_path / "plan_drift.json"
    plan_valid_file.write_text(json.dumps(valid_doc), encoding="utf-8")
    with pytest.raises(ValueError, match="corpus manifest drift"):
        load_plan(plan_valid_file, catalog=cat)


def test_refuses_zero_trial_experiment(tmp_path: Path) -> None:
    # Corpus with only short tasks (long_horizon=False)
    manifest_file, corpus_sha = _make_corpus_manifest(
        tmp_path,
        [{"task_id": "t1", "long_horizon": False}],
    )
    cat = {
        "testprov": {
            "m1": {
                "cost": {"input": 2, "output": 10},
                "thinking": {"efforts": ["low", "high"]},
            }
        }
    }

    # Experiment asking for tasks="long_horizon" has 0 tasks matched
    doc_lh = _create_test_plan_doc(
        manifest_file,
        corpus_sha,
        experiments=[
            {
                "id": "exp_lh",
                "arms": ["plain_arm"],
                "models": [{"provider": "testprov", "id": "m1"}],
                "tasks": "long_horizon",
                "repetitions": 1,
            }
        ],
    )
    plan_lh_file = tmp_path / "plan_lh.json"
    plan_lh_file.write_text(json.dumps(doc_lh), encoding="utf-8")
    with pytest.raises(ValueError, match="zero available trials"):
        load_plan(plan_lh_file, catalog=cat)

    # Experiment where all trials are unavailable (unavailable arm)
    doc_unavail = _create_test_plan_doc(
        manifest_file,
        corpus_sha,
        arms=[
            {
                "id": "plain_arm",
                "command": "omp {instruction}",
                "terminal": {"agent_end": True},
                "status": "unavailable",
                "reason": "arm disabled for maintenance",
            }
        ],
        experiments=[
            {
                "id": "exp_unavail",
                "arms": ["plain_arm"],
                "models": [{"provider": "testprov", "id": "m1"}],
                "tasks": "all",
                "repetitions": 1,
            }
        ],
    )
    plan_unavail_file = tmp_path / "plan_unavail.json"
    plan_unavail_file.write_text(json.dumps(doc_unavail), encoding="utf-8")
    with pytest.raises(ValueError, match="zero available trials"):
        load_plan(plan_unavail_file, catalog=cat)


def test_unlisted_effort_row_unavailable(tmp_path: Path) -> None:
    manifest_file, corpus_sha = _make_corpus_manifest(
        tmp_path,
        [{"task_id": "t1", "long_horizon": False}],
    )
    cat = {
        "testprov": {
            "m1": {
                "cost": {"input": 2, "output": 10},
                "thinking": {"efforts": ["low", "high"]},
            }
        }
    }

    doc = _create_test_plan_doc(
        manifest_file,
        corpus_sha,
        experiments=[
            {
                "id": "exp1",
                "arms": ["plain_arm"],
                "models": [
                    {"provider": "testprov", "id": "m1", "thinking": {"effort": "low"}},
                    {"provider": "testprov", "id": "m1", "thinking": {"effort": "extreme"}},
                ],
                "tasks": "all",
                "repetitions": 1,
            }
        ],
    )
    plan_file = tmp_path / "plan_effort.json"
    plan_file.write_text(json.dumps(doc), encoding="utf-8")

    plan_obj = load_plan(plan_file, catalog=cat)
    trials = schedule(plan_obj, catalog=cat)

    assert len(trials) == 2
    t_avail = [t for t in trials if t.status == "available"]
    t_unavail = [t for t in trials if t.status == "unavailable"]

    assert len(t_avail) == 1
    assert t_avail[0].model.thinking == {"effort": "low"}
    assert t_avail[0].reason is None

    assert len(t_unavail) == 1
    assert t_unavail[0].model.thinking == {"effort": "extreme"}
    assert t_unavail[0].reason == "effort extreme not listed"


def test_tmp_catalog_model_price_exact_and_long_context() -> None:
    # 1. Base 2/12 + longContext 4/18 -> 4/18, exact false
    cat_lc = {
        "prov": {
            "m_lc": {
                "cost": {
                    "input": 2,
                    "output": 12,
                    "longContext": {
                        "input": 4,
                        "output": 18,
                    },
                }
            }
        }
    }
    p_lc = model_price(cat_lc, "prov", "m_lc")
    assert p_lc.input == Decimal("4")
    assert p_lc.output == Decimal("18")
    assert p_lc.exact is False

    # 2. Base only -> exact true
    cat_base = {
        "prov": {
            "m_base": {
                "cost": {
                    "input": 2,
                    "output": 12,
                }
            }
        }
    }
    p_base = model_price(cat_base, "prov", "m_base")
    assert p_base.input == Decimal("2")
    assert p_base.output == Decimal("12")
    assert p_base.exact is True

    # 3. Exact usd calculation with cache price
    cat_with_cache = {
        "prov": {
            "m_cache": {
                "cost": {
                    "input": 2,
                    "output": 12,
                    "cacheRead": 0.5,
                }
            }
        }
    }
    p_wc = model_price(cat_with_cache, "prov", "m_cache")
    assert p_wc.cache_read == Decimal("0.5")
    assert p_wc.exact is True
    # usd: ((1000 - 400) * 2 + 400 * 0.5 + 200 * 12) / 1e6 = (1200 + 200 + 2400) / 1e6 = 3800 / 1e6 = 0.0038
    val_wc = usd(p_wc, inp=1000, cached=400, out=200)
    assert val_wc == Decimal("3800") / Decimal(1_000_000)

    # 4. Exact usd calculation without cache price (falls back to input price 2)
    p_woc = model_price(cat_base, "prov", "m_base")
    assert p_woc.cache_read == Decimal("2")
    # usd: ((1000 - 400) * 2 + 400 * 2 + 200 * 12) / 1e6 = (1200 + 800 + 2400) / 1e6 = 4400 / 1e6 = 0.0044
    val_woc = usd(p_woc, inp=1000, cached=400, out=200)
    assert val_woc == Decimal("4400") / Decimal(1_000_000)


def test_boundary_cache_read_zero_vs_absent() -> None:
    # Explicit cacheRead: 0 is preserved
    cat_zero = {
        "prov": {
            "m_zero": {
                "cost": {
                    "input": 2,
                    "output": 10,
                    "cacheRead": 0,
                }
            }
        }
    }
    p_zero = model_price(cat_zero, "prov", "m_zero")
    assert p_zero.cache_read == Decimal("0")
    # All input cached (inp=1000, cached=1000, out=0) -> usd = 0
    val_zero = usd(p_zero, inp=1000, cached=1000, out=0)
    assert val_zero == Decimal("0")

    # Absent cacheRead -> falls back to input price (2)
    cat_absent = {
        "prov": {
            "m_absent": {
                "cost": {
                    "input": 2,
                    "output": 10,
                }
            }
        }
    }
    p_absent = model_price(cat_absent, "prov", "m_absent")
    assert p_absent.cache_read == Decimal("2")
    # All input cached (inp=1000, cached=1000, out=0) -> usd = (0 + 1000 * 2 + 0) / 1e6 = 2000 / 1e6
    val_absent = usd(p_absent, inp=1000, cached=1000, out=0)
    assert val_absent == Decimal("2000") / Decimal(1_000_000)


def test_exact_estimate(tmp_path: Path) -> None:
    manifest_file, corpus_sha = _make_corpus_manifest(
        tmp_path,
        [
            {"task_id": "t1", "long_horizon": False},
            {"task_id": "t2", "long_horizon": True},
        ],
    )
    cat = {
        "testprov": {
            "m_exact": {
                "cost": {"input": 2, "output": 12},
                "thinking": {"efforts": ["low"]},
            }
        }
    }

    doc = _create_test_plan_doc(
        manifest_file,
        corpus_sha,
        max_input_tokens=100_000,
        max_output_tokens=10_000,
        experiments=[
            {
                "id": "exp1",
                "arms": ["plain_arm"],
                "models": [{"provider": "testprov", "id": "m_exact", "thinking": "low"}],
                "tasks": "all",
                "repetitions": 2,
            }
        ],
    )
    plan_file = tmp_path / "plan_exact.json"
    plan_file.write_text(json.dumps(doc), encoding="utf-8")

    plan_obj = load_plan(plan_file, catalog=cat)
    est = estimate(plan_obj, catalog=cat)

    assert est.exact is True
    # 2 tasks * 1 arm * 1 model * 2 repetitions = 4 trials
    assert len(est.trials) == 4
    assert est.trial_counts == {"exp1": {"available": 4, "unavailable": 0}}
    # Per trial tokens = 100,000 + 10,000 = 110,000; total tokens = 440,000
    assert est.tokens == 440_000
    # Per trial usd = (100,000 * 2 + 10,000 * 12) / 1e6 = (200,000 + 120,000) / 1e6 = 320,000 / 1e6 = 0.32
    # Total usd for 4 trials = 1.28
    assert est.usd_upper_bound == Decimal("1.28")


def test_gate_three_results() -> None:
    est = Estimate(
        plan_id="p1",
        usd_upper_bound=Decimal("1.28"),
        tokens=440_000,
        exact=True,
        trial_counts={"exp1": {"available": 4, "unavailable": 0}},
        trials=[],
    )

    # 1. within_budget (tier 1)
    budget_ok = {
        "usd": "5.00",
        "tokens": 500_000,
        "wall_clock_seconds": 1800,
        "max_subagents": 0,
    }
    res_ok = gate(est, budget_ok)
    assert res_ok.status == "within_budget"
    assert res_ok.tier == 1
    assert res_ok == "within_budget"

    # 2. refused/spend_beyond_threshold (tier 2)
    budget_low = {
        "usd": "1.00",
        "tokens": 500_000,
        "wall_clock_seconds": 1800,
        "max_subagents": 0,
    }
    res_over = gate(est, budget_low)
    assert res_over.status == "refused/spend_beyond_threshold"
    assert res_over.tier == 2
    assert res_over == "refused/spend_beyond_threshold"

    # 3. refused/no_budget (tier 3)
    res_none = gate(est, None)
    assert res_none.status == "refused/no_budget"
    assert res_none.tier == 3
    assert res_none == "refused/no_budget"

    res_invalid = gate(est, {"usd": "-1.00", "tokens": 10})
    assert res_invalid.status == "refused/no_budget"
    assert res_invalid.tier == 3


def test_cli_estimate_and_gate(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    manifest_file, corpus_sha = _make_corpus_manifest(
        tmp_path,
        [{"task_id": "t1", "long_horizon": False}],
    )
    cat = {
        "testprov": {
            "m_base": {"cost": {"input": 2, "output": 10}},
        }
    }
    cat_file = tmp_path / "models.json"
    cat_file.write_text(json.dumps(cat), encoding="utf-8")

    doc = _create_test_plan_doc(manifest_file, corpus_sha)
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(doc), encoding="utf-8")

    budget_file = tmp_path / "budget.json"
    budget_file.write_text(
        json.dumps({
            "usd": "5.00",
            "tokens": 500_000,
            "wall_clock_seconds": 1800,
            "max_subagents": 0,
        }),
        encoding="utf-8",
    )

    # CLI estimate --plan P --catalog C
    rc = main(["estimate", "--plan", str(plan_file), "--catalog", str(cat_file)])
    assert rc == 0
    captured = capsys.readouterr()
    est_out = json.loads(captured.out)
    assert est_out["plan_id"] == "plan_test"
    assert "usd_upper_bound" in est_out

    # CLI gate --plan P --budget B --catalog C (within budget -> exit 0)
    rc_gate = main([
        "gate",
        "--plan",
        str(plan_file),
        "--budget",
        str(budget_file),
        "--catalog",
        str(cat_file),
    ])
    assert rc_gate == 0
    captured = capsys.readouterr()
    assert "within_budget" in captured.out

    # CLI gate with low budget -> refused -> exit 1
    low_budget_file = tmp_path / "low_budget.json"
    low_budget_file.write_text(
        json.dumps({
            "usd": "0.01",
            "tokens": 500_000,
            "wall_clock_seconds": 1800,
            "max_subagents": 0,
        }),
        encoding="utf-8",
    )
    rc_low = main([
        "gate",
        "--plan",
        str(plan_file),
        "--budget",
        str(low_budget_file),
        "--catalog",
        str(cat_file),
    ])
    assert rc_low == 1
    captured = capsys.readouterr()
    assert "refused/spend_beyond_threshold" in captured.out


def test_real_catalog_model_prices() -> None:
    # Google Gemini 3.8 Flash
    p_flash = model_price(None, "google", "gemini-3.8-flash")
    assert p_flash.input == Decimal("0.75")
    assert p_flash.output == Decimal("3.75")
    assert p_flash.exact is True

    # OpenAI GPT-5.6 Terra (has longContext 4/18)
    p_terra = model_price(None, "openai", "gpt-5.6-terra")
    assert p_terra.input == Decimal("4")
    assert p_terra.output == Decimal("18")
    assert p_terra.exact is False

    # Kimi Code K3
    p_k3 = model_price(None, "kimi-code", "k3")
    assert p_k3.input == Decimal("3")
    assert p_k3.output == Decimal("15")
    assert p_k3.exact is True

    # Kimi Code K3-256K
    p_k3_256k = model_price(None, "kimi-code", "k3-256k")
    assert p_k3_256k.input == Decimal("3")
    assert p_k3_256k.output == Decimal("15")
    assert p_k3_256k.exact is True


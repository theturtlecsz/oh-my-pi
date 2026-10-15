"""Real CLI comparison and receipt/selection evidence on production source (OMP-605)."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from omp_work.jobs.campaign_completion import read_trial_completion
from omp_work.jobs.campaign_decisions import decode, encode, observations_digest
from omp_work.jobs.campaign_definition import load_definition, search_steps
from omp_work.jobs.research_trial import trial_job_id
from omp_work.research.campaign_comparison import SEEDS, compute_student_t
from omp_work.research.search import next_step
from omp_work.research.zlib_source_task import create_seed_task
from test_research_campaign_cli_http import _serve_app
from test_research_campaign_runner import campaign  # noqa: F401

pytest_plugins = ("test_workflow_service",)
pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


def test_real_source_comparison_cli(
    service: SimpleNamespace, campaign: SimpleNamespace, tmp_path: Path,
) -> None:
    """Ten concluded campaigns expose bound scores, bounded spend and replayable choices."""
    root = Path(__file__).resolve().parents[3]
    output = Path(os.environ.get("OMP_605_REPORT", str(tmp_path / "comparison.json")))
    if not output.is_absolute():
        output = root / output
    output.parent.mkdir(parents=True, exist_ok=True)
    bearer = service.capabilities / f"campaign-runner-{campaign.workspace_id}.json"
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith(("PG", "POSTGRES"))
        and k not in ("HOME", "XDG_CONFIG_HOME")
        and not k.endswith(("API_KEY", "AUTH_TOKEN"))
    }
    for name in ("HOME", "XDG_CONFIG_HOME"):
        directory = tmp_path / name.lower()
        directory.mkdir()
        env[name] = str(directory)

    with _serve_app(service) as base_url:
        config = tmp_path / "client.json"
        config.write_text(json.dumps({
            "base_url": base_url, "workspace_id": str(campaign.workspace_id),
            "bearer_file": str(bearer),
        }))
        started = time.perf_counter()
        proc = subprocess.run([
            sys.executable, "-m", "omp_work", "research", "compare",
            "--key", campaign.key, "--repo-root", str(root), "--output", str(output),
            "--client-config", str(config),
            "--config-dir", str(service.config.config_dir),
            "--state-dir", str(service.config.state_dir),
            "--data-dir", str(service.config.data_dir),
            "--db-port", str(service.config.port),
        ], cwd=root, env=env, capture_output=True, text=True, timeout=300, check=False)
        elapsed = time.perf_counter() - started
    assert proc.returncode == 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    report = json.loads(output.read_text())
    assert report == json.loads(proc.stdout)
    assert [row["seed"] for row in report["seeds"]] == list(SEEDS)
    assert report["missing_scores"] == []

    view = campaign.client.research(campaign.key)
    assert len(view.campaigns) == 10
    ids = set()
    sensitivity = None
    for row in report["seeds"]:
        seed = row["seed"]
        greedy_path, fixed_path, hashes, tree_hash = create_seed_task(
            root, seed, tmp_path / "replay",
        )
        assert report["corpus_hashes"][str(seed)] == hashes
        assert report["corpus_tree"][str(seed)] == tree_hash
        assert len(hashes) == 40
        for relative, digest in hashes.items():
            assert hashlib.sha256((root / relative).read_bytes()).hexdigest() == digest
        definitions = [load_definition(path) for path in (greedy_path, fixed_path)]
        assert search_steps(definitions[0]) == search_steps(definitions[1])
        for policy, definition in zip(("greedy", "fixed"), definitions, strict=True):
            arm = row[policy]
            cid = UUID(arm["campaign_id"])
            assert cid not in ids
            ids.add(cid)
            persisted = next(c for c in view.campaigns if c.campaign_id == cid)
            assert persisted.state == "concluded"
            assert persisted.outcome == "supported"
            assert definition.search is not None
            assert definition.search.budget == 5
            tree = search_steps(definition)
            opened: set[str] = set()
            pairs = []
            receipts = []
            replay_seconds = 0.0
            model_calls = 0
            trials = {str(t.trial_id): t for t in view.trials if t.campaign_id == cid}
            assert len(trials) == arm["actual_trials"] == len(arm["decisions"])
            assert 0 < arm["actual_trials"] <= 5
            for index, (trial_result, decision_json) in enumerate(zip(
                arm["trials"], arm["decisions"], strict=True,
            )):
                trial = trials[trial_result["trial_id"]]
                decision = decode(trial.reason)
                assert decision is not None
                assert json.loads(encode(decision)) == decision_json
                assert trial.seed == seed
                assert trial.candidate_digest == tree[decision.step_id]["candidate_digest"]
                assert trial.experiment_spec_sha256 == persisted.spec_sha256
                assert decision.index == decision.observations_count == index
                assert decision.policy == policy
                assert decision.observations_sha256 == observations_digest(pairs)
                tick = time.perf_counter()
                pick, _ = next_step(policy, tree, opened, seed=seed)
                replay_seconds += time.perf_counter() - tick
                assert pick == decision.step_id == trial_result["step_id"]
                # Change an actual observed child's score across its parent's rank.
                # Unexecuted nodes still have no score; nothing is prefilled.
                if policy == "greedy" and index == 2 and sensitivity is None:
                    child = arm["selected_ids"][1]
                    parent = tree[child]["parent_id"]
                    assert parent in opened and child in opened
                    changed = copy.deepcopy(tree)
                    original = tree[child]["metric"]
                    parent_metric = tree[parent]["metric"]
                    changed[child]["metric"] = (
                        parent_metric - 1 if original >= parent_metric else parent_metric + 1
                    )
                    changed_pick, _ = next_step(policy, changed, opened, seed=seed)
                    assert changed_pick != pick
                    sensitivity = {
                        "seed": seed, "decision_index": index, "child": child,
                        "parent": parent, "observed_child_metric": original,
                        "parent_metric": parent_metric,
                        "counterfactual_child_metric": changed[child]["metric"],
                        "persisted_next_choice": pick, "changed_next_choice": changed_pick,
                    }
                completion = read_trial_completion(
                    campaign.client, campaign.store, campaign.key, cid,
                    trial, campaign.actor_id, "ratio",
                )
                assert completion.execution_status == "completed"
                assert completion.infra_error is None
                assert completion.metrics == trial_result["metrics"]
                with campaign.store.transaction(campaign.workspace_id, campaign.actor_id) as cur:
                    job = campaign.store.job_view(cur, campaign.workspace_id, trial_job_id(trial.trial_id))
                assert job["status"] == "sealed"
                assert job["settlement"]["outcome"] == "succeeded"
                assert job["kind"] == "compute"
                model_calls += job["resources"]["model_calls"]
                assert job["settlement"].get("usage", []) == []
                receipts.append({
                    "trial_id": str(trial.trial_id), "job_id": job["job_id"],
                    "evaluator_receipt": job["settlement"]["receipts"][0],
                })
                tree[pick]["metric"] = completion.value
                opened.add(pick)
                pairs.append((str(trial.trial_id), completion.value))
            assert model_calls == 0
            assert arm["actual_cost"] == arm["spent"] == sum(tree[s]["cost"] for s in opened)
            assert 0 < arm["actual_cost"] <= 5
            assert arm["best_metric"] == max(tree[s]["metric"] for s in opened)
            arm["verification"] = {
                "state": persisted.state, "trial_cap": definition.search.budget,
                "receipts": receipts, "selection_replay_seconds": replay_seconds,
                "selection_model_cost_usd": 0, "helper_model_cost_usd": 0,
                "model_calls": model_calls,
                "cost_basis": "Measured compute jobs: zero model-call resources, empty settlement usage, and no workspace usage events; deterministic policy replay. No model evaluator or helper.",
            }
        assert row["difference"] == row["greedy"]["best_metric"] - row["fixed"]["best_metric"]
    with campaign.store.transaction(campaign.workspace_id, campaign.actor_id) as cur:
        cur.execute(
            "SELECT count(*) FROM omp_jobs.usage_events WHERE workspace_id = %s",
            (campaign.workspace_id,),
        )
        usage_count = cur.fetchone()["count"]
    assert usage_count == 0
    assert len(ids) == 10
    assert sensitivity is not None
    stats = compute_student_t([row["difference"] for row in report["seeds"]])
    for key, value in stats.items():
        assert report[key] == value
    report["verification"] = {
        "cli_elapsed_seconds": elapsed, "concluded_campaigns": len(ids),
        "workspace_model_usage_events": usage_count,
        "selection_sensitivity": sensitivity,
        "scope": "Five seeds on one production-source compression task: local decision only, not programme qualification.",
        "adaptive_default": "off; greedy requires explicit search.policy",
        "cost_units": "Trial cost is unit compute reservation, not dollars; selection replay seconds exclude CLI/DB overhead. Model costs are recorded USD charges.",
    }
    output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")

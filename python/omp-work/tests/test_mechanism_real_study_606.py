"""Real OMP-606 study. Frozen inputs; null/loss accepted; no mocked execution."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import subprocess
import sys
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

import pytest

from native_jobs_support import native_jobs  # noqa: F401
from mechanism_cases_606 import CASES
from omp_work.jobs.managed_trial import CAPABILITY, evaluate_candidate
from omp_work.jobs.runner import LocalJailBackend
from omp_work.jobs.store import NativeJobStore
from omp_work.research import mechanism_report, strategy_execution, strategy_protocol
from omp_work.research.managed import verify_managed_trial
from omp_work.research.mechanism_study import MemberRun
from omp_work.research.receipt import ReceiptExpectation, TrialReceipt, load_protocol, verify_receipt
from omp_work.research_mechanisms import freeze
from omp_work.v1.canonical import canonical_json, sha256
from test_jobs_managed_trial import _project_in
from test_mission_links_store import _approve, _link, _seed_budget, _submit
from test_research_campaign_cli_http import _serve_app
from test_research_campaign_runner import campaign  # noqa: F401
from test_workflow_service import service  # noqa: F401

pytest_plugins = ("test_workflow_service",)
pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
REPO = Path(__file__).resolve().parents[3]
COMMAND = (
    "OMP_WORK_POSTGRES_INTEGRATION=1 OMP_606_REPORT=docs/reports/omp-606-study.json "
    "uv run --project python/omp-work --extra dev pytest "
    "python/omp-work/tests/test_mechanism_real_study_606.py"
)


def _bytes(custody, digest):
    blob = gzip.decompress(base64.b64decode(custody["blobs"][digest], validate=True))
    assert hashlib.sha256(blob).hexdigest() == digest
    return blob


def _offline_receipts(document):
    """Recheck bindings without DB, paths, HTTP server or bearer credentials."""
    custody = document["custody"]
    tables = custody["tables"]
    artifacts = {row["artifact_sha256"]: row for row in tables["artifacts"]}
    for digest, row in artifacts.items():
        assert len(_bytes(custody, digest)) == row["manifest"]["size_bytes"]
        assert sha256(row["manifest"]) == row["manifest_sha256"]
    trials = {row["trial_id"]: row for row in tables["trials"]}
    campaigns = {row["campaign_id"]: row for row in tables["campaigns"]}
    components = {row["component_sha256"]: row["descriptor"] for row in tables["components"]}
    jobs = {row["job_id"]: row for row in tables["jobs"]}
    workers = {row["worker_id"]: row for row in tables["workers"]}
    observations = {row["source_ref"]: row["payload"] for row in tables["observations"]}
    verified = {}
    for run in document["runs"]:
        for link in run["result"]["receipt_links"]:
            blob = _bytes(custody, link["receipt_sha256"])
            receipt = TrialReceipt.model_validate_json(blob)
            trial = trials[receipt.trial_id]
            campaign = campaigns[trial["campaign_id"]]
            commitment_sha = campaign["spec"]["evaluation_protocol_id"].removeprefix("generated:")
            commitment = json.loads(_bytes(custody, commitment_sha))
            assert commitment["source"] == run["source_sha256"]
            assert commitment["recipe"] == run["recipe_sha256"]
            assert commitment["task"] == run["manifest_sha256"]
            assert hashlib.sha256(_bytes(custody, commitment["source"])).hexdigest() == run["source_sha256"]
            assert commitment["definition"]["budget"] == run["caps"]["node_budget"]
            assert commitment["selection_attempts"] == run["caps"]["selection_attempts"]
            descriptor = components[trial["evaluator_sha256"]]
            protocol_sha = campaign["spec"]["evaluation_protocol_sha256"]
            protocol = load_protocol(_bytes(custody, protocol_sha), expected_sha256=protocol_sha)
            assert verify_receipt(
                blob, expected_sha256=link["receipt_sha256"],
                expected=ReceiptExpectation(
                    trial_id=trial["trial_id"], campaign_id=trial["campaign_id"],
                    job_id=link["job_id"], candidate_digest=trial["candidate_digest"],
                    evaluator_component_sha256=trial["evaluator_sha256"],
                    evaluator_artifact_sha256=descriptor["artifact_sha256"],
                ), protocol=protocol, protocol_sha256=protocol_sha,
            ) == []
            assert receipt.evaluator_component_sha256 in campaign["compatibility"]["evaluators"]
            assert receipt.protocol_sha256 == run["protocol_sha256"]
            assert receipt.evaluator_artifact_sha256 == run["evaluator_sha256"]
            assert receipt.output_sha256 and receipt.candidate_manifest and receipt.evaluator_manifest
            _bytes(custody, receipt.candidate_digest)
            _bytes(custody, receipt.evaluator_artifact_sha256)
            _bytes(custody, receipt.confirmation_sha256)
            job = jobs[receipt.job_id]
            settlement = job["settlement"]
            assert job["status"] == "sealed" and job["source"] == "native"
            assert settlement["outcome"] == "succeeded"
            assert CAPABILITY in workers[settlement["worker_id"]]["capabilities"]
            issued = [r for r in settlement["receipts"] if r["role"] == "evaluator"]
            assert len(issued) == 1
            assert issued[0]["issuer_component_sha256"] == trial["evaluator_sha256"]
            assert issued[0]["evidence"]["content_digest"] == link["receipt_sha256"]
            assert issued[0]["evidence"]["trust"] == "trusted"
            observation = observations[f"job:{receipt.job_id}"]
            assert observation["receipt_sha256"] == link["receipt_sha256"]
            assert observation["metrics"] == receipt.metrics
            assert observation["verdict"] == receipt.verdict == "qualified"
            verified[receipt.trial_id] = receipt
    for measurement in document["report"]["measurements"]:
        metric = "score" if measurement["domain"] == "simulation" else "ratio"
        values = [verified[trial].metrics[metric] for trial in measurement["trials"]]
        assert measurement["value"] == pytest.approx(sum(values) / len(values))
    return verified


def _selection(policy, case):
    result = strategy_execution.execute_strategy(
        policy["source"].encode(), deepcopy(case["steps"]), case["opened"],
        width=case["width"], allowed_actions=("open",), resource_caps={}, timeout=10,
    )
    assert result.failure is None, asdict(result)
    assert result.selection is not None
    assert strategy_protocol.validate_proposal(
        result.selection, case["steps"], case["opened"],
        allowed_actions=("open",), resource_caps={},
    ) == []
    receipt = {"source_sha256": policy["source_sha256"], "input_sha256": sha256(case),
               "execution": asdict(result), "validation_reasons": []}
    return {**receipt, "receipt_sha256": sha256(receipt)}


def _controlled(document, verified):
    """Designed snapshots measure selection, never supply campaign scores."""
    policies = document["study"]["policies"]
    matrix = []
    for policy in policies:
        linked = next(run["result"]["receipt_links"] for run in document["runs"]
                      if run["family"] == policy["family"] and run["threshold"] == policy["threshold"])
        for name, case in CASES.items():
            matrix.append({"family": policy["family"], "threshold": policy["threshold"],
                           "case": name, "protocol_evaluation": _selection(policy, case),
                           "native_evaluator_receipts": linked})
    picks = {(r["family"], r["threshold"], r["case"]):
             r["protocol_evaluation"]["execution"]["selection"]["step_id"] for r in matrix}
    families = list(dict.fromkeys(p["family"] for p in policies))
    for name, case in CASES.items():
        if case["designed_to_differ"]:
            assert picks[(families[0], 0.5, name)] != picks[(families[1], 0.5, name)]
    fingerprints = {
        family: sha256([picks[(family, t, name)] for t in (0.25, 0.5, 0.75) for name in CASES])
        for family in families
    }
    assert len(set(fingerprints.values())) == 2
    # Same generated source, topology, width and opened set. Only trusted metric
    # changes: a poor optimizer root versus a converged one in the same task.
    run = next(r for r in document["runs"] if r["cohort"] == "development"
               and r["seed"] == 0 and r["threshold"] == 0.5 and r["role"] == "baseline")
    links = run["result"]["receipt_links"]
    low = next(link for link in links if verified[link["trial_id"]].metrics["score"] < 0.5)
    high = next(link for link in links if verified[link["trial_id"]].metrics["score"] > 0.5)
    policy = next(p for p in policies if p["family"] == run["family"] and p["threshold"] == 0.5)
    contrasts = []
    for link in (low, high):
        case = deepcopy(CASES["root_vs_child_trap"])
        case["steps"]["r1"]["metric"] = verified[link["trial_id"]].metrics["score"]
        contrasts.append({"receipt_link": link, "snapshot": case, "evaluation": _selection(policy, case)})
    assert [row["evaluation"]["execution"]["selection"]["step_id"] for row in contrasts] == ["r2", "r1a"]
    return {"cases": CASES, "matrix": matrix, "family_fingerprints": fingerprints,
            "receipt_metric_intervention": contrasts,
            "scope": "Designed snapshots are controlled selection probes, not scored experiment attempts. "
                     "Native evaluator receipts identify real policy runs; only the intervention uses their metrics.",
            "protocol_evaluator_source": Path(strategy_protocol.__file__).read_text(),
            "protocol_evaluator_sha256": hashlib.sha256(Path(strategy_protocol.__file__).read_bytes()).hexdigest()}


def _markdown(document):
    report = document["report"]
    costs = report["costs"]
    counts = {k: sum(c[k] for c in costs) for k in ("selection_attempts", "node_attempts", "spent",
                                                    "selection_seconds", "node_seconds")}
    return f'''# OMP-606 real mechanism study

Command:

```sh
{COMMAND}
```

Frozen before execution: `{document["study"]["digest"]}` at {document["frozen_at"]}.
Both generators emit thresholds 0.25, 0.5, 0.75. Matched two-node budgets
keep 180 trials within existing 200-trial read contract. This is a bounded local
comparison, not evidence about larger search budgets. Seeds 0..4 in each of development,
fresh confirmation, and held-out engineering. Simulation draws use separate cohort
namespaces; engineering compresses 40 sampled production files per seed. Task
manifests, source files and constraints are archived in companion JSON. No code or
threshold revised after confirmation or holdout. Caps: `{json.dumps(document["study"]["caps"])}`.

L1: all actual next-node selections appear under `report.l1` and `runs`.
Controlled ties, gradients and traps pass actual jailed generated code plus protocol
validation; two distinct family fingerprints appear under `controlled`.
A separate intervention changes only trusted receipt metric and changes next choice.

L2: every member contributes; no best-member claim. Overall outcome: **{report["outcome"]}**.
Source-domain claim: `{json.dumps(report["source"])}`.
Held-out claim (no transfer inferred from source): `{json.dumps(report["transfer"])}`.
Protocols differ per task, so pooled CI may be unavailable; per-cohort population
seed deltas and local 95% CI are recorded below. Nulls stay in denominators.

L3: proposed structural change `{document["l3_decision"]["change"]}`: prefer unopened
roots before descending, keeping threshold grid and caps fixed. Fresh confirmation
all-pairs-positive passed: {document["l3_decision"]["confirmation_passed"]}.
No standing-change admission requested; no default activated. Measurements cannot
substitute for recursive confirmation authority. Goal retained.

Actual campaign costs: `{json.dumps(counts)}`. USD/tokens are not metered by this
CPU-only harness; `spent` means unit node reservations, not dollars. Failed and
canceled attempts remain in `runs`/`report.costs`; no fixture scores used.
Local five-seed decision only; not programme qualification (20 consecutive accepted
trials, 72 hours/30 items across both repos, 100 actual attempts remain separate).

Owner options: revise, defer, retire. Next useful trial estimate:
`{json.dumps(report["next_trial"])}`. Revisions require new frozen cohorts; these
held-out results cannot be reused to tune candidates.

Custody: compressed content-addressed artifacts, receipt bytes, native trial/job/
campaign IDs, evaluator descriptors, worker capabilities, settlements and observations
archived before service teardown. Offline receipt bindings and metrics reverified.
Run-manifest/output hashes are receipt attestations; raw ephemeral output files are
not retained by existing managed runner. Candidate archives, evaluator and sealed
inputs permit rerun. Archive is a local service attestation, not independently signed.
No credentials exported. HTTP and PostgreSQL fixtures stop at test teardown.
'''


@pytest.fixture(scope="module")
def report_path(tmp_path_factory):
    return Path(os.environ.get("OMP_606_REPORT", str(tmp_path_factory.mktemp("mechanism-report") / "study.json"))).resolve()


def test_real_frozen_mechanism_study(service, campaign, native_jobs, tmp_path, report_path):
    # Existing native fixture migrates the job store; it supplies no study scores.
    workspace_id, actor_id = campaign.workspace_id, campaign.actor_id
    view = campaign.client.work_item(campaign.key)
    item = {"key": campaign.key, "work_id": str(view.work_id),
            "revision_id": str(view.revision.revision_id)}
    _seed_budget(service, workspace_id, work_id=item["work_id"], revision_id=item["revision_id"],
                 budget={"usd": "5000", "tokens": 500000, "wall_clock_seconds": 500000, "max_subagents": 2000})
    project = _project_in(service, workspace_id)
    mission, _ = _submit(service, workspace_id, project, kind="research.run",
                         requested_capabilities=(CAPABILITY,), budget_policy={
                             "usd": "5000", "tokens": 500000, "wall_clock_seconds": 500000, "max_subagents": 2000})
    _approve(service, workspace_id, mission)
    status, body = _link(service, workspace_id, mission, item["work_id"])
    assert status == 200, body
    bearer = service.capabilities / f"campaign-runner-{workspace_id}.json"
    token = json.loads(bearer.read_text())["token"]
    output = report_path
    frozen = freeze(REPO)
    with _serve_app(service) as base_url:
        client_config = tmp_path / "client.json"
        client_config.write_text(json.dumps({"base_url": base_url, "workspace_id": str(workspace_id),
                                             "bearer_file": str(bearer)}))
        command = [sys.executable, "-m", "omp_work", "research", "mechanisms", "--key", item["key"],
                   "--repo-root", str(REPO), "--output", str(output), "--client-config", str(client_config),
                   "--config-dir", str(service.config.config_dir), "--state-dir", str(service.config.state_dir),
                   "--data-dir", str(service.config.data_dir), "--db-port", str(service.config.port)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=3600)
        assert result.returncode == 0, result.stdout + result.stderr
    document = json.loads(output.read_text())
    assert document["study"]["digest"] == frozen.digest
    assert len(document["runs"]) == 90
    assert len(document["report"]["pairs"]) == 45
    assert document["report"]["ok"], document["report"]["failures"]
    assert document["report"]["failures"] == []
    assert not document["report"]["default_activated"]
    assert not document["report"]["programme_qualification"]
    assert not document["l3_decision"]["admitted"]
    for cohort in ("development", "confirmation", "heldout"):
        runs = [run for run in document["runs"] if run["cohort"] == cohort]
        assert len(runs) == 30
        assert {run["seed"] for run in runs} == set(range(5))
        assert all(run["failure"] is None for run in runs)
        assert all(len(run["result"]["receipt_links"]) == 2 for run in runs)
    verified = _offline_receipts(document)
    assert len(verified) == 180
    store = NativeJobStore(service.config)
    for run in document["runs"]:
        for link in run["result"]["receipt_links"]:
            with store.transaction(workspace_id, actor_id) as cur:
                receipt, reasons = verify_managed_trial(cur, workspace_id, link["trial_id"],
                                                        data_dir=service.config.data_dir)
            assert reasons == []
            assert receipt == verified[link["trial_id"]]
    # P1 regression: candidate/runner-reported scores cannot change a measurement.
    poisoned = deepcopy(document["runs"])
    for run in poisoned:
        run["result"]["best"] = {"metric": 999999}
        run["result"]["candidate_score"] = 999999
        for trial in run["result"]["trials"]:
            trial["metrics"] = {run["metric"]: 999999}
    rebuilt = mechanism_report.build_report(
        [MemberRun(**run) for run in poisoned], study=frozen, store=store,
        workspace_id=workspace_id, actor_id=actor_id, work_id=item["work_id"],
        data_dir=service.config.data_dir, summary={"transfer": "win", "delta": 999999},
    )
    assert rebuilt.measurements == document["report"]["measurements"]
    assert rebuilt.transfer == document["report"]["transfer"]
    document["controlled"] = _controlled(document, verified)
    document["command"] = COMMAND
    # Local paired population estimates supplement task-protocol-specific claims.
    document["cohort_population_estimates"] = {
        cohort: mechanism_report._slice_claim([
            {"seed": seed, "difference": sum(p["difference"] for p in document["report"]["pairs"]
                                             if p["task_id"] in cohort_ids and p["seed"] == seed) / 3}
            for seed in range(5)
        ]) for cohort in ("development", "confirmation", "heldout")
        for cohort_ids in ({task.task_id for task in frozen.tasks if task.cohort == cohort},)
    }
    encoded = json.dumps(document, indent=2) + "\n"
    assert token not in encoded
    assert str(bearer) not in encoded
    output.write_text(encoded)
    markdown = _markdown(document) + "\nPopulation seed estimates (local only):\n\n```json\n" + json.dumps(
        document["cohort_population_estimates"], indent=2) + "\n```\n"
    output.with_suffix(".md").write_text(markdown)
    _replay_archived_inputs(output)


def test_mechanisms_rejects_partial_db_configuration_before_execution(tmp_path):
    output = tmp_path / "refused.json"
    result = subprocess.run(
        [sys.executable, "-m", "omp_work", "research", "mechanisms", "--key", "OMP-1",
         "--repo-root", str(REPO), "--output", str(output), "--config-dir", str(tmp_path)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 2
    assert "must all be set or all be omitted" in result.stderr
    assert not output.exists()


def _replay_archived_inputs(report_path):
    """Archived bytes alone reproduce one simulation and one engineering score."""
    document = json.loads(report_path.read_text())
    verified = _offline_receipts(document)
    custody = document["custody"]
    replays = []
    for domain in ("simulation", "engineering"):
        run = next(run for run in document["runs"] if run["domain"] == domain)
        link = run["result"]["receipt_links"][0]
        receipt = verified[link["trial_id"]]
        result = evaluate_candidate(
            LocalJailBackend(), load_protocol(_bytes(custody, receipt.protocol_sha256)),
            candidate_tar=_bytes(custody, receipt.candidate_digest), candidate_digest=receipt.candidate_digest,
            evaluator_script=_bytes(custody, receipt.evaluator_artifact_sha256),
            confirmation=_bytes(custody, receipt.confirmation_sha256),
            cancel=None,
        )
        assert result.verdict == "qualified", result.failure_reason
        assert result.metrics == receipt.metrics
        assert result.output_sha256 == receipt.output_sha256
        replays.append({"domain": domain, "receipt_link": link, "evaluation": asdict(result)})
    document["offline_replays"] = replays
    controlled = document["controlled"]
    probes = [row["protocol_evaluation"] for row in controlled["matrix"]]
    probes.extend(row["evaluation"] for row in controlled["receipt_metric_intervention"])
    document["controlled_costs"] = {
        "selection_attempts": len(probes),
        "selection_seconds": sum(probe["execution"]["usage"]["duration"] for probe in probes),
        "scored_experiment_attempts": 0,
    }
    report_path.write_text(json.dumps(document, indent=2) + "\n")
    with report_path.with_suffix(".md").open("a") as markdown:
        markdown.write(
            "\nOffline replay: archived candidate/evaluator/protocol/sealed inputs reproduced exact "
            "metrics and output hashes for one simulation and one engineering trial, without "
            "DB, HTTP, bearer or source-file access. Two extra candidate/evaluator/negative-control "
            "replays are verification costs, excluded from population inference.\n\n"
            f"Controlled probe costs: `{json.dumps(document['controlled_costs'])}`.\n"
        )


def test_mechanisms_requires_caller_mission_authority(service, campaign, native_jobs, tmp_path):
    """Opt-in CLI cannot grant itself an approved research mission or compute job."""
    output = tmp_path / "unauthorized.json"
    bearer = service.capabilities / f"campaign-runner-{campaign.workspace_id}.json"
    with _serve_app(service) as base_url:
        client_config = tmp_path / "client.json"
        client_config.write_text(json.dumps({"base_url": base_url, "workspace_id": str(campaign.workspace_id),
                                             "bearer_file": str(bearer)}))
        result = subprocess.run(
            [sys.executable, "-m", "omp_work", "research", "mechanisms", "--key", campaign.key,
             "--repo-root", str(REPO), "--output", str(output), "--client-config", str(client_config),
             "--config-dir", str(service.config.config_dir), "--state-dir", str(service.config.state_dir),
             "--data-dir", str(service.config.data_dir), "--db-port", str(service.config.port)],
            capture_output=True, text=True, timeout=60,
        )
    assert result.returncode == 1
    assert "mission_not_authorized" in result.stderr
    assert not output.exists()
    with campaign.store.transaction(campaign.workspace_id, campaign.actor_id) as cur:
        cur.execute("SELECT count(*) AS n FROM omp_jobs.jobs WHERE workspace_id=%s", (campaign.workspace_id,))
        assert cur.fetchone()["n"] == 0

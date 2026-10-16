"""Opt-in CLI orchestration. No authority creation or default activation.

This five-seed study extends the small s06 simulation cohorts without changing
research defaults. All inputs are frozen before the first native campaign.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import random
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from psycopg import sql

from omp_work.jobs.store import NativeJobStore
from omp_work.operations.config import OperationsConfig
from omp_work.research import mechanism_report, mechanism_study, mechanism_tasks
from omp_work.research.custody import artifact_path
from omp_work.v1.canonical import canonical_json
from omp_work.v1.client import WorkClient


def freeze(repo_root: Path) -> mechanism_study.FrozenStudy:
    """Five independent draws per cohort, with disjoint cohort namespaces.

    Reuse the simulation task packager, candidate computation and sealed
    evaluator. Only the preregistered problem draw expands beyond s06's 3/2
    seeds. No module constants, default cohorts or production rules are changed.
    """
    tasks = []
    for cohort in ("development", "confirmation"):
        for seed in range(5):
            rng = random.Random(f"OMP-606-s07:bounded-read.v1:{cohort}:{seed}")
            problem = {
                "kind": "shifted-quadratic",
                "weights": [round(rng.uniform(1, 3), 6) for _ in range(4)],
                "shift": [round(rng.uniform(-5, 5), 6) for _ in range(4)],
                "start": [0.0] * 4,
            }
            tasks.append(mechanism_tasks._freeze(
                "simulation", cohort, seed, canonical_json(problem).encode(), problem,
                b"", "real", {"generator": "OMP-606-s07:bounded-read.v1", "cohort_seed": seed},
            ))
    tasks.extend(mechanism_tasks.engineering_cohort(repo_root))
    # Existing per-item research view exposes 200 trials. Ninety campaigns at
    # two nodes fit that contract (180); five nodes would make later trials
    # invisible. This matched opt-in study budget changes no research defaults.
    return mechanism_study.freeze_study(tasks=tasks, caps={"node_budget": 2, "selection_attempts": 8})


def archive(ctx: mechanism_study.CampaignContext) -> dict:
    """Export credential-free native provenance and custody bytes before cleanup.

    These are local-service attestations, not independently signed receipts.
    Their hashes permit offline binding/score verification, not proof that an
    untrusted party could never rewrite the whole archive.
    """
    tables = {
        "campaigns": ("omp_research", "campaigns", "*"),
        "trials": ("omp_research", "trials", "*"),
        "components": ("omp_research", "components", "*"),
        "observations": ("omp_research", "observations", "*"),
        "artifacts": ("omp_research", "artifacts", "*"),
        "jobs": ("omp_jobs", "jobs", "job_id, source, status, settlement"),
        "workers": ("omp_jobs", "workers", "worker_id, capabilities"),
    }
    evidence = {}
    with ctx.store.transaction(ctx.workspace_id, ctx.actor_id) as cur:
        for name, (schema, table, columns) in tables.items():
            cur.execute(sql.SQL("SELECT {} FROM {}.{} WHERE workspace_id=%s").format(
                sql.SQL(columns), sql.Identifier(schema), sql.Identifier(table),
            ), (ctx.workspace_id,))
            evidence[name] = cur.fetchall()
    blobs = {}
    for row in evidence["artifacts"]:
        digest = row["artifact_sha256"]
        blob = artifact_path(ctx.data_dir, ctx.workspace_id, digest).read_bytes()
        if hashlib.sha256(blob).hexdigest() != digest or len(blob) != row["manifest"]["size_bytes"]:
            raise ValueError("archive custody mismatch")
        blobs[digest] = base64.b64encode(gzip.compress(blob, mtime=0)).decode("ascii")
    return {"encoding": "gzip+base64", "tables": evidence, "blobs": blobs,
            "trust": "local native service verified before cleanup; no independent signature"}


def run_study(
    client: WorkClient, config: OperationsConfig, actor_id: UUID,
    key: str, repo_root: Path, harness_dir: Path,
) -> dict:
    item = client.work_item(key)
    study = freeze(repo_root)
    frozen_at = datetime.now(UTC).isoformat()
    ctx = mechanism_study.CampaignContext(
        client, NativeJobStore(config), key, item.workspace_id, actor_id,
        item.work_id, harness_dir, config.data_dir,
    )
    # Sequential exposure: no confirmation or held-out result can revise code.
    runs = mechanism_study.execute(study, ctx)
    report = mechanism_report.build_report(
        runs, study=study, store=ctx.store, workspace_id=ctx.workspace_id,
        actor_id=actor_id, work_id=ctx.work_id, data_dir=config.data_dir,
    )
    result = asdict(report)
    result["history"] = report.history.snapshot()
    result["default_activated"] = False
    confirmation_ids = {task.task_id for task in study.tasks if task.cohort == "confirmation"}
    confirmation = [pair for pair in report.pairs if pair["task_id"] in confirmation_ids]
    # This is the measured decision, not a recursive_confirmation authority proof.
    # Even positive results cannot manufacture a standing-change grant here.
    l3 = {
        "change": f"{mechanism_study.BASELINE_FAMILY} -> {mechanism_study.CANDIDATE_FAMILY}",
        "rule_change": "prefer unopened roots before descending, keeping the threshold grid and caps fixed",
        "pass_rule": "all-pairs-positive.v1",
        "confirmation_pairs": confirmation,
        "confirmation_passed": bool(confirmation) and all(
            pair["difference"] is not None and pair["difference"] > 0 for pair in confirmation
        ),
        "admitted": False,
        "reason": "measurement only; no standing-change admission requested",
    }
    return {
        "schema": "omp-606-real-study.v1", "frozen_at": frozen_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "workspace_id": str(ctx.workspace_id), "actor_id": str(actor_id), "work_id": str(item.work_id),
        "study": {"digest": study.digest, "spec": json.loads(study.spec_json),
                  "spec_sha256": study.spec_sha256, "caps": study.caps,
                  "cohorts": study.cohort_sha256,
                  "policies": [asdict(policy) for policy in study.policies],
                  "tasks": [task.manifest for task in study.tasks]},
        "runs": [asdict(run) for run in runs], "report": result,
        "l3_decision": l3, "custody": archive(ctx),
    }

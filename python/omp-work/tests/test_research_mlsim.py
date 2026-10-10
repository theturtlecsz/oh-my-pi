"""Tests for ML and simulation experiment support (R09, OMP-316)."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
import subprocess
from typing import Any

import pytest

from omp_work.jobs.runner import BackendIncompatible
import omp_work.research.mlsim as mlsim
from omp_work.research.mlsim import (
    SimulationManifest,
    TrainingManifest,
    adapt_metrics,
    artifact_sha256,
    candidate_binds,
    check_splits,
    classify_checkpoint,
    manifest_bytes,
    repeated_analysis,
    run_manifest,
    suggest,
)


def _unshare_probe_fails() -> bool:
    try:
        proc = subprocess.run(
            ["unshare", "--user", "--map-root-user", "--net", "--mount", "true"],
            check=False,
            capture_output=True,
            timeout=5,
        )
        return proc.returncode != 0
    except (OSError, subprocess.SubprocessError):
        return True


_skip_jail = pytest.mark.skipif(_unshare_probe_fails(), reason="unshare probe failed")


def test_check_splits() -> None:
    rows = [
        {"entity": "E1", "time": 100, "features": {"x": 1}, "label": 10, "split": "train"},
        {"entity": "E1", "time": 200, "features": {"x": 2}, "label": 20, "split": "test"},
        {"entity": "E2", "time": 250, "features": {"x": 3}, "label": 30, "split": "train"},
        {"entity": "E3", "time": 50, "features": {"f1": 99, "f2": 5}, "label": 99, "split": "train"},
        {"entity": "E4", "time": 210, "features": {"x": 4}, "label": 40, "split": "test"},
    ]
    codes = check_splits(rows)
    assert len(codes) == 3
    assert "entity_overlap:E1" in codes
    assert "future_leak:E2" in codes
    assert "label_in_features:E3" in codes


def test_repeated_analysis_misleading_aggregate() -> None:
    baseline = [1, 1, 1]
    candidate = [0, 0, 9]
    res = repeated_analysis(baseline, candidate)

    assert res["misleading_aggregate"] is True
    assert res.misleading_aggregate is True
    assert res["median_gain"] < 0
    assert res.median_gain < 0
    assert res["median_gain"] == -1.0
    assert res["mean_gain"] == 2.0
    assert res["medians"]["baseline"] == 1.0
    assert res["medians"]["candidate"] == 0.0


def test_classify_checkpoint() -> None:
    m1 = TrainingManifest(data_sha256="sha-a", params={"lr": 0.01, "step": 10}, seed=42, step=10, command=["echo"])
    m2 = TrainingManifest(data_sha256="sha-b", params={"lr": 0.01, "step": 10}, seed=42, step=10, command=["echo"])
    m3 = TrainingManifest(data_sha256="sha-a", params={"lr": 0.01, "step": 20}, seed=42, step=20, command=["echo"])

    assert classify_checkpoint(None, m1) == "interrupted"
    assert classify_checkpoint(m1, m2) == "incompatible"
    assert classify_checkpoint(m1, m3) == "resumed"


def test_adapt_metrics() -> None:
    with pytest.raises(ValueError, match="metric_not_finite"):
        adapt_metrics({"loss": float("nan")})

    with pytest.raises(ValueError, match="metric_not_finite"):
        adapt_metrics({"loss": float("inf")})

    valid = adapt_metrics({"loss": 0.25, "acc": 1})
    assert valid == {"loss": 0.25, "acc": 1.0}


def test_manifest_bytes_stability_and_difference() -> None:
    t1 = TrainingManifest(
        data_sha256="hash1",
        params={"lr": 0.01},
        seed=1,
        step=0,
        command=["run"],
        device="cpu",
        confirmation_sha256=None,
    )
    t2 = TrainingManifest(
        data_sha256="hash1",
        params={"lr": 0.01},
        seed=1,
        step=0,
        command=["run"],
        device="cpu",
        confirmation_sha256=None,
    )
    s1 = SimulationManifest(
        data_sha256="hash1",
        params={"lr": 0.01},
        seed=1,
        step=0,
        command=["run"],
        device="cpu",
        confirmation_sha256=None,
    )

    b_t1 = manifest_bytes(t1)
    b_t2 = manifest_bytes(t2)
    b_s1 = manifest_bytes(s1)

    assert b_t1 == b_t2
    assert b_t1 != b_s1
    assert t1.kind == "training"
    assert s1.kind == "simulation"


def test_suggest_and_no_optuna_import() -> None:
    space = {"a": [0, 1], "b": [2]}
    s0 = suggest(space, 0)
    assert s0["optimizer"] == "optuna-bridge.v1"
    assert s0["space"] == space
    assert s0["index"] == 0
    assert s0["params"] == {"a": 0, "b": 2}

    s1 = suggest(space, 1)
    assert s1["optimizer"] == "optuna-bridge.v1"
    assert s1["index"] == 1
    assert s1["params"] == {"a": 1, "b": 2}

    s2 = suggest(space, 2)
    assert s2["optimizer"] == "optuna-bridge.v1"
    assert s2["index"] == 2
    assert s2["params"] == s0["params"]

    source = Path(mlsim.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "optuna" not in alias.name
        elif isinstance(node, ast.ImportFrom):
            assert node.module is None or "optuna" not in node.module
    assert "import optuna" not in source
    assert "from optuna" not in source


def test_candidate_binds_absent_confirmation(tmp_path: Path) -> None:
    conf_path = tmp_path / "confirmation.bin"
    conf_path.write_bytes(b"private-key")
    other_file = tmp_path / "data.txt"
    other_file.write_text("data", encoding="utf-8")

    binds = candidate_binds(
        conf_path,
        argv=["/bin/echo", "param", str(conf_path)],
        ro_files=[other_file, conf_path],
        env={"PATH": "/usr/bin:/bin", "CONF": str(conf_path)},
    )
    assert conf_path not in binds
    assert str(conf_path) not in binds
    assert str(conf_path) not in binds["ro_files"]
    assert str(conf_path) not in binds.ro_files
    assert str(conf_path) not in binds["argv"]
    assert str(conf_path) not in binds.argv
    assert not any(str(conf_path) in v for v in binds["env"].values())
    assert str(other_file) in binds["ro_files"]


def test_artifact_sha256() -> None:
    digest = artifact_sha256(b"secret")
    assert digest == hashlib.sha256(b"secret").hexdigest()
    assert digest == "2bb80d537b1da3e38bd30361aa855686bde0eacd7162fef6a25fe97bf527a25b"


@_skip_jail
def test_jail_execution(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()

    # 1. argv ["/bin/echo", "metric=1"] completes twice with the same output
    cpu_manifest = TrainingManifest(command=["/bin/echo", "metric=1"], device="cpu")
    r1 = run_manifest(cpu_manifest, workdir)
    r2 = run_manifest(cpu_manifest, workdir)
    assert r1.status == "completed"
    assert r2.status == "completed"
    assert r1.output == r2.output
    assert b"metric=1" in r1.output

    # 2. device gpu raises backend_incompatible and a following cpu echo still completes
    gpu_manifest = TrainingManifest(command=["/bin/echo", "metric=1"], device="gpu")
    with pytest.raises(BackendIncompatible) as exc_info:
        run_manifest(gpu_manifest, workdir)
    assert exc_info.value.code == "backend_incompatible"

    r3 = run_manifest(cpu_manifest, workdir)
    assert r3.status == "completed"
    assert r3.output == r1.output

    # 3. a script under workdir that reads a sibling file secret.txt containing host-secret does not put host-secret in the output
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("host-secret", encoding="utf-8")
    script = workdir / "read_secret.sh"
    script.write_text(
        "#!/bin/sh\ncat ../secret.txt 2>&1 || true\ncat /work/../secret.txt 2>&1 || true\n",
        encoding="utf-8",
    )
    script.chmod(0o755)

    script_manifest = TrainingManifest(command=["/bin/sh", "/work/read_secret.sh"], device="cpu")
    r4 = run_manifest(script_manifest, workdir)
    assert b"host-secret" not in r4.output

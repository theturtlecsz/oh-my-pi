"""Verifier sandbox config transport and closed worker environment contracts."""

from pathlib import Path

from omp_work.egress_sandbox import _mount_config
from omp_work.egress_sandbox_helper import _worker_profile_env


BASE_ENV = {
    "PATH": "/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "TERM": "xterm",
    "TZ": "UTC",
    "OMP_TASK_TOKEN": "task-token",
    "OMP_WORK_URL": "http://127.0.0.1:8000",
}
PYTHON_ENV = {"PYTHONPATH": "/opt/runtime", "PYTHONSAFEPATH": "1"}


def test_defaults_omit_verifier_config_and_python_environment() -> None:
    assert _mount_config() == {}
    env = {**BASE_ENV, **PYTHON_ENV, "HOME": "/home/worker"}
    assert _worker_profile_env({"env": env}) == BASE_ENV


def test_verifier_config_serializes_paths_and_allows_only_python_environment() -> None:
    config = _mount_config(
        worktree_readonly=True,
        ro_dirs=(Path("/repo/.git"), "/opt/runtime"),
        audit_dir=Path("/repo/.omp"),
        verifier_runtime=True,
    )
    assert config == {
        "worktree_readonly": True,
        "ro_dirs": ["/repo/.git", "/opt/runtime"],
        "audit_dir": "/repo/.omp",
        "verifier_runtime": True,
    }
    env = {**BASE_ENV, **PYTHON_ENV, "HOME": "/home/worker"}
    assert _worker_profile_env({**config, "env": env}) == {**BASE_ENV, **PYTHON_ENV}


def test_verifier_drops_empty_python_values_and_preserves_model_manifest_environment() -> None:
    model_env = {
        "OMP_MODEL_PROXY_URL": "http://127.0.0.1:9000",
        "OMP_MODEL_TOKEN": "model-token",
        "OMP_WORKER_MANIFEST": "/work/manifest.json",
    }
    env = {
        **BASE_ENV,
        **model_env,
        "PYTHONPATH": "",
        "PYTHONSAFEPATH": "",
        "HOME": "/home/worker",
        "OTHER": "excluded",
    }
    assert _worker_profile_env({"env": env, "verifier_runtime": True}) == {**BASE_ENV, **model_env}

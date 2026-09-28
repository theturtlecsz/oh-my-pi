"""Task environment: compose worker settings, never the host environment."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from harbor.models.task.task import Task

from omp_harbor_eval.isolation import load_compose
from omp_harbor_eval.task_env import load_task

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
HOME = "/home/agent"
WORKSERVICE_URL = "http://127.0.0.1:8080"
MODEL_URL = "http://127.0.0.1:9090/v1"
WORKING_DIR = "/workspace"
WORKSERVICE_IMAGE = "omp-workservice:dev"
HOST_HOME = "/tmp/host-home"
HOST_URL = "http://host.invalid/v1"


def _diverge_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", HOST_HOME)
    monkeypatch.setenv("OMP_WORKSERVICE_URL", "http://host.invalid:8080")
    monkeypatch.setenv("OMP_SCRIPTED_MODEL_URL", HOST_URL)


def _assert_settings(loaded: Any, fixture_id: str) -> None:
    assert loaded.fixture.id == fixture_id
    assert loaded.fixture.directory.name == fixture_id
    assert loaded.home == HOME
    assert loaded.workservice_url == WORKSERVICE_URL
    assert loaded.model_url == MODEL_URL
    assert loaded.working_dir == WORKING_DIR
    assert loaded.workservice_image == WORKSERVICE_IMAGE


def _copy_f1(tmp_path: Path, mutate: Callable[[dict[str, Any]], None]) -> Path:
    root = tmp_path / "f1"
    shutil.copytree(FIXTURES / "f1", root)
    path = root / "environment" / "docker-compose.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(document)
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return root / "environment"


@pytest.mark.parametrize("fixture_id", ["f1", "f2"])
def test_load_task_reads_compose_not_the_host_env(monkeypatch: pytest.MonkeyPatch, fixture_id: str) -> None:
    _diverge_host(monkeypatch)
    environment = FIXTURES / fixture_id / "environment"
    loaded = load_task(environment)
    _assert_settings(loaded, fixture_id)
    assert loaded.fixture.directory == FIXTURES / fixture_id
    assert loaded.compose == load_compose(environment / "docker-compose.yaml")
    assert loaded.compose["services"]["worker"]["environment"]["HOME"] == HOME


def test_privileged_worker_raises(tmp_path: Path) -> None:
    def mutate(document: dict[str, Any]) -> None:
        document["services"]["worker"]["privileged"] = True

    with pytest.raises(ValueError, match="worker is privileged"):
        load_task(_copy_f1(tmp_path, mutate))


def test_topology_violations_are_all_named(tmp_path: Path) -> None:
    def mutate(document: dict[str, Any]) -> None:
        worker = document["services"]["worker"]
        worker["privileged"] = True
        worker["network_mode"] = "bridge"

    with pytest.raises(ValueError) as caught:
        load_task(_copy_f1(tmp_path, mutate))
    message = str(caught.value)
    assert "rule 1: worker network_mode must be 'service:workservice', got 'bridge'" in message
    assert "rule 2: worker is privileged" in message


@pytest.mark.parametrize(
    ("key", "remove"),
    [
        ("HOME", lambda document: document["services"]["worker"]["environment"].pop("HOME")),
        (
            "OMP_WORKSERVICE_URL",
            lambda document: document["services"]["worker"]["environment"].pop("OMP_WORKSERVICE_URL"),
        ),
        (
            "OMP_SCRIPTED_MODEL_URL",
            lambda document: document["services"]["worker"]["environment"].pop("OMP_SCRIPTED_MODEL_URL"),
        ),
        ("working_dir", lambda document: document["services"]["worker"].pop("working_dir")),
        ("image", lambda document: document["services"]["workservice"].pop("image")),
    ],
)
def test_missing_setting_names_the_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    remove: Callable[[dict[str, Any]], None],
) -> None:
    _diverge_host(monkeypatch)
    with pytest.raises(ValueError, match=rf"missing {key}(?:,|$)") as caught:
        load_task(_copy_f1(tmp_path, remove))
    message = str(caught.value)
    assert HOST_HOME not in message
    assert "host.invalid" not in message


def test_environment_list_form_ignores_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _diverge_host(monkeypatch)

    def mutate(document: dict[str, Any]) -> None:
        document["services"]["worker"]["environment"] = [
            "HOME=/home/agent",
            "OMP_WORKSERVICE_URL=http://127.0.0.1:8080",
            "OMP_SCRIPTED_MODEL_URL=http://127.0.0.1:9090/v1",
        ]

    _assert_settings(load_task(_copy_f1(tmp_path, mutate)), "f1")


def test_null_and_bare_env_keys_are_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", HOST_HOME)

    def null_home(document: dict[str, Any]) -> None:
        document["services"]["worker"]["environment"]["HOME"] = None

    with pytest.raises(ValueError, match=r"missing HOME(?:,|$)") as caught:
        load_task(_copy_f1(tmp_path, null_home))
    assert HOST_HOME not in str(caught.value)

    def bare_home(document: dict[str, Any]) -> None:
        document["services"]["worker"]["environment"] = [
            "HOME",
            "OMP_WORKSERVICE_URL=http://127.0.0.1:8080",
            "OMP_SCRIPTED_MODEL_URL=http://127.0.0.1:9090/v1",
        ]

    bare = tmp_path / "bare"
    bare.mkdir()
    with pytest.raises(ValueError, match=r"missing HOME(?:,|$)") as caught:
        load_task(_copy_f1(bare, bare_home))
    assert HOST_HOME not in str(caught.value)


def test_harbor_tasks_load() -> None:
    for fixture_id in ("f1", "f2"):
        task = Task(FIXTURES / fixture_id)
        assert task.paths.environment_dir.is_dir()

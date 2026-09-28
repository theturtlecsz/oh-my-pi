"""Tests defending build recipe presence for Harbor fixtures and main service."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from omp_harbor_eval.build_recipe import (
    check_fixture_build_recipe,
    get_known_build_recipes,
    main,
)

HARBOR_DIR = Path(__file__).resolve().parent.parent
FIXTURES_DIR = HARBOR_DIR / "fixtures"
BUILD_SCRIPT = HARBOR_DIR / "scripts" / "build-images.sh"


def test_build_script_exists_and_is_executable() -> None:
    assert BUILD_SCRIPT.is_file(), f"{BUILD_SCRIPT} does not exist"
    assert os.access(BUILD_SCRIPT, os.X_OK), f"{BUILD_SCRIPT} is not executable"


def test_get_known_build_recipes_covers_required_images() -> None:
    recipes = get_known_build_recipes(HARBOR_DIR)
    for expected in (
        "omp-verifier:dev",
        "omp-workservice:dev",
        "omp-agent:dev",
        "omp-f1-agent:dev",
        "omp-f2-agent:dev",
    ):
        assert expected in recipes, f"missing build recipe for {expected}"
        assert recipes[expected].is_file(), f"recipe file {recipes[expected]} does not exist"


def test_fixture_f1_has_valid_build_recipe() -> None:
    f1_dir = FIXTURES_DIR / "f1"
    violations = check_fixture_build_recipe(f1_dir, harbor_dir=HARBOR_DIR)
    assert violations == [], f"f1 has unexpected build recipe violations: {violations}"


def test_fixture_f2_has_valid_build_recipe() -> None:
    f2_dir = FIXTURES_DIR / "f2"
    violations = check_fixture_build_recipe(f2_dir, harbor_dir=HARBOR_DIR)
    assert violations == [], f"f2 has unexpected build recipe violations: {violations}"


def test_fails_when_compose_references_unrecipe_image(tmp_path: Path) -> None:
    fixture_copy = tmp_path / "f1"
    shutil.copytree(FIXTURES_DIR / "f1", fixture_copy)

    compose_file = fixture_copy / "environment" / "docker-compose.yaml"
    data = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    data["services"]["worker"]["image"] = "unknown-missing-image:dev"
    compose_file.write_text(yaml.safe_dump(data), encoding="utf-8")

    violations = check_fixture_build_recipe(fixture_copy, harbor_dir=HARBOR_DIR)
    assert len(violations) == 1
    assert "unknown-missing-image:dev" in violations[0]
    assert "no build recipe" in violations[0]


def test_fails_when_main_service_lacks_dockerfile_and_docker_image(tmp_path: Path) -> None:
    fixture_copy = tmp_path / "f1"
    shutil.copytree(FIXTURES_DIR / "f1", fixture_copy)

    # Remove Dockerfile
    dockerfile = fixture_copy / "environment" / "Dockerfile"
    if dockerfile.is_file():
        dockerfile.unlink()

    # Remove docker_image from task.toml
    task_toml = fixture_copy / "task.toml"
    lines = task_toml.read_text(encoding="utf-8").splitlines()
    filtered = [line for line in lines if not line.strip().startswith("docker_image")]
    task_toml.write_text("\n".join(filtered) + "\n", encoding="utf-8")

    violations = check_fixture_build_recipe(fixture_copy, harbor_dir=HARBOR_DIR)
    assert any("lacks main service definition" in v for v in violations)


def test_fails_when_main_service_docker_image_has_no_recipe(tmp_path: Path) -> None:
    fixture_copy = tmp_path / "f1"
    shutil.copytree(FIXTURES_DIR / "f1", fixture_copy)

    # Remove Dockerfile so it relies on docker_image
    dockerfile = fixture_copy / "environment" / "Dockerfile"
    if dockerfile.is_file():
        dockerfile.unlink()

    # Set unknown docker_image in task.toml
    task_toml = fixture_copy / "task.toml"
    lines = task_toml.read_text(encoding="utf-8").splitlines()
    updated: list[str] = []
    for line in lines:
        if line.strip().startswith("docker_image"):
            updated.append('docker_image = "nonexistent-main:dev"')
        else:
            updated.append(line)
    task_toml.write_text("\n".join(updated) + "\n", encoding="utf-8")

    violations = check_fixture_build_recipe(fixture_copy, harbor_dir=HARBOR_DIR)
    assert any("nonexistent-main:dev" in v and "no build recipe" in v for v in violations)


def test_cli_main_exits_zero_on_shipped_fixtures() -> None:
    assert main([]) == 0


def test_cli_subprocess_exits_zero() -> None:
    repo_root = HARBOR_DIR.parent.parent
    src_dir = str(HARBOR_DIR / "src")
    env = {**os.environ, "PYTHONPATH": src_dir}
    result = subprocess.run(
        [sys.executable, "-m", "omp_harbor_eval.build_recipe"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == ""


def test_cli_subprocess_fails_on_broken_fixture(tmp_path: Path) -> None:
    fixture_copy = tmp_path / "broken"
    shutil.copytree(FIXTURES_DIR / "f1", fixture_copy)

    # Corrupt compose image
    compose_file = fixture_copy / "environment" / "docker-compose.yaml"
    data = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    data["services"]["worker"]["image"] = "bad-image:latest"
    compose_file.write_text(yaml.safe_dump(data), encoding="utf-8")

    repo_root = HARBOR_DIR.parent.parent
    src_dir = str(HARBOR_DIR / "src")
    env = {**os.environ, "PYTHONPATH": src_dir}
    result = subprocess.run(
        [sys.executable, "-m", "omp_harbor_eval.build_recipe", str(fixture_copy)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 1
    assert "bad-image:latest" in result.stdout

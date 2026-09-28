"""Every image a fixture references must have a repository build recipe.

The contract is the fixture as Harbor sees it: all images in
``environment/docker-compose.yaml`` and Harbor's ``main`` service
(``task.toml [environment].docker_image`` or ``environment/Dockerfile``) must
resolve to something ``scripts/build-images.sh`` produces, the workservice must
start the real WorkService, and no ``evals/harbor`` entrypoint may define its own
HTTP routes in place of it.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from omp_harbor_eval.build_recipe import (
    build_recipes,
    check_fixture,
    compose_images,
    main,
    parse_build_script,
    task_env_docker_image,
)

REPO = Path(__file__).resolve().parents[3]
HARBOR = REPO / "evals" / "harbor"
FIXTURES = HARBOR / "fixtures"
BUILD_SCRIPT = HARBOR / "scripts" / "build-images.sh"


def _copy_fixture(tmp_path: Path, fixture_id: str) -> Path:
    root = tmp_path / "fixtures"
    shutil.copytree(FIXTURES / fixture_id, root / fixture_id)
    return root / fixture_id


@pytest.mark.parametrize("fixture_id", ["f1", "f2"])
def test_fixture_references_only_images_the_build_script_produces(fixture_id: str) -> None:
    recipes = build_recipes(HARBOR)
    assert recipes, "scripts/build-images.sh must build the fixture images"

    fixture = FIXTURES / fixture_id
    compose = yaml.safe_load((fixture / "environment" / "docker-compose.yaml").read_text(encoding="utf-8"))
    referenced = set(compose_images(compose))
    assert referenced <= set(recipes), referenced - set(recipes)

    docker_image = task_env_docker_image(fixture / "task.toml")
    assert docker_image in recipes

    assert check_fixture(fixture, HARBOR) == []


def test_unknown_compose_image_is_named_as_a_violation(tmp_path: Path) -> None:
    fixture = _copy_fixture(tmp_path, "f1")
    compose_path = fixture / "environment" / "docker-compose.yaml"
    document = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    document["services"]["worker"]["image"] = "omp-unbuildable:dev"
    compose_path.write_text(yaml.safe_dump(document), encoding="utf-8")

    violations = check_fixture(fixture, HARBOR)
    assert any("omp-unbuildable:dev" in violation and "no build recipe" in violation for violation in violations)


def test_missing_main_service_definition_is_a_violation(tmp_path: Path) -> None:
    fixture = _copy_fixture(tmp_path, "f1")
    task_toml = fixture / "task.toml"
    task_toml.write_text(
        task_toml.read_text(encoding="utf-8").replace('docker_image = "omp-f1-agent:dev"\n', ""),
        encoding="utf-8",
    )

    violations = check_fixture(fixture, HARBOR)
    assert any("main service" in violation for violation in violations)


def test_workservice_must_serve_the_real_service(tmp_path: Path) -> None:
    fixture = _copy_fixture(tmp_path, "f1")
    compose_path = fixture / "environment" / "docker-compose.yaml"
    document = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    document["services"]["workservice"]["command"] = ["python", "-m", "omp_work.service"]
    compose_path.write_text(yaml.safe_dump(document), encoding="utf-8")

    violations = check_fixture(fixture, HARBOR)
    assert any("rejected stand-in" in violation for violation in violations)
    assert any("real WorkService" in violation for violation in violations)


def test_entrypoint_with_its_own_http_routes_is_a_violation(tmp_path: Path) -> None:
    harbor = tmp_path / "harbor"
    (harbor / "docker").mkdir(parents=True)
    (harbor / "scripts").mkdir(parents=True)
    shutil.copyfile(BUILD_SCRIPT, harbor / "scripts" / "build-images.sh")
    (harbor / "docker" / "Dockerfile.stub").write_text("FROM alpine\n", encoding="utf-8")
    (harbor / "docker" / "stub-entrypoint.sh").write_text(
        "#!/bin/sh\n"
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/v1/health/ready')\n"
        "def ready():\n"
        "    return {'ready': True}\n",
        encoding="utf-8",
    )

    fixture = harbor / "fixtures" / "stub"
    (fixture / "environment").mkdir(parents=True)
    (fixture / "environment" / "docker-compose.yaml").write_text(
        "services:\n"
        "  workservice:\n"
        "    image: stubbed:dev\n"
        "    command: [sh, /opt/stub-entrypoint.sh]\n",
        encoding="utf-8",
    )

    violations = check_fixture(fixture, harbor)
    assert any("defines its own HTTP routes" in violation for violation in violations)


def test_build_script_parser_reads_build_and_tag_lines() -> None:
    text = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "docker build -t omp-verifier:dev -f docker/Dockerfile.verifier docker\n"
        'docker build --tag "omp-workservice:dev" --file docker/Dockerfile.workservice "$ROOT"\n'
        "docker tag omp-agent:dev omp-f1-agent:dev\n"
    )
    assert parse_build_script(text) == {
        "omp-verifier:dev": "build",
        "omp-workservice:dev": "build",
        "omp-f1-agent:dev": "tag",
    }


def test_cli_runs_clean_on_the_shipped_fixtures(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([str(FIXTURES / "f1"), str(FIXTURES / "f2")]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""

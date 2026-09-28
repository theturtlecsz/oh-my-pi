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


def _copy_harbor(tmp_path: Path) -> tuple[Path, Path]:
    harbor = tmp_path / "harbor"
    shutil.copytree(HARBOR / "docker", harbor / "docker")
    shutil.copytree(HARBOR / "scripts", harbor / "scripts")
    shutil.copytree(FIXTURES, harbor / "fixtures")
    return harbor, harbor / "fixtures" / "f1"


def test_worker_image_and_command_stay_up() -> None:
    violations = check_fixture(FIXTURES / "f1", HARBOR) + check_fixture(FIXTURES / "f2", HARBOR)
    assert not any("stay up" in violation for violation in violations)
    assert not any("supplied credential" in violation for violation in violations)
    assert not any("mints credentials" in violation for violation in violations)
    assert not any("passwordless" in violation for violation in violations)


def test_worker_without_a_staying_command_is_a_violation(tmp_path: Path) -> None:
    harbor, fixture = _copy_harbor(tmp_path)
    compose_path = fixture / "environment" / "docker-compose.yaml"
    document = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    del document["services"]["worker"]["command"]
    compose_path.write_text(yaml.safe_dump(document), encoding="utf-8")

    violations = check_fixture(fixture, harbor)
    assert any("worker command does not stay up" in violation for violation in violations)


def test_agent_image_that_exits_is_a_violation(tmp_path: Path) -> None:
    harbor, fixture = _copy_harbor(tmp_path)
    dockerfile = harbor / "docker" / "Dockerfile.agent"
    text = dockerfile.read_text(encoding="utf-8")
    text = text.replace("ENTRYPOINT []\n", "").replace('CMD ["sleep", "infinity"]\n', "")
    dockerfile.write_text(text, encoding="utf-8")

    violations = check_fixture(fixture, harbor)
    assert any("Dockerfile.agent does not stay up" in violation for violation in violations)


def test_entrypoint_that_mints_credentials_is_a_violation(tmp_path: Path) -> None:
    harbor, fixture = _copy_harbor(tmp_path)
    entrypoint = harbor / "docker" / "workservice-entrypoint.sh"
    entrypoint.write_text(
        entrypoint.read_text(encoding="utf-8") + "\npython -m omp_work ops credentials init\n",
        encoding="utf-8",
    )

    violations = check_fixture(fixture, harbor)
    assert any("mints credentials" in violation for violation in violations)


def test_passwordless_database_auth_is_a_violation(tmp_path: Path) -> None:
    harbor, fixture = _copy_harbor(tmp_path)
    entrypoint = harbor / "docker" / "workservice-entrypoint.sh"
    entrypoint.write_text(
        entrypoint.read_text(encoding="utf-8") + "\ninitdb -D \"$DATA_DIR\" -A trust\n",
        encoding="utf-8",
    )

    violations = check_fixture(fixture, harbor)
    assert any("passwordless database access" in violation for violation in violations)


def test_workservice_without_supplied_credentials_is_a_violation(tmp_path: Path) -> None:
    harbor, fixture = _copy_harbor(tmp_path)
    compose_path = fixture / "environment" / "docker-compose.yaml"
    document = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    document["services"]["workservice"]["volumes"] = ["pg_data:/var/lib/postgresql/data"]
    compose_path.write_text(yaml.safe_dump(document), encoding="utf-8")

    violations = check_fixture(fixture, harbor)
    assert any("supplied credential directory" in violation for violation in violations)


def test_provision_credentials_writes_the_token_field(tmp_path: Path) -> None:
    import json
    import os
    import stat
    import subprocess

    dest = tmp_path / "bundle"
    script = HARBOR / "scripts" / "provision-credentials.sh"
    env = {**os.environ, "OMP_HARBOR_CREDENTIALS_DIR": str(dest)}
    subprocess.run(["bash", str(script)], check=True, env=env)

    owner = json.loads((dest / "capabilities" / "owner.json").read_text(encoding="utf-8"))
    token = owner["token"]
    workspace = (dest / "credentials" / "workspace-id").read_text(encoding="utf-8").strip()
    assert isinstance(token, str) and token
    assert "\n" not in token
    assert token != (dest / "capabilities" / "owner.json").read_text(encoding="utf-8").strip()
    assert workspace == owner["workspaces"][0]
    assert stat.S_IMODE((dest / "capabilities" / "owner.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((dest / "credentials" / "postgres").stat().st_mode) == 0o600

    sourced = subprocess.run(
        [
            "bash",
            "-c",
            'set -a; . "$1"; printf %s "$OMP_HARBOR_BEARER $OMP_HARBOR_WORKSPACE_ID"',
            "bash",
            str(dest / "harbor.env"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert sourced.stdout == f"{token} {workspace}"

    again = subprocess.run(["bash", str(script)], check=True, env=env, capture_output=True, text=True)
    assert again.returncode == 0
    assert json.loads((dest / "capabilities" / "owner.json").read_text(encoding="utf-8"))["token"] == token

    build = (HARBOR / "scripts" / "build-images.sh").read_text(encoding="utf-8")
    assert "provision-credentials.sh" in build


def test_provision_credentials_refuses_a_partial_bundle(tmp_path: Path) -> None:
    import os
    import subprocess

    dest = tmp_path / "partial"
    (dest / "credentials").mkdir(parents=True)
    (dest / "credentials" / "postgres").write_text("secret\n", encoding="utf-8")
    result = subprocess.run(
        ["bash", str(HARBOR / "scripts" / "provision-credentials.sh")],
        env={**os.environ, "OMP_HARBOR_CREDENTIALS_DIR": str(dest)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "incomplete bundle" in result.stderr
    assert (dest / "credentials" / "postgres").read_text(encoding="utf-8") == "secret\n"


def test_readme_sources_the_token_before_harbor_run() -> None:
    readme = (HARBOR / "README.md").read_text(encoding="utf-8")
    assert "bash evals/harbor/scripts/build-images.sh" in readme
    assert "harbor.env" in readme
    assert "token" in readme
    assert "exec workservice cat" not in readme
    assert "interactive_env` does not open the file" in readme


def test_cli_runs_clean_on_the_shipped_fixtures(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([str(FIXTURES / "f1"), str(FIXTURES / "f2")]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""

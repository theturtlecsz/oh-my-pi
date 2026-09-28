"""Build-recipe contract for the Harbor fixtures.

Harbor builds its ``main`` service from ``environment/Dockerfile`` unless the
task's ``[environment]`` names a prebuilt ``docker_image``, and every image a
fixture's compose file references must exist locally. This module derives the
images a fixture references, resolves each to a recipe the repository can
build (a ``docker build``/``docker tag`` in ``scripts/build-images.sh``), and
asserts the fixture gives Harbor something to build its main service.

It also holds the workservice ruling: the fixture's workservice service must
start the real WorkService (``omp_work.v1.server.create_app`` via
``python -m omp_work serve``), never ``python -m omp_work.service``, and no
entrypoint under ``evals/harbor`` may define its own HTTP routes in place of
the WorkService.
"""

from __future__ import annotations

import argparse
import re
import shlex
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from .isolation import load_compose

BUILD_SCRIPT_REL = Path("scripts") / "build-images.sh"
DOCKER_DIR_REL = Path("docker")
WORKSERVICE = "workservice"

# The rejected attempt-1 entrypoint module. The real service is `omp_work serve`.
_FORBIDDEN_COMMAND_TOKENS = ("omp_work.service",)
# The real serve path the workservice command (or its entrypoint) must run.
_REAL_SERVE_TOKENS = ("omp_work", "serve")
# Tokens that mean an entrypoint mounted its own HTTP API instead of serving
# the contract app.
_ROUTE_TOKENS = (
    "FastAPI(",
    "APIRouter(",
    "add_api_route",
    "@app.get",
    "@app.post",
    "@app.put",
    "@app.patch",
    "@app.delete",
    "uvicorn.run(",
)

_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*(?::[A-Za-z0-9._-]+)?$")


def harbor_dir_of(repo_root: Path) -> Path:
    return repo_root / "evals" / "harbor"


def _tokens(line: str) -> list[str]:
    try:
        return shlex.split(line)
    except ValueError:
        return line.split()


def parse_build_script(text: str) -> dict[str, str]:
    """Map every image tag the build script produces to ``build`` or ``tag``.

    ``docker build -t X`` (with optional ``--tag X``) and ``docker tag A B``
    both count; a recipe is any image the script can produce. Lines whose first
    token is not the ``docker`` CLI (variables, comments, empty lines) are
    skipped, so the script's own naming is the single source of truth.
    """

    recipes: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        tokens = _tokens(line)
        if not tokens or tokens[0] != "docker":
            continue
        if len(tokens) >= 2 and tokens[1] == "build":
            index = 2
            while index < len(tokens):
                token = tokens[index]
                if token == "-t" or token == "--tag":
                    if index + 1 < len(tokens):
                        recipes[tokens[index + 1]] = "build"
                    index += 2
                    continue
                if token.startswith("--tag="):
                    recipes[token.split("=", 1)[1]] = "build"
                index += 1
        elif len(tokens) >= 4 and tokens[1] == "tag":
            recipes[tokens[-1]] = "tag"
    return recipes


def build_recipes(harbor_dir: Path) -> dict[str, str]:
    script = harbor_dir / BUILD_SCRIPT_REL
    if not script.is_file():
        return {}
    return parse_build_script(script.read_text(encoding="utf-8"))


def compose_images(compose: Any) -> list[str]:
    images: list[str] = []
    services = compose.get("services") if isinstance(compose, dict) else None
    if isinstance(services, dict):
        for service in services.values():
            if isinstance(service, dict):
                image = service.get("image")
                if isinstance(image, str) and image.strip():
                    images.append(image.strip())
    return images


def task_env_docker_image(task_toml: Path) -> str | None:
    if not task_toml.is_file():
        return None
    import tomllib

    document = tomllib.loads(task_toml.read_text(encoding="utf-8"))
    environment = document.get("environment")
    if isinstance(environment, dict):
        image = environment.get("docker_image")
        if isinstance(image, str) and image.strip():
            return image.strip()
    return None


def _command(service: Any) -> tuple[str, ...]:
    raw = service.get("command") if isinstance(service, dict) else None
    if isinstance(raw, str):
        return tuple(_tokens(raw))
    if isinstance(raw, (list, tuple)) and all(isinstance(item, str) for item in raw):
        return tuple(raw)
    return ()


def _entrypoints(harbor_dir: Path) -> list[Path]:
    """Shell entrypoints under the harbor ``docker/`` and ``scripts/`` trees."""

    found: list[Path] = []
    for relative in (DOCKER_DIR_REL, BUILD_SCRIPT_REL.parent):
        root = harbor_dir / relative
        if root.is_dir():
            found.extend(path for path in root.rglob("*.sh") if path.is_file())
    return sorted(found)


def _referenced_entrypoints(command: tuple[str, ...], harbor_dir: Path) -> list[Path]:
    names = {PurePosixPath(token).name for token in command if token.endswith(".sh")}
    found: list[Path] = []
    for path in _entrypoints(harbor_dir):
        if PurePosixPath(path.name).name in names:
            found.append(path)
    return found


def _runs_real_serve(command: tuple[str, ...], harbor_dir: Path) -> bool:
    joined = " ".join(command)
    if all(token in joined for token in _REAL_SERVE_TOKENS):
        return True
    for entrypoint in _referenced_entrypoints(command, harbor_dir):
        if all(token in entrypoint.read_text(encoding="utf-8") for token in _REAL_SERVE_TOKENS):
            return True
    return False


def _entrypoint_route_violations(harbor_dir: Path) -> list[str]:
    violations: list[str] = []
    for path in _entrypoints(harbor_dir):
        text = path.read_text(encoding="utf-8")
        for token in _ROUTE_TOKENS:
            if token in text:
                violations.append(
                    f"entrypoint {path.relative_to(harbor_dir)} defines its own HTTP routes ({token!r}) "
                    "instead of serving the WorkService"
                )
                break
    return violations


def check_fixture(fixture_dir: str | Path, harbor_dir: Path) -> list[str]:
    """Return build-recipe violations for ``fixture_dir``; empty means complete."""

    fixture = Path(fixture_dir)
    recipes = build_recipes(harbor_dir)
    violations: list[str] = []

    compose_path = fixture / "environment" / "docker-compose.yaml"
    compose: dict[str, Any] | None = None
    if compose_path.is_file():
        compose = load_compose(compose_path)
        for image in compose_images(compose):
            if image not in recipes:
                violations.append(
                    f"image {image!r} referenced in {compose_path.relative_to(fixture)} "
                    "has no build recipe in the repository"
                )

    docker_image = task_env_docker_image(fixture / "task.toml")
    has_dockerfile = (fixture / "environment" / "Dockerfile").is_file()
    if docker_image is None and not has_dockerfile:
        violations.append(
            f"fixture {fixture.name} lacks what Harbor needs to build its main service: "
            "neither environment/Dockerfile nor [environment].docker_image is set"
        )
    elif docker_image is not None and docker_image not in recipes:
        violations.append(
            f"main service docker_image {docker_image!r} in task.toml has no build recipe in the repository"
        )

    if compose is not None:
        services = compose.get("services")
        workservice = services.get(WORKSERVICE) if isinstance(services, dict) else None
        command = _command(workservice)
        for token in command:
            if any(forbidden in token for forbidden in _FORBIDDEN_COMMAND_TOKENS):
                violations.append(f"workservice command runs the rejected stand-in {token!r}")
                break
        if not _runs_real_serve(command, harbor_dir):
            violations.append(
                "workservice command does not start the real WorkService "
                "(python -m omp_work serve, or an evals/harbor entrypoint that runs it)"
            )

    violations.extend(_entrypoint_route_violations(harbor_dir))
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m omp_harbor_eval.build_recipe",
        description="Verify every image a Harbor fixture references has a repository build recipe.",
    )
    parser.add_argument(
        "fixtures",
        nargs="*",
        help="fixture directories (defaults to every directory under evals/harbor/fixtures)",
    )
    args = parser.parse_args(argv)

    harbor_dir = Path(__file__).resolve().parents[2]
    fixtures_root = harbor_dir / "fixtures"
    targets = (
        [Path(target) for target in args.fixtures]
        if args.fixtures
        else sorted(path for path in fixtures_root.iterdir() if path.is_dir())
    )

    violations: list[str] = []
    for target in targets:
        for violation in check_fixture(target, harbor_dir):
            violations.append(f"{target.name}: {violation}")

    if violations:
        for violation in violations:
            print(violation)
        return 1
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main())

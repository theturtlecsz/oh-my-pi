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
the WorkService. The worker image must stay up for ``docker exec``, the
workservice must mount the host credential bundle read-only instead of minting
one, and its database entrypoint must require a password.
"""

from __future__ import annotations

import argparse
import re
import shlex
from pathlib import Path, PurePosixPath
from typing import Any

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
_AGENT_IMAGES = frozenset({"omp-agent:dev", "omp-f1-agent:dev"})
_WORKSERVICE_IMAGE = "omp-workservice:dev"
_SUPPLIED_TARGET = "/run/omp/supplied"
_SUPPLIED_MARKER = "harbor-fixtures"
_MINT_RE = re.compile(r"credentials\s+init|capabilities\s+init")
_TRUST_RE = re.compile(r"(^|[\s=])trust(\s|$)")


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
                argument = tokens[index]
                if argument == "-t" or argument == "--tag":
                    if index + 1 < len(tokens):
                        recipes[tokens[index + 1]] = "build"
                    index += 2
                    continue
                if argument.startswith("--tag="):
                    recipes[argument.split("=", 1)[1]] = "build"
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


def _last_dockerfile_instruction(text: str, name: str) -> str | None:
    found: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        head = line.split(None, 1)[0]
        if head.upper() == name.upper():
            found = line
    return found


def _command_stays_up(command: tuple[str, ...]) -> bool:
    return "sleep" in command and "infinity" in command


def _line_stays_up(line: str | None) -> bool:
    return line is not None and "sleep" in line and "infinity" in line


def _shell_runtime_violations(harbor_dir: Path) -> list[str]:
    """Credential minting and passwordless database access in harbor shell."""

    violations: list[str] = []
    for path in _entrypoints(harbor_dir):
        text = path.read_text(encoding="utf-8")
        relative = path.relative_to(harbor_dir)
        if _MINT_RE.search(text):
            violations.append(
                f"entrypoint {relative} mints credentials; the build script must supply them"
            )
        if "initdb" not in text:
            continue
        for line in text.splitlines():
            if _TRUST_RE.search(line):
                violations.append(
                    f"entrypoint {relative} allows passwordless database access ({line.strip()!r})"
                )
                break
        if "scram-sha-256" not in text:
            violations.append(
                f"entrypoint {relative} does not require scram-sha-256 database authentication"
            )
    return violations


def _supplied_mount_ok(service: Any) -> bool:
    raw = service.get("volumes") if isinstance(service, dict) else None
    for entry in raw or []:
        if isinstance(entry, str):
            parts = entry.split(":")
            if len(parts) < 3:
                continue
            source, target, mode = parts[0], parts[1], parts[2]
            modes = {item.strip() for item in mode.split(",") if item.strip()}
            if target.rstrip("/") == _SUPPLIED_TARGET and "ro" in modes and _SUPPLIED_MARKER in source:
                return True
        elif isinstance(entry, dict):
            target = str(entry.get("target") or "")
            source = str(entry.get("source") or "")
            if (
                target.rstrip("/") == _SUPPLIED_TARGET
                and entry.get("read_only") is True
                and _SUPPLIED_MARKER in source
            ):
                return True
    return False


def _image_runtime_violations(compose: dict[str, Any] | None, harbor_dir: Path) -> list[str]:
    if not isinstance(compose, dict):
        return []
    services = compose.get("services")
    if not isinstance(services, dict):
        return []
    violations: list[str] = []
    worker = services.get("worker")
    if isinstance(worker, dict) and worker.get("image") in _AGENT_IMAGES:
        dockerfile = harbor_dir / DOCKER_DIR_REL / "Dockerfile.agent"
        if not dockerfile.is_file():
            violations.append(
                "Dockerfile.agent is missing, so the worker image exits before Harbor can exec into it"
            )
        else:
            text = dockerfile.read_text(encoding="utf-8")
            if _last_dockerfile_instruction(text, "ENTRYPOINT") != "ENTRYPOINT []" or not _line_stays_up(
                _last_dockerfile_instruction(text, "CMD")
            ):
                violations.append(
                    "Dockerfile.agent does not stay up for docker exec: "
                    "clear ENTRYPOINT and set CMD to sleep infinity"
                )
        if not _command_stays_up(_command(worker)):
            violations.append(
                'worker command does not stay up for docker exec: set command to ["sleep", "infinity"]'
            )
    workservice = services.get(WORKSERVICE)
    if isinstance(workservice, dict) and workservice.get("image") == _WORKSERVICE_IMAGE:
        if not _supplied_mount_ok(workservice):
            violations.append(
                "workservice does not mount the supplied credential directory "
                "read-only at /run/omp/supplied"
            )
    return violations


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
    violations.extend(_shell_runtime_violations(harbor_dir))
    violations.extend(_image_runtime_violations(compose, harbor_dir))
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
    raise SystemExit(main())

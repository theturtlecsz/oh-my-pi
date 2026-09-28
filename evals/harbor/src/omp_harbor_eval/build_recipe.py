"""Validate that Harbor fixtures define build recipes for all images and main service.

Every image referenced in a fixture's ``environment/docker-compose.yaml`` must
have a build recipe in ``evals/harbor`` (a Dockerfile under ``docker/`` or a
build script entry in ``scripts/build-images.sh``).
Harbor's default ``main`` service also requires either
``fixtures/<id>/environment/Dockerfile`` or ``fixtures/<id>/task.toml``
with ``[environment].docker_image`` set to a known recipe image.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml

from .isolation import load_compose

# Default image recipes provided by evals/harbor
_KNOWN_RECIPE_DOCKERFILES = {
    "omp-verifier:dev": "docker/Dockerfile.verifier",
    "omp-workservice:dev": "docker/Dockerfile.workservice",
    "omp-agent:dev": "docker/Dockerfile.agent",
}

# Images tagged or derived from built base images
_TAGGED_IMAGE_ALIASES = {
    "omp-f1-agent:dev": "omp-agent:dev",
    "omp-f2-agent:dev": "omp-agent:dev",
}


def _harbor_dir(repo_root: Path | None = None) -> Path:
    if repo_root is not None:
        return repo_root / "evals" / "harbor"
    return Path(__file__).resolve().parents[2]


def get_known_build_recipes(harbor_dir: Path) -> dict[str, Path]:
    """Map image name to its recipe file (Dockerfile or build script)."""
    recipes: dict[str, Path] = {}
    build_script = harbor_dir / "scripts" / "build-images.sh"

    for image, rel_dockerfile in _KNOWN_RECIPE_DOCKERFILES.items():
        dockerfile = harbor_dir / rel_dockerfile
        if dockerfile.is_file():
            recipes[image] = dockerfile

    for alias, target in _TAGGED_IMAGE_ALIASES.items():
        if target in recipes and build_script.is_file():
            recipes[alias] = build_script

    return recipes


def _extract_compose_images(compose_doc: dict[str, Any]) -> list[str]:
    images: list[str] = []
    services = compose_doc.get("services")
    if isinstance(services, dict):
        for svc_data in services.values():
            if isinstance(svc_data, dict):
                img = svc_data.get("image")
                if isinstance(img, str) and img.strip():
                    images.append(img.strip())
    return images


def _load_task_env_docker_image(task_toml_path: Path) -> str | None:
    if not task_toml_path.is_file():
        return None
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]
    try:
        doc = tomllib.loads(task_toml_path.read_text(encoding="utf-8"))
        env_section = doc.get("environment")
        if isinstance(env_section, dict):
            img = env_section.get("docker_image")
            if isinstance(img, str) and img.strip():
                return img.strip()
    except Exception:
        pass
    return None


def check_fixture_build_recipe(
    fixture_dir: str | Path,
    harbor_dir: Path | None = None,
) -> list[str]:
    """Return violations if ``fixture_dir`` has unbuildable images or missing main service."""
    fixture_path = Path(fixture_dir).resolve()
    h_dir = harbor_dir or fixture_path.parent.parent
    recipes = get_known_build_recipes(h_dir)
    violations: list[str] = []

    # 1. Check all images in environment/docker-compose.yaml
    compose_path = fixture_path / "environment" / "docker-compose.yaml"
    if compose_path.is_file():
        try:
            compose_doc = load_compose(compose_path)
            for img in _extract_compose_images(compose_doc):
                if img not in recipes:
                    violations.append(
                        f"image {img!r} referenced in {compose_path.name} has no build recipe in repo"
                    )
        except Exception as exc:
            violations.append(f"failed to read {compose_path.name}: {exc}")

    # 2. Check main service definition (Dockerfile or task.toml docker_image)
    main_dockerfile = fixture_path / "environment" / "Dockerfile"
    task_toml = fixture_path / "task.toml"
    docker_image = _load_task_env_docker_image(task_toml)

    has_main_dockerfile = main_dockerfile.is_file()
    has_docker_image = docker_image is not None

    if not has_main_dockerfile and not has_docker_image:
        violations.append(
            f"fixture {fixture_path.name} lacks main service definition: "
            "neither environment/Dockerfile nor task.toml [environment].docker_image is specified"
        )
    elif has_docker_image and docker_image not in recipes and not has_main_dockerfile:
        violations.append(
            f"main service docker_image {docker_image!r} in {task_toml.name} has no build recipe in repo"
        )

    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify Harbor fixture image build recipes and main service definition."
    )
    parser.add_argument(
        "fixtures",
        nargs="*",
        help="Fixture directories to validate (defaults to all fixtures in evals/harbor/fixtures)",
    )
    args = parser.parse_args(argv)

    harbor_root = _harbor_dir()
    if args.fixtures:
        targets = [Path(f).resolve() for f in args.fixtures]
    else:
        fixtures_dir = harbor_root / "fixtures"
        targets = sorted(p for p in fixtures_dir.iterdir() if p.is_dir())

    all_violations: list[str] = []
    for target in targets:
        violations = check_fixture_build_recipe(target, harbor_dir=harbor_root)
        for v in violations:
            all_violations.append(f"{target.name}: {v}")

    if all_violations:
        for v in all_violations:
            print(v)
        return 1
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())

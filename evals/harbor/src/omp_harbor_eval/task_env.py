"""Fixture and in-container worker settings for one Harbor task directory.

``load_task`` reads ``fixtures/<id>/environment``. ``HOME``, the WorkService
URL, and the scripted model URL come from the worker service environment in
that compose file. A null or bare key is missing; the host environment is
not read.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .fixtures import Fixture, load_fixture
from .isolation import check_topology, load_compose

_ENV_KEYS = ("HOME", "OMP_WORKSERVICE_URL", "OMP_SCRIPTED_MODEL_URL")


@dataclass(frozen=True)
class TaskEnv:
    fixture: Fixture
    compose: dict[str, Any]
    home: str
    workservice_url: str
    model_url: str
    working_dir: str
    workservice_image: str


def _environment(raw: object) -> dict[str, str]:
    """Compose ``environment`` as a mapping or a ``KEY=VALUE`` list.

    Null mapping values and bare list keys are omitted. They are not filled
    from the host environment.
    """

    if isinstance(raw, Mapping):
        parsed: dict[str, str] = {}
        for key, value in raw.items():
            if not isinstance(key, str):
                raise ValueError(f"{key} must be a string")
            if value is None:
                continue
            if not isinstance(value, str):
                raise ValueError(f"{key} must be a string")
            parsed[key] = value
        return parsed
    if not isinstance(raw, list):
        return {}
    parsed = {}
    for entry in raw:
        if not isinstance(entry, str):
            continue
        key, separator, value = entry.partition("=")
        if separator and key:
            parsed[key] = value
    return parsed


def _service(compose: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    services = compose.get("services")
    service = services.get(name) if isinstance(services, Mapping) else None
    if not isinstance(service, Mapping):
        raise ValueError(f"missing {name}")
    return service


def _take(source: Mapping[str, Any], key: str, found: dict[str, str], missing: list[str]) -> None:
    if key not in source or source[key] is None:
        missing.append(key)
        return
    value = source[key]
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    found[key] = value


def load_task(environment_dir: str | Path) -> TaskEnv:
    """Return the fixture and worker settings for ``environment_dir``.

    ``environment_dir`` is ``fixtures/<id>/environment``. Every
    ``check_topology`` violation raises ``ValueError`` naming them all. A
    missing ``HOME``, ``OMP_WORKSERVICE_URL``, ``OMP_SCRIPTED_MODEL_URL``,
    ``working_dir``, or workservice ``image`` raises ``ValueError`` naming
    each missing key.
    """

    directory = Path(environment_dir)
    fixture = load_fixture(directory.parent.parent, directory.parent.name)
    compose = load_compose(directory / "docker-compose.yaml")
    violations = check_topology(compose)
    if violations:
        raise ValueError("; ".join(violations))

    worker = _service(compose, "worker")
    workservice = _service(compose, "workservice")
    environment = _environment(worker.get("environment"))
    found: dict[str, str] = {}
    missing: list[str] = []
    for key in _ENV_KEYS:
        _take(environment, key, found, missing)
    _take(worker, "working_dir", found, missing)
    _take(workservice, "image", found, missing)
    if missing:
        raise ValueError("missing " + ", ".join(missing))
    return TaskEnv(
        fixture=fixture,
        compose=compose,
        home=found["HOME"],
        workservice_url=found["OMP_WORKSERVICE_URL"],
        model_url=found["OMP_SCRIPTED_MODEL_URL"],
        working_dir=found["working_dir"],
        workservice_image=found["image"],
    )

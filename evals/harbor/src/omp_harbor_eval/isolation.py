"""Zero-spend isolated Harbor compose topology check.

Enforces five isolation rules on compose services:
1. worker shares workservice network namespace (network_mode == service:workservice).
2. worker is not privileged and mounts no docker socket.
3. worker shares no volume with workservice or verifier, and specifies no volumes_from.
4. worker environment has no key containing MIGRATOR, POSTGRES, PGPASSWORD, or ADMIN,
   and any WorkService URL is loopback.
5. verifier mounts evidence read-only, mounts no worker volume, specifies no volumes_from,
   and does not share the worker network namespace.

CLI:
    python -m omp_harbor_eval.isolation <compose.yaml>
Exits 0 and prints nothing if qualified; prints violations and exits 1 if any.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
import urllib.parse

import yaml

from .adapter import _LOOPBACK_HOSTS

WORKER = "worker"
WORKSERVICE = "workservice"
VERIFIER = "verifier"
SERVICES = (WORKSERVICE, WORKER, VERIFIER)

WORKER_SERVICE_NETNS = f"service:{WORKSERVICE}"
FORBIDDEN_ENV_TOKENS = ("MIGRATOR", "POSTGRES", "PGPASSWORD", "ADMIN")


@dataclass(frozen=True)
class Mount:
    source: str
    target: str
    is_bind: bool
    read_only: bool


def _is_bind_source(source: str, declared_volumes: set[str]) -> bool:
    if not source:
        return False
    if source in declared_volumes:
        return False
    if source.startswith(("/", ".", "~")) or "/" in source or "\\" in source or Path(source).is_absolute():
        return True
    return False


def _sequence(value: object) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _parse_mount(entry: Any, declared_volumes: set[str]) -> Mount | None:
    if isinstance(entry, str):
        parts = entry.split(":")
        if len(parts) == 1:
            source = ""
            target = parts[0].strip()
            mode = ""
        elif len(parts) >= 2:
            source = parts[0].strip()
            target = parts[1].strip()
            mode = parts[2].strip() if len(parts) > 2 else ""
        else:
            return None
        modes = [m.strip().lower() for m in mode.split(",") if m.strip()]
        read_only = "ro" in modes
        is_bind = _is_bind_source(source, declared_volumes)
        return Mount(source=source, target=target, is_bind=is_bind, read_only=read_only)
    elif isinstance(entry, Mapping):
        if entry.get("type") == "tmpfs":
            return None
        source = str(entry.get("source") or "").strip()
        target = str(entry.get("target") or "").strip()
        read_only = entry.get("read_only") is True
        mount_type = entry.get("type")
        if mount_type == "bind":
            is_bind = True
        elif mount_type == "volume":
            is_bind = False
        else:
            is_bind = _is_bind_source(source, declared_volumes)
        return Mount(source=source, target=target, is_bind=is_bind, read_only=read_only)
    return None


def _get_mounts(service: Mapping[str, Any], declared_volumes: set[str]) -> list[Mount]:
    mounts: list[Mount] = []
    for entry in _sequence(service.get("volumes")):
        mount = _parse_mount(entry, declared_volumes)
        if mount is not None:
            mounts.append(mount)
    return mounts


def _last_path_component(path_str: str) -> str:
    cleaned = path_str.strip().rstrip("/\\")
    if not cleaned:
        return ""
    return PurePosixPath(cleaned).name


def _is_evidence_mount(mount: Mount) -> bool:
    src_comp = _last_path_component(mount.source).lower()
    tgt_comp = _last_path_component(mount.target).lower()
    return src_comp == "evidence" or tgt_comp == "evidence"


def _shares_volume(m1: Mount, m2: Mount) -> tuple[bool, str]:
    if not m1.source or not m2.source:
        return False, ""
    if not m1.is_bind and not m2.is_bind:
        if m1.source == m2.source:
            return True, m1.source
        return False, ""
    if m1.is_bind and m2.is_bind:
        p1 = os.path.normpath(m1.source)
        p2 = os.path.normpath(m2.source)
        if p1 == p2:
            return True, p1
        with contextlib.suppress(TypeError, ValueError):
            pp1 = PurePosixPath(p1)
            pp2 = PurePosixPath(p2)
            if pp1.is_absolute() == pp2.is_absolute():
                if pp1.is_relative_to(pp2) or pp2.is_relative_to(pp1):
                    return True, p1
        return False, ""
    return False, ""


def _env(service: Mapping[str, Any]) -> dict[str, str]:
    raw = service.get("environment")
    if isinstance(raw, Mapping):
        return {str(k): "" if v is None else str(v) for k, v in raw.items()}
    result: dict[str, str] = {}
    if isinstance(raw, (list, tuple)):
        for entry in raw:
            if isinstance(entry, str):
                k, sep, v = entry.partition("=")
                result[k] = v if sep else ""
    return result


def _is_work_url_key(key: str) -> bool:
    raw = key.upper()
    cleaned = raw.replace("_", "")
    return ("WORKSERVICE" in raw or "WORKSERVICE" in cleaned) and "URL" in raw


def is_loopback(value: str) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urllib.parse.urlsplit(value.strip())
        return parsed.scheme in {"http", "https"} and parsed.hostname in _LOOPBACK_HOSTS
    except Exception:
        return False


def _shares_worker_netns(netns: str | None) -> bool:
    if not isinstance(netns, str) or not netns:
        return False
    return netns in (f"service:{WORKER}", f"service:{WORKSERVICE}") or netns.startswith("container:")


def check_topology(compose: Any) -> list[str]:
    """Return the topology violations in ``compose``; empty means qualified."""
    if not isinstance(compose, Mapping):
        return ["topology: compose document must be a mapping"]
    services = compose.get("services")
    if not isinstance(services, Mapping):
        return ["topology: services must be a mapping"]

    violations: list[str] = []
    service_map: dict[str, Mapping[str, Any] | None] = {}
    for name in SERVICES:
        svc = services.get(name)
        if not isinstance(svc, Mapping):
            violations.append(f"topology: missing service: {name}")
            service_map[name] = None
        else:
            service_map[name] = svc

    worker = service_map[WORKER]
    workservice = service_map[WORKSERVICE]
    verifier = service_map[VERIFIER]

    if worker is None:
        return violations

    raw_volumes = compose.get("volumes")
    declared_volumes = set(raw_volumes.keys()) if isinstance(raw_volumes, Mapping) else set()
    worker_mounts = _get_mounts(worker, declared_volumes)

    # Rule 1: worker network_mode == service:workservice
    worker_netns = worker.get("network_mode")
    if worker_netns != WORKER_SERVICE_NETNS:
        violations.append(
            f"rule 1: worker network_mode must be {WORKER_SERVICE_NETNS!r}, got {worker_netns!r}"
        )

    # Rule 2: worker privileged not truthy; no volume source containing docker.sock
    if bool(worker.get("privileged")):
        violations.append("rule 2: worker is privileged")
    for mount in worker_mounts:
        if "docker.sock" in mount.source.lower():
            violations.append("rule 2: worker mounts a docker socket")
            break

    # Rule 3: worker shares no volume with workservice or verifier; volumes_from check
    if worker.get("volumes_from"):
        violations.append("rule 3: worker specifies volumes_from")
    for other_name, other in ((WORKSERVICE, workservice), (VERIFIER, verifier)):
        if other is None:
            continue
        other_mounts = _get_mounts(other, declared_volumes)
        for wm in worker_mounts:
            for om in other_mounts:
                shared, desc = _shares_volume(wm, om)
                if shared:
                    violations.append(f"rule 3: worker shares volume {desc!r} with {other_name}")
                    break
            else:
                continue
            break

    # Rule 4: worker env has no forbidden substrings; WorkService URLs loopback
    worker_env = _env(worker)
    for key, value in worker_env.items():
        upper = key.upper()
        for token in FORBIDDEN_ENV_TOKENS:
            if token in upper:
                violations.append(f"rule 4: worker env {key} contains {token}")
                break
        if _is_work_url_key(key) and not is_loopback(value):
            violations.append(f"rule 4: worker env {key} is not loopback: {value!r}")

    # Rule 5: verifier evidence mount read-only; no worker volume; no worker netns; volumes_from
    if verifier is not None:
        verifier_mounts = _get_mounts(verifier, declared_volumes)
        if verifier.get("volumes_from"):
            violations.append("rule 5: verifier specifies volumes_from")

        evidence_mounts = [m for m in verifier_mounts if _is_evidence_mount(m)]
        if not evidence_mounts:
            violations.append("rule 5: verifier mounts no evidence volume")
        elif not all(m.read_only for m in evidence_mounts):
            violations.append("rule 5: verifier evidence mount is not read-only")

        for vm in verifier_mounts:
            for wm in worker_mounts:
                shared, desc = _shares_volume(vm, wm)
                if shared:
                    violations.append(f"rule 5: verifier mounts worker volume {desc!r}")
                    break
            else:
                continue
            break

        verifier_netns = verifier.get("network_mode")
        if _shares_worker_netns(verifier_netns):
            violations.append(
                f"rule 5: verifier shares worker network namespace {verifier_netns!r}"
            )

    return violations


def check_capabilities(compose: Any) -> list[str]:
    """Return capability violations across all services in ``compose``.

    Every service must drop all capabilities (cap_drop: [ALL]), declare an explicit
    cap_add list, and must not be privileged (privileged not truthy).
    """
    if not isinstance(compose, Mapping):
        return ["capabilities: compose document must be a mapping"]
    services = compose.get("services")
    if not isinstance(services, Mapping):
        return ["capabilities: services must be a mapping"]

    violations: list[str] = []
    for name, svc in services.items():
        if not isinstance(svc, Mapping):
            continue
        if bool(svc.get("privileged")):
            violations.append(f"service '{name}' is privileged")
        cap_drop = svc.get("cap_drop")
        if not isinstance(cap_drop, (list, tuple)):
            violations.append(f"service '{name}' lacks cap_drop: [ALL]")
        elif not any(str(c).upper() == "ALL" for c in cap_drop):
            violations.append(f"service '{name}' cap_drop does not contain 'ALL'")
        cap_add = svc.get("cap_add")
        if cap_add is None or not isinstance(cap_add, (list, tuple)):
            violations.append(f"service '{name}' lacks explicit cap_add list")

    return violations


def load_compose(path: str | Path) -> dict[str, Any]:
    """Parse one compose document; the root must be a mapping."""
    document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, Mapping):
        raise TypeError(f"{path}: compose document must be a mapping")
    return dict(document)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m omp_harbor_eval.isolation",
        description="Check the zero-spend isolated Harbor compose topology.",
    )
    parser.add_argument("compose", help="path to the compose document")
    args = parser.parse_args(argv)
    try:
        path = Path(args.compose)
        text = path.read_text(encoding="utf-8")
        document = yaml.safe_load(text)
    except (OSError, yaml.YAMLError) as exc:
        print(f"topology: error loading compose: {exc}", file=sys.stderr)
        return 2

    violations = check_topology(document)
    if violations:
        for violation in violations:
            print(violation)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Write-surface audit probe for the loopback ingress gate (OMP-490-s04).

The probe sends one request per client-contract operation, plus a fixed set of
extra requests that must never pass, to a running ingress gate. It compares each
answer with the gate configuration: an operation outside the configuration, and
every extra, must get a 404 with the ``X-OMP-Ingress: refused`` header. An
allowed operation must pass the gate: 401 without a token, and with a token any
answer other than 401, 403, or a refusal. A POST body is ``{}`` with no
``request_id``, so the upstream answers 400 after authentication and mutates
nothing. The report never holds the token.
"""

from __future__ import annotations

import asyncio
import json
import stat
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import load_contract
from .client_ingress import IngressConfig

__all__ = ["read_bearer", "run_probe"]

_REFUSED_HEADER = "x-omp-ingress"
_TIMEOUT = 10.0


@dataclass(frozen=True)
class _Probe:
    name: str
    method: str
    path: str
    refuse: bool
    operation: str | None = None


def _fill(path: str, workspace_id: uuid.UUID) -> str:
    segments = []
    for segment in path.split("/"):
        if segment == "{workspace_id}":
            segments.append(str(workspace_id))
        elif segment.startswith("{") and segment.endswith("}"):
            segments.append(str(uuid.uuid4()))
        else:
            segments.append(segment)
    return "/".join(segments)


def _probes(config: IngressConfig) -> list[_Probe]:
    workspace = config.workspace_id
    probes: list[_Probe] = []
    list_path = ""
    for operation in load_contract().client_contract.operations:
        path = _fill(operation.path, workspace)
        if operation.name == "project.list":
            list_path = path
        probes.append(
            _Probe(
                operation.name,
                operation.method,
                path,
                operation.name not in config.operations,
                operation.name,
            )
        )
    base = f"/v1/workspaces/{workspace}"
    probes.extend(
        (
            _Probe("extra.commands", "POST", "/v1/commands", True),
            _Probe("extra.health", "GET", "/v1/health/ready", True),
            _Probe("extra.stop", "GET", f"{base}/stop", True),
            _Probe("extra.events", "GET", f"{base}/events", True),
            _Probe("extra.query", "GET", f"{list_path}?detail=true", True),
            _Probe("extra.put", "PUT", list_path, True),
        )
    )
    return probes


def _expected(probe: _Probe, authenticated: bool) -> str:
    if probe.refuse:
        return "404 refused"
    if not authenticated:
        return "401 not refused"
    if probe.operation in ("project.list", "stop.status"):
        return "200 not refused"
    if probe.method == "POST":
        return "400 not refused"
    return "not refused, not 401 or 403"


def _met(probe: _Probe, authenticated: bool, status: int | None, refused: bool) -> bool:
    if status is None:
        return False
    if probe.refuse:
        return status == 404 and refused
    if refused:
        return False
    if not authenticated:
        return status == 401
    if probe.operation in ("project.list", "stop.status"):
        return status == 200
    if probe.method == "POST":
        return status == 400
    return status not in (401, 403)


async def _probe_all(
    base_url: str,
    probes: list[_Probe],
    headers: dict[str, str],
    transport: httpx.AsyncBaseTransport | None,
) -> list[tuple[int | None, bool]]:
    answers: list[tuple[int | None, bool]] = []
    async with httpx.AsyncClient(
        base_url=base_url, transport=transport, timeout=_TIMEOUT
    ) as client:
        client.headers.clear()
        for probe in probes:
            content = b"{}" if probe.method == "POST" else None
            request_headers = dict(headers)
            if content is not None:
                request_headers["Content-Type"] = "application/json"
            try:
                response = await client.request(
                    probe.method, probe.path, headers=request_headers, content=content
                )
            except httpx.HTTPError:
                answers.append((None, False))
                continue
            refused = response.headers.get(_REFUSED_HEADER) == "refused"
            answers.append((response.status_code, refused))
    return answers


def run_probe(
    base_url: str,
    config: IngressConfig,
    contract_sha256: str,
    token: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """Probe the gate at ``base_url`` and return the audit report."""
    authenticated = token is not None
    headers = {"X-OMP-Contract-SHA256": contract_sha256}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    probes = _probes(config)
    answers = asyncio.run(_probe_all(base_url, probes, headers, transport))
    results = []
    failed: list[str] = []
    for probe, (status, refused) in zip(probes, answers, strict=True):
        if not _met(probe, authenticated, status, refused):
            failed.append(probe.name)
        results.append(
            {
                "probe": probe.name,
                "method": probe.method,
                "path": probe.path,
                "expected": _expected(probe, authenticated),
                "status": status,
                "refused": refused,
            }
        )
    return {
        "base_url": base_url,
        "authenticated": authenticated,
        "passed": not failed,
        "failed": failed,
        "results": results,
    }


def read_bearer(path: Path) -> str:
    """Read the token from a 0600 capability JSON file."""
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ValueError("unsafe bearer file permissions")
    token = json.loads(path.read_text())["token"]
    if not isinstance(token, str) or not token:
        raise ValueError("bearer file has no token")
    return token


def run_cli(args: object) -> int:
    """Run ``ingress probe``. Exit 0 only when every probe met its expectation."""
    from . import contract_sha256
    from .client_ingress import load_ingress_config

    try:
        config = load_ingress_config(args.config)  # type: ignore[attr-defined]
        bearer_file = args.bearer_file  # type: ignore[attr-defined]
        token = read_bearer(bearer_file) if bearer_file is not None else None
    except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        print(f"ingress probe: {type(error).__name__}", file=sys.stderr)
        return 2
    report = run_probe(
        args.base_url,  # type: ignore[attr-defined]
        config,
        contract_sha256(),
        token,
    )
    Path(args.out).write_text(json.dumps(report, indent=2) + "\n")  # type: ignore[attr-defined]
    print(f"ingress probe: passed={report['passed']} failed={report['failed']}")
    return 0 if report["passed"] else 1

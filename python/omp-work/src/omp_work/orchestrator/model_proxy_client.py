"""Loopback model proxy client for orchestrator tasks (OMP-417).

Callers must use attribute lookup (``model_proxy_client.register``,
``model_proxy_client.revoke_task``) so tests can wrap or intercept those names.
"""

from __future__ import annotations

from collections.abc import Mapping
import ipaddress
import json
from pathlib import Path
from typing import Any
import urllib.parse

import httpx

from omp_work.model_proxy import TaskRequest

__all__ = ["TaskRequest", "register", "revoke_task"]


def _is_loopback(hostname: str | None) -> bool:
    if not hostname:
        return False
    if hostname.lower() in ("localhost", "localhost.localdomain"):
        return True
    try:
        ip = ipaddress.ip_address(hostname)
        return ip.is_loopback
    except ValueError:
        return False


def _validate_base_url(base_url: str) -> str:
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("base_url must be a non-empty string")
    try:
        parsed = urllib.parse.urlsplit(base_url)
    except Exception as exc:
        raise ValueError(f"invalid base_url: {base_url!r}") from exc

    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"base_url scheme must be http or https, got {parsed.scheme!r}")
    if parsed.username or parsed.password:
        raise ValueError("userinfo not allowed in base_url")
    if parsed.query or parsed.fragment:
        raise ValueError("query and fragment not allowed in base_url")
    if parsed.path not in ("", "/"):
        raise ValueError(f"base_url must not contain a path, got {parsed.path!r}")
    if not _is_loopback(parsed.hostname):
        raise ValueError(f"base_url must be a loopback URL, got {base_url!r}")
    if parsed.port is not None and not (1 <= parsed.port <= 65535):
        raise ValueError(f"invalid port in base_url: {parsed.port}")
    return base_url.rstrip("/")


def _load_control_token(capability_path: Path | str) -> str:
    path = Path(capability_path)
    content = path.read_text(encoding="utf-8")
    capability = json.loads(content)
    if not isinstance(capability, dict):
        raise ValueError("capability must be a JSON object")

    token = capability.get("token")
    if not isinstance(token, str) or not token.strip():
        raise ValueError("invalid capability: token must be a non-empty string")

    if capability.get("actor_kind") != "automation":
        raise ValueError("invalid capability: actor_kind must be 'automation'")

    scopes = capability.get("scopes")
    if not isinstance(scopes, (list, tuple, set)) or "work.execute" not in scopes:
        raise ValueError("invalid capability: scopes must contain 'work.execute'")

    workspaces = capability.get("workspaces")
    if not isinstance(workspaces, list):
        raise ValueError("invalid capability: workspaces must be a list")

    return token


def register(
    capability_path: Path | str,
    base_url: str,
    payload: TaskRequest | Mapping[str, Any],
    *,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Register a task with the model proxy and return descriptor and model token.

    Validates that ``base_url`` is loopback before any network interaction.
    The payload fields are exactly ``TaskRequest``: ``workspace_id``, ``project_id``,
    ``mission_id``, ``work_id``, ``job_id``, ``worker_id``, ``fence``,
    ``stage`` ("implement"), ``expires_at``, and ``ceiling_usd``.

    Returns the JSON object unchanged: ``id``, ``model_token``, ``descriptor``.
    """
    url = _validate_base_url(base_url)
    token = _load_control_token(capability_path)

    if isinstance(payload, TaskRequest):
        task = payload
    elif isinstance(payload, Mapping):
        data = dict(payload)
        if "stage" not in data:
            data["stage"] = "implement"
        task = TaskRequest.model_validate(data)
    else:
        raise TypeError(f"payload must be TaskRequest or Mapping, got {type(payload).__name__}")

    body = task.model_dump(mode="json")
    with httpx.Client(base_url=url, transport=transport, timeout=timeout, trust_env=False) as client:
        response = client.post(
            "/tasks",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError(f"expected JSON object response, got {type(result).__name__}")
        return result


def revoke_task(
    capability_path: Path | str,
    base_url: str,
    task_id: str,
    *,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Revoke a task registration and return settled refusal and spend facts.

    Validates that ``base_url`` is loopback before any network interaction.
    DELETEs ``/tasks/{task_id}`` with the control bearer and returns
    ``refused`` and ``spent_usd``.
    """
    url = _validate_base_url(base_url)
    token = _load_control_token(capability_path)

    cleaned_id = str(task_id).strip()
    if not cleaned_id:
        raise ValueError("task_id must be a non-empty string")

    with httpx.Client(base_url=url, transport=transport, timeout=timeout, trust_env=False) as client:
        response = client.delete(
            f"/tasks/{urllib.parse.quote(cleaned_id, safe='')}",
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError(f"expected JSON object response, got {type(result).__name__}")
        return result

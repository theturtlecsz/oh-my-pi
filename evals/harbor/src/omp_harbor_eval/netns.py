"""Read a WorkService from inside the worker netns and run the model sidecar.

``ExecProbe`` implements ``ServiceProbe`` by running ``SCRIPT`` (a stdlib
urllib GET) through ``docker exec -i`` on the worker container, so the same
loopback URL the worker sees is the one the host reads. The bearer and
workspace headers travel on stdin, never in an argv. ``model_port`` extracts
the port of a loopback model URL. ``start_model_sidecar`` writes the script to
a staging volume and starts the scripted model in a container that shares the
worker's network namespace, then polls ``GET /`` until the 404 the server
returns for every non-chat path proves it is listening.
"""

from __future__ import annotations

import json
import time
import urllib.parse
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import docker_ops
from .adapter import _LOOPBACK_HOSTS, ProbeError, ServiceProbe
from .docker_ops import DockerError

HARBOR_SRC = Path(__file__).resolve().parents[1]
RPC_SRC = Path(__file__).resolve().parents[4] / "python" / "omp-rpc" / "src"
MODEL_SCRIPT_NAME = "model-script.json"
# In-container path inside the sidecar; not on the host filesystem.
_MODEL_LOG = "/tmp/model.jsonl"  # nosec B108
READY_POLL_S = 0.05

# Runs inside the worker container. URL is argv[1], headers are a JSON object
# on stdin, and a nonzero exit means the service could not be reached.
SCRIPT = """\
import json, sys, urllib.error, urllib.request

raw = sys.stdin.read()
headers = {str(k): str(v) for k, v in json.loads(raw).items()} if raw.strip() else {}
request = urllib.request.Request(sys.argv[1], headers=headers, method="GET")
try:
    with urllib.request.urlopen(request, timeout=5) as response:
        status, payload = response.status, response.read().decode("utf-8")
except urllib.error.HTTPError as exc:
    status, payload = exc.code, exc.read().decode("utf-8")
except (urllib.error.URLError, OSError):
    sys.exit(1)
try:
    body = json.loads(payload)
except (ValueError, UnicodeError):
    body = payload
print(json.dumps({"status": status, "body": body}), flush=True)
"""


def _script_get(
    container: str,
    url: str,
    headers: Mapping[str, str],
    docker: str,
) -> tuple[int, Any]:
    """Run ``SCRIPT`` in ``container`` and return its ``(status, body)``.

    Raises ``DockerError`` when the exec exits non-zero or prints anything
    other than a ``{"status", "body"}`` object.
    """

    payload = json.dumps({str(key): str(value) for key, value in headers.items()}, sort_keys=True).encode("utf-8")
    completed = docker_ops.run(docker, ["exec", "-i", container, "python", "-c", SCRIPT, url], input=payload)
    try:
        document = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DockerError(f"probe did not print JSON: {completed.stdout!r}") from exc
    if not isinstance(document, dict) or "body" not in document or not isinstance(document.get("status"), int):
        raise DockerError(f"probe did not print a status/body object: {completed.stdout!r}")
    return document["status"], document["body"]


class ExecProbe(ServiceProbe):
    """``ServiceProbe`` whose reads run through ``docker exec`` on ``container``."""

    def __init__(self, base_url: str, bearer: str, workspace_id: str, container: str, docker: str = "docker") -> None:
        super().__init__(base_url, bearer, workspace_id)
        self.container = container
        self.docker = docker

    def _get(self, path: str, *, auth: bool) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {self.bearer}"
            headers["X-OMP-Workspace-ID"] = self.workspace_id
        try:
            status, body = _script_get(self.container, self.base_url + path, headers, self.docker)
        except DockerError as exc:
            raise ProbeError(f"{path} unavailable: {exc}") from exc
        if status != 200:
            raise ProbeError(f"{path} returned {status}")
        if not isinstance(body, dict):
            raise ProbeError(f"{path} was not a JSON object")
        return body


def model_port(url: str) -> int:
    """Port of a loopback http(s) ``url``; ``ValueError`` for anything else."""

    parsed = urllib.parse.urlsplit(url if isinstance(url, str) else "")
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in _LOOPBACK_HOSTS or parsed.port is None:
        raise ValueError(f"model url must be a loopback http(s) URL with a port, got {url!r}")
    return parsed.port


def start_model_sidecar(
    ws: str,
    image: str,
    model_script: Sequence[Any],
    port: int,
    staging: str | Path,
    docker: str = "docker",
    ready_timeout_s: float = 20,
) -> str:
    """Start the scripted model sharing ``ws``'s netns; return its container id.

    Writes ``staging/model-script.json``, runs the sidecar detached, and polls
    ``GET /`` on ``ws`` until the model answers 404. On timeout the container is
    removed and ``DockerError`` is raised.
    """

    staging_path = Path(staging)
    staging_path.mkdir(parents=True, exist_ok=True)
    (staging_path / MODEL_SCRIPT_NAME).write_bytes((json.dumps(model_script) + "\n").encode("utf-8"))
    argv = [
        "run",
        "-d",
        "--rm",
        "--network",
        f"container:{ws}",
        "-e",
        "PYTHONPATH=/opt/h:/opt/r",
        "-v",
        f"{HARBOR_SRC}:/opt/h:ro",
        "-v",
        f"{RPC_SRC}:/opt/r:ro",
        "-v",
        f"{staging_path}:/opt/s:ro",
        str(image),
        "python",
        "-m",
        "omp_harbor_eval.scripted_model",
        "--script",
        "/opt/s/model-script.json",
        "--log",
        _MODEL_LOG,
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    cid = docker_ops.run(docker, argv).stdout.decode("utf-8", errors="replace").strip()
    deadline = time.monotonic() + ready_timeout_s
    while True:
        try:
            status, _body = _script_get(ws, f"http://127.0.0.1:{port}/", {}, docker)
        except DockerError:
            status = None
        if status == 404:
            return cid
        if time.monotonic() >= deadline:
            break
        time.sleep(READY_POLL_S)
    try:
        stop_container(cid, docker)
    except DockerError:
        pass
    raise DockerError(f"model sidecar {cid} did not answer GET / within {ready_timeout_s}s")


def stop_container(cid: str, docker: str) -> None:
    """``rm -f <cid>``."""

    docker_ops.run(docker, ["rm", "-f", cid])

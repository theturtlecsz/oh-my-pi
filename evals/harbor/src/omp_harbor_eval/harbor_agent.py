"""Harbor 0.23.0 agent driving one omp fixture scenario over RPC.

``OmpRpcAgent`` resolves the fixture from ``environment.environment_dir``,
stages the worker tree (seed always, plus the solution when
``OMP_HARBOR_VARIANT`` is ``known_good``), points the worker's omp at a
keyless scripted model through ``models_yml`` and a netns-shared model
sidecar, then runs ``RpcAdapter`` on a worker thread with a pidfile killer and
a repository-bundle hook. Every docker argv goes through ``docker_ops`` /
``netns`` with the injectable ``docker`` kwarg, and ``run`` seals the evidence
directory under ``OMP_HARBOR_EVIDENCE_ROOT`` before the sidecar stops.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from harbor.agents.base import AgentCapabilities, BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.environments.docker.docker import _sanitize_docker_compose_project_name
from harbor.models.agent.context import AgentContext

from .adapter import RpcAdapter
from .docker_ops import (
    apply_patch,
    compose_container,
    export_bundle,
    pid_killer,
    read_file,
    rpc_command,
    write_file,
)
from .evidence import MANIFEST_NAME, EvidenceWriter
from .netns import ExecProbe, model_port, start_model_sidecar, stop_container
from .scripted_model import models_yml
from .task_env import load_task

_VARIANT = "OMP_HARBOR_VARIANT"
_EVIDENCE_ROOT = "OMP_HARBOR_EVIDENCE_ROOT"
_BEARER = "OMP_HARBOR_BEARER"
_WORKSPACE_ID = "OMP_HARBOR_WORKSPACE_ID"

_KNOWN_VARIANTS: tuple[str, ...] = ("known_good", "known_bad")
_WORKER_SERVICE = "worker"
_WORKSERVICE_SERVICE = "workservice"
_MODELS_PATH = ".omp/agent/models.yml"
_SESSION_DIR = "omp-sessions"
_BUNDLE_NAME = "worker-repo.bundle"


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"{name} must be set")
    return value


def _variant() -> str:
    value = os.environ.get(_VARIANT)
    if value not in _KNOWN_VARIANTS:
        raise ValueError(f"{_VARIANT} must be one of {_KNOWN_VARIANTS}, got {value!r}")
    return value


def _evidence_root() -> Path:
    value = os.environ.get(_EVIDENCE_ROOT)
    if not value:
        raise ValueError(f"{_EVIDENCE_ROOT} must be set")
    root = Path(value)
    if not root.is_absolute():
        raise ValueError(f"{_EVIDENCE_ROOT} must be an absolute path, got {value!r}")
    return root


def _manifest_digest(directory: Path) -> str:
    return hashlib.sha256((directory / MANIFEST_NAME).read_bytes()).hexdigest()


def _bundle_hook(worker: str, workdir: str, bundle: Path, docker: str) -> Callable[[EvidenceWriter], None]:
    def before_seal(writer: EvidenceWriter) -> None:
        export_bundle(worker, workdir, bundle, docker=docker)
        writer.add_file(_BUNDLE_NAME, bundle.read_bytes())

    return before_seal


def _omp_args(task_home: str) -> list[str]:
    return ["--mode", "rpc", "--model", "scripted/scripted", "--session-dir", f"{task_home}/{_SESSION_DIR}"]


class OmpRpcAgent(BaseAgent):
    """Host-side Harbor agent staging a fixture and driving omp over RPC."""

    capabilities = AgentCapabilities()

    @staticmethod
    def name() -> str:
        return "omp-rpc"

    def version(self) -> str:
        return "0.1.0"

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        *,
        docker: str = "docker",
        **kwargs: Any,
    ) -> None:
        super().__init__(logs_dir=logs_dir, model_name=model_name, **kwargs)
        self.docker = docker

    async def setup(self, environment: BaseEnvironment) -> None:
        """No setup: ``run`` stages the fixture before it drives omp."""

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        variant = _variant()
        evidence_root = _evidence_root()
        bearer = _required(_BEARER)
        workspace_id = _required(_WORKSPACE_ID)

        task = load_task(environment.environment_dir)
        session_id = environment.session_id
        project = _sanitize_docker_compose_project_name(session_id)
        worker = compose_container(project, _WORKER_SERVICE, docker=self.docker)
        workservice = compose_container(project, _WORKSERVICE_SERVICE, docker=self.docker)

        fixture = task.fixture
        apply_patch(worker, (fixture.directory / fixture.seed_patch).read_bytes(), task.working_dir, docker=self.docker)
        if variant == "known_good":
            apply_patch(
                worker,
                (fixture.directory / fixture.solution_patch).read_bytes(),
                task.working_dir,
                docker=self.docker,
            )

        with tempfile.TemporaryDirectory() as staging_root:
            staging = Path(staging_root)
            write_file(
                worker,
                f"{task.home}/{_MODELS_PATH}",
                models_yml(task.model_url).encode("utf-8"),
                docker=self.docker,
            )
            sidecar = start_model_sidecar(
                workservice,
                task.workservice_image,
                fixture.scenario.model_script,
                model_port(task.model_url),
                staging / "model",
                docker=self.docker,
            )
            try:
                evidence_dir = evidence_root / f"{fixture.id}-{variant}"
                writer = EvidenceWriter(
                    evidence_dir,
                    session_id,
                    uuid.uuid4().hex,
                    fixture.id,
                    fixture.digest,
                    variant,
                    fixture.scored_experiment,
                )
                adapter = RpcAdapter(
                    command=rpc_command(worker, _omp_args(task.home), docker=self.docker),
                    cwd=None,
                    env=None,
                    probe=ExecProbe(task.workservice_url, bearer, workspace_id, worker, docker=self.docker),
                    evidence=writer,
                    scenario=fixture.scenario,
                    killer=pid_killer(worker, docker=self.docker),
                    session_reader=lambda path: read_file(worker, path, docker=self.docker),
                    before_seal=_bundle_hook(worker, task.working_dir, staging / _BUNDLE_NAME, self.docker),
                )
                outcome = await asyncio.to_thread(adapter.run)
                context.metadata = {
                    "evidence_dir": str(evidence_dir),
                    "manifest_sha256": _manifest_digest(evidence_dir),
                    "outcome": outcome,
                }
            finally:
                stop_container(sidecar, self.docker)

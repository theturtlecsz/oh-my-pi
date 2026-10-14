"""Fill one validated model-proxy settlement request from a workspace project.

``--config`` is JSON with ``workspace_id``, ``capability`` (a principal file),
``control_repo``, and ``operations`` (work-ledger connection fields). The
command writes the request to ``--out`` and prints its mission id. It does
not submit the mission.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import UUID, uuid4

from omp_work.operations.config import OperationsConfig
from omp_work.orchestrator import candidate_git
from omp_work.orchestrator.service import load_request
from omp_work.v1.service import Principal
from omp_work.v1.store import PostgresWorkStore

_TEMPLATE = Path(__file__).with_name("model-proxy-request.json")


class DrillError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _principal(path: Path) -> Principal:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise DrillError("invalid_request")
    try:
        return Principal(
            actor_id=UUID(str(data["actor_id"])),
            actor_kind=str(data.get("actor_kind") or "automation"),
            workspaces=frozenset(UUID(str(item)) for item in data["workspaces"]),
            scopes=frozenset(str(item) for item in data["scopes"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise DrillError("invalid_request") from error


def _operations(value: object) -> OperationsConfig:
    if not isinstance(value, dict):
        raise DrillError("invalid_request")
    try:
        return OperationsConfig(
            config_dir=Path(value["config_dir"]),
            state_dir=Path(value["state_dir"]),
            data_dir=Path(value["data_dir"]),
            database=str(value.get("database") or "omp_work"),
            host=str(value.get("host") or "127.0.0.1"),
            port=int(value.get("port") or 54321),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise DrillError("invalid_request") from error


def _load_config(path: Path) -> tuple[UUID, Principal, Path, OperationsConfig]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise DrillError("invalid_request")
    try:
        workspace_id = UUID(str(data["workspace_id"]))
        principal = _principal(Path(data["capability"]))
        control_repo = Path(data["control_repo"])
        operations = _operations(data["operations"])
    except (KeyError, TypeError, ValueError) as error:
        raise DrillError("invalid_request") from error
    if workspace_id not in principal.workspaces or "work.read" not in principal.scopes:
        raise DrillError("forbidden")
    return workspace_id, principal, control_repo, operations


def _first_repository(view: dict[str, object]) -> tuple[str, str, str]:
    """Key, url, and default branch of the project's first repository.

    ``PostgresWorkStore.read_project`` orders repositories by key.
    """
    repositories = view.get("repositories")
    if not isinstance(repositories, list) or not repositories:
        raise DrillError("repository_missing")
    repository = repositories[0]
    if not isinstance(repository, dict):
        raise DrillError("invalid_request")
    try:
        key = str(repository["key"])
        url = str(repository["url"])
        branch = str(repository["default_branch"])
    except KeyError as error:
        raise DrillError("invalid_request") from error
    if not key or not url or not branch:
        raise DrillError("invalid_request")
    return key, url, branch


def prepare(config_path: Path, project_key: str, out: Path) -> UUID:
    """Write one filled request and return its mission id after ``load_request``."""
    workspace_id, principal, control_repo, operations = _load_config(config_path)
    store = PostgresWorkStore(operations)
    found = store.find_project(workspace_id, principal.actor_id, project_key)
    project_id = UUID(str(found["project_id"]))
    view = store.read_project(workspace_id, principal.actor_id, project_id)
    key, url, branch = _first_repository(view)
    head = candidate_git.git(control_repo, "rev-parse", "HEAD").decode("ascii").strip()
    if not head:
        raise DrillError("invalid_request")

    mission_id = uuid4()
    document = json.loads(_TEMPLATE.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise DrillError("invalid_request")
    try:
        document["mission_id"] = str(mission_id)
        document["project_id"] = str(project_id)
        document["scope"]["project_id"] = str(project_id)
        document["scope"]["repositories"] = [key]
        document["work"]["project_id"] = str(project_id)
        document["work"]["client_ref"] = str(mission_id)
        document["repository"] = key
        document["remote"] = url
        document["target_ref"] = branch
        document["base_commit"] = head
        document["candidate_ref"] = f"refs/heads/omp/{mission_id}"
    except (KeyError, TypeError) as error:
        raise DrillError("invalid_request") from error

    destination = Path(out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    loaded = load_request(destination)
    if loaded.mission_id != mission_id:
        raise DrillError("invalid_request")
    return mission_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="model_proxy_drill")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--project-key", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        mission_id = prepare(args.config, args.project_key, args.out)
    except DrillError as error:
        print(error.code, file=sys.stderr)
        return 1
    print(mission_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

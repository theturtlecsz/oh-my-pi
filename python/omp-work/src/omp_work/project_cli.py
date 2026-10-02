"""OMP-418/527: owner-run projects CLI (seed, show, check, sides, link-world, move-item)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

from .project_store import ProjectNotFound, WorkItemNotFound, WorkStoreError

_WILDCARD_CHARS = ("*", "?", "[")

_MEDIA_DISCOVERY = "media-discovery"


def validate_project_seed(data: Any) -> tuple[bool, str]:
    if not isinstance(data, dict):
        return False, "payload must be a JSON object"

    for field in ("key", "name", "kind"):
        val = data.get(field)
        if not isinstance(val, str) or not val.strip():
            return False, f"missing or invalid required field '{field}'"

    repos = data.get("repositories")
    if not isinstance(repos, list):
        return False, "missing or invalid required field 'repositories' (must be a list)"

    for i, repo in enumerate(repos):
        if not isinstance(repo, dict):
            return False, f"repository [{i}] must be an object"
        for rfield in ("key", "name", "url", "default_branch"):
            rval = repo.get(rfield)
            if not isinstance(rval, str) or not rval.strip():
                return False, f"repository [{i}] missing or invalid field '{rfield}'"
        rkey = repo["key"]
        if any(c in rkey for c in _WILDCARD_CHARS):
            return False, f"repository [{i}] key contains wildcard characters: {rkey}"
        protected = repo.get("protected_branches")
        if not isinstance(protected, list) or not all(isinstance(b, str) for b in protected):
            return False, f"repository [{i}] missing or invalid 'protected_branches'"
        secret_free = repo.get("automation_ci_secret_free")
        if not isinstance(secret_free, bool):
            return False, f"repository [{i}] missing or invalid 'automation_ci_secret_free'"

    if "purpose" in data and not isinstance(data["purpose"], str):
        return False, "invalid 'purpose' field (must be string)"
    if "goals" in data and (
        not isinstance(data["goals"], list) or not all(isinstance(g, str) for g in data["goals"])
    ):
        return False, "invalid 'goals' field (must be list of strings)"
    if "questions" in data and (
        not isinstance(data["questions"], list)
        or not all(isinstance(q, str) for q in data["questions"])
    ):
        return False, "invalid 'questions' field (must be list of strings)"
    if "refs" in data and not isinstance(data["refs"], list):
        return False, "invalid 'refs' field (must be list)"

    return True, ""


def seed(
    store: Any,
    workspace_id: UUID,
    actor_id: UUID,
    file: Path | str,
) -> int:
    path = Path(file)
    if not path.is_file():
        print(f"projects seed: file not found: {path}", file=sys.stderr)
        return 2

    try:
        content = path.read_text(encoding="utf-8")
        data = json.loads(content)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"projects seed: failed to read JSON from {path}: {exc}", file=sys.stderr)
        return 2

    valid, err = validate_project_seed(data)
    if not valid:
        print(f"projects seed: validation failed: {err}", file=sys.stderr)
        return 2

    project_id = store.ensure_project(
        workspace_id,
        actor_id,
        key=data["key"],
        name=data["name"],
        kind=data["kind"],
    )
    store.update_profile(
        workspace_id,
        actor_id,
        project_id,
        purpose=data.get("purpose", ""),
        goals=data.get("goals", ()),
        questions=data.get("questions", ()),
        refs=data.get("refs", ()),
        repositories=data.get("repositories", ()),
    )
    print(json.dumps({"project_id": str(project_id)}))
    return 0


def show(
    store: Any,
    workspace_id: UUID,
    actor_id: UUID,
    key: str,
) -> int:
    try:
        project = store.find_project(workspace_id, actor_id, key)
    except (ProjectNotFound, Exception) as exc:
        print(f"projects show: project not found for key {key}: {exc}", file=sys.stderr)
        return 1

    project_id = project["project_id"]
    if isinstance(project_id, str):
        project_id = UUID(project_id)

    read = store.read_project(workspace_id, actor_id, project_id)
    print(json.dumps(read, indent=2))
    return 0


def check(
    store: Any,
    workspace_id: UUID,
    actor_id: UUID,
) -> int:
    projects, missing = store.count_missing_records(workspace_id, actor_id)
    media_discovery_id: str | None = None
    try:
        found = store.find_project(workspace_id, actor_id, "media-discovery")
        if found and "project_id" in found and found["project_id"] is not None:
            media_discovery_id = str(found["project_id"])
    except (ProjectNotFound, KeyError):
        media_discovery_id = None

    payload = {
        "projects": projects,
        "missing_records": missing,
        "media_discovery": media_discovery_id,
    }
    print(json.dumps(payload))
    if missing > 0 or media_discovery_id is None:
        return 1
    return 0


seed_command = seed
show_command = show
check_command = check
cmd_seed = seed
cmd_show = show
cmd_check = check


def sides(
    store: Any,
    workspace_id: UUID,
    actor_id: UUID,
) -> int:
    payload = store.project_sides(workspace_id, actor_id)
    print(json.dumps(payload))
    return 0


def link_world(
    store: Any,
    workspace_id: UUID,
    actor_id: UUID,
    world: str,
    project_id: UUID,
) -> int:
    if world != _MEDIA_DISCOVERY:
        print(f"projects link-world: unknown world: {world}", file=sys.stderr)
        return 2
    try:
        payload = store.link_world(workspace_id, actor_id, project_id)
    except ProjectNotFound as exc:
        print(f"projects link-world: project not found: {exc}", file=sys.stderr)
        return 1
    except WorkStoreError as exc:
        print(f"projects link-world: refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload))
    return 0


def move_item(
    store: Any,
    workspace_id: UUID,
    actor_id: UUID,
    key: str,
    project_id: UUID,
) -> int:
    try:
        payload = store.move_item(workspace_id, actor_id, key, project_id)
    except (ProjectNotFound, WorkItemNotFound) as exc:
        print(f"projects move-item: not found: {exc}", file=sys.stderr)
        return 1
    except WorkStoreError as exc:
        print(f"projects move-item: refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload))
    return 0


sides_command = sides
link_world_command = link_world
move_item_command = move_item
cmd_sides = sides
cmd_link_world = link_world
cmd_move_item = move_item


def run_projects(args: Any, store: Any = None) -> int:
    if store is None:
        from .operations.config import OperationsConfig
        from .v1.store import PostgresWorkStore

        store = PostgresWorkStore(OperationsConfig.defaults())

    command = getattr(args, "projects_command", None)
    if command == "seed":
        return seed(store, args.workspace, args.actor, args.file)
    if command == "show":
        return show(store, args.workspace, args.actor, args.key)
    if command == "check":
        return check(store, args.workspace, args.actor)
    if command == "sides":
        return sides(store, args.workspace, args.actor)
    if command == "link-world":
        return link_world(store, args.workspace, args.actor, args.world, args.project)
    if command == "move-item":
        return move_item(store, args.workspace, args.actor, args.key, args.project)
    return 2

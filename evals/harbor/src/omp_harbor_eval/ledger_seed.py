"""Insert a fixture ledger seed as the WorkService app role.

The file is fixture data (``fixtures/<id>/ledger-seed.json``). The workservice
entrypoint runs this script after ``python -m omp_work ops bootstrap`` and
before ``python -m omp_work serve``. It connects with the service's own
``omp_work_app`` credential and writes the rows the public create command
cannot pin: a predetermined alias and revision id. Re-applying the same seed
is a no-op. A key that already exists at a different revision fails.

Copied to ``/opt/harbor/ledger_seed.py`` in the workservice image, so this
module has no package-relative imports.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from uuid import UUID, uuid4

_KEY_RE = re.compile(r"^(HOME|OMP)-([1-9][0-9]*)$")
_CLOSED = frozenset({"DONE", "CANCELED", "CANCELLED"})
_ITEM_KEYS = frozenset(
    {
        "key",
        "work_id",
        "revision_id",
        "revision_number",
        "title",
        "description",
        "scope",
        "acceptance_criteria",
        "state",
    }
)


def load_ledger_seed(path: Path) -> list[dict[str, object]]:
    """Return the seed items. Raises ``ValueError`` when the file is not fixture data."""

    document = json.loads(path.read_text(encoding="utf-8"))
    items = document.get("items") if isinstance(document, dict) else None
    if not isinstance(items, list) or not items:
        raise ValueError(f"ledger seed {path} must contain a non-empty items list")
    loaded: list[dict[str, object]] = []
    for index, raw in enumerate(items):
        if not isinstance(raw, dict) or set(raw) != _ITEM_KEYS:
            raise ValueError(f"ledger seed {path} item {index} has the wrong keys")
        key = raw["key"]
        match = _KEY_RE.fullmatch(key) if isinstance(key, str) else None
        if match is None:
            raise ValueError(f"ledger seed {path} item {index} has an invalid key")
        state = raw["state"]
        if not isinstance(state, str) or not state.strip() or state in _CLOSED:
            raise ValueError(f"ledger seed {path} item {index} must be an open state")
        criteria = raw["acceptance_criteria"]
        if not isinstance(criteria, list) or not all(isinstance(item, str) for item in criteria):
            raise ValueError(f"ledger seed {path} item {index} acceptance_criteria must be strings")
        revision_number = raw["revision_number"]
        if not isinstance(revision_number, int) or revision_number < 1:
            raise ValueError(f"ledger seed {path} item {index} revision_number must be a positive int")
        title = raw["title"]
        description = raw["description"]
        scope = raw["scope"]
        if not all(isinstance(value, str) and value.strip() for value in (title, description, scope)):
            raise ValueError(f"ledger seed {path} item {index} needs title, description, and scope")
        loaded.append(
            {
                "key": key,
                "alias_number": int(match.group(2)),
                "work_id": UUID(str(raw["work_id"])),
                "revision_id": UUID(str(raw["revision_id"])),
                "revision_number": revision_number,
                "title": title.strip(),
                "description": description,
                "scope": scope,
                "acceptance_criteria": list(criteria),
                "state": state,
            }
        )
    return loaded


def apply_ledger_seed(path: Path) -> None:
    """Insert ``path`` into the bootstrapped ledger named by the service environment."""

    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

    from omp_work.operations.config import OperationsConfig
    from omp_work.v1.canonical import sha256

    items = load_ledger_seed(path)
    config = OperationsConfig.defaults()
    workspace_id = config.workspace_id()
    actor_id = config.actor_id()
    next_alias = max(int(item["alias_number"]) for item in items) + 1
    with psycopg.connect(**config.connection_kwargs("omp_work_app"), row_factory=dict_row) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SET LOCAL search_path = pg_catalog")
                cur.execute(
                    "SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)",
                    (str(workspace_id), str(actor_id)),
                )
                cur.execute(
                    "INSERT INTO omp_control.workspaces(workspace_id) VALUES (%s) ON CONFLICT DO NOTHING",
                    (workspace_id,),
                )
                _ensure_authority(cur, workspace_id, Jsonb)
                for item in items:
                    _insert_item(cur, workspace_id, item, sha256)
                cur.execute(
                    "UPDATE omp_control.workspaces SET next_alias = GREATEST(next_alias, %s) WHERE workspace_id = %s",
                    (next_alias, workspace_id),
                )


def _ensure_authority(cur: object, workspace_id: UUID, jsonb: object) -> None:
    cur.execute(
        "SELECT 1 FROM omp_control.workspace_authority WHERE workspace_id = %s",
        (workspace_id,),
    )
    if cur.fetchone() is not None:
        return
    epoch_id = uuid4()
    cur.execute(
        "INSERT INTO omp_control.cutover_epochs"
        "(epoch_id, workspace_id, state, candidate_manifest, candidate_manifest_sha256) "
        "VALUES (%s, %s, 'sealed', %s, %s)",
        (epoch_id, workspace_id, jsonb({"seeded": True}), "0" * 64),
    )
    cur.execute(
        "INSERT INTO omp_control.workspace_authority(workspace_id, epoch_id) VALUES (%s, %s)",
        (workspace_id, epoch_id),
    )


def _insert_item(cur: object, workspace_id: UUID, item: dict[str, object], sha256: object) -> None:
    cur.execute(
        "SELECT i.current_revision_id FROM omp_work.work_aliases a "
        "JOIN omp_work.work_items i ON i.work_id = a.work_id AND i.workspace_id = a.workspace_id "
        "WHERE a.workspace_id = %s AND a.key = %s",
        (workspace_id, item["key"]),
    )
    existing = cur.fetchone()
    if existing is not None:
        current = existing["current_revision_id"]
        if current != item["revision_id"]:
            raise RuntimeError(
                f"ledger seed {item['key']} exists at revision {current}, fixture declares {item['revision_id']}"
            )
        return
    content_hash = sha256(
        {
            "title": item["title"],
            "description": item["description"],
            "scope": item["scope"],
            "acceptance_criteria": item["acceptance_criteria"],
        }
    )
    cur.execute(
        "INSERT INTO omp_work.work_items(work_id, workspace_id, state, current_revision_id) "
        "VALUES (%s, %s, %s, %s)",
        (item["work_id"], workspace_id, item["state"], item["revision_id"]),
    )
    cur.execute(
        "INSERT INTO omp_work.work_aliases(work_id, workspace_id, key, origin) VALUES (%s, %s, %s, 'local')",
        (item["work_id"], workspace_id, item["key"]),
    )
    cur.execute(
        "INSERT INTO omp_work.work_revisions"
        "(revision_id, work_id, workspace_id, revision_number, title, description, scope, "
        "content_sha256, created_by, supplied_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'service', clock_timestamp())",
        (
            item["revision_id"],
            item["work_id"],
            workspace_id,
            item["revision_number"],
            item["title"],
            item["description"],
            item["scope"],
            content_hash,
        ),
    )
    for position, criterion in enumerate(item["acceptance_criteria"]):
        cur.execute(
            "INSERT INTO omp_work.acceptance_criteria(revision_id, workspace_id, position, criterion) "
            "VALUES (%s, %s, %s, %s)",
            (item["revision_id"], workspace_id, position, criterion),
        )


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: ledger_seed.py <ledger-seed.json>", file=sys.stderr)
        return 2
    apply_ledger_seed(Path(args[0]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

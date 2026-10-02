"""In-flight mission inspection for the drain gate."""

from __future__ import annotations

from psycopg.rows import dict_row

from omp_work.operations.config import OperationsConfig
from omp_work.operations.database import _connect
from omp_work.v1.missions import running_missions

__all__ = ["in_flight"]


def in_flight(config: OperationsConfig) -> list[dict[str, object]]:
    """Workspace missions currently in status running.

    Opens one read-only transaction as omp_work_readonly with workspace and
    actor claims set for RLS.
    """
    workspace_id = config.workspace_id()
    actor_id = config.actor_id()

    with _connect(config, "omp_work_readonly") as conn:
        conn.read_only = True
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SET LOCAL search_path = pg_catalog")
                cur.execute(
                    "SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)",
                    (str(workspace_id), str(actor_id)),
                )
                return running_missions(cur, workspace_id)

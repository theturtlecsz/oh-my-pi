"""Project reads use indexes to exclude other projects in the same workspace."""

from __future__ import annotations

import os
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

pytest_plugins = ["test_workflow_service"]

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


@pytest.mark.parametrize(
    ("table", "id_column", "extra_columns", "extra_values", "order_by"),
    [
        ("project_missions", "mission_id", "objective, status", "'Build', 'draft'", "mission_id"),
        ("project_history", "history_id", "kind, summary", "'created', 'Created'", "at, history_id"),
    ],
)
def test_project_read_uses_both_workspace_and_project_index_keys(
    service, table, id_column, extra_columns, extra_values, order_by
) -> None:
    workspace_id, project_id, other_project_id = uuid4(), uuid4(), uuid4()
    with psycopg.connect(**service.config.connection_kwargs("postgres")) as conn:
        conn.execute(
            "INSERT INTO omp_control.workspaces (workspace_id) VALUES (%s)",
            (workspace_id,),
        )
        for project in (project_id, other_project_id):
            conn.execute(
                "INSERT INTO omp_work.projects"
                " (workspace_id, project_id, key, name, kind, provenance)"
                " VALUES (%s, %s, %s, 'Index test', 'surface', '{}'::jsonb)",
                (workspace_id, project, str(project)),
            )
        conn.execute(
            sql.SQL(
                "INSERT INTO omp_work.{} (workspace_id, project_id, {}, {})"
                " SELECT %s, CASE WHEN n = 1 THEN %s ELSE %s END,"
                " gen_random_uuid(), {} FROM generate_series(1, 3000) AS n"
            ).format(
                sql.Identifier(table),
                sql.Identifier(id_column),
                sql.SQL(extra_columns),
                sql.SQL(extra_values),
            ),
            (workspace_id, project_id, other_project_id),
        )
        conn.execute(sql.SQL("ANALYZE omp_work.{}").format(sql.Identifier(table)))
        plan = conn.execute(
            sql.SQL(
                "EXPLAIN (FORMAT JSON) SELECT * FROM omp_work.{}"
                " WHERE workspace_id = %s AND project_id = %s ORDER BY {}"
            ).format(sql.Identifier(table), sql.SQL(order_by)),
            (workspace_id, project_id),
        ).fetchone()[0][0]["Plan"]

        nodes = [plan]
        scans = []
        while nodes:
            node = nodes.pop()
            nodes.extend(node.get("Plans", []))
            if node.get("Relation Name") == table or "Index Name" in node:
                scans.append(node)
        assert not any(node["Node Type"] == "Seq Scan" for node in scans), plan
        assert any(
            "workspace_id" in node.get("Index Cond", "")
            and "project_id" in node.get("Index Cond", "")
            for node in scans
        ), plan

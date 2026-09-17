"""Contract checks for ordered context-bundle migrations."""

from __future__ import annotations

import psycopg


def test_identity_migration_precedes_content_migration(pg_cluster) -> None:
    with psycopg.connect(pg_cluster.pg_connection_string()) as conn:
        rows = conn.execute(
            "SELECT ordinal, filename FROM omp_knowledge.schema_migrations WHERE filename IN (%s, %s) ORDER BY ordinal",
            ("0004_context_identity_bytes.sql", "0005_context_content_bytes.sql"),
        ).fetchall()
    assert rows == [
        (4, "0004_context_identity_bytes.sql"),
        (5, "0005_context_content_bytes.sql"),
    ]


def test_context_bundle_columns_exist_after_ordered_migrations(pg_cluster) -> None:
    with psycopg.connect(pg_cluster.pg_connection_string()) as conn:
        columns = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = 'omp_knowledge' AND table_name = 'context_bundles'"
            ).fetchall()
        }
    assert {"identity_encoding", "identity_canonical_json", "content_canonical_json"} <= columns

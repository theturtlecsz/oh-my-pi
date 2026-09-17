from __future__ import annotations

import hashlib
import os
import shutil
import socket
import sys
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

# Ensure package and test directories are on sys.path for test discovery (Failure 9)
tests_dir = Path(__file__).resolve().parent
pkg_dir = tests_dir.parent
work_tests_dir = pkg_dir.parent / "omp-work" / "tests"
work_src_dir = pkg_dir.parent / "omp-work" / "src"

for p in (str(tests_dir), str(pkg_dir), str(work_tests_dir), str(work_src_dir)):
    if p not in sys.path:
        sys.path.insert(0, p)

from omp_knowledge.config import KnowledgeConfig
from omp_knowledge.storage.db import apply_migrations

try:
    from pg_native import native_postgres
    PG_NATIVE_AVAILABLE = shutil.which("initdb") is not None and shutil.which("pg_ctl") is not None
except ImportError:
    PG_NATIVE_AVAILABLE = False


@pytest.fixture
def pg_cluster(tmp_path: Path):
    """Spawns an isolated temporary PostgreSQL cluster using native PostgreSQL binaries (Failures 7 & 8).
    Creates a clean omp_knowledge database and applies migrations.
    """
    if not PG_NATIVE_AVAILABLE:
        if os.environ.get("OMP_KNOWLEDGE_REQUIRE_PG") == "1":
            pytest.fail("PostgreSQL 18 binaries (initdb/pg_ctl) required but not available")
        pytest.skip("PostgreSQL 18 binaries (initdb/pg_ctl) not available for native postgres fixture")

    root = tmp_path / "pgdata"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    with native_postgres(root, port):
        admin_dsn = {
            "host": "127.0.0.1",
            "port": port,
            "user": "postgres",
            "dbname": "postgres",
            "autocommit": True,
        }
        with psycopg.connect(**admin_dsn) as conn:
            conn.execute("CREATE DATABASE omp_knowledge;")

        cfg = KnowledgeConfig(
            pg_host="127.0.0.1",
            pg_port=port,
            pg_database="omp_knowledge",
            pg_user="postgres",
            pg_password="",
            native_pg_host="127.0.0.1",
            native_pg_port=port,
            native_pg_database="omp_work",
            native_pg_user="omp_work_readonly",
            native_pg_password="",
            state_dir=tmp_path / "state",
            config_dir=tmp_path / "config",
        )
        with psycopg.connect(cfg.pg_connection_string(), autocommit=True) as conn:
            apply_migrations(conn)
        yield cfg


@pytest.fixture
def dual_pg_cluster(tmp_path: Path):
    """Spawns an isolated temporary PostgreSQL cluster with both omp_knowledge
    and native omp_work databases. Applies schemas and roles to both.
    """
    if not PG_NATIVE_AVAILABLE:
        if os.environ.get("OMP_KNOWLEDGE_REQUIRE_PG") == "1":
            pytest.fail("PostgreSQL 18 binaries (initdb/pg_ctl) required but not available")
        pytest.skip("PostgreSQL 18 binaries (initdb/pg_ctl) not available for native postgres fixture")

    root = tmp_path / "pgdata"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    with native_postgres(root, port):
        admin_dsn = {
            "host": "127.0.0.1",
            "port": port,
            "user": "postgres",
            "dbname": "postgres",
            "autocommit": True,
        }
        with psycopg.connect(**admin_dsn) as conn:
            conn.execute("CREATE DATABASE omp_knowledge;")
            conn.execute("CREATE DATABASE omp_work;")

        # Set up native omp_work roles and schema
        work_ops_dir = pkg_dir.parent / "omp-work" / "src" / "omp_work" / "operations"
        roles_sql = (work_ops_dir / "sql" / "roles.sql").read_text(encoding="utf-8")
        with psycopg.connect(host="127.0.0.1", port=port, user="postgres", dbname="omp_work", autocommit=True) as conn:
            conn.execute(roles_sql)
            # Preserve test passwords for native roles
            for role in (
                "omp_work_migrator",
                "omp_work_app",
                "omp_work_importer",
                "omp_work_readonly",
                "omp_work_backup",
            ):
                conn.execute(f"ALTER ROLE {role} PASSWORD '';")

            try:
                from omp_work.operations.database import migrations as work_migrations
                mig_paths = [path for _, path in work_migrations()]
            except Exception:
                mig_paths = sorted((work_ops_dir / "migrations").glob("*.sql"), key=lambda p: p.name)

            assert len(mig_paths) == 23, f"Expected 23 migrations (0001 through 0023), found {len(mig_paths)}"

            for mig_path in mig_paths:
                conn.execute(mig_path.read_text(encoding="utf-8"))
                ordinal = int(mig_path.name.split("_", 1)[0])
                file_sha = hashlib.sha256(mig_path.read_bytes()).hexdigest()
                conn.execute(
                    """
                    INSERT INTO omp_control.schema_migrations (
                        ordinal, filename, sha256, contract_version, contract_sha256, postgres_major
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (ordinal) DO UPDATE SET
                        filename = EXCLUDED.filename,
                        sha256 = EXCLUDED.sha256
                    """,
                    (ordinal, mig_path.name, file_sha, "1.0", "0" * 64, 18),
                )

            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM omp_control.schema_migrations")
                applied_count = cur.fetchone()[0]
                assert applied_count == 23, f"Expected 23 applied migrations in schema_migrations, got {applied_count}"

            try:
                from omp_work import CONTRACT_VERSION, contract_sha256
                from omp_work.operations.database import migration_set_sha256
                c_ver = CONTRACT_VERSION
                c_sha = contract_sha256()
                m_sha = migration_set_sha256()
            except Exception:
                c_ver = "1.0"
                c_sha = "0" * 64
                m_sha = "0" * 64
            conn.execute(
                """
                INSERT INTO omp_control.runtime_compatibility (
                    contract_version, contract_sha256, migration_set_sha256, postgres_major
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT (singleton) DO UPDATE SET
                    contract_version = EXCLUDED.contract_version,
                    contract_sha256 = EXCLUDED.contract_sha256,
                    migration_set_sha256 = EXCLUDED.migration_set_sha256,
                    postgres_major = EXCLUDED.postgres_major
                """,
                (c_ver, c_sha, m_sha, 18),
            )

        cfg = KnowledgeConfig(
            pg_host="127.0.0.1",
            pg_port=port,
            pg_database="omp_knowledge",
            pg_user="postgres",
            pg_password="",
            native_pg_host="127.0.0.1",
            native_pg_port=port,
            native_pg_database="omp_work",
            native_pg_user="omp_work_readonly",
            native_pg_password="",
            state_dir=tmp_path / "state",
            config_dir=tmp_path / "config",
        )
        with psycopg.connect(cfg.pg_connection_string(), autocommit=True) as conn:
            apply_migrations(conn)
        yield cfg


@pytest.fixture
def native_ledger(dual_pg_cluster: KnowledgeConfig) -> KnowledgeConfig:
    """Convenience alias fixture for dual_pg_cluster matching plan operation references."""
    return dual_pg_cluster

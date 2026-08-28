from __future__ import annotations

from hashlib import sha256
from importlib.resources import files
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from pydantic import BaseModel, Field

from omp_work import CONTRACT_VERSION, contract_sha256, validate_bundle

from .config import OperationsConfig

POSTGRES_MAJOR = 18
MIGRATION_LOCK = "omp-work/migrations"


class _RollbackProbe(Exception):
    pass


class HealthReport(BaseModel):
    live: bool = False
    ready: bool = False
    postgres: dict[str, Any] = Field(default_factory=dict)
    migration: dict[str, Any] = Field(default_factory=dict)
    backup: dict[str, Any] = Field(default_factory=dict)
    wal: dict[str, Any] = Field(default_factory=dict)
    restore: dict[str, Any] = Field(default_factory=dict)
    capacity: dict[str, Any] = Field(default_factory=dict)
    alerts: list[str] = Field(default_factory=list)


def _operations_path(part: str) -> Path:
    return Path(str(files("omp_work.operations").joinpath(part)))


def migrations() -> list[tuple[int, Path]]:
    result: list[tuple[int, Path]] = []
    for path in _operations_path("migrations").glob("*.sql"):
        ordinal = int(path.name.split("_", 1)[0])
        result.append((ordinal, path))
    return sorted(result)


def migration_set_sha256() -> str:
    digest = sha256()
    for _, path in migrations():
        digest.update(path.relative_to(_operations_path(".")).as_posix().encode())
        digest.update(b"\0")
        digest.update(sha256(path.read_bytes()).hexdigest().encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _redacted(error: Exception) -> RuntimeError:
    return RuntimeError("postgres operation failed")


def _connect(
    config: OperationsConfig,
    role: str,
    database: str | None = None,
    *,
    autocommit: bool = False,
) -> psycopg.Connection[Any]:
    kwargs = config.connection_kwargs(role)
    if database is not None:
        kwargs["dbname"] = database
    return psycopg.connect(**kwargs, autocommit=autocommit)


def bootstrap(config: OperationsConfig) -> None:
    validate_bundle(require_approval=True)
    try:
        with _connect(config, "postgres", "postgres", autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SET log_statement = 'none'; SET log_min_error_statement = 'panic'"
                )
                cur.execute("SHOW server_version_num")
                if int(cur.fetchone()[0]) // 10000 != POSTGRES_MAJOR:
                    raise ValueError("postgres_major_mismatch")
                cur.execute(_operations_path("sql/roles.sql").read_text())
                for role in (
                    "omp_work_migrator",
                    "omp_work_app",
                    "omp_work_importer",
                    "omp_work_readonly",
                    "omp_work_backup",
                ):
                    cur.execute(
                        sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                            sql.Identifier(role), sql.Literal(config.read_secret(role))
                        )
                    )
                cur.execute(
                    "SELECT 1 FROM pg_database WHERE datname = %s", (config.database,)
                )
                if cur.fetchone() is None:
                    cur.execute(
                        sql.SQL("CREATE DATABASE {} OWNER omp_work_owner").format(
                            sql.Identifier(config.database)
                        )
                    )
    except Exception as error:
        raise _redacted(error)
    migrate(config)


def check_migrations(
    conn: psycopg.Connection[Any], *, allow_pending: bool
) -> dict[str, list[str]]:
    expected = migrations()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ordinal, filename, sha256 FROM omp_control.schema_migrations ORDER BY ordinal"
        )
        actual = [(int(row[0]), row[1], row[2]) for row in cur.fetchall()]
    expected_rows = [
        (ordinal, path.name, sha256(path.read_bytes()).hexdigest())
        for ordinal, path in expected
    ]
    prefix = expected_rows[: len(actual)]
    drift = [] if actual == prefix else ["migration_drift"]
    pending = [name for _, name, _ in expected_rows[len(actual) :]]
    if drift or (pending and not allow_pending):
        raise ValueError("migration_drift" if drift else "migration_pending")
    return {"pending": pending, "drift": drift}


def migrate(
    config: OperationsConfig,
    target: int | None = None,
    lock_timeout: int = 30,
) -> None:
    validate_bundle(require_approval=True)
    try:
        with _connect(config, "omp_work_migrator") as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL search_path = pg_catalog")
                    cur.execute(
                        "SELECT set_config('lock_timeout', %s, true)",
                        (f"{lock_timeout}s",),
                    )
                    cur.execute("SET LOCAL ROLE omp_work_owner")
                    try:
                        cur.execute(
                            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                            (MIGRATION_LOCK,),
                        )
                    except psycopg.errors.LockNotAvailable as error:
                        raise ValueError("migration_lock_timeout") from error
                    cur.execute("SELECT to_regclass('omp_control.schema_migrations')")
                    if cur.fetchone()[0] is None:
                        state = {
                            "pending": [path.name for _, path in migrations()],
                            "drift": [],
                        }
                    else:
                        state = check_migrations(conn, allow_pending=True)
                    if state["drift"]:
                        raise ValueError("migration_drift")
                    for ordinal, path in migrations():
                        if target is not None and ordinal > target:
                            break
                        if path.name not in state["pending"]:
                            continue
                        cur.execute(path.read_text())
                        cur.execute(
                            "INSERT INTO omp_control.schema_migrations (ordinal, filename, sha256, contract_version, contract_sha256, postgres_major) VALUES (%s,%s,%s,%s,%s,%s)",
                            (
                                ordinal,
                                path.name,
                                sha256(path.read_bytes()).hexdigest(),
                                CONTRACT_VERSION,
                                contract_sha256(),
                                POSTGRES_MAJOR,
                            ),
                        )
                    cur.execute(
                        "SELECT to_regclass('omp_control.runtime_compatibility')"
                    )
                    if cur.fetchone()[0]:
                        cur.execute(
                            "INSERT INTO omp_control.runtime_compatibility (contract_version, contract_sha256, migration_set_sha256, postgres_major) VALUES (%s,%s,%s,%s) ON CONFLICT (singleton) DO UPDATE SET contract_version=EXCLUDED.contract_version, contract_sha256=EXCLUDED.contract_sha256, migration_set_sha256=EXCLUDED.migration_set_sha256, postgres_major=EXCLUDED.postgres_major",
                            (
                                CONTRACT_VERSION,
                                contract_sha256(),
                                migration_set_sha256(),
                                POSTGRES_MAJOR,
                            ),
                        )
    except ValueError:
        raise
    except Exception as error:
        raise _redacted(error)


def collect_health(
    config: OperationsConfig, role: str = "omp_work_migrator"
) -> HealthReport:
    report = HealthReport()
    try:
        with _connect(config, role) as conn:
            with conn.cursor() as cur:
                cur.execute("SHOW server_version_num")
                version = int(cur.fetchone()[0])
                cur.execute(
                    "SELECT pg_is_in_recovery(), current_setting('transaction_read_only')"
                )
                recovery, read_only = cur.fetchone()
                report.live = True
                report.postgres = {
                    "major": version // 10000,
                    "in_recovery": recovery,
                    "read_only": read_only == "on",
                }
                try:
                    migration = check_migrations(conn, allow_pending=False)
                except ValueError as error:
                    migration = {"pending": [], "drift": [str(error)]}
                report.migration = migration | {"set_sha256": migration_set_sha256()}
                cur.execute(
                    "SELECT contract_version, contract_sha256, migration_set_sha256, postgres_major FROM omp_control.runtime_compatibility"
                )
                compatible = cur.fetchone() == (
                    CONTRACT_VERSION,
                    contract_sha256(),
                    migration_set_sha256(),
                    POSTGRES_MAJOR,
                )
                if not compatible:
                    report.alerts.append("MIGRATION_DRIFT")
                if recovery:
                    report.alerts.append("POSTGRES_RECOVERY")
                if read_only == "on":
                    report.alerts.append("POSTGRES_READ_ONLY")
                if migration["pending"]:
                    report.alerts.append("MIGRATION_PENDING")
                if migration["drift"]:
                    report.alerts.append("MIGRATION_DRIFT")
                cur.execute(
                    "SELECT DISTINCT ON (kind) kind, outcome, ended_at::text, byte_count, duration_seconds::text FROM omp_control.operations_evidence ORDER BY kind, ended_at DESC NULLS LAST"
                )
                evidence = {
                    row[0]: {
                        "outcome": row[1],
                        "ended_at": row[2],
                        "byte_count": row[3],
                        "duration_seconds": row[4],
                    }
                    for row in cur.fetchall()
                }
                report.backup = evidence.get("backup", {})
                report.restore = evidence.get("restore_drill", {})
                report.wal = {"current_lsn": None}
                cur.execute(
                    "SELECT pg_current_wal_lsn()::text, pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0')::bigint, pg_database_size(current_database())"
                )
                current_lsn, wal_bytes, database_bytes = cur.fetchone()
                report.wal = {
                    "current_lsn": current_lsn,
                    "bytes_since_init": wal_bytes,
                    **evidence.get("wal_upload", {}),
                }
                report.capacity = {"database_bytes": database_bytes}
                if not report.backup:
                    report.alerts.append("BACKUP_MISSING")
                if not report.restore:
                    report.alerts.append("RESTORE_DRILL_MISSING")
                try:
                    with conn.transaction():
                        cur.execute(
                            "INSERT INTO omp_control.readiness_probe DEFAULT VALUES ON CONFLICT (singleton) DO UPDATE SET checked_at = clock_timestamp()"
                        )
                        raise _RollbackProbe()
                except _RollbackProbe:
                    pass
                report.ready = (
                    not any(
                        alert
                        in {
                            "MIGRATION_DRIFT",
                            "POSTGRES_RECOVERY",
                            "POSTGRES_READ_ONLY",
                            "MIGRATION_PENDING",
                        }
                        for alert in report.alerts
                    )
                    and version // 10000 == POSTGRES_MAJOR
                    and compatible
                )
    except Exception:
        report.alerts.append("POSTGRES_UNAVAILABLE")
    return report


def check(config: OperationsConfig) -> dict[str, Any]:
    validate_bundle(require_approval=True)
    with _connect(config, "omp_work_migrator") as conn:
        migration = check_migrations(conn, allow_pending=False)
        with conn.cursor() as cur:
            cur.execute("SHOW server_version")
            version = cur.fetchone()[0]
    return {
        "postgres": version,
        "contract": CONTRACT_VERSION,
        "contract_sha256": contract_sha256(),
        "migration_set_sha256": migration_set_sha256(),
        **migration,
    }

"""Apply omp_jobs schema migrations (separate from work.omp.dev/v1).

NOT wired into migration_set_sha256() or the main migrate() path.
Uses OperationsConfig.defaults() + _connect as omp_work_migrator.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_JOBS_MIGRATIONS = _HERE / "jobs_migrations"


def jobs_migrations_dir() -> Path:
    return _JOBS_MIGRATIONS


def jobs_migrations() -> list[Path]:
    """List jobs_migrations/*.sql sorted by ordinal prefix."""
    d = jobs_migrations_dir()
    if not d.is_dir():
        return []
    return sorted(d.glob("*.sql"), key=_ordinal_key)


def _ordinal_key(p: Path) -> tuple[int, str]:
    m = re.match(r"^(\d+)_", p.name)
    return (int(m.group(1)) if m else 10**9, p.name)


def _sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _load_ops():
    """Resolve OperationsConfig + _connect across known module layouts."""
    errors = []
    # try config module paths
    cfg_cls = None
    for modname in (
        "omp_work.operations.config",
        "omp_work.operations",
        "omp_work.ops.config",
    ):
        try:
            mod = __import__(modname, fromlist=["OperationsConfig", "OperationsConfig"])
            cfg_cls = getattr(mod, "OperationsConfig", None) or getattr(mod, "OperationsConfig", None)
            if cfg_cls is not None:
                break
        except Exception as e:
            errors.append(f"{modname}:{e}")
    if cfg_cls is None:
        raise RuntimeError("OperationsConfig not found: " + "; ".join(errors))

    connect = None
    mig = None
    for modname in (
        "omp_work.operations.database",
        "omp_work.operations.migrate",
        "omp_work.operations.migrations",
        "omp_work.operations",
    ):
        try:
            mig = __import__(modname, fromlist=["_connect", "migration_set_sha256"])
            connect = getattr(mig, "_connect", None)
            if connect is not None:
                break
        except Exception as e:
            errors.append(f"{modname}:{e}")
    if connect is None:
        raise RuntimeError("_connect not found: " + "; ".join(errors))
    return cfg_cls, connect, mig


def apply_jobs_migrations(config=None) -> dict:
    """Apply pending jobs_migrations/*.sql via migrator role.

    Records each applied file in omp_jobs.schema_migrations.
    Does not touch work.omp.dev/v1 migration bookkeeping.
    """
    cfg_cls, connect, _mig = _load_ops()
    if config is None:
        if hasattr(cfg_cls, "defaults"):
            config = cfg_cls.defaults()
        elif hasattr(cfg_cls, "default"):
            config = cfg_cls.default()
        else:
            config = cfg_cls()

    files = jobs_migrations()
    applied: list[dict] = []
    skipped: list[str] = []

    with connect(config, "omp_work_migrator") as conn:
        # psycopg2 connection or context manager yielding connection
        if hasattr(conn, "cursor"):
            real = conn
        else:
            # some wrappers
            real = conn
        autocommit_set = False
        if hasattr(real, "autocommit"):
            try:
                real.autocommit = False
                autocommit_set = True
            except Exception:
                pass
        with real.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('omp-jobs/migrations'))")
            cur.execute(
                """
                SELECT EXISTS(
                  SELECT 1 FROM information_schema.tables
                  WHERE table_schema='omp_jobs' AND table_name='schema_migrations'
                )
                """
            )
            has_track = bool(cur.fetchone()[0])
            already: dict[int, tuple[str, str]] = {}
            if has_track:
                cur.execute("SELECT ordinal,filename,sha256 FROM omp_jobs.schema_migrations")
                already = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

            for path in files:
                ordinal = _ordinal_key(path)[0]
                digest = _sha256_file(path)
                if ordinal in already:
                    if already[ordinal] != (path.name, digest):
                        raise RuntimeError(f"jobs migration drift at ordinal {ordinal}")
                    skipped.append(path.name)
                    continue
                sql = path.read_text()
                cur.execute(sql)
                cur.execute(
                    """
                    INSERT INTO omp_jobs.schema_migrations (ordinal, filename, sha256)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (ordinal) DO NOTHING
                    """,
                    (ordinal, path.name, digest),
                )
                applied.append(
                    {"ordinal": ordinal, "filename": path.name, "sha256": digest}
                )
        if hasattr(real, "commit"):
            real.commit()

    return {
        "applied": applied,
        "skipped": skipped,
        "files_seen": [p.name for p in files],
    }


def migration_set_sha256_work():
    """Proxy to work.omp.dev/v1 migration_set_sha256 (must remain unchanged)."""
    _cfg_cls, _connect, mig = _load_ops()
    fn = getattr(mig, "migration_set_sha256", None)
    if not callable(fn):
        raise RuntimeError("migration_set_sha256 missing on migrate module")
    try:
        return fn()
    except TypeError:
        cfg = _cfg_cls.defaults() if hasattr(_cfg_cls, "defaults") else _cfg_cls()
        return fn(cfg)


def main() -> None:
    import json
    import os

    os.environ.setdefault("OMP_WORK_POSTGRES_PORT", "54321")
    result = apply_jobs_migrations()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

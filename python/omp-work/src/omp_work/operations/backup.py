from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import time
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from subprocess import CalledProcessError, run
from uuid import uuid4

from omp_work import contract_sha256, validate_bundle

from .artifacts import decrypt_file, encrypt_file
from .config import OperationsConfig
from .database import migration_set_sha256, migrations


def _run(arguments: list[str], *, env: dict[str, str] | None = None) -> bytes:
    try:
        return run(arguments, capture_output=True, check=True, env=env).stdout
    except CalledProcessError as error:
        raise RuntimeError("backup operation failed") from error


def _aws(config: OperationsConfig, arguments: list[str]) -> bytes:
    command = ["aws", "--profile", config.aws_profile, "--region", config.aws_region]
    if config.endpoint_url:
        command += ["--endpoint-url", config.endpoint_url]
    return _run(command + arguments)


def _postgres_env(config: OperationsConfig) -> dict[str, str]:
    return os.environ | {"PGPASSWORD": config.read_secret("omp_work_backup")}


def provision_target(config: OperationsConfig) -> None:
    validate_bundle(require_approval=True)
    existing = _aws(
        config,
        [
            "s3api",
            "list-buckets",
            "--query",
            f"Buckets[?Name=='{config.bucket}'].Name",
            "--output",
            "text",
        ],
    ).strip()
    if existing:
        raise ValueError("backup target already exists")
    _aws(config, ["s3api", "create-bucket", "--bucket", config.bucket])
    _aws(
        config,
        [
            "s3api",
            "put-bucket-versioning",
            "--bucket",
            config.bucket,
            "--versioning-configuration",
            "Status=Enabled",
        ],
    )
    lifecycle = '{"Rules":[{"ID":"work-ledger-safety","Status":"Enabled","Filter":{"Prefix":"work-ledger/v1/"},"AbortIncompleteMultipartUpload":{"DaysAfterInitiation":7},"NoncurrentVersionExpiration":{"NoncurrentDays":30}}]}'
    _aws(
        config,
        [
            "s3api",
            "put-bucket-lifecycle-configuration",
            "--bucket",
            config.bucket,
            "--lifecycle-configuration",
            lifecycle,
        ],
    )


def verify_target(config: OperationsConfig) -> None:
    key = f"{config.prefix}/probe/{uuid4()}"
    # aws CLI rejects /dev/null as --body (character device, not a regular file).
    with tempfile.NamedTemporaryFile() as empty:
        _aws(
            config,
            [
                "s3api",
                "put-object",
                "--bucket",
                config.bucket,
                "--key",
                key,
                "--body",
                empty.name,
            ],
        )
    _aws(config, ["s3api", "head-object", "--bucket", config.bucket, "--key", key])
    _aws(config, ["s3api", "delete-object", "--bucket", config.bucket, "--key", key])


def create(config: OperationsConfig) -> str:
    validate_bundle(require_approval=True)
    started = datetime.now(UTC)
    began = time.monotonic()
    backup_id = str(uuid4())
    stamp = datetime.now(UTC).strftime("%Y/%m/%d")
    staging = config.state_dir / "staging" / backup_id
    staging.mkdir(mode=0o700, parents=True, exist_ok=False)
    try:
        dump = staging / "ledger.dump"
        physical_dir = staging / "physical"
        physical = staging / "physical.tar"
        artifacts = [dump]
        _run(
            [
                "pg_dump",
                "--host",
                config.host,
                "--port",
                str(config.port),
                "--username",
                "omp_work_backup",
                "--format=custom",
                "--no-owner",
                "--no-privileges",
                "--file",
                str(dump),
                config.database,
            ],
            env=_postgres_env(config),
        )
        _run(
            [
                "pg_basebackup",
                "--host",
                config.host,
                "--port",
                str(config.port),
                "--username",
                "omp_work_backup",
                "--format=tar",
                "--gzip",
                "--wal-method=stream",
                "--pgdata",
                str(physical_dir),
            ],
            env=_postgres_env(config),
        )
        _run(["tar", "-C", str(physical_dir), "-cf", str(physical), "."])
        artifacts.append(physical)
        base = f"{config.prefix}/base/{stamp}/{backup_id}"
        objects: list[tuple[str, str]] = []
        for artifact in artifacts:
            encrypted = artifact.with_suffix(artifact.suffix + ".gpg")
            digest = encrypt_file(
                artifact, encrypted, config.secret_path("gpg-passphrase")
            )
            key = f"{base}/{encrypted.name}"
            metadata = f"sha256={digest},backup-id={backup_id},contract-sha256={contract_sha256()},migration-set-sha256={migration_set_sha256()}"
            _aws(
                config,
                [
                    "s3api",
                    "put-object",
                    "--bucket",
                    config.bucket,
                    "--key",
                    key,
                    "--body",
                    str(encrypted),
                    "--metadata",
                    metadata,
                ],
            )
            _aws(
                config,
                ["s3api", "head-object", "--bucket", config.bucket, "--key", key],
            )
            objects.append((key, digest))
        manifest = staging / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "backup_id": backup_id,
                    "contract_sha256": contract_sha256(),
                    "migration_set_sha256": migration_set_sha256(),
                    "objects": [
                        {"key": key, "sha256": digest} for key, digest in objects
                    ],
                }
            )
            + "\n"
        )
        encrypted_manifest = staging / "manifest.json.gpg"
        encrypt_file(manifest, encrypted_manifest, config.secret_path("gpg-passphrase"))
        _aws(
            config,
            [
                "s3api",
                "put-object",
                "--bucket",
                config.bucket,
                "--key",
                f"{base}/manifest.json.gpg",
                "--body",
                str(encrypted_manifest),
            ],
        )
        complete = staging / "COMPLETE"
        complete.touch()
        _aws(
            config,
            [
                "s3api",
                "put-object",
                "--bucket",
                config.bucket,
                "--key",
                f"{base}/COMPLETE",
                "--body",
                str(complete),
            ],
        )
        _record_evidence(
            config,
            kind="backup",
            started=started,
            backup_id=backup_id,
            prefix=base,
            outcome="passed",
            duration=time.monotonic() - began,
            byte_count=sum(artifact.stat().st_size for artifact in artifacts),
        )
        return backup_id
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def upload_wal(config: OperationsConfig) -> int:
    spool = config.data_dir / "wal"
    if not spool.exists():
        return 0
    uploaded = 0
    for segment in spool.iterdir():
        if not segment.is_file() or segment.suffix == ".gpg":
            continue
        encrypted = segment.with_suffix(segment.suffix + ".gpg")
        try:
            digest = encrypt_file(
                segment, encrypted, config.secret_path("gpg-passphrase")
            )
            key = f"{config.prefix}/wal/unknown/{segment.name}.gpg"
            _aws(
                config,
                [
                    "s3api",
                    "put-object",
                    "--bucket",
                    config.bucket,
                    "--key",
                    key,
                    "--body",
                    str(encrypted),
                    "--metadata",
                    f"sha256={digest}",
                ],
            )
            _aws(
                config,
                ["s3api", "head-object", "--bucket", config.bucket, "--key", key],
            )
            segment.unlink()
            uploaded += 1
        finally:
            encrypted.unlink(missing_ok=True)
    return uploaded


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def clone_primary(config: OperationsConfig, destination: Path) -> None:
    """Take a consistent physical clone without stopping the authoritative primary."""
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _run(
        [
            "pg_basebackup",
            "--host",
            config.host,
            "--port",
            str(config.port),
            "--username",
            "omp_work_backup",
            "--format=plain",
            "--wal-method=stream",
            "--checkpoint=fast",
            "--pgdata",
            str(destination),
        ],
        env=_postgres_env(config),
    )


def _latest_backup(
    config: OperationsConfig, backup_id: str | None = None
) -> tuple[str, dict[str, object]]:
    response = json.loads(
        _aws(
            config,
            [
                "s3api",
                "list-objects-v2",
                "--bucket",
                config.bucket,
                "--prefix",
                f"{config.prefix}/base/",
                "--output",
                "json",
            ],
        )
    )
    completed = [
        item
        for item in response.get("Contents", [])
        if isinstance(item, dict)
        and isinstance(item.get("Key"), str)
        and item["Key"].endswith("/COMPLETE")
    ]
    if backup_id is not None:
        completed = [
            item for item in completed if item["Key"].endswith(f"/{backup_id}/COMPLETE")
        ]
        if len(completed) != 1:
            raise RuntimeError("requested complete backup not found")
    elif not completed:
        raise RuntimeError("no complete backup available")
    prefix = max(completed, key=lambda item: str(item.get("LastModified", "")))[
        "Key"
    ].removesuffix("COMPLETE")
    return prefix, response


def _download_decrypt(config: OperationsConfig, key: str, destination: Path) -> None:
    encrypted = destination.with_suffix(destination.suffix + ".gpg")
    _aws(
        config,
        [
            "s3api",
            "get-object",
            "--bucket",
            config.bucket,
            "--key",
            key,
            str(encrypted),
        ],
    )
    decrypt_file(encrypted, destination, config.secret_path("gpg-passphrase"))


def _record_evidence(
    config: OperationsConfig,
    *,
    kind: str,
    started: datetime,
    backup_id: str | None,
    prefix: str | None,
    outcome: str,
    duration: float,
    byte_count: int | None = None,
) -> str:
    from .database import _connect

    with _connect(config, "omp_work_backup") as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_current_wal_lsn()")
            source_lsn = cur.fetchone()[0]
            receipt_sha256 = sha256(
                json.dumps(
                    {
                        "backup_id": backup_id,
                        "byte_count": byte_count,
                        "contract_sha256": contract_sha256(),
                        "duration_seconds": duration,
                        "kind": kind,
                        "migration_set_sha256": migration_set_sha256(),
                        "object_prefix": prefix,
                        "outcome": outcome,
                        "source_lsn": str(source_lsn),
                        "source_timeline": "source-primary",
                        "started_at": started.isoformat(),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            cur.execute(
                "INSERT INTO omp_control.operations_evidence (kind, started_at, ended_at, source_timeline, source_lsn, contract_sha256, migration_set_sha256, backup_id, object_prefix, byte_count, outcome, duration_seconds, receipt_sha256) VALUES (%s, %s, clock_timestamp(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (
                    kind,
                    started,
                    "source-primary",
                    source_lsn,
                    contract_sha256(),
                    migration_set_sha256(),
                    backup_id,
                    prefix,
                    byte_count,
                    outcome,
                    duration,
                    receipt_sha256,
                ),
            )
            return str(cur.fetchone()[0])


def restore_drill(
    config: OperationsConfig, *, reason: str, backup_id: str | None = None
) -> str:
    validate_bundle(require_approval=True)
    started = datetime.now(UTC)
    began = time.monotonic()
    prefix: str | None = None
    staging = config.state_dir / "restore-drills" / str(uuid4())
    sock_dir: Path | None = None
    try:
        prefix, _ = _latest_backup(config, backup_id)
        manifest = staging / "manifest.json"
        staging.mkdir(mode=0o700, parents=True, exist_ok=False)
        _download_decrypt(config, f"{prefix}manifest.json.gpg", manifest)
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        backup_id = payload["backup_id"]
        if (
            payload["contract_sha256"] != contract_sha256()
            or payload["migration_set_sha256"] != migration_set_sha256()
        ):
            raise RuntimeError("backup compatibility mismatch")
        dump = staging / "ledger.dump"
        dump_key = next(
            item["key"]
            for item in payload["objects"]
            if item["key"].endswith("/ledger.dump.gpg")
        )
        _download_decrypt(config, dump_key, dump)
        port = _free_port()
        password = config.read_secret("postgres")
        data_dir = staging / "pgdata"
        sock_dir = Path(tempfile.mkdtemp(prefix="omp-work-pg-"))
        _run(
            [
                "initdb",
                "-D",
                str(data_dir),
                "-U",
                "postgres",
                "--auth-local=trust",
                "--auth-host=scram-sha-256",
                "-E",
                "UTF8",
                f"--pwfile={config.secret_path('postgres')}",
            ]
        )
        _run(
            [
                "pg_ctl",
                "-D",
                str(data_dir),
                "-l",
                str(staging / "postgres.log"),
                "-w",
                "-o",
                f"-p {port} -k {sock_dir} -c listen_addresses=127.0.0.1",
                "start",
            ]
        )
        env = os.environ | {"PGPASSWORD": password}
        _run(
            [
                "pg_restore",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--username",
                "postgres",
                "--dbname",
                "postgres",
                "--no-owner",
                "--no-privileges",
                "--exit-on-error",
                str(dump),
            ],
            env=env,
        )
        actual = (
            _run(
                [
                    "psql",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--username",
                    "postgres",
                    "--dbname",
                    "postgres",
                    "--tuples-only",
                    "--no-align",
                    "--command",
                    "SELECT count(*) FROM omp_control.schema_migrations",
                ],
                env=env,
            )
            .decode()
            .strip()
        )
        if actual != str(len(migrations())):
            raise RuntimeError("restore migration verification failed")
        return _record_evidence(
            config,
            kind="restore_drill",
            started=started,
            backup_id=backup_id,
            prefix=prefix,
            outcome=f"passed:{reason}",
            duration=time.monotonic() - began,
        )
    except Exception:
        try:
            _record_evidence(
                config,
                kind="restore_drill",
                started=started,
                backup_id=backup_id,
                prefix=prefix,
                outcome=f"failed:{reason}",
                duration=time.monotonic() - began,
            )
        except Exception:
            pass
        raise
    finally:
        if (staging / "pgdata" / "PG_VERSION").exists():
            run(
                [
                    "pg_ctl",
                    "-D",
                    str(staging / "pgdata"),
                    "-m",
                    "immediate",
                    "-w",
                    "stop",
                ],
                capture_output=True,
            )
        shutil.rmtree(staging, ignore_errors=True)
        if sock_dir is not None:
            shutil.rmtree(sock_dir, ignore_errors=True)

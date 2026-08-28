from __future__ import annotations

import argparse
import json
import os
import secrets
from pathlib import Path
from uuid import UUID, uuid4

from psycopg import sql

from omp_work.integration.importer import LinearImporter

from . import backup
from .capabilities import _write_secret, provision_candidate_reader, provision_owner
from .config import OperationsConfig
from .database import bootstrap, check, collect_health, migrate


def _read_or_create_uuid(config: OperationsConfig, name: str) -> UUID:
    path = config.secret_path(name)
    if path.exists():
        try:
            return UUID(config.read_secret(name))
        except ValueError as error:
            raise ValueError(
                f"existing {name} credential is malformed or unprotected; refusing to replace it"
            ) from error
    _write_secret(path, str(uuid4()))
    return UUID(config.read_secret(name))


def credentials_init(config: OperationsConfig) -> tuple[UUID, UUID]:
    for role in (
        "postgres",
        "omp_work_migrator",
        "omp_work_app",
        "omp_work_importer",
        "omp_work_readonly",
        "omp_work_backup",
    ):
        path = config.secret_path(role)
        if not path.exists():
            _write_secret(path, secrets.token_urlsafe(32))
    passphrase = config.secret_path("gpg-passphrase")
    if not passphrase.exists():
        _write_secret(passphrase, secrets.token_urlsafe(48))
    actor_id = _read_or_create_uuid(config, "operator-actor-id")
    workspace_id = _read_or_create_uuid(config, "workspace-id")
    return workspace_id, actor_id


def credentials_rotate(config: OperationsConfig, role: str) -> None:
    if role not in {
        "omp_work_migrator",
        "omp_work_app",
        "omp_work_importer",
        "omp_work_readonly",
        "omp_work_backup",
    }:
        raise ValueError("invalid role")
    from .database import _connect

    replacement = secrets.token_urlsafe(32)
    temporary = config.secret_path(role).with_suffix(".next")
    _write_secret(temporary, replacement)
    try:
        with _connect(config, "postgres", "postgres") as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SET log_statement = 'none'; SET log_min_error_statement = 'panic'"
                )
                cur.execute(
                    sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                        sql.Identifier(role), sql.Literal(replacement)
                    )
                )
    except Exception as error:
        raise RuntimeError(
            "credential rotation failed; recovery credential retained"
        ) from error
    os.replace(temporary, config.secret_path(role))


def add_parser(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="ops_command", required=True)
    commands.add_parser("bootstrap")
    migrate_parser = commands.add_parser("migrate")
    migrate_parser.add_argument("--target", type=int)
    migrate_parser.add_argument("--lock-timeout", type=int, default=30)
    commands.add_parser("check")
    health = commands.add_parser("health")
    health.add_argument("--mode", choices=("live", "ready"), default="ready")
    health.add_argument("--json", action="store_true")
    credentials = commands.add_parser("credentials").add_subparsers(
        dest="credentials_command", required=True
    )
    credentials.add_parser("init")
    rotate = credentials.add_parser("rotate")
    rotate.add_argument("role")
    capabilities = commands.add_parser("capabilities").add_subparsers(
        dest="capabilities_command", required=True
    )
    capability_init = capabilities.add_parser("init")
    capability_init.add_argument("--workspace-id", required=True)
    capability_init.add_argument("--owner-id", required=True)
    capability_init.add_argument("--base-url", default="http://127.0.0.1:54322")
    reader = capabilities.add_parser("candidate-reader")
    reader.add_argument("--workspace-id", required=True)
    reader.add_argument("--candidate-id", action="append", required=True)
    reader.add_argument("--name", default="candidate-reader")
    backup_parser = commands.add_parser("backup").add_subparsers(
        dest="backup_command", required=True
    )
    backup_parser.add_parser("provision-target")
    backup_parser.add_parser("verify-target")
    backup_parser.add_parser("create")
    backup_parser.add_parser("wal")
    restore = commands.add_parser("restore").add_subparsers(
        dest="restore_command", required=True
    )
    drill = restore.add_parser("drill")
    drill.add_argument("--source", choices=("latest",), default="latest")
    drill.add_argument(
        "--reason", choices=("clean-instance", "monthly", "manual"), required=True
    )
    linear_import = commands.add_parser("linear-import").add_subparsers(
        dest="linear_import_command", required=True
    )
    stage = linear_import.add_parser("stage")
    stage.add_argument("--workspace-id", required=True)
    stage.add_argument("--export-id", required=True)
    stage.add_argument("--mapping-file", required=True)
    reconcile = linear_import.add_parser("reconcile")
    reconcile.add_argument("--batch-id", required=True)
    promote = linear_import.add_parser("promote")
    promote.add_argument("--batch-id", required=True)


def run(args: argparse.Namespace, config: OperationsConfig | None = None) -> None:
    config = config or OperationsConfig.defaults()
    command = args.ops_command
    if command == "bootstrap":
        bootstrap(config)
    elif command == "migrate":
        migrate(config, args.target, args.lock_timeout)
    elif command == "check":
        print(json.dumps(check(config), sort_keys=True))
    elif command == "health":
        report = collect_health(config)
        if args.json:
            print(report.model_dump_json())
        if (args.mode == "live" and not report.live) or (
            args.mode == "ready" and not report.ready
        ):
            raise SystemExit(1)
    elif command == "credentials":
        if args.credentials_command == "init":
            workspace_id, actor_id = credentials_init(config)
            print(
                json.dumps(
                    {"workspace_id": str(workspace_id), "owner_id": str(actor_id)},
                    sort_keys=True,
                )
            )
        else:
            credentials_rotate(config, args.role)
    elif command == "capabilities":
        if args.capabilities_command == "init":
            path = provision_owner(
                config,
                workspace_id=UUID(args.workspace_id),
                owner_id=UUID(args.owner_id),
                base_url=args.base_url,
            )
        else:
            path = provision_candidate_reader(
                config,
                workspace_id=UUID(args.workspace_id),
                candidate_ids=tuple(UUID(value) for value in args.candidate_id),
                name=args.name,
            )
        print(path)
    elif command == "backup":
        if args.backup_command == "provision-target":
            backup.provision_target(config)
        elif args.backup_command == "verify-target":
            backup.verify_target(config)
        elif args.backup_command == "create":
            print(backup.create(config))
        else:
            print(backup.upload_wal(config))
    elif command == "linear-import":
        importer = LinearImporter(config)
        if args.linear_import_command == "stage":
            summary = importer.stage(
                UUID(args.workspace_id), UUID(args.export_id), Path(args.mapping_file)
            )
        elif args.linear_import_command == "reconcile":
            summary = importer.reconcile(UUID(args.batch_id))
        else:
            summary = importer.promote(UUID(args.batch_id))
        output = {
            "anomaly_codes": summary.anomaly_codes,
            "artifacts": summary.artifacts,
            "base_batch_id": str(summary.base_batch_id)
            if summary.base_batch_id
            else None,
            "batch_id": str(summary.batch_id),
            "disposition_counts": summary.disposition_counts,
            "export_id": str(summary.export_id),
            "mapping_file_sha256": summary.mapping_file_sha256,
            "parity_hashes": summary.parity_hashes,
            "reconciliation_sha256": summary.reconciliation_sha256,
            "state": summary.state,
            "transformation_version": summary.transformation_version,
            "workspace_id": str(summary.workspace_id),
        }
        print(json.dumps(output, sort_keys=True))
        if summary.state == "blocked":
            raise SystemExit(2)
    else:
        print(backup.restore_drill(config, reason=args.reason))

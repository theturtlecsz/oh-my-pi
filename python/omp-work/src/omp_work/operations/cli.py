from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
from uuid import UUID, uuid4
from psycopg import sql

from . import backup
from .capabilities import _write_secret, provision_candidate_reader, provision_cutover, provision_owner
from .config import OperationsConfig
from .database import bootstrap, check, collect_health, migrate
from omp_work.integration.exporter import LinearExporter
from omp_work.integration.importer import LinearImporter
from omp_work.integration.linear import oauth_login


def _read_or_create_uuid(config: OperationsConfig, name: str) -> UUID:
    path = config.secret_path(name)
    if path.exists():
        try:
            return UUID(config.read_secret(name))
        except ValueError as error:
            raise ValueError(f"existing {name} credential is malformed or unprotected; refusing to replace it") from error
    _write_secret(path, str(uuid4()))
    return UUID(config.read_secret(name))


def credentials_init(config: OperationsConfig) -> tuple[UUID, UUID]:
    for role in ("postgres", "omp_work_migrator", "omp_work_app", "omp_work_importer", "omp_work_readonly", "omp_work_backup"):
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
    if role not in {"omp_work_migrator", "omp_work_app", "omp_work_importer", "omp_work_readonly", "omp_work_backup"}:
        raise ValueError("invalid role")
    from .database import _connect
    replacement = secrets.token_urlsafe(32)
    temporary = config.secret_path(role).with_suffix(".next")
    _write_secret(temporary, replacement)
    try:
        with _connect(config, "postgres", "postgres") as conn:
            with conn.cursor() as cur:
                cur.execute("SET log_statement = 'none'; SET log_min_error_statement = 'panic'")
                cur.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(sql.Identifier(role), sql.Literal(replacement)))
    except Exception as error:
        raise RuntimeError("credential rotation failed; recovery credential retained") from error
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
    credentials = commands.add_parser("credentials").add_subparsers(dest="credentials_command", required=True)
    credentials.add_parser("init")
    rotate = credentials.add_parser("rotate")
    rotate.add_argument("role")
    capabilities = commands.add_parser("capabilities").add_subparsers(dest="capabilities_command", required=True)
    capability_init = capabilities.add_parser("init")
    capability_init.add_argument("--workspace-id", required=True)
    capability_init.add_argument("--owner-id", required=True)
    capability_init.add_argument("--base-url", default="http://127.0.0.1:54322")
    reader = capabilities.add_parser("candidate-reader")
    reader.add_argument("--workspace-id", required=True)
    reader.add_argument("--candidate-id", action="append", required=True)
    reader.add_argument("--name", default="candidate-reader")
    cutover = capabilities.add_parser("cutover")
    cutover.add_argument("--rotate", action="store_true")
    backup_parser = commands.add_parser("backup").add_subparsers(dest="backup_command", required=True)
    backup_parser.add_parser("provision-target")
    backup_parser.add_parser("verify-target")
    backup_parser.add_parser("create")
    backup_parser.add_parser("wal")
    restore = commands.add_parser("restore").add_subparsers(dest="restore_command", required=True)
    drill = restore.add_parser("drill")
    drill.add_argument("--source", choices=("latest",), default="latest")
    drill.add_argument("--reason", choices=("clean-instance", "monthly", "manual"), required=True)
    linear_export = commands.add_parser("linear-export").add_subparsers(dest="linear_export_command", required=True)
    oauth_login_parser = linear_export.add_parser("oauth-login")
    oauth_login_parser.add_argument("--client-id", required=True)
    oauth_login_parser.add_argument("--force", action="store_true")
    for mode in ("full", "delta"):
        export = linear_export.add_parser(mode)
        export.add_argument("--workspace-id", required=True)
    resume = linear_export.add_parser("resume")
    resume.add_argument("--export-id", required=True)
    cutover = commands.add_parser("cutover").add_subparsers(dest="cutover_command", required=True)
    cutover.add_parser("preflight")
    rehearse = cutover.add_parser("rehearse")
    rehearse.add_argument("--ordinal", type=int, required=True, choices=(1, 2))
    rehearse.add_argument("--retain-candidate", action="store_true")
    cutover.add_parser("execute")
    cutover.add_parser("finalize")
    cutover.add_parser("rollback")
    cutover.add_parser("status")
    linear_import = commands.add_parser("linear-import").add_subparsers(dest="linear_import_command", required=True)
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
        if (args.mode == "live" and not report.live) or (args.mode == "ready" and not report.ready):
            raise SystemExit(1)
    elif command == "credentials":
        if args.credentials_command == "init":
            workspace_id, actor_id = credentials_init(config)
            print(json.dumps({"workspace_id": str(workspace_id), "owner_id": str(actor_id)}, sort_keys=True))
        else:
            credentials_rotate(config, args.role)
    elif command == "capabilities":
        if args.capabilities_command == "init":
            path = provision_owner(config, workspace_id=UUID(args.workspace_id), owner_id=UUID(args.owner_id), base_url=args.base_url)
        elif args.capabilities_command == "cutover":
            path = provision_cutover(config, workspace_id=config.workspace_id(), actor_id=config.actor_id(), rotate=args.rotate)
        else:
            path = provision_candidate_reader(config, workspace_id=UUID(args.workspace_id), candidate_ids=tuple(UUID(value) for value in args.candidate_id), name=args.name)
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
    elif command == "linear-export":
        if args.linear_export_command == "oauth-login":
            credential = oauth_login(config.secret_path("linear-export.json"), client_id=args.client_id, force=args.force)
            print(json.dumps({"expires_at": credential.expires_at.isoformat(), "scopes": sorted(credential.scopes)}, sort_keys=True))
            return
        exporter = LinearExporter(config)
        if args.linear_export_command == "full":
            manifest = exporter.full(UUID(args.workspace_id))
        elif args.linear_export_command == "delta":
            manifest = exporter.delta(UUID(args.workspace_id))
        else:
            manifest = exporter.resume(UUID(args.export_id))
        blocking = any(anomaly.disposition == "blocking" for anomaly in manifest.anomalies)
        print(json.dumps({
            "anomaly_codes": sorted({anomaly.code for anomaly in manifest.anomalies}),
            "attachment_dispositions": manifest.attachment_dispositions.model_dump(),
            "base_export_id": str(manifest.base_export_id) if manifest.base_export_id else None,
            "counts": manifest.dimension_counts.model_dump(),
            "hashes": manifest.dimension_hashes.model_dump(),
            "export_id": str(manifest.export_id),
            "manifest_path": manifest.artifacts["manifest"].path,
            "manifest_sha256": manifest.manifest_sha256,
            "mode": manifest.mode,
            "raw_export_sha256": manifest.raw_export_sha256,
            "source_lower_bound": manifest.source_lower_bound.isoformat() if manifest.source_lower_bound else None,
            "source_watermark": manifest.source_boundary.isoformat(),
            "state": "blocked" if blocking else "complete",
        }, sort_keys=True))
        if blocking:
            raise SystemExit(2)
    elif command == "linear-import":
        importer = LinearImporter(config)
        if args.linear_import_command == "stage":
            summary = importer.stage(UUID(args.workspace_id), UUID(args.export_id), Path(args.mapping_file))
        elif args.linear_import_command == "reconcile":
            summary = importer.reconcile(UUID(args.batch_id))
        else:
            summary = importer.promote(UUID(args.batch_id))
        output = {
            "anomaly_codes": summary.anomaly_codes,
            "artifacts": summary.artifacts,
            "base_batch_id": str(summary.base_batch_id) if summary.base_batch_id else None,
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
    elif command == "cutover":
        from . import cutover as cutover_ops
        mapping = Path(os.environ.get("OMP_WORK_IMPORT_MAP", "infra/work-ledger/linear-import-map.json"))
        try:
            if args.cutover_command == "preflight":
                print(json.dumps(cutover_ops.preflight(config, mapping_file=mapping), indent=2, sort_keys=True, default=str))
            elif args.cutover_command == "rehearse":
                print(json.dumps(cutover_ops.rehearse(config, ordinal=args.ordinal, retain_candidate=args.retain_candidate, mapping_file=mapping), indent=2, sort_keys=True, default=str))
            elif args.cutover_command == "execute":
                print(json.dumps(cutover_ops.execute(config, mapping_file=mapping), indent=2, sort_keys=True, default=str))
            elif args.cutover_command == "finalize":
                print(json.dumps(cutover_ops.finalize(config), indent=2, sort_keys=True, default=str))
            elif args.cutover_command == "rollback":
                print(json.dumps(cutover_ops.rollback(config), indent=2, sort_keys=True, default=str))
            elif args.cutover_command == "status":
                print(json.dumps(cutover_ops.status(config), indent=2, sort_keys=True, default=str))
        except cutover_ops.CutoverBlocked as err:
            print(json.dumps({"blocked": err.blockers}, indent=2, sort_keys=True))
            raise SystemExit(2) from err
    else:
        print(backup.restore_drill(config, reason=args.reason))

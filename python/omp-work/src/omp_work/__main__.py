from __future__ import annotations

import argparse
import json
from pathlib import Path

import uvicorn

from . import CONTRACT_VERSION, _contract_dir, contract_sha256, generate_api_schema, generate_schema, validate_bundle
from .operations.config import OperationsConfig
from .operations import cli as operations_cli
from .operations.database import collect_health
from .v1.server import create_app

_SAFE_OPERATION_ERRORS = {
    "artifact cryptography failed",
    "linear_credential_expired",
    "linear_credential_invalid",
    "linear_credential_missing",
    "linear_credential_permission_denied",
    "linear_credential_permissions_invalid",
    "linear_credential_scope_invalid",
    "linear_delta_base_missing",
    "linear_export_missing",
    "linear_export_not_resumable",
    "linear_manifest_missing",
    "linear_transport_failed",
    "pagination_count_hash_gap",
    "linear_import_mapping_invalid",
    "linear_import_missing",
    "linear_import_base_invalid",
    "linear_import_not_reconciled",
    "linear_import_blocked",
    "linear_import_drift",
}


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m omp_work")
    subcommands = parser.add_subparsers(dest="command", required=True)
    schema = subcommands.add_parser("schema")
    schema.add_argument("--check", action="store_true")
    schema.add_argument("--api", action="store_true")
    schema.add_argument("--write", action="store_true")
    subcommands.add_parser("hash")
    validate = subcommands.add_parser("validate")
    validate.add_argument("--require-approval", action="store_true")
    ops = subcommands.add_parser("ops")
    serve = subcommands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=54322)
    serve.add_argument("--capabilities-dir", required=True)
    operations_cli.add_parser(ops)
    args = parser.parse_args()
    if args.command == "serve":
        if args.host not in {"127.0.0.1", "::1", "localhost"}:
            raise SystemExit("non-loopback bind refused")
        validate_bundle(require_approval=True)
        report = collect_health(OperationsConfig.defaults(), role="omp_work_app")
        if not report.ready:
            raise SystemExit("service database is not ready")
        uvicorn.run(create_app(OperationsConfig.defaults(), capabilities_dir=Path(args.capabilities_dir)), host=args.host, port=args.port, access_log=False)
        return
    if args.command == "ops":
        try:
            operations_cli.run(args)
        except Exception as error:
            code = str(error)
            raise SystemExit(code if code in _SAFE_OPERATION_ERRORS else "operation_failed") from None
        return
    if args.command == "schema":
        path = _contract_dir() / ("api-schema.json" if args.api else "schema.json")
        content = json.dumps(generate_api_schema() if args.api else generate_schema(), indent=2, sort_keys=True) + "\n"
        if args.write:
            path.write_text(content)
        if args.check and path.read_text() != content:
            raise SystemExit("schema drift")
        return
    if args.command == "hash":
        print(contract_sha256())
        return
    try:
        validate_bundle(require_approval=args.require_approval)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    print(f"{CONTRACT_VERSION} {contract_sha256()} valid")


if __name__ == "__main__":
    main()

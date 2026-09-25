from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import uvicorn

from . import (
    CONTRACT_VERSION,
    _contract_dir,
    contract_sha256,
    generate_api_schema,
    generate_schema,
    validate_bundle,
)
from .operations import cli as operations_cli
from .operations.config import OperationsConfig
from .operations.database import collect_health
from .v1.models import Approval
from .v1.server import create_app
from .engine_pipeline import run_retrieve_compile_campaign
from .budget_headroom import compute_headroom
from .always_running import check_stall
from .context_compile_bar import measure_compile
from . import parallel_streams as ps

_SAFE_OPERATION_ERRORS = {
    "artifact cryptography failed",
    "pagination_count_hash_gap",
    "linear_manifest_missing",
    "linear_import_mapping_invalid",
    "linear_import_missing",
    "linear_import_base_invalid",
    "linear_import_not_reconciled",
    "linear_import_blocked",
    "linear_import_drift",
}


def _approve(issue: str) -> None:
    if not sys.stdin.isatty():
        raise SystemExit("owner approval requires an interactive terminal")
    digest = contract_sha256()
    now = datetime.now(UTC)
    approved_at = now.isoformat()
    payload = {
        "contract_version": CONTRACT_VERSION,
        "contract_sha256": digest,
        "approved_by": "owner",
        "approved_at": approved_at,
        "issue": issue,
    }
    try:
        Approval.model_validate(payload)
    except Exception:
        raise SystemExit("approval issue is not allowed by the current contract")
    content = json.dumps(payload) + "\n"
    print(digest)
    print(content, end="")
    try:
        entered = input("Type the full contract SHA-256 to approve: ")
    except (EOFError, KeyboardInterrupt):
        raise SystemExit("approval digest mismatch")
    if entered != digest:
        raise SystemExit("approval digest mismatch")
    second_digest = contract_sha256()
    if second_digest != digest:
        raise SystemExit("contract changed during approval")
    approval_path = _contract_dir() / "approval.json"
    prior_bytes = approval_path.read_bytes() if approval_path.exists() else None
    temp_path = approval_path.with_suffix(f".tmp.{os.getpid()}")
    try:
        temp_path.write_text(content)
        os.replace(temp_path, approval_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    try:
        validate_bundle(require_approval=True)
    except Exception as error:
        if prior_bytes is not None:
            temp_restore = approval_path.with_suffix(f".restore.{os.getpid()}")
            temp_restore.write_bytes(prior_bytes)
            os.replace(temp_restore, approval_path)
        else:
            approval_path.unlink(missing_ok=True)
        raise SystemExit(str(error)) from error
    print(f"approved {digest} for {issue}")


def main(argv: list[str] | None = None) -> int | None:
    parser = argparse.ArgumentParser(prog="python -m omp_work")
    subcommands = parser.add_subparsers(dest="command", required=True)

    schema = subcommands.add_parser("schema")
    schema.add_argument("--check", action="store_true")
    schema.add_argument("--api", action="store_true")
    schema.add_argument("--write", action="store_true")
    subcommands.add_parser("hash")
    approve = subcommands.add_parser("approve")
    approve.add_argument("--issue", required=True)
    validate = subcommands.add_parser("validate")
    validate.add_argument("--require-approval", action="store_true")
    ops = subcommands.add_parser("ops")
    serve = subcommands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=54322)
    serve.add_argument("--capabilities-dir", required=True)
    operations_cli.add_parser(ops)

    pl = subcommands.add_parser("pipeline")
    pl.add_argument("--job-id", required=True)
    pl.add_argument("--query", required=True)
    pl.add_argument("--objective", required=True)
    pl.add_argument("--no-enola", action="store_true")
    pl.add_argument("--no-ledger", action="store_true")

    subcommands.add_parser("headroom")
    st = subcommands.add_parser("stall-check")
    st.add_argument("--max-idle-minutes", type=float, default=20.0)

    cb = subcommands.add_parser("compile-bar")
    cb.add_argument("--job-id", required=True)
    cb.add_argument("--objective", required=True)
    cb.add_argument("--finding", action="append", default=[])

    pa = subcommands.add_parser("parallel-admit")
    pa.add_argument("action", choices=["tick", "status", "init", "enqueue"])
    pa.add_argument("--job-id")
    pa.add_argument("--partition", default="gemini_flash")
    pa.add_argument("--path-lease", default="")
    pa.add_argument("--expected-max", type=int, default=40000)
    pa.add_argument("--packet")
    pa.add_argument("--mission")

    args = parser.parse_args(argv)
    if args.command == "serve":
        if args.host not in {"127.0.0.1", "::1", "localhost"}:
            raise SystemExit("non-loopback bind refused")
        validate_bundle(require_approval=True)
        report = collect_health(OperationsConfig.defaults(), role="omp_work_app")
        if not report.ready:
            raise SystemExit("service database is not ready")
        uvicorn.run(
            create_app(
                OperationsConfig.defaults(),
                capabilities_dir=Path(args.capabilities_dir),
            ),
            host=args.host,
            port=args.port,
            access_log=False,
        )
        return 0
    if args.command == "ops":
        try:
            operations_cli.run(args)
        except Exception as error:
            code = str(error)
            raise SystemExit(
                code if code in _SAFE_OPERATION_ERRORS else "operation_failed"
            ) from None
        return 0
    if args.command == "schema":
        path = _contract_dir() / ("api-schema.json" if args.api else "schema.json")
        content = (
            json.dumps(
                generate_api_schema() if args.api else generate_schema(),
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        if args.write:
            path.write_text(content)
        if args.check and path.read_text() != content:
            raise SystemExit("schema drift")
        return 0
    if args.command == "hash":
        print(contract_sha256())
        return 0
    if args.command == "approve":
        _approve(args.issue)
        return 0
    if args.command == "validate":
        try:
            validate_bundle(require_approval=args.require_approval)
        except ValueError as error:
            raise SystemExit(str(error)) from error
        print(f"{CONTRACT_VERSION} {contract_sha256()} valid")
        return 0
    if args.command == "pipeline":
        r = run_retrieve_compile_campaign(
            job_id=args.job_id,
            query=args.query,
            objective=args.objective,
            enola_enabled=not args.no_enola,
            ledger_attach=not args.no_ledger,
        )
        print(json.dumps(r.to_dict(), indent=2))
        return 0
    if args.command == "headroom":
        caps = "/home/thetu/.codex/workflows/economy/ACTIVE/BUDGET-CAPS.json"
        print(json.dumps(compute_headroom(caps_path=caps).to_dict(), indent=2))
        return 0
    if args.command == "stall-check":
        print(json.dumps(check_stall(max_idle_minutes=args.max_idle_minutes).to_dict(), indent=2))
        return 0
    if args.command == "compile-bar":
        findings = args.finding or ["Prefer write-first"]
        print(json.dumps(measure_compile(job_id=args.job_id, findings=findings, objective=args.objective), indent=2))
        return 0
    if args.command == "parallel-admit":
        if args.action == "init":
            ps.init_db()
            print(json.dumps({"ok": True, "db": str(ps.DB)}))
            return 0
        if args.action == "status":
            print(json.dumps(ps.status(), indent=2))
            return 0
        if args.action == "tick":
            print(json.dumps(ps.tick(), indent=2))
            return 0
        if args.action == "enqueue":
            if not args.job_id or not args.path_lease:
                print(json.dumps({"ok": False, "error": "need --job-id and --path-lease"}))
                return 2
            print(json.dumps(ps.enqueue(
                job_id=args.job_id,
                provider_partition=args.partition,
                path_lease=args.path_lease,
                expected_max=args.expected_max,
                mission_id=args.mission,
                packet_path=args.packet,
            ), indent=2))
            return 0
    return 2


if __name__ == "__main__":
    code = main()
    if code is not None and code != 0:
        raise SystemExit(code)

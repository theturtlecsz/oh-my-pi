from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any, Callable
from uuid import UUID, uuid4

import uvicorn

from . import (
    CONTRACT_VERSION,
    _contract_dir,
    approval_attestation,
    contract_sha256,
    generate_api_schema,
    generate_schema,
    validate_approval_attestation,
    validate_bundle,
)
from .operations import cli as operations_cli
from .operations import stop as stop_ops
from .operations.config import OperationsConfig
from .operations.database import collect_health
from .v1.client import WorkClient
from .v1.models import Approval
from .v1.server import create_app
from .alarm_dispatch import AlarmStateMissing, init_alarms, run_alarms, run_digest
from .credential_watch import DEFAULT_ROOTS, watch_credentials
from .grokbot import send as grokbot_send
from .budget_headroom import compute_headroom
from .always_running import check_stall
from . import owner_key
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


def _budget_alerts(workspace_id: UUID, actor_id: UUID) -> int:
    """Sweep item budgets, then deliver committed or failed budget_alert rows.

    The URL is ``OMP_GROKBOT_ALERT_URL``. The bearer token is the stripped
    contents of the file named by ``OMP_GROKBOT_ALERT_TOKEN_FILE``. Either
    missing, unreadable, or empty exits 2. On success the process prints the
    number of alerts sent as JSON.
    """
    url = os.environ.get("OMP_GROKBOT_ALERT_URL", "").strip()
    token_file = os.environ.get("OMP_GROKBOT_ALERT_TOKEN_FILE", "").strip()
    if not url:
        print("budget-alerts: OMP_GROKBOT_ALERT_URL is not set", file=sys.stderr)
        return 2
    if not token_file:
        print("budget-alerts: OMP_GROKBOT_ALERT_TOKEN_FILE is not set", file=sys.stderr)
        return 2
    try:
        token = Path(token_file).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        print("budget-alerts: token file is unreadable", file=sys.stderr)
        return 2
    if not token:
        print("budget-alerts: token file is empty", file=sys.stderr)
        return 2

    from .jobs.budget import sweep_item_budgets
    from .jobs.grokbot import deliver_budget_alerts
    from .jobs.store import NativeJobStore

    store = NativeJobStore(OperationsConfig.defaults())
    sweep_item_budgets(
        store,
        operation_id=str(uuid4()),
        workspace_id=workspace_id,
        actor_id=actor_id,
    )
    sent = deliver_budget_alerts(
        store,
        workspace_id=workspace_id,
        actor_id=actor_id,
        url=url,
        token=token,
    )
    print(json.dumps(sent))
    return 0


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
        "attestation": approval_attestation(digest, issue, approved_at),
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


def _default_client_config() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "omp-work" / "client.json"


def _alarm_client(path: str | Path) -> tuple[WorkClient, UUID]:
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    workspace_id = UUID(str(config["workspace_id"]))
    return (
        WorkClient(
            str(config["base_url"]),
            workspace_id,
            Path(str(config["bearer_file"])),
        ),
        workspace_id,
    )


def _alarm_sender() -> Callable[[str, dict[str, Any]], None] | None:
    url = os.environ.get("OMP_GROKBOT_ALERT_URL")
    token_file = os.environ.get("OMP_GROKBOT_ALERT_TOKEN_FILE")
    if not url or not token_file:
        return None
    try:
        token = Path(token_file).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not token:
        return None
    return partial(grokbot_send, url, token)


def _run_alarms_command(args: argparse.Namespace) -> int:
    client, workspace_id = _alarm_client(args.client_config)
    state_path = Path(args.state)
    if args.alarms_command == "init":
        state = init_alarms(client, state_path)
        print(json.dumps({"after_sequence": state.after_sequence}))
        return 0
    if args.alarms_command == "watch-credentials":
        roots = tuple(Path(root) for root in args.root) if args.root else DEFAULT_ROOTS
        signalled = watch_credentials(
            client,
            workspace_id=workspace_id,
            state_path=state_path,
            roots=roots,
        )
        print(json.dumps({"signalled": signalled}))
        return 0
    sender = _alarm_sender()
    if sender is None:
        return 2
    if args.alarms_command == "run":
        try:
            sent = run_alarms(client, state_path, sender)
        except AlarmStateMissing:
            print("run `omp-work alarms init` first", file=sys.stderr)
            return 2
        print(json.dumps({"sent": sent}))
        return 0
    if args.alarms_command == "digest":
        day = args.day or (datetime.now(UTC).date() - timedelta(days=1))
        body = run_digest(client, workspace_id, day, sender)
        print(json.dumps(body))
        return 0
    return 2


def _run_events_command(args: argparse.Namespace) -> int:
    from . import event_push

    config_dir = OperationsConfig.defaults().config_dir
    if args.events_command == "push-key":
        try:
            master_key = event_push.load_master_key(config_dir)
        except ValueError as error:
            print(f"events: {error}", file=sys.stderr)
            return 2
        print(event_push.subscription_key(master_key, args.subscription).hex())
        return 0

    try:
        client, workspace_id = stop_ops.load_client(
            args.client_config, args.bearer_file
        )
    except Exception as error:  # noqa: BLE001 - credential/transport failure is reported as 255
        print(f"events: {error}", file=sys.stderr)
        return 255
    try:
        try:
            master_key = event_push.load_master_key(config_dir)
        except ValueError as error:
            print(f"events: {error}", file=sys.stderr)
            return 2
        allowed_hosts = event_push.load_allowed_hosts(config_dir)
        result = event_push.run_push(
            client,
            workspace_id=workspace_id,
            master_key=master_key,
            allowed_hosts=allowed_hosts,
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as error:  # noqa: BLE001 - surfaced as the CLI failure code
        print(f"events: {error}", file=sys.stderr)
        return 255
    finally:
        client.close()


def _run_stop_command(args: argparse.Namespace) -> int:
    if args.stop_command == "install-guards":
        try:
            stop_ops.install_guards(
                args.units,
                systemd_dir=args.systemd_dir,
                interval=args.interval,
            )
            return 0
        except Exception as error:
            print(f"stop: {error}", file=sys.stderr)
            return 2

    client_config = args.client_config or str(_default_client_config())
    try:
        client, workspace_id = stop_ops.load_client(
            Path(client_config), args.bearer_file
        )
    except Exception as error:
        print(f"stop: {error}", file=sys.stderr)
        return 255
    try:
        if args.stop_command == "watch":
            stop_ops.watch(client, args.units, interval=args.interval)
            return 0
        if args.stop_command == "check":
            return stop_ops.check(client)
        if args.stop_command == "status":
            view = stop_ops.status(client)
            print(json.dumps(view.model_dump(mode="json"), sort_keys=True))
            return 0
        if args.stop_command == "engage":
            response = stop_ops.engage(client, workspace_id, args.reason)
        else:
            response = stop_ops.release(client, workspace_id, args.reason)
        print(response.model_dump_json())
        return 0
    except Exception as error:
        print(f"stop: {error}", file=sys.stderr)
        return 255
    finally:
        client.close()


def main(argv: list[str] | None = None) -> int | None:
    parser = argparse.ArgumentParser(prog="python -m omp_work")
    subcommands = parser.add_subparsers(
        dest="command",
        required=True,
        metavar="{schema,hash,approve,validate,ops,serve,headroom,stall-check,parallel-admit,budget-alerts,projects,stop,alarms,jobs,owner-key}",
    )

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

    subcommands.add_parser("headroom")
    st = subcommands.add_parser("stall-check")
    st.add_argument("--max-idle-minutes", type=float, default=20.0)

    pa = subcommands.add_parser("parallel-admit")
    pa.add_argument("action", choices=["tick", "status", "init", "enqueue"])
    pa.add_argument("--job-id")
    pa.add_argument("--partition", default="gemini_flash")
    pa.add_argument("--path-lease", default="")
    pa.add_argument("--expected-max", type=int, default=40000)
    pa.add_argument("--packet")
    pa.add_argument("--mission")

    alerts = subcommands.add_parser("budget-alerts")
    alerts.add_argument("--workspace", required=True, type=UUID)
    alerts.add_argument("--actor", required=True, type=UUID)

    projects = subcommands.add_parser("projects")
    projects_sub = projects.add_subparsers(dest="projects_command", required=True)
    project_scope = argparse.ArgumentParser(add_help=False)
    project_scope.add_argument("--workspace", required=True, type=UUID)
    project_scope.add_argument("--actor", required=True, type=UUID)
    seed_parser = projects_sub.add_parser("seed", parents=[project_scope])
    seed_parser.add_argument("--file", required=True, type=Path)
    show_parser = projects_sub.add_parser("show", parents=[project_scope])
    show_parser.add_argument("--key", required=True)
    projects_sub.add_parser("check", parents=[project_scope])

    stop = subcommands.add_parser("stop")
    stop_commands = stop.add_subparsers(dest="stop_command", required=True)
    stop_scope = argparse.ArgumentParser(add_help=False)
    stop_scope.add_argument("--client-config")
    stop_scope.add_argument("--bearer-file", type=Path)
    stop_commands.add_parser("status", parents=[stop_scope])
    stop_commands.add_parser("check", parents=[stop_scope])
    engage = stop_commands.add_parser("engage", parents=[stop_scope])
    engage.add_argument("--reason", required=True)
    release = stop_commands.add_parser("release", parents=[stop_scope])
    release.add_argument("--reason", required=True)
    watch_parser = stop_commands.add_parser("watch", parents=[stop_scope])
    watch_parser.add_argument(
        "--unit", action="extend", nargs="+", dest="units", required=True
    )
    watch_parser.add_argument("--interval", type=float, default=5.0)
    install_parser = stop_commands.add_parser("install-guards", parents=[stop_scope])
    install_parser.add_argument(
        "--unit", action="extend", nargs="+", dest="units", required=True
    )
    install_parser.add_argument("--systemd-dir", type=Path, default=None)
    install_parser.add_argument("--interval", type=float, default=5.0)

    alarms = subcommands.add_parser("alarms")
    alarm_common = argparse.ArgumentParser(add_help=False)
    alarm_common.add_argument("--state", required=True)
    alarm_common.add_argument("--client-config", default=None)
    alarm_commands = alarms.add_subparsers(dest="alarms_command", required=True)
    alarm_commands.add_parser("init", parents=[alarm_common])
    alarm_commands.add_parser("run", parents=[alarm_common])
    digest = alarm_commands.add_parser("digest", parents=[alarm_common])
    digest.add_argument("--day")
    watch = alarm_commands.add_parser("watch-credentials", parents=[alarm_common])
    watch.add_argument("--root", action="append")

    events = subcommands.add_parser("events")
    events_sub = events.add_subparsers(dest="events_command", required=True)
    push_parser = events_sub.add_parser("push")
    push_parser.add_argument("--client-config", required=True, type=Path)
    push_parser.add_argument("--bearer-file", type=Path)
    push_key_parser = events_sub.add_parser("push-key")
    push_key_parser.add_argument("--subscription", required=True, type=UUID)

    owner_key.add_parser(subcommands)

    jobs_parser = subcommands.add_parser("jobs")
    jobs_sub = jobs_parser.add_subparsers(dest="jobs_command", required=True)

    worker_parser = jobs_sub.add_parser("worker")
    worker_parser.add_argument("--config", required=True, type=Path)
    worker_parser.add_argument("--once", action="store_true", default=False)

    check_parser = jobs_sub.add_parser("check")
    check_parser.add_argument("--config", required=True, type=Path)
    check_parser.add_argument("--work-id", default=None)
    check_parser.add_argument("--count", type=int, default=1)
    check_parser.add_argument("--timeout", type=float, default=30.0)
    check_parser.add_argument("--lease", type=int, default=None)
    check_parser.add_argument("--sleep", type=float, default=0.0)

    reg_parser = jobs_sub.add_parser("register-component")
    reg_parser.add_argument("--config", required=True, type=Path)

    args = parser.parse_args(argv)
    if args.command == "alarms":
        if args.client_config is None:
            args.client_config = str(_default_client_config())
        return _run_alarms_command(args)
    if args.command == "events":
        return _run_events_command(args)
    if args.command == "stop":
        return _run_stop_command(args)
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
            if args.require_approval:
                validate_approval_attestation()
        except ValueError as error:
            raise SystemExit(str(error)) from error
        print(f"{CONTRACT_VERSION} {contract_sha256()} valid")
        return 0
    if args.command == "headroom":
        active_dir = Path(
            os.environ.get("OMP_ECONOMY_ACTIVE_DIR")
            or (Path.home() / ".codex/workflows/economy/ACTIVE")
        )
        caps = active_dir / "BUDGET-CAPS.json"
        if not caps.is_file():
            from .budget_headroom import Headroom

            print(
                json.dumps(
                    Headroom(
                        soft_warn_fraction=0.7,
                        hard_refuse_fraction=1.0,
                        milestone_soft_tokens=None,
                        milestone_hard_tokens=None,
                        spent_tokens=0,
                        soft_remaining=None,
                        hard_remaining=None,
                        soft_fraction_used=None,
                        hard_fraction_used=None,
                        soft_warn=False,
                        hard_refuse=False,
                        notes=["BUDGET-CAPS missing"],
                    ).to_dict(),
                    indent=2,
                )
            )
            return 0
        print(json.dumps(compute_headroom(caps_path=caps).to_dict(), indent=2))
        return 0
    if args.command == "stall-check":
        print(json.dumps(check_stall(max_idle_minutes=args.max_idle_minutes).to_dict(), indent=2))
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
    if args.command == "budget-alerts":
        return _budget_alerts(args.workspace, args.actor)
    if args.command == "projects":
        from .project_cli import run_projects

        return run_projects(args)
    if args.command == "owner-key":
        return owner_key.run(args)
    if args.command == "jobs":
        from .jobs.process import check, register_component, run_worker

        if args.jobs_command == "worker":
            res = run_worker(args.config, once=args.once)
            return 0 if res is None else res
        if args.jobs_command == "register-component":
            register_component(args.config)
            return 0
        if args.jobs_command == "check":
            result = check(
                args.config,
                work_id=args.work_id,
                count=args.count,
                timeout=args.timeout,
                lease=args.lease,
                sleep=args.sleep,
            )
            print(json.dumps(result, indent=2))
            return 0 if result.get("passed") else 1
    return 2


if __name__ == "__main__":
    code = main()
    if code is not None and code != 0:
        raise SystemExit(code)

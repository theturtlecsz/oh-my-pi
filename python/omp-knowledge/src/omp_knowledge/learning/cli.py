from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any
from uuid import UUID

from omp_work.v1.client import WorkClient

from .capture import NativeEvents, RunRecord, drain, retry
from .generation import LessonGenerator, LocalChatGenerator
from .policy import NativeReceipts
from .store import LearningStore
from .uses import record_outcome, record_use, supply

EXIT_OK = 0
EXIT_RUN_FAILED = 1
SUCCESS_STATUSES = frozenset({"succeeded", "no_lesson"})


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m omp_knowledge.learning")
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("drain", "retry"):
        sub = subcommands.add_parser(name)
        sub.add_argument("--state-dir", required=True)
        sub.add_argument("--work-url", required=True)
        sub.add_argument("--workspace", required=True)
        sub.add_argument("--bearer-file", required=True)
        sub.add_argument("--generator-url", required=True)
        sub.add_argument("--model", required=True)
        sub.add_argument("--profile", required=True)
        sub.add_argument("--limit", type=int, default=50)
        sub.add_argument("--json", action="store_true")

    supply_parser = subcommands.add_parser("supply")
    supply_parser.add_argument("--state-dir", required=True)
    supply_parser.add_argument("--workspace", required=True)
    supply_parser.add_argument("--work-key", required=True)
    supply_parser.add_argument("--project-id", default=None)
    supply_parser.add_argument("--cwd", default=".")
    supply_parser.add_argument("--limit", type=int, default=3)
    supply_parser.add_argument("--json", action="store_true")

    use_parser = subcommands.add_parser("use")
    use_parser.add_argument("--state-dir", required=True)
    use_parser.add_argument("--work-url", required=True)
    use_parser.add_argument("--workspace", required=True)
    use_parser.add_argument("--bearer-file", required=True)
    use_parser.add_argument("--supply-id", "--supply", dest="supply_id", required=True)
    use_parser.add_argument("--candidate-id", "--candidate", dest="candidate_id", required=True)
    use_parser.add_argument(
        "--receipt-id",
        "--receipt",
        "--receipts",
        dest="receipt_ids",
        action="extend",
        nargs="+",
        required=True,
    )
    use_parser.add_argument("--json", action="store_true")

    outcome_parser = subcommands.add_parser("outcome")
    outcome_parser.add_argument("--state-dir", required=True)
    outcome_parser.add_argument("--work-url", required=True)
    outcome_parser.add_argument("--workspace", required=True)
    outcome_parser.add_argument("--bearer-file", required=True)
    outcome_parser.add_argument("--use-id", "--use", dest="use_id", required=True)
    outcome_parser.add_argument("--receipt-id", "--receipt", dest="receipt_id", required=True)
    outcome_parser.add_argument("--json", action="store_true")

    return parser


def _run_payload(record: RunRecord) -> dict[str, Any]:
    return record.model_dump(mode="json")


def _resolve_repository(cwd: str | Path | None) -> str | None:
    if not cwd:
        return None
    try:
        res = subprocess.run(
            ["git", "-C", str(cwd), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            raw = res.stdout.strip()
            from omp_work.knowledge_source import normalize_remote_url

            return normalize_remote_url(raw)
    except Exception:
        return None
    return None


def main(
    argv: list[str] | None = None,
    *,
    store: LearningStore | None = None,
    events: NativeEvents | None = None,
    receipts: NativeReceipts | None = None,
    generator: LessonGenerator | None = None,
) -> int:
    args = _build_parser().parse_args(argv)

    owned_store = store is None
    active_store = store or LearningStore(args.state_dir)

    client: WorkClient | None = None
    active_generator = generator
    try:
        if args.command in ("drain", "retry"):
            if events is None or receipts is None:
                client = WorkClient(
                    args.work_url,
                    UUID(str(args.workspace)),
                    Path(args.bearer_file),
                )
                events = events or client
                receipts = receipts or client

            if active_generator is None:
                active_generator = LocalChatGenerator(
                    args.generator_url, args.model, args.profile
                )

            if args.command == "drain":
                record = drain(
                    active_store,
                    events,
                    receipts,
                    active_generator,
                    workspace_id=args.workspace,
                    limit=args.limit,
                )
            else:
                record = retry(active_store, receipts, active_generator)

            payload = _run_payload(record)
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(
                    f"{record.status} units={record.units_total} failed={record.units_failed}"
                )
                for unit in record.units:
                    print(
                        f"  {unit.unit_id} {unit.state}"
                        f"{f' error={unit.error_code}' if unit.error_code else ''}"
                    )

            return EXIT_OK if record.status in SUCCESS_STATUSES else EXIT_RUN_FAILED

        if args.command == "supply":
            context: dict[str, str] = {}
            if args.project_id:
                context["project_id"] = args.project_id
            repo_url = _resolve_repository(args.cwd)
            if repo_url:
                context["repository"] = repo_url

            result = supply(
                active_store,
                workspace_id=args.workspace,
                work_key=args.work_key,
                context=context,
                limit=args.limit,
            )
            if args.json:
                print(json.dumps(list(result.lines), indent=2))
            else:
                for line in result.lines:
                    print(line)
            return EXIT_OK

        if args.command == "use":
            if receipts is None:
                client = WorkClient(
                    args.work_url,
                    UUID(str(args.workspace)),
                    Path(args.bearer_file),
                )
                receipts = client
            record = record_use(
                active_store,
                receipts,
                supply_id=args.supply_id,
                candidate_id=args.candidate_id,
                receipt_ids=args.receipt_ids,
            )
            if args.json:
                print(json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True))
            else:
                print(
                    f"USE {record.use_id} supply={record.supply_id} candidate={record.candidate_id}"
                )
            return EXIT_OK

        if args.command == "outcome":
            if receipts is None:
                client = WorkClient(
                    args.work_url,
                    UUID(str(args.workspace)),
                    Path(args.bearer_file),
                )
                receipts = client
            record = record_outcome(
                active_store,
                receipts,
                use_id=args.use_id,
                receipt_id=args.receipt_id,
            )
            if args.json:
                print(json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True))
            else:
                print(
                    f"OUTCOME {record.outcome_id} use={record.use_id} verdict={record.verdict}"
                )
            return EXIT_OK

        raise SystemExit(2)
    finally:
        if client is not None:
            client.close()
        if owned_store:
            active_store.close()


__all__ = ["EXIT_OK", "EXIT_RUN_FAILED", "SUCCESS_STATUSES", "main"]

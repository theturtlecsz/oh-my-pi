from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from uuid import UUID

from omp_work.v1.client import WorkClient

from .capture import NativeEvents, RunRecord, drain, retry
from .generation import LessonGenerator, LocalChatGenerator
from .policy import NativeReceipts
from .store import LearningStore

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
    return parser


def _run_payload(record: RunRecord) -> dict[str, Any]:
    return record.model_dump(mode="json")


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
        elif args.command == "retry":
            record = retry(active_store, receipts, active_generator)
        else:  # pragma: no cover - argparse enforces the choice
            raise SystemExit(2)
    finally:
        if client is not None:
            client.close()
        if owned_store:
            active_store.close()

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


__all__ = ["EXIT_OK", "EXIT_RUN_FAILED", "SUCCESS_STATUSES", "main"]

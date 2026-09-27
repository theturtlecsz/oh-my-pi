"""Maintenance CLI for a Fleet Knowledge state root (OMP-312).

``backup``, ``rebuild`` and ``rollback`` are the offline recovery operations for
``learning.sqlite``, ``context-bundles.sqlite``, ``structural-publications.sqlite``,
``publications.sqlite`` and the ``sources/`` tree. Every refusal exits 2 with the
reason on stderr; ``--json`` prints one machine line on success.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, TextIO

from .core import create_backup, rebuild_from_records, rollback
from .records import MaintenanceError

EXIT_OK = 0
EXIT_REFUSED = 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m omp_knowledge.maintenance")
    subcommands = parser.add_subparsers(dest="command", required=True)

    backup_parser = subcommands.add_parser("backup")
    backup_parser.add_argument("--state-root", required=True)
    backup_parser.add_argument("--backup-dir", required=True)
    backup_parser.add_argument("--json", action="store_true")

    rebuild_parser = subcommands.add_parser("rebuild")
    rebuild_parser.add_argument("--backup-dir", required=True)
    rebuild_parser.add_argument("--target-root", required=True)
    rebuild_parser.add_argument("--json", action="store_true")

    rollback_parser = subcommands.add_parser("rollback")
    rollback_parser.add_argument("--backup-dir", required=True)
    rollback_parser.add_argument("--state-root", required=True)
    rollback_parser.add_argument("--json", action="store_true")
    return parser


def _stderr(message: str) -> None:
    print(message.strip() or "error", file=sys.stderr)


def _emit(stdout: TextIO | None, payload: dict[str, Any]) -> None:
    line = json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n"
    stream = sys.stdout if stdout is None else stdout
    stream.write(line)
    flush = getattr(stream, "flush", None)
    if callable(flush):
        flush()


def main(argv: list[str] | None = None, *, stdout: TextIO | None = None) -> int:
    """Run one maintenance command. Returns a process exit code."""
    try:
        args = _build_parser().parse_args(argv)
        if args.command == "backup":
            payload = create_backup(args.state_root, args.backup_dir)
        elif args.command == "rebuild":
            payload = rebuild_from_records(args.backup_dir, args.target_root)
        elif args.command == "rollback":
            payload = rollback(args.backup_dir, args.state_root)
        else:
            raise MaintenanceError(f"unknown command {args.command!r}")
    except MaintenanceError as exc:
        _stderr(str(exc))
        return EXIT_REFUSED
    except Exception as exc:  # noqa: BLE001 - any failure is a refusal with a reason
        _stderr(str(exc) or type(exc).__name__)
        return EXIT_REFUSED
    if args.json:
        _emit(stdout, payload)
    return EXIT_OK


__all__ = ["EXIT_OK", "EXIT_REFUSED", "main"]

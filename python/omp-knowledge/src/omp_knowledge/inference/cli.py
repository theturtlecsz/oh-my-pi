"""``python -m omp_knowledge.inference health --routes FILE``.

Probes every declared role's primary and fallback and prints one row per role
(``role``, ``primary``, ``fallback``, ``used``, ``reason``). Exits 0 when every
declared role resolves and 2 when any role has no healthy profile.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from typing import Any, TextIO

from omp_knowledge.inference.routes import (
    RouteConfigError,
    RouteHealth,
    RouteProfile,
    RouteSet,
    RouteUnavailable,
    load_routes,
    probe,
    resolve,
)

EXIT_OK = 0
EXIT_UNRESOLVED = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m omp_knowledge.inference")
    subcommands = parser.add_subparsers(dest="command", required=True)
    health_parser = subcommands.add_parser(
        "health", help="resolve each declared inference role"
    )
    health_parser.add_argument("--routes", required=True)
    health_parser.add_argument("--json", action="store_true")
    return parser


def _role_rows(
    route_set: RouteSet, prober: Callable[[RouteProfile], RouteHealth]
) -> tuple[list[dict[str, Any]], bool]:
    rows: list[dict[str, Any]] = []
    healthy = True
    for entry in route_set.routes:
        fallback_name = entry.fallback.name if entry.fallback is not None else None
        try:
            resolved = resolve(route_set, entry.role, prober)
        except RouteUnavailable as exc:
            healthy = False
            rows.append(
                {
                    "role": entry.role,
                    "primary": entry.primary.name,
                    "fallback": fallback_name,
                    "used": "none",
                    "reason": str(exc),
                }
            )
            continue
        rows.append(
            {
                "role": resolved.role,
                "primary": entry.primary.name,
                "fallback": fallback_name,
                "used": resolved.used,
                "reason": resolved.reason,
            }
        )
    return rows, healthy


def _emit(stdout: TextIO | None, row: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        line = json.dumps(row, sort_keys=True, ensure_ascii=False)
    else:
        fallback = row["fallback"] if row["fallback"] is not None else "-"
        reason = row["reason"] if row["reason"] else "-"
        line = (
            f"{row['role']} primary={row['primary']} fallback={fallback} "
            f"used={row['used']} reason={reason}"
        )
    stream = sys.stdout if stdout is None else stdout
    stream.write(line + "\n")
    flush = getattr(stream, "flush", None)
    if callable(flush):
        flush()


def _stderr(message: str) -> None:
    print(message.strip() or "error", file=sys.stderr)


def main(
    argv: list[str] | None = None,
    *,
    prober: Callable[[RouteProfile], RouteHealth] = probe,
    stdout: TextIO | None = None,
) -> int:
    """Run the ``health`` command. Exits 0 when every role resolves, else 2."""
    args = build_parser().parse_args(argv)
    try:
        route_set = load_routes(args.routes)
    except RouteConfigError as exc:
        _stderr(str(exc))
        return EXIT_UNRESOLVED
    rows, healthy = _role_rows(route_set, prober)
    if args.json:
        _emit(stdout, {"roles": rows}, as_json=True)
    else:
        for row in rows:
            _emit(stdout, row, as_json=False)
    return EXIT_OK if healthy else EXIT_UNRESOLVED


__all__ = ["EXIT_OK", "EXIT_UNRESOLVED", "build_parser", "main"]

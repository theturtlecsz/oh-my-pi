"""Compare two Harbor runs on authorization, routing, transitions, and result.

``capture`` reads one session JSONL and one service readback. ``compare``
names the top-level fields whose captured values differ.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_FIELDS = ("authorization", "routing", "transitions", "result")
_LIMITS = ("max_continuations", "max_close_attempts", "max_no_progress")


def capture(session_jsonl: str | Path, readback: str | Path) -> dict[str, Any]:
    """Return ``{authorization, routing, transitions, result}`` for one run.

    Authorization is the grant mode, its three limits, and the sealed
    criteria revision. Routing is the command plus model roles in session
    order (a missing role is ``default``). Transitions are the ordered
    execution states from the session, or the grant state when the session
    records none. Result is the terminal grant and item.
    """

    entries = _read_jsonl(Path(session_jsonl))
    document = _read_json(Path(readback))
    if not isinstance(document, dict):
        raise ValueError("readback must be a JSON object")
    execution = document.get("execution")
    if not isinstance(execution, dict):
        execution = {}
    grant = execution.get("grant")
    if not isinstance(grant, dict):
        grant = {}
    item = _terminal_item(execution)
    work_item = document.get("work_item")
    if not isinstance(work_item, dict):
        work_item = {}
    return {
        "authorization": _authorization(grant, item, work_item),
        "routing": _routing(entries, item),
        "transitions": _transitions(entries, grant),
        "result": _result(grant, item, work_item),
    }


def compare(left: Mapping[str, Any], right: Mapping[str, Any]) -> list[str]:
    """Return the captured fields whose values differ, in capture order."""

    if not isinstance(left, Mapping) or not isinstance(right, Mapping):
        raise TypeError("compare expects two captures")
    return [field for field in _FIELDS if left.get(field) != right.get(field)]


def _authorization(grant: dict[str, Any], item: dict[str, Any], work_item: dict[str, Any]) -> dict[str, Any]:
    return {
        "mode": grant.get("mode"),
        "limits": {name: grant.get(name) for name in _LIMITS},
        "sealed_revision": _sealed_revision(item, work_item),
    }


def _sealed_revision(item: dict[str, Any], work_item: dict[str, Any]) -> str | None:
    criteria = item.get("criteria_revision_id")
    if isinstance(criteria, str) and criteria:
        return criteria
    revision = work_item.get("revision")
    if isinstance(revision, dict):
        revision_id = revision.get("revision_id")
        if isinstance(revision_id, str) and revision_id:
            return revision_id
    if isinstance(revision, str) and revision:
        return revision
    return None


def _routing(entries: list[dict[str, Any]], item: dict[str, Any]) -> dict[str, Any]:
    command: str | None = None
    roles: list[dict[str, Any]] = []
    for entry in entries:
        if command is None and entry.get("type") == "message":
            message = entry.get("message")
            if isinstance(message, dict) and message.get("role") == "user":
                command = _message_text(message)
        if entry.get("type") == "model_change":
            role = entry.get("role")
            if not isinstance(role, str) or role == "":
                role = "default"
            roles.append({"model": entry.get("model"), "role": role})
    if command is None:
        original = item.get("original_request")
        command = original if isinstance(original, str) else None
    return {"command": command, "model_roles": roles}


def _message_text(message: dict[str, Any]) -> str | None:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
    if not parts:
        return None
    return "".join(parts)


def _transitions(entries: list[dict[str, Any]], grant: dict[str, Any]) -> list[str]:
    states = [state for entry in entries if (state := _execution_state(entry)) is not None]
    if states:
        return states
    grant_state = grant.get("state")
    if isinstance(grant_state, str):
        return [grant_state]
    return []


def _execution_state(entry: dict[str, Any]) -> str | None:
    if entry.get("type") == "execution_state" and isinstance(entry.get("state"), str):
        return entry["state"]
    if entry.get("type") != "custom" or entry.get("customType") != "execution_state":
        return None
    if isinstance(entry.get("state"), str):
        return entry["state"]
    data = entry.get("data")
    if isinstance(data, dict) and isinstance(data.get("state"), str):
        return data["state"]
    return None


def _result(grant: dict[str, Any], item: dict[str, Any], work_item: dict[str, Any]) -> dict[str, Any]:
    work_id = item.get("work_id")
    if not isinstance(work_id, str) or work_id == "":
        work_id = work_item.get("work_id") if isinstance(work_item.get("work_id"), str) else None
    return {
        "grant": {"state": grant.get("state"), "terminal_reason": grant.get("terminal_reason")},
        "item": {
            "work_id": work_id,
            "phase": item.get("phase"),
            "state": work_item.get("state"),
            "terminal_reason": item.get("terminal_reason"),
        },
    }


def _terminal_item(execution: dict[str, Any]) -> dict[str, Any]:
    active = execution.get("active_item")
    if isinstance(active, dict):
        return active
    items = execution.get("items")
    if not isinstance(items, list):
        return {}
    chosen: dict[str, Any] | None = None
    for item in items:
        if not isinstance(item, dict):
            continue
        if chosen is None:
            chosen = item
            continue
        position = item.get("position")
        previous = chosen.get("position")
        if isinstance(position, int) and not isinstance(position, bool) and (
            not isinstance(previous, int) or isinstance(previous, bool) or position >= previous
        ):
            chosen = item
    return chosen or {}


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    entries: list[dict[str, Any]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if line == "":
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"malformed {path}:{line_no}") from exc
        if not isinstance(entry, dict):
            raise ValueError(f"malformed {path}:{line_no}")
        entries.append(entry)
    return entries


def _print_json(document: Any) -> None:
    json.dump(document, sys.stdout, sort_keys=True, indent=2)
    sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m omp_harbor_eval.parity")
    sub = parser.add_subparsers(dest="cmd", required=True)

    capture_parser = sub.add_parser("capture")
    capture_parser.add_argument("--session", required=True)
    capture_parser.add_argument("--readback", required=True)
    capture_parser.add_argument("--out", required=True)

    compare_parser = sub.add_parser("compare")
    compare_parser.add_argument("left")
    compare_parser.add_argument("right")

    args = parser.parse_args(argv)
    try:
        if args.cmd == "capture":
            document = capture(args.session, args.readback)
            Path(args.out).write_text(
                json.dumps(document, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            return 0
        left = _read_json(Path(args.left))
        right = _read_json(Path(args.right))
        if not isinstance(left, dict) or not isinstance(right, dict):
            raise ValueError("compare expects two JSON objects")
        diff = compare(left, right)
    except (OSError, ValueError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    _print_json(diff)
    return 1 if diff else 0


if __name__ == "__main__":
    raise SystemExit(main())

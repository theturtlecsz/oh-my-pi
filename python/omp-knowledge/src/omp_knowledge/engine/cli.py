"""``python -m omp_knowledge.engine <query|lookup|correct|check>``.

Commands run on ``RealCogneeAdapter`` built from ``KnowledgeConfig`` (environment).
Pass ``engine=`` to ``main`` to supply an adapter. An unavailable engine exits 1.

``check --enola-dir D --repository-id R`` ingests that Enola directory, queries,
corrects one fact, and looks it up. It prints
``{capability, passed, fact_id, before, after}``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

from omp_knowledge.config import load_config
from omp_knowledge.engine.cognee_adapter import RealCogneeAdapter
from omp_knowledge.engine.protocol import FactRecord, KnowledgeEngine
from omp_knowledge.errors import EngineUnavailableError

_DEFAULT_WORKSPACE = UUID("00000000-0000-4000-8000-0000000000e1")
_CORRECTION_KEY = "corrected"
_CORRECTION_VALUE = "yes"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m omp_knowledge.engine")
    subcommands = parser.add_subparsers(dest="command", required=True)
    scope = argparse.ArgumentParser(add_help=False)
    scope.add_argument("--workspace-id", type=UUID, default=_DEFAULT_WORKSPACE)
    scope.add_argument("--repository-id", type=UUID, required=True)
    scope.add_argument("--snapshot-id", required=True)

    query_parser = subcommands.add_parser("query", parents=[scope])
    query_parser.add_argument("--query", required=True)
    query_parser.add_argument("--limit", type=int, default=20)

    lookup_parser = subcommands.add_parser("lookup", parents=[scope])
    lookup_parser.add_argument("--fact-id", required=True)

    correct_parser = subcommands.add_parser("correct", parents=[scope])
    correct_parser.add_argument("--fact-id", required=True)
    correct_parser.add_argument("--set", action="append", dest="sets", required=True)

    check_parser = subcommands.add_parser("check")
    check_parser.add_argument("--enola-dir", required=True)
    check_parser.add_argument("--repository-id", type=UUID, required=True)
    check_parser.add_argument("--workspace-id", type=UUID, default=_DEFAULT_WORKSPACE)
    return parser


def _engine(engine: KnowledgeEngine | None) -> KnowledgeEngine:
    if engine is not None:
        return engine
    return RealCogneeAdapter(load_config())


def _fact_payload(fact: FactRecord) -> dict[str, Any]:
    return {
        "fact_id": fact.fact_id,
        "name": fact.name,
        "kind": fact.kind,
        "file_path": fact.file_path,
        "properties": fact.properties,
    }


def _property(properties: dict[str, Any], key: str) -> Any:
    if key in properties:
        return properties[key]
    nested = properties.get("fact_properties")
    if isinstance(nested, dict) and key in nested:
        return nested[key]
    return None


def _parse_sets(pairs: list[str]) -> dict[str, str]:
    updates: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise ValueError(f"--set must be key=value, got {pair!r}")
        updates[key] = value
    return updates


def _read_enola(enola_dir: Path) -> tuple[list[dict[str, Any]], str]:
    facts_path = enola_dir / "facts.jsonl"
    if not facts_path.is_file():
        raise ValueError(f"facts.jsonl is missing in {enola_dir}")
    facts: list[dict[str, Any]] = []
    for line_no, raw in enumerate(facts_path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError(f"facts.jsonl line {line_no} is not an object")
        facts.append(row)
    if not facts:
        raise ValueError("facts.jsonl is empty")
    snapshot_id = "e" * 64
    receipt_path = enola_dir / "receipt.json"
    if receipt_path.is_file():
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if isinstance(receipt, dict):
            raw_id = str(receipt.get("snapshot_id") or "").strip()
            if raw_id.startswith("sha256:"):
                raw_id = raw_id.removeprefix("sha256:")
            if raw_id:
                snapshot_id = raw_id
    return facts, snapshot_id


def _query_text(facts: list[dict[str, Any]]) -> str:
    for fact in facts:
        name = str(fact.get("name") or "")
        tokens = [token for token in re.findall(r"\w+", name) if len(token) >= 3]
        if tokens:
            return max(tokens, key=len)
    return "*"


def _failed_check() -> dict[str, Any]:
    return {
        "capability": "cognee_enola",
        "passed": False,
        "fact_id": None,
        "before": None,
        "after": None,
    }


async def _check(engine: KnowledgeEngine, args: argparse.Namespace) -> dict[str, Any]:
    facts, snapshot_id = _read_enola(Path(args.enola_dir))
    workspace_id = args.workspace_id
    repository_id = args.repository_id
    await engine.ingest_snapshot(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snapshot_id,
        facts=facts,
        receipt=None,
        insights=[],
    )
    queried = await engine.query(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snapshot_id,
        query_text=_query_text(facts),
    )
    if not queried.facts:
        queried = await engine.query(
            workspace_id=workspace_id,
            repository_id=repository_id,
            snapshot_id=snapshot_id,
            query_text="*",
        )
    if not queried.facts:
        return _failed_check()
    fact = queried.facts[0]
    before = _property(fact.properties, _CORRECTION_KEY)
    corrected = await engine.correct(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snapshot_id,
        fact_id=fact.fact_id,
        properties_update={_CORRECTION_KEY: _CORRECTION_VALUE},
    )
    looked = await engine.lookup(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snapshot_id,
        fact_id=fact.fact_id,
    )
    after = None
    if looked.found and looked.fact is not None:
        after = _property(looked.fact.properties, _CORRECTION_KEY)
    passed = bool(corrected.success and looked.found and after == _CORRECTION_VALUE and after != before)
    return {
        "capability": "cognee_enola",
        "passed": passed,
        "fact_id": fact.fact_id,
        "before": before,
        "after": after,
    }


async def _execute(engine: KnowledgeEngine, args: argparse.Namespace) -> dict[str, Any]:
    status = await engine.status()
    if not status.available:
        raise EngineUnavailableError(
            "engine_unavailable: cognee and ladybug packages must be installed"
        )
    workspace_id = args.workspace_id
    if args.command == "check":
        return await _check(engine, args)
    repository_id = args.repository_id
    snapshot_id = args.snapshot_id
    if args.command == "query":
        result = await engine.query(
            workspace_id=workspace_id,
            repository_id=repository_id,
            snapshot_id=snapshot_id,
            query_text=args.query,
            limit=args.limit,
        )
        return {
            "query": result.query,
            "snapshot_id": result.snapshot_id,
            "total_matched": result.total_matched,
            "facts": [_fact_payload(fact) for fact in result.facts],
        }
    if args.command == "lookup":
        result = await engine.lookup(
            workspace_id=workspace_id,
            repository_id=repository_id,
            snapshot_id=snapshot_id,
            fact_id=args.fact_id,
        )
        fact = _fact_payload(result.fact) if result.fact is not None else None
        return {
            "found": result.found,
            "snapshot_id": result.snapshot_id,
            "fact_id": result.fact.fact_id if result.fact is not None else args.fact_id,
            "fact": fact,
            "properties": fact["properties"] if fact is not None else {},
        }
    updates = _parse_sets(args.sets)
    result = await engine.correct(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snapshot_id,
        fact_id=args.fact_id,
        properties_update=updates,
    )
    return {
        "success": result.success,
        "snapshot_id": result.snapshot_id,
        "fact_id": result.corrected_fact_id,
        "properties_update": updates,
    }


def _exit_code(command: str, payload: dict[str, Any]) -> int:
    if command == "check":
        return 0 if payload.get("passed") else 1
    if command == "lookup":
        return 0 if payload.get("found") else 1
    if command == "correct":
        return 0 if payload.get("success") else 1
    return 0


def main(argv: list[str] | None = None, engine: KnowledgeEngine | None = None) -> int:
    """Run one engine command. An unavailable engine returns 1."""
    args = _build_parser().parse_args(argv)
    try:
        payload = asyncio.run(_execute(_engine(engine), args))
    except EngineUnavailableError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(payload, sort_keys=True))
    return _exit_code(args.command, payload)


__all__ = ["main"]

"""Append-only cpk5/v1 research-program facts shared with research-views.ts."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from pathlib import Path

from omp_work.v1.canonical import canonical_json


def _string(value: object) -> bool:
    return isinstance(value, str)


def _number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _count(value: object) -> bool:
    return type(value) is int and value >= 0


def _strings(value: object) -> bool:
    return isinstance(value, list) and all(_string(item) for item in value)


def _fields(value: object, fields: dict) -> bool:
    return isinstance(value, dict) and all(
        key in value and check(value[key]) for key, check in fields.items()
    )


_PAYLOADS = {
    "research.campaign": {
        "campaignId": _string,
        "status": _string,
        "objective": _string,
        "domain": _string,
        "revisionId": _string,
        "activeTrials": _count,
        "completedTrials": _count,
    },
    "research.graph": {
        "nodes": lambda value: (
            isinstance(value, list)
            and all(
                _fields(
                    node,
                    {
                        "id": _string,
                        "parentId": lambda item: item is None or _string(item),
                        "metric": lambda item: item is None or _number(item),
                        "cost": _number,
                    },
                )
                for node in value
            )
        ),
        "edges": lambda value: (
            isinstance(value, list)
            and all(_fields(edge, {"from": _string, "to": _string}) for edge in value)
        ),
    },
    "research.evidence": {
        "claimId": _string,
        "status": lambda value: value in ("supported", "contested", "unresolved"),
        "sourceId": _string,
        "passage": _string,
    },
    "research.usage": {
        "computeSeconds": _number,
        "modelTokens": _count,
        "costUsd": _string,
        "quotaExceeded": lambda value: type(value) is bool,
    },
    "research.worker": {
        "workerId": _string,
        "capabilities": _strings,
        "activeLeases": _count,
        "status": _string,
        "queueDepth": _count,
    },
    "research.operation": {
        "operationId": _string,
        "campaignId": _string,
        "name": lambda value: (
            value
            in (
                "create_research_campaign",
                "cancel_research_campaign",
                "conclude_research_campaign",
            )
        ),
    },
}


def _timestamp(value: object) -> bool:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value
    ):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _validate(fact: object) -> None:
    if not isinstance(fact, dict) or not _fields(
        fact,
        {
            "schema": lambda value: value == "cpk5/v1",
            "sourceIdentity": lambda value: value == "research-program",
            "sourceVersion": lambda value: value == "research-program.v1",
            "sequence": lambda value: _count(value) and value > 0,
            "timestamp": _timestamp,
            "payload": lambda value: isinstance(value, dict),
        },
    ):
        raise ValueError("invalid_schema")
    fact_type = fact.get("factType")
    if not isinstance(fact_type, str) or fact_type not in _PAYLOADS:
        raise ValueError("unknown_fact")
    if not _fields(fact["payload"], _PAYLOADS[fact_type]):
        raise ValueError("invalid_schema")


def read_program_facts(path: str | Path) -> list[dict]:
    """Read validated JSONL facts; an absent log is empty."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    facts = []
    for line in text.splitlines():
        fact = json.loads(line)
        _validate(fact)
        facts.append(fact)
    return facts


def append_program_fact(path: str | Path, fact: dict) -> None:
    """Validate before appending canonical JSON and reject reused sequences."""
    _validate(fact)
    if any(row["sequence"] == fact["sequence"] for row in read_program_facts(path)):
        raise ValueError("duplicate_sequence")
    with Path(path).open("a", encoding="utf-8") as log:
        log.write(canonical_json(fact) + "\n")

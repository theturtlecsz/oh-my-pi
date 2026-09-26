from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def make_test_enola_fixture(
    *,
    repo_name: str = "test-repo",
    snapshot_id: str = "a" * 64,
) -> tuple[bytes, bytes, bytes, list[dict[str, Any]]]:
    """Generates valid Enola artifact bytes including fact ID collision:
    Same FQN symbol 'User' across two distinct files.
    """
    fact1_id = hashlib.sha256(f"{repo_name}\0symbol\0User\0src/models/user.py".encode()).hexdigest()[:32]
    fact2_id = hashlib.sha256(f"{repo_name}\0symbol\0User\0src/auth/user.py".encode()).hexdigest()[:32]
    fact3_id = hashlib.sha256(f"{repo_name}\0module\0src/models/user.py\0src/models/user.py".encode()).hexdigest()[:32]

    facts = [
        {
            "id": fact1_id,
            "kind": "symbol",
            "name": "User",
            "repo": repo_name,
            "file": "src/models/user.py",
            "line": 10,
            "end_line": 50,
            "props": {"symbol_kind": "class"},
            "relations": [{"kind": "declares", "target": "src/models/user.py", "target_id": fact3_id}],
        },
        {
            "id": fact2_id,
            "kind": "symbol",
            "name": "User",
            "repo": repo_name,
            "file": "src/auth/user.py",
            "line": 15,
            "end_line": 60,
            "props": {"symbol_kind": "class"},
            "relations": [],
        },
        {
            "id": fact3_id,
            "kind": "module",
            "name": "src/models/user.py",
            "repo": repo_name,
            "file": "src/models/user.py",
            "line": 1,
            "end_line": 50,
            "props": {},
            "relations": [],
        },
    ]

    facts_lines = [json.dumps(f) for f in facts]
    facts_bytes = ("\n".join(facts_lines) + "\n").encode("utf-8")

    insights = [
        {
            "title": "Clean Separation",
            "description": "User models and auth are separated into two modules.",
            "confidence": 1.0,
            "evidence": [{"fact_id": fact1_id}, {"fact_id": fact2_id}],
        }
    ]
    insights_bytes = json.dumps(insights, indent=2).encode("utf-8")

    facts_sha256 = hashlib.sha256(facts_bytes).hexdigest()
    insights_sha256 = hashlib.sha256(insights_bytes).hexdigest()

    receipt = {
        "snapshot_id": snapshot_id,
        "format_version": 1,
        "enola_version": "v0.4.18",
        "generated_at": "2026-09-12T12:00:00Z",
        "duration": "150ms",
        "repo_path": f"/repo/{repo_name}",
        "fact_count": len(facts),
        "insight_count": len(insights),
        "quality": {
            "files_seen": 2,
            "files_parsed": 2,
            "files_skipped": 0,
            "parse_errors": 0,
        },
        "output_hashes": {
            "facts.jsonl": f"sha256:{facts_sha256}",
            "insights.json": f"sha256:{insights_sha256}",
        },
    }
    receipt_bytes = json.dumps(receipt, indent=2).encode("utf-8")

    return facts_bytes, receipt_bytes, insights_bytes, facts


def load_staged_fixture(
    name: str = "A",
) -> tuple[bytes, bytes, bytes, list[dict[str, Any]]]:
    """Load actual minimal A/B staged fixtures from tests/fixtures/{name}."""
    fixtures_dir = Path(__file__).resolve().parent.parent / "fixtures" / name
    facts_bytes = (fixtures_dir / "facts.jsonl").read_bytes()
    receipt_bytes = (fixtures_dir / "receipt.json").read_bytes()
    insights_bytes = (fixtures_dir / "insights.json").read_bytes()
    raw_facts = [
        json.loads(line) for line in facts_bytes.decode("utf-8").splitlines() if line.strip()
    ]
    return facts_bytes, receipt_bytes, insights_bytes, raw_facts


__all__ = [
    "load_staged_fixture",
    "make_test_enola_fixture",
]

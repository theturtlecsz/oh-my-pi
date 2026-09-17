from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from omp_work.knowledge_contracts import (
    ExtractionCoverage,
    ExtractorInfo,
    SnapshotRef,
)


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
        json.loads(line) for line in facts_bytes.splitlines() if line.strip()
    ]
    return facts_bytes, receipt_bytes, insights_bytes, raw_facts


def make_synthetic_enola_snapshot_ref(
    *,
    repository_id: UUID,
    fixture_name: str = "A",
    snapshot_id: str | None = None,
) -> SnapshotRef:
    """Synthetic metadata paired with real retained Enola output."""
    from datetime import datetime, timezone

    if fixture_name == "B":
        sid = snapshot_id or "sha256:b3649242961413ac43d41254871ebfd9a74bed88045d1c58972f67267216db55"
        dt = datetime(2026, 9, 12, 12, 55, 26, tzinfo=timezone.utc)
    else:
        sid = snapshot_id or "sha256:5dc87df1316f9afab59d47c42eed60f6a3d78286d9dc852d5b9ff827b66d4aee"
        dt = datetime(2026, 9, 12, 12, 54, 41, tzinfo=timezone.utc)

    return SnapshotRef(
        repository_id=repository_id,
        snapshot_id=sid,
        base_commit="375c62487eec02913cf68f705853276306baaf1f",
        tree_sha="375c62487eec02913cf68f705853276306baaf1f",
        candidate_tree_sha=None,
        included_untracked=(),
        excluded=(),
        extractor=ExtractorInfo(
            name="enola",
            version="0.4.18",
            binary_sha256="f8a032381788d289e6ef26223564d8918e39759efa03805389500add1d4a75f5",
            config_hash="f8233549c49910b52327defe5523379e050b55de00b039017b649b3c0545ee96",
        ),
        coverage=ExtractionCoverage(
            files_seen=10,
            files_parsed=7,
            files_skipped=2,
            parse_errors=0,
            unsupported_features=(),
        ),
        created_at=dt,
    )


def make_test_snapshot_ref(
    *,
    repository_id: UUID,
    snapshot_id: str = "a" * 64,
) -> SnapshotRef:
    from datetime import datetime, timezone

    return SnapshotRef(
        repository_id=repository_id,
        snapshot_id=snapshot_id,
        base_commit="375c62487eec02913cf68f705853276306baaf1f",
        tree_sha="375c62487eec02913cf68f705853276306baaf1f",
        candidate_tree_sha=None,
        included_untracked=(),
        excluded=(),
        extractor=ExtractorInfo(
            name="enola",
            version="0.4.18",
            binary_sha256="f8a032381788d289e6ef26223564d8918e39759efa03805389500add1d4a75f5",
            config_hash="f8233549c49910b52327defe5523379e050b55de00b039017b649b3c0545ee96",
        ),
        coverage=ExtractionCoverage(
            files_seen=2,
            files_parsed=2,
            files_skipped=0,
            parse_errors=0,
            unsupported_features=(),
        ),
        created_at=datetime.now(timezone.utc),
    )


def write_test_capability_file(
    capabilities_dir: Path,
    *,
    token: str,
    actor_id: UUID,
    workspaces: list[UUID],
    scopes: list[str],
    name: str = "test-cap",
) -> Path:
    capabilities_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    cap_file = capabilities_dir / f"{name}.json"
    data = {
        "token": token,
        "actor_id": str(actor_id),
        "actor_kind": "test-agent",
        "workspaces": [str(w) for w in workspaces],
        "scopes": scopes,
    }
    cap_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    cap_file.chmod(0o600)
    return cap_file

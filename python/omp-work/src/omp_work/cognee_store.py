"""File-backed Cognee-shaped knowledge store for WorkService jobs.

Not Cursor agent memory. Persists claim nodes so retrieval survives process restart.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import json
import time

from omp_work.cognee_adapter import CogneeQueryResult, query_graph


DEFAULT_SEED: dict[str, dict[str, Any]] = {
    "write-first": {
        "claim": "Prefer write-first packets; empty soft with 0 product writes is FAIL",
        "source": "ACTIVE-POLICY first_write_gate",
    },
    "effort": {
        "claim": "Default E1 Flash; escalate only with effort_reason",
        "source": "effort_routing",
    },
}


@dataclass
class CogneeStore:
    """JSON-backed node store keyed by short id."""

    path: Path
    nodes: dict[str, dict[str, Any]]

    @classmethod
    def open(cls, path: Path | str, *, seed_if_empty: bool = True) -> "CogneeStore":
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.is_file():
            data = json.loads(p.read_text())
            nodes = dict(data.get("nodes") or {})
        else:
            nodes = dict(DEFAULT_SEED) if seed_if_empty else {}
        store = cls(path=p, nodes=nodes)
        if not p.is_file():
            store.flush()
        return store

    def flush(self) -> None:
        payload = {
            "version": 1,
            "updated_at": time.time(),
            "nodes": self.nodes,
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n")
        tmp.replace(self.path)

    def upsert(self, node_id: str, *, claim: str, source: str, **extra: Any) -> None:
        if not node_id.strip():
            raise ValueError("node_id required")
        if not claim.strip():
            raise ValueError("claim required")
        row = {"claim": claim, "source": source, **extra, "updated_at": time.time()}
        self.nodes[node_id] = row
        self.flush()

    def query(self, *, query: str, job_id: str) -> CogneeQueryResult:
        return query_graph(query=query, job_id=job_id, store=self.nodes)


def run_job_query(
    *,
    store_path: Path | str,
    job_id: str,
    query: str,
) -> CogneeQueryResult:
    if not job_id.strip():
        raise ValueError("job_id required — adapters run via WorkService, not chat")
    store = CogneeStore.open(store_path)
    return store.query(query=query, job_id=job_id)

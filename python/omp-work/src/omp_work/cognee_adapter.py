"""Thin Cognee adapter for WorkService jobs (W3). Not Cursor agent memory."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CogneeQueryResult:
    adapter: str
    query: str
    nodes: list[dict[str, Any]]
    invoked_via: str

    def finding(self) -> str:
        if not self.nodes:
            return ""
        return str(self.nodes[0].get("claim", ""))


def query_graph(*, query: str, job_id: str, store: dict[str, Any] | None = None) -> CogneeQueryResult:
    """Invoke Cognee-shaped retrieval under a WorkService job id (demo store)."""
    if not job_id.strip():
        raise ValueError("job_id required — adapters run via WorkService, not chat")
    store = store or {
        "write-first": {
            "claim": "Prefer write-first packets; empty soft with 0 product writes is FAIL",
            "source": "ACTIVE-POLICY first_write_gate",
        },
        "effort": {
            "claim": "Default E1 Flash; escalate only with effort_reason",
            "source": "effort_routing",
        },
    }
    nodes: list[dict[str, Any]] = []
    q = query.lower()
    for key, val in store.items():
        if key in q or any(part in q for part in key.split("-")):
            nodes.append({"id": key, **val})
    if not nodes and store:
        k, v = next(iter(store.items()))
        nodes.append({"id": k, **v})
    return CogneeQueryResult(
        adapter="cognee-omp-v1",
        query=query,
        nodes=nodes,
        invoked_via=f"workservice:{job_id}",
    )

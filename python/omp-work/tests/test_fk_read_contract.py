"""OMP-279: close the contract over the FK-1 exact reads added by s03/s04.

Every declared read in ``_READS`` must name a GET route the service actually
registers, and ``contract.json`` must declare exactly that same set.
"""

from __future__ import annotations

from pathlib import Path

from omp_work import _READS, load_contract
from omp_work.operations.config import OperationsConfig
from omp_work.v1.server import create_app


def _registered_get_paths(tmp_path: Path) -> set[str]:
    config = OperationsConfig(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
    )
    app = create_app(config, capabilities_dir=tmp_path / "capabilities")
    return {
        route.path
        for route in app.routes
        if "GET" in getattr(route, "methods", set())
    }


def test_every_declared_read_matches_a_registered_get_route(tmp_path: Path) -> None:
    registered = _registered_get_paths(tmp_path)
    for read in _READS:
        method, _, path = read.partition(" ")
        assert method == "GET", read
        assert path in registered, f"declared read has no registered route: {read}"


def test_contract_reads_equal_declared_reads() -> None:
    contract = load_contract()
    assert frozenset(contract.reads) == _READS
    # The FK-1 reads are appended after the pre-existing reads.
    assert contract.reads[-4:] == (
        "GET /v1/work-items/{key}/revisions/{selector}",
        "GET /v1/receipts/{receipt_id}",
        "GET /v1/workspaces/{workspace_id}/work-items",
        "GET /v1/workspaces/{workspace_id}/events",
    )

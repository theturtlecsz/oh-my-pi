"""OMP-490-s04: write-surface audit probe tests.

Failure mode defended: an ingress gate that lets a write operation or an extra
route through, or a probe that hides it, leaves the write surface unaudited. The
probe runs against the real ingress app with a recording upstream.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

import httpx
import pytest

from omp_work import contract_sha256, load_contract
from omp_work.__main__ import main
from omp_work.client_ingress import (
    DEFAULT_OPERATIONS,
    IngressConfig,
    create_ingress_app,
)
from omp_work.client_ingress_probe import run_probe

WORKSPACE = uuid.UUID("11111111-1111-4111-8111-111111111111")
TOKEN = "probe-secret-token"
RUNBOOK = (
    Path(__file__).resolve().parents[3]
    / "docs"
    / "runbooks"
    / "client-ingress-threat-model.md"
)


def _upstream(seen: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "authorization" not in request.headers:
            return httpx.Response(401, json={"error": "unauthorized"})
        return httpx.Response(400 if request.method == "POST" else 200, json={})

    return httpx.MockTransport(handler)


def _probe(
    operations: tuple[str, ...] = DEFAULT_OPERATIONS, token: str | None = None
) -> tuple[dict, list[httpx.Request]]:
    seen: list[httpx.Request] = []
    config = IngressConfig(workspace_id=WORKSPACE, operations=operations)
    app = create_ingress_app(config, "http://upstream", _upstream(seen))
    report = run_probe(
        "http://gate",
        config,
        contract_sha256(),
        token,
        transport=httpx.ASGITransport(app=app),
    )
    return report, seen


@pytest.mark.parametrize("token", [None, TOKEN])
def test_default_gate_passes(token: str | None) -> None:
    report, seen = _probe(token=token)
    assert report["passed"] is True
    assert report["authenticated"] is (token is not None)
    assert len(report["results"]) == 17 + 6
    assert TOKEN not in json.dumps(report)
    if token is None:
        assert seen and all("authorization" not in r.headers for r in seen)
    else:
        assert all(r.headers["x-omp-contract-sha256"] == contract_sha256() for r in seen)
        # every probe that reached upstream is an allowed operation
        assert len(seen) == len(DEFAULT_OPERATIONS)


def test_widened_gate_fails_naming_operation() -> None:
    wide = (*DEFAULT_OPERATIONS, "mission.resume")
    seen: list[httpx.Request] = []
    gate_config = IngressConfig(workspace_id=WORKSPACE, operations=wide)
    app = create_ingress_app(gate_config, "http://upstream", _upstream(seen))
    report = run_probe(
        "http://gate",
        IngressConfig(workspace_id=WORKSPACE, operations=DEFAULT_OPERATIONS),
        contract_sha256(),
        transport=httpx.ASGITransport(app=app),
    )
    assert report["passed"] is False
    assert report["failed"] == ["mission.resume"]


def test_runbook_allow_rows_equal_default_operations() -> None:
    allowed = set()
    for line in RUNBOOK.read_text().splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("|") and len(cells) == 6 and cells[5] == "allow":
            allowed.add(cells[0])
    assert allowed == set(DEFAULT_OPERATIONS)
    assert allowed <= {op.name for op in load_contract().client_contract.operations}


def test_cli_rejects_loose_bearer_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"workspace_id": str(WORKSPACE)}))
    bearer = tmp_path / "bearer.json"
    bearer.write_text(json.dumps({"token": TOKEN}))
    os.chmod(bearer, 0o644)
    code = main(
        [
            "ingress", "probe", "--base-url", "http://127.0.0.1:1",
            "--config", str(config), "--out", str(tmp_path / "out.json"),
            "--bearer-file", str(bearer),
        ]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert TOKEN not in captured.out + captured.err
    assert not (tmp_path / "out.json").exists()

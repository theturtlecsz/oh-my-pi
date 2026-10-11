from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

import omp_work
from omp_work.v1.models import Approval, ResearchComponentDescriptor


def worker(capabilities: tuple[str, ...]) -> ResearchComponentDescriptor:
    return ResearchComponentDescriptor.model_validate(
        {
            "contract_version": "research-component.v1",
            "kind": "worker",
            "name": "capability-worker",
            "version": "1.0.0",
            "artifact_sha256": "a" * 64,
            "roles": ("research_worker",),
            "capabilities": capabilities,
        }
    )


def test_worker_accepts_compute_instrument_and_location_capabilities() -> None:
    capabilities = (
        "compute.cpu",
        "compute.gpu",
        "instrument:scope",
        "location:local",
        "location:remote",
    )
    assert worker(capabilities).capabilities == capabilities


@pytest.mark.parametrize(
    "token",
    (
        "compute",
        "location:",
        "instrument:Scope",
        "location:local.extra",
        "compute:cpu",
    ),
)
def test_worker_rejects_invalid_capability_tokens(token: str) -> None:
    with pytest.raises(ValidationError) as exc:
        worker((token,))
    assert exc.value.errors()[0]["loc"] == ("capabilities", 0)
    assert exc.value.errors()[0]["type"] == "string_pattern_mismatch"


@pytest.mark.parametrize("prefix", ("compute.", "location:"))
def test_capability_length_limit_remains_128(prefix: str) -> None:
    token = prefix + "a" * (128 - len(prefix))
    assert worker((token,)).capabilities == (token,)
    with pytest.raises(ValidationError) as exc:
        worker((token + "a",))
    assert exc.value.errors()[0]["type"] == "string_too_long"


@pytest.mark.parametrize(
    ("capabilities", "message"),
    (
        (("location:remote", "location:local"), "capabilities must be sorted"),
        (("instrument:scope", "instrument:scope"), "capabilities must contain unique values"),
    ),
)
def test_worker_requires_sorted_unique_capabilities(
    capabilities: tuple[str, ...], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        worker(capabilities)


def test_owner_approval_accepts_omp_318() -> None:
    approval = Approval.model_validate(
        {
            "contract_version": "work.omp.dev/v1",
            "contract_sha256": "b" * 64,
            "approved_by": "owner",
            "approved_at": datetime(2026, 10, 11, tzinfo=UTC),
            "issue": "OMP-318",
        }
    )
    assert approval.issue == "OMP-318"


@pytest.mark.parametrize("api", (False, True))
def test_committed_schemas_match_generators(api: bool) -> None:
    name = "api-schema.json" if api else "schema.json"
    generated = omp_work.generate_api_schema() if api else omp_work.generate_schema()
    committed = json.loads((Path(omp_work._contract_dir()) / name).read_text())
    assert committed == generated

from __future__ import annotations

import json
from pathlib import Path

import omp_work
from omp_work.v1.canonical import sha256
from omp_work.v1.models import ResearchComponentDescriptor


def test_research_component_hash_matches_golden_vectors() -> None:
    fixture = json.loads(
        (Path(omp_work._contract_dir()) / "research-component-hash.json").read_text(
            encoding="utf-8"
        )
    )
    assert fixture["algorithm"] == "research-component.v1"
    for vector in fixture["vectors"]:
        model = ResearchComponentDescriptor.model_validate(vector["descriptor"])
        assert (
            sha256(model.model_dump(mode="json")) == vector["component_sha256"]
        ), vector["name"]

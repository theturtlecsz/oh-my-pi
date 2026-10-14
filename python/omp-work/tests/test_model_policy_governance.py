"""Test-only qualification: declarations are not proof of model invocation."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json

import pytest

from omp_work.model_proxy import ProxyConfig
from omp_work.model_proxy_upstream import GEMINI, OLLAMA
from omp_work.operations.config import OperationsConfig
from omp_work.routing.policy import RoutingPolicy, load_policy
from omp_work.routing.router import route_stage
from test_model_proxy_upstream import gemini_route, ollama_route


ROLE_STAGES = {
    "plan": {"plan"},
    "implement": {"implement"},
    "review": {"audit", "acceptance_review"},
    "research": {"research"},
    "judge": set(),
    "operator": set(),
    "verify": {"verify"},
}


def discrepancies(policy: RoutingPolicy, qualified_wires: dict[str, str]) -> list[str]:
    """Name mismatches against executable routes and exact association evidence."""
    governance = policy["governance"]
    found = []
    roles = governance["roles"]
    for name in ROLE_STAGES.keys() - roles.keys():
        found.append(f"missing_role:{name}")
    for name in roles.keys() - ROLE_STAGES.keys():
        found.append(f"unknown_role:{name}")
    for name, role in roles.items():
        stages, reviewers = set(), set()
        for selector in role["selectors"]:
            stage = selector["stage"]
            if stage not in policy.stages:
                found.append(f"invalid_stage_selector:{name}:{stage}")
                continue
            if "reviewer_index" in selector:
                index = selector["reviewer_index"]
                if type(index) is not int or not 0 <= index < len(policy.stages[stage].reviewers):
                    found.append(f"invalid_reviewer_selector:{name}:{stage}")
                else:
                    reviewers.add((stage, index))
            else:
                stages.add(stage)
                selected = route_stage(policy, stage)
                if selected.provider not in governance["lanes"]:
                    found.append(f"unrecorded_route_lane:{name}:{selected.provider}")
                if (selected.provider, selected.model) != (
                    policy.stages[stage].provider, policy.stages[stage].model
                ):
                    found.append(f"route_mismatch:{name}:{stage}")
        if stages != ROLE_STAGES.get(name):
            found.append(f"role_stage_coverage:{name}")
        expected_reviewers = {
            (stage, index)
            for stage in ROLE_STAGES.get(name, set())
            for index in range(len(policy.stages[stage].reviewers))
        }
        if reviewers != expected_reviewers:
            found.append(f"role_reviewer_coverage:{name}")
        expected_route_status = "resolved" if stages else "pending_owner"
        if role["route_status"] != expected_route_status:
            found.append(f"route_status:{name}")
        invocation = role["invocation"]
        # Only implement has recorded production-worker test evidence.
        expected_status = "test_verified" if name == "implement" else "unverified"
        if invocation["status"] != expected_status:
            found.append(f"invocation_status:{name}")
        expected_caller = "omp_work.model_proxy" if name == "implement" else None
        if invocation["caller"] != expected_caller:
            found.append(f"invocation_caller:{name}")
        if not invocation["evidence"]:
            found.append(f"missing_role_evidence:{name}")
        if role["unresolved_owner"] != (None if name == "implement" else "Chris"):
            found.append(f"role_owner:{name}")
    lanes = governance["lanes"]
    if lanes.keys() != policy.concurrency.providers.keys():
        found.append("lane_coverage")
    for lane, association in lanes.items():
        wire = association["wire"]
        if wire is None:
            if association["status"] != "pending_owner" or association["unresolved_owner"] != "Chris":
                found.append(f"pending_lane_owner:{lane}")
        elif (
            qualified_wires.get(lane) != wire
            or association["status"] != "test_verified"
            or association["unresolved_owner"] is not None
            or not association["evidence"]
        ):
            found.append(f"unqualified_lane_wire:{lane}")
    supervisor = governance["supervisor"]
    if (supervisor["kind"], supervisor["selectors"], supervisor["model"], supervisor["lane"]) != (
        "deterministic_software", [], None, None
    ):
        found.append("supervisor_model_binding")
    return found


@pytest.fixture
def qualified_wires(tmp_path) -> dict[str, str]:
    policy = load_policy()
    primary = route_stage(policy, "implement")
    overflow = route_stage(policy, "implement", provider_in_flight={
        primary.provider: policy.concurrency.providers[primary.provider]
    })
    transports = {
        primary.provider: gemini_route(model=primary.model),
        overflow.provider: ollama_route(model=overflow.model),
    }
    config = ProxyConfig(
        OperationsConfig(tmp_path / "config", tmp_path / "state", tmp_path / "data"),
        tmp_path / "capability", transports,
    )
    # Actual proxy validation refuses a valid API attached to the wrong lane.
    for lane, other_wire in ((primary.provider, OLLAMA), (overflow.provider, GEMINI)):
        with pytest.raises(ValueError, match="invalid_transport_map"):
            replace(config, transports={**transports, lane: replace(transports[lane], api=other_wire)})
    return {lane: transport.api for lane, transport in config.transports.items()}


def test_packaged_governance_qualifies_declared_routes_without_claiming_live_calls(qualified_wires):
    assert discrepancies(load_policy(), qualified_wires) == []


@pytest.mark.parametrize(("mutation", "expected"), [
    ("missing_role", "missing_role:verify"),
    ("invalid_stage", "invalid_stage_selector:plan:missing"),
    ("invalid_reviewer", "invalid_reviewer_selector:review:audit"),
    ("missing_acceptance", "role_stage_coverage:review"),
    ("judge_research", "role_stage_coverage:judge"),
    ("false_invocation", "invocation_status:research"),
    ("prefix_as_wire", "unqualified_lane_wire:openai_codex"),
    ("wrong_lane_wire", "unqualified_lane_wire:gemini_flash"),
    ("pending_without_owner", "pending_lane_owner:kimi"),
    ("supervisor_model", "supervisor_model_binding"),
])
def test_metadata_regressions_produce_named_discrepancies(tmp_path, qualified_wires, mutation, expected):
    raw = deepcopy(load_policy().raw)
    governance = raw["governance"]
    roles, lanes = governance["roles"], governance["lanes"]
    if mutation == "missing_role":
        del roles["verify"]
    elif mutation == "invalid_stage":
        roles["plan"]["selectors"][0]["stage"] = "missing"
    elif mutation == "invalid_reviewer":
        roles["review"]["selectors"][2]["reviewer_index"] = -1
    elif mutation == "missing_acceptance":
        roles["review"]["selectors"] = [s for s in roles["review"]["selectors"] if s["stage"] != "acceptance_review"]
    elif mutation == "judge_research":
        roles["judge"]["selectors"] = [{"stage": "research"}]
    elif mutation == "false_invocation":
        roles["research"]["invocation"]["status"] = "test_verified"
    elif mutation == "prefix_as_wire":
        lanes["openai_codex"] = {"wire": "openai-codex", "status": "test_verified", "evidence": ["reviewer prefix"], "unresolved_owner": None}
    elif mutation == "wrong_lane_wire":
        lanes["gemini_flash"]["wire"] = OLLAMA
    elif mutation == "pending_without_owner":
        lanes["kimi"]["unresolved_owner"] = None
    elif mutation == "supervisor_model":
        governance["supervisor"]["model"] = "gpt-6-sol"
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps(raw))
    assert expected in discrepancies(load_policy(policy_file), qualified_wires)

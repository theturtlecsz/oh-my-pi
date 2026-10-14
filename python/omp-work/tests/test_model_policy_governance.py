"""Test-only qualification: declarations are not proof of model invocation."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import subprocess

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


# Documentation is a consumer-facing binding record. Qualify its parsed data,
# never TypeScript source text or incidental caller implementation spelling.
_REPO = Path(__file__).resolve().parents[3]
_DOC = _REPO / "docs/model-policy-governance.md"


def binding_tables(text: str) -> dict[str, list[dict[str, str]]]:
    tables: dict[str, list[dict[str, str]]] = {}
    label = None
    header = None
    for line in text.splitlines():
        if line.startswith("## "):
            name = line.removeprefix("## ")
            label = name if name.endswith("-bindings") else None
            header = None
            if label is not None:
                assert label not in tables, f"duplicate table: {label}"
                tables[label] = []
        elif label is not None and line.startswith("|"):
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if header is None:
                header = cells
                assert len(set(header)) == len(header), f"duplicate columns: {label}"
            elif set("".join(cells)) <= set("-: "):
                continue
            else:
                assert len(cells) == len(header), f"malformed row: {label}"
                tables[label].append(dict(zip(header, cells, strict=True)))
    return tables


def _values(values) -> str:
    return "; ".join(values) or "null"


def expected_bindings(policy: RoutingPolicy) -> dict[str, list[dict[str, str]]]:
    governance = policy["governance"]
    roles = []
    for name, role in governance["roles"].items():
        stages = [s["stage"] for s in role["selectors"] if "reviewer_index" not in s]
        reviewers = [policy.stages[s["stage"]].reviewers[s["reviewer_index"]]
                     for s in role["selectors"] if "reviewer_index" in s]
        invocation = role["invocation"]
        roles.append({
            "Role": name, "Stages": _values(stages),
            "Model": _values(policy.stages[s].model for s in stages),
            "Lane": _values(policy.stages[s].provider for s in stages),
            "Effort": _values(policy.stages[s].effort for s in stages),
            "Reviewers": _values(reviewers), "Route status": role["route_status"],
            "Caller": invocation["caller"] or "null", "Status": invocation["status"],
            "Owner": role["unresolved_owner"] or "null", "Evidence": _values(invocation["evidence"]),
        })
    stages = [{
        "Stage": name, "Rule": stage.rule, "Model": stage.model, "Lane": stage.provider,
        "Effort": stage.effort, "Reviewers": _values(stage.reviewers),
        "Max retries": str(stage.max_retries),
        "Best of N": json.dumps(policy["stages"][name].get("best_of_n"), separators=(",", ":")),
        "Caller": "omp_work.routing.router::route_stage", "Status": "declared",
        "Evidence": f"python/omp-work/src/omp_work/routing/routing-policy.v1.json#stages.{name}",
    } for name, stage in policy.stages.items()]
    tiers = [{
        "Tier": name, "Model": tier.model, "Lane": tier.provider,
        "Est tokens": str(tier.est_tokens), "Caller": "omp_work.routing.escalation::next_route",
        "Status": "declared",
        "Evidence": f"python/omp-work/src/omp_work/routing/routing-policy.v1.json#tiers.{name}",
    } for name, tier in policy.tiers.items()]
    lanes = [{
        "Lane": name, "Cap": str(cap), "Wire": governance["lanes"][name]["wire"] or "null",
        "Caller": "omp_work.model_proxy" if governance["lanes"][name]["wire"] else "null",
        "Status": governance["lanes"][name]["status"],
        "Owner": governance["lanes"][name]["unresolved_owner"] or "null",
        "Evidence": _values(governance["lanes"][name]["evidence"]),
    } for name, cap in policy.concurrency.providers.items()]
    alternates = [{
        "Rule": alternate.rule, "From lane": alternate.from_provider,
        "Model": alternate.to.model, "Lane": alternate.to.provider,
        "Caller": "omp_work.routing.router::route_stage", "Status": "pending_owner",
        "Owner": "Chris", "Approval": "null",
        "Evidence": "python/omp-work/src/omp_work/routing/substitutions.v1.json#overflow",
    } for alternate in policy.alternates]
    return {"role-bindings": roles, "stage-bindings": stages, "tier-bindings": tiers,
            "lane-bindings": lanes, "alternate-bindings": alternates}


def binding_discrepancies(tables, policy: RoutingPolicy) -> list[str]:
    found = []
    expected = expected_bindings(policy)
    if tables.keys() - {"agent-bindings"} != expected.keys():
        found.append("binding_table_coverage")
    for label, rows in expected.items():
        key = next(iter(rows[0]))
        actual = tables.get(label, [])
        keys = [r.get(key) for r in actual]
        if len(set(keys)) != len(keys) or set(keys) != {r[key] for r in rows}:
            found.append(f"binding_key_coverage:{label}")
        by_key = {r.get(key): r for r in actual}
        for row in rows:
            observed = by_key.get(row[key])
            if observed is None:
                continue
            if observed.keys() != row.keys():
                found.append(f"binding_columns:{label}:{row[key]}")
            for column, value in row.items():
                if observed.get(column) != value:
                    found.append(f"binding_value:{label}:{row[key]}:{column}")
    return found


def test_documented_bindings_cover_policy_identities_lanes_and_qualification():
    assert binding_discrepancies(binding_tables(_DOC.read_text()), load_policy()) == []


@pytest.mark.parametrize("label", [
    "role-bindings", "stage-bindings", "tier-bindings", "lane-bindings", "alternate-bindings",
])
def test_missing_or_duplicate_binding_cannot_hide_a_policy_selector(label):
    tables = binding_tables(_DOC.read_text())
    removed = deepcopy(tables)
    removed[label].pop()
    assert f"binding_key_coverage:{label}" in binding_discrepancies(removed, load_policy())
    tables[label].append(dict(tables[label][0]))
    assert f"binding_key_coverage:{label}" in binding_discrepancies(tables, load_policy())


@pytest.mark.parametrize(("label", "key", "column", "value"), [
    ("stage-bindings", "audit", "Reviewers", "kimi-code/k3-256k"),
    ("role-bindings", "judge", "Model", "deepseek-v4.1-flash:cloud"),
    ("role-bindings", "operator", "Lane", "openai_codex"),
    ("role-bindings", "plan", "Status", "test_verified"),
    ("role-bindings", "verify", "Stages", "evaluate"),
    ("stage-bindings", "plan", "Effort", "E1"),
    ("tier-bindings", "E2", "Model", "claude-fable-5-1"),
    ("lane-bindings", "openai_codex", "Wire", "openai-codex"),
    ("lane-bindings", "chatgpt_astra", "Cap", "1"),
    ("alternate-bindings", "gemini-flash-overflow", "Approval", "approved"),
])
def test_changed_binding_reports_exact_consumer_discrepancy(label, key, column, value):
    tables = binding_tables(_DOC.read_text())
    row = next(r for r in tables[label] if next(iter(r.values())) == key)
    row[column] = value
    assert f"binding_value:{label}:{key}:{column}" in binding_discrepancies(tables, load_policy())


def test_agent_binding_selectors_parse_with_shared_bun_yaml_loader():
    rows = binding_tables(_DOC.read_text())["agent-bindings"]
    paths = [row["Agent"] for row in rows]
    assert len(set(paths)) == len(paths)
    assert set(paths) == {
        "packages/coding-agent/src/prompts/agents/scout.md",
        "packages/coding-agent/src/prompts/agents/reviewer.md",
        "session-system/agents/auditor.md",
    }
    script = """
import { parseFrontmatter } from "@oh-my-pi/pi-utils/frontmatter";
const paths = await Bun.stdin.json();
const selectors = {};
for (const path of paths) {
    selectors[path] = parseFrontmatter(await Bun.file(path).text(), { level: "fatal" }).frontmatter.model;
}
process.stdout.write(JSON.stringify(selectors));
"""
    result = subprocess.run(["bun", "-e", script], cwd=_REPO, input=json.dumps(paths),
                            capture_output=True, text=True, check=True)
    assert {row["Agent"]: row["Selector"] for row in rows} == json.loads(result.stdout)
    for row in rows:
        assert row["Status"] == "test_verified"
        assert row["Evidence"] == "packages/coding-agent/test/model-policy-governance.test.ts"

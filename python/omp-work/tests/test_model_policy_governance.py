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


def discrepancies(
    policy: RoutingPolicy,
    qualified_wires: dict[str, str],
    *,
    tables: dict[str, list[dict[str, str]]] | None = None,
    substitutions: dict | None = None,
    agent_selectors: dict[str, str] | None = None,
) -> list[str]:
    """Name mismatches against executable routes and exact association evidence.

    Optional inputs extend the same qualifier: parsed doc table records, the
    parsed substitutions file and independently parsed agent model selectors.
    """
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
            reviewer_prefixes = {r.partition("/")[0] for st in policy.stages.values() for r in st.reviewers}
            if qualified_wires.get(lane) is None and reviewer_prefixes & {wire, *association["evidence"]}:
                # A reviewer prefix names a provider, never a proxy lane's wire.
                found.append(f"unsupported_wire_association:{lane}")
    supervisor = governance["supervisor"]
    if (supervisor["kind"], supervisor["selectors"], supervisor["model"], supervisor["lane"]) != (
        "deterministic_software", [], None, None
    ):
        found.append("supervisor_model_binding")
    if tables is not None:
        found += binding_discrepancies(tables, policy, agent_selectors)
    if substitutions is not None:
        found += substitution_discrepancies(substitutions, policy)
    return found


def substitution_discrepancies(substitutions: dict, policy: RoutingPolicy) -> list[str]:
    found = []
    expected = {
        "stages": ("stage", {name: {
            "rule": s.rule, "provider": s.provider, "model": s.model, "reviewers": list(s.reviewers),
        } for name, s in policy.stages.items()}),
        "overflow": ("rule", {a.rule: {
            "from_provider": a.from_provider, "provider": a.to.provider, "model": a.to.model,
        } for a in policy.alternates}),
    }
    for section, (key, rows) in expected.items():
        actual = substitutions.get(section, [])
        keys = [r.get(key) for r in actual]
        for name in sorted(rows.keys() - set(keys)):
            found.append(f"substitution_missing:{section}:{name}")
        for name in sorted(set(keys) - rows.keys()):
            found.append(f"substitution_extra:{section}:{name}")
        for name in sorted({k for k in keys if keys.count(k) > 1}):
            found.append(f"substitution_duplicate:{section}:{name}")
        for row in actual:
            for field, value in rows.get(row.get(key), {}).items():
                if row.get(field) != value:
                    found.append(f"substitution_value:{section}:{row[key]}:{field}")
    return found


def substitution_status(substitutions: dict | None, policy: RoutingPolicy) -> str:
    """Absent substitutions are pending, never verified."""
    if substitutions is None:
        return "pending_absent"
    return "mismatch" if substitution_discrepancies(substitutions, policy) else "compared"


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


_BINDING_KEYS = {
    "role-bindings": "Role", "stage-bindings": "Stage", "tier-bindings": "Tier",
    "lane-bindings": "Lane", "alternate-bindings": "Rule",
}


def binding_discrepancies(tables, policy: RoutingPolicy, agent_selectors: dict[str, str] | None = None) -> list[str]:
    found = []
    expected = expected_bindings(policy)
    if tables.keys() - {"agent-bindings"} != expected.keys():
        found.append("binding_table_coverage")
    for label, rows in expected.items():
        key = _BINDING_KEYS[label]
        actual = tables.get(label, [])
        keys = [r.get(key) for r in actual]
        wanted = {r[key] for r in rows}
        if len(set(keys)) != len(keys) or set(keys) != wanted:
            found.append(f"binding_key_coverage:{label}")
        found += [f"missing_binding:{label}:{k}" for k in sorted(wanted - set(keys))]
        found += [f"extra_binding:{label}:{k}" for k in sorted(set(keys) - wanted, key=str)]
        found += [f"duplicate_binding:{label}:{k}" for k in sorted({k for k in keys if keys.count(k) > 1}, key=str)]
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
    if agent_selectors is not None:
        agents = tables.get("agent-bindings", [])
        paths = [r.get("Agent") for r in agents]
        found += [f"missing_binding:agent-bindings:{p}" for p in sorted(agent_selectors.keys() - set(paths))]
        found += [f"extra_binding:agent-bindings:{p}" for p in sorted(set(paths) - agent_selectors.keys(), key=str)]
        found += [f"duplicate_binding:agent-bindings:{p}" for p in sorted({p for p in paths if paths.count(p) > 1}, key=str)]
        for row in agents:
            if row.get("Agent") in agent_selectors and row.get("Selector") != agent_selectors[row["Agent"]]:
                found.append(f"binding_value:agent-bindings:{row['Agent']}:Selector")
    return found


_AGENT_SCRIPT = """
import { parseFrontmatter } from "@oh-my-pi/pi-utils/frontmatter";
const paths = await Bun.stdin.json();
const selectors = {};
for (const path of paths) {
    selectors[path] = parseFrontmatter(await Bun.file(path).text(), { level: "fatal" }).frontmatter.model;
}
process.stdout.write(JSON.stringify(selectors));
"""


def parse_agent_selectors(paths: list[str]) -> dict[str, str]:
    """Selectors as the shared Bun YAML loader reads them, independent of the doc."""
    result = subprocess.run(["bun", "-e", _AGENT_SCRIPT], cwd=_REPO, input=json.dumps(paths),
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


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
    selectors = parse_agent_selectors(paths)
    assert {row["Agent"]: row["Selector"] for row in rows} == selectors
    for row in rows:
        assert row["Status"] == "test_verified"
        assert row["Evidence"] == "packages/coding-agent/test/model-policy-governance.test.ts"


# --- review-failure qualification: every input flows through the shared qualifier ---

_SUBSTITUTIONS = _REPO / "python/omp-work/src/omp_work/routing/substitutions.v1.json"


@pytest.fixture(scope="module")
def agent_selectors() -> dict[str, str]:
    return parse_agent_selectors([r["Agent"] for r in binding_tables(_DOC.read_text())["agent-bindings"]])


def _qualify(policy, qualified_wires, agent_selectors, *, tables=None, substitutions=None):
    return discrepancies(
        policy, qualified_wires,
        tables=binding_tables(_DOC.read_text()) if tables is None else tables,
        substitutions=json.loads(_SUBSTITUTIONS.read_text()) if substitutions is None else substitutions,
        agent_selectors=agent_selectors,
    )


def test_all_inputs_qualify_cleanly_and_absent_substitutions_stay_pending(qualified_wires, agent_selectors):
    policy = load_policy()
    assert _qualify(policy, qualified_wires, agent_selectors) == []
    assert substitution_status(json.loads(_SUBSTITUTIONS.read_text()), policy) == "compared"
    assert substitution_status(None, policy) == "pending_absent"


def _mutated(tmp_path, mutate):
    raw = deepcopy(load_policy().raw)
    mutate(raw)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(raw))
    return load_policy(path)  # loads: the drift is a valid but different policy


def _other(values, current):
    return next(v for v in values if v != current)


def _stage_cases():
    policy = load_policy()
    lanes = list(policy.concurrency.providers)
    models = sorted({t.model for t in policy.tiers.values()})
    for name, stage in policy.stages.items():
        yield pytest.param(name, "provider", _other(lanes, stage.provider), "Lane", id=f"{name}-lane")
        yield pytest.param(name, "model", _other(models, stage.model), "Model", id=f"{name}-model")


@pytest.mark.parametrize(("stage", "field", "value", "column"), list(_stage_cases()))
def test_stage_route_drift_to_another_valid_lane_or_model_is_named(
    tmp_path, qualified_wires, agent_selectors, stage, field, value, column,
):
    policy = _mutated(tmp_path, lambda raw: raw["stages"][stage].__setitem__(field, value))
    assert policy.stages[stage].__getattribute__(field) == value
    found = _qualify(policy, qualified_wires, agent_selectors)
    assert f"binding_value:stage-bindings:{stage}:{column}" in found


@pytest.mark.parametrize("tier", ["E1", "E2", "E3", "E4"])
@pytest.mark.parametrize("field", ["provider", "model"])
def test_tier_drift_names_tier_and_field(tmp_path, qualified_wires, agent_selectors, tier, field):
    base = load_policy()
    pool = [getattr(t, field) for t in base.tiers.values()]
    value = _other(pool, getattr(base.tiers[tier], field))
    policy = _mutated(tmp_path, lambda raw: raw["tiers"][tier].__setitem__(field, value))
    column = "Lane" if field == "provider" else "Model"
    assert f"binding_value:tier-bindings:{tier}:{column}" in _qualify(policy, qualified_wires, agent_selectors)


@pytest.mark.parametrize(("path", "value", "column"), [
    (("from",), "kimi", "From lane"),
    (("to", "provider"), "kimi", "Lane"),
    (("to", "model"), "gpt-6-sol", "Model"),
])
def test_alternate_source_and_target_drift_is_named(tmp_path, qualified_wires, agent_selectors, path, value, column):
    def mutate(raw):
        node = raw["alternates"][0]
        for part in path[:-1]:
            node = node[part]
        node[path[-1]] = value
    policy = _mutated(tmp_path, mutate)
    found = _qualify(policy, qualified_wires, agent_selectors)
    assert f"binding_value:alternate-bindings:gemini-flash-overflow:{column}" in found
    # The substitutions file is compared against the same policy.
    assert any(f.startswith("substitution_value:overflow:gemini-flash-overflow:") for f in found)


@pytest.mark.parametrize("stage", ["audit", "acceptance_review"])
def test_reviewer_effort_suffix_drift_is_named(tmp_path, qualified_wires, agent_selectors, stage):
    def mutate(raw):
        reviewers = raw["stages"][stage]["reviewers"]
        reviewers[0] = reviewers[0].partition(":")[0] + ":high"
    policy = _mutated(tmp_path, mutate)
    found = _qualify(policy, qualified_wires, agent_selectors)
    assert f"binding_value:stage-bindings:{stage}:Reviewers" in found
    assert f"substitution_value:stages:{stage}:reviewers" in found
    assert "binding_value:role-bindings:review:Reviewers" in found


@pytest.mark.parametrize("lane", ["gemini_flash", "kimi", "openai_codex"])
def test_lane_cap_drift_is_named(tmp_path, qualified_wires, agent_selectors, lane):
    base = load_policy().concurrency.providers
    cap = _other(sorted(set(base.values())), base[lane])
    policy = _mutated(tmp_path, lambda raw: raw["concurrency"]["providers"].__setitem__(lane, cap))
    assert f"binding_value:lane-bindings:{lane}:Cap" in _qualify(policy, qualified_wires, agent_selectors)


def test_lane_record_drift_is_named(tmp_path, qualified_wires, agent_selectors):
    policy = _mutated(
        tmp_path, lambda raw: raw["governance"]["lanes"]["gemini_flash"].__setitem__("wire", OLLAMA))
    found = _qualify(policy, qualified_wires, agent_selectors)
    assert "binding_value:lane-bindings:gemini_flash:Wire" in found
    assert "unqualified_lane_wire:gemini_flash" in found


def test_agent_selector_drift_names_agent_and_selector(qualified_wires, agent_selectors):
    tables = binding_tables(_DOC.read_text())
    row = tables["agent-bindings"][0]
    row["Selector"] = "pi/" + row["Selector"]
    found = _qualify(load_policy(), qualified_wires, agent_selectors, tables=tables)
    assert f"binding_value:agent-bindings:{row['Agent']}:Selector" in found
    # Independent selector results drifting is caught the same way.
    drifted = {**agent_selectors, row["Agent"]: "other"}
    assert f"binding_value:agent-bindings:{row['Agent']}:Selector" in _qualify(
        load_policy(), qualified_wires, drifted)


def test_removed_judge_and_operator_rows_are_both_reported(qualified_wires, agent_selectors):
    tables = binding_tables(_DOC.read_text())
    tables["role-bindings"] = [r for r in tables["role-bindings"] if r["Role"] not in {"judge", "operator"}]
    found = _qualify(load_policy(), qualified_wires, agent_selectors, tables=tables)
    assert {"missing_binding:role-bindings:judge", "missing_binding:role-bindings:operator"} <= set(found)


def test_removed_judge_and_operator_roles_are_both_reported_from_policy(tmp_path, qualified_wires, agent_selectors):
    def mutate(raw):
        del raw["governance"]["roles"]["judge"], raw["governance"]["roles"]["operator"]
    found = _qualify(_mutated(tmp_path, mutate), qualified_wires, agent_selectors)
    assert {"missing_role:judge", "missing_role:operator"} <= set(found)


def test_duplicate_extra_and_missing_keys_are_distinct(qualified_wires, agent_selectors):
    tables = binding_tables(_DOC.read_text())
    tables["role-bindings"].append(dict(tables["role-bindings"][0]))
    tables["role-bindings"].append({**tables["role-bindings"][0], "Role": "auditor"})
    tables["tier-bindings"] = [r for r in tables["tier-bindings"] if r["Tier"] != "E2"]
    tables["lane-bindings"].append({**tables["lane-bindings"][0], "Lane": "phantom"})
    tables["alternate-bindings"] = []
    found = _qualify(load_policy(), qualified_wires, agent_selectors, tables=tables)
    assert {
        "duplicate_binding:role-bindings:plan", "extra_binding:role-bindings:auditor",
        "missing_binding:tier-bindings:E2", "extra_binding:lane-bindings:phantom",
        "missing_binding:alternate-bindings:gemini-flash-overflow",
    } <= set(found)


def test_agent_bindings_report_missing_duplicate_and_extra(qualified_wires, agent_selectors):
    tables = binding_tables(_DOC.read_text())
    first = tables["agent-bindings"].pop(0)
    tables["agent-bindings"].append(dict(tables["agent-bindings"][0]))
    tables["agent-bindings"].append({**tables["agent-bindings"][0], "Agent": "rogue.md"})
    found = _qualify(load_policy(), qualified_wires, agent_selectors, tables=tables)
    assert {
        f"missing_binding:agent-bindings:{first['Agent']}", "extra_binding:agent-bindings:rogue.md",
        f"duplicate_binding:agent-bindings:{tables['agent-bindings'][0]['Agent']}",
    } <= set(found)


@pytest.mark.parametrize(("mutation", "expected"), [
    ("drop_stage", "substitution_missing:stages:research"),
    ("extra_stage", "substitution_extra:stages:ghost"),
    ("dup_stage", "substitution_duplicate:stages:verify"),
    ("stage_model", "substitution_value:stages:implement:model"),
    ("stage_provider", "substitution_value:stages:plan:provider"),
    ("stage_rule", "substitution_value:stages:audit:rule"),
    ("overflow_model", "substitution_value:overflow:gemini-flash-overflow:model"),
    ("overflow_lane", "substitution_value:overflow:gemini-flash-overflow:provider"),
    ("overflow_rule", "substitution_missing:overflow:gemini-flash-overflow"),
    ("overflow_from", "substitution_value:overflow:gemini-flash-overflow:from_provider"),
])
def test_substitution_drift_names_section_key_and_field(qualified_wires, agent_selectors, mutation, expected):
    subs = json.loads(_SUBSTITUTIONS.read_text())
    stages = {s["stage"]: s for s in subs["stages"]}
    overflow = subs["overflow"][0]
    if mutation == "drop_stage":
        subs["stages"].remove(stages["research"])
    elif mutation == "extra_stage":
        subs["stages"].append({**stages["plan"], "stage": "ghost"})
    elif mutation == "dup_stage":
        subs["stages"].append(dict(stages["verify"]))
    elif mutation == "stage_model":
        stages["implement"]["model"] = "gpt-6-sol"
    elif mutation == "stage_provider":
        stages["plan"]["provider"] = "kimi"
    elif mutation == "stage_rule":
        stages["audit"]["rule"] = "acceptance_review"
    elif mutation == "overflow_model":
        overflow["model"] = "gpt-6-sol"
    elif mutation == "overflow_lane":
        overflow["provider"] = "kimi"
    elif mutation == "overflow_rule":
        overflow["rule"] = "renamed"
    elif mutation == "overflow_from":
        overflow["from_provider"] = "kimi"
    assert expected in _qualify(load_policy(), qualified_wires, agent_selectors, substitutions=subs)
    assert substitution_status(subs, load_policy()) == "mismatch"


def _forge(lanes, openai_wire, kimi_wire, openai_evidence, kimi_evidence):
    lanes["openai_codex"] = {"wire": openai_wire, "status": "test_verified",
                             "evidence": openai_evidence, "unresolved_owner": None}
    lanes["kimi"] = {"wire": kimi_wire, "status": "test_verified",
                     "evidence": kimi_evidence, "unresolved_owner": None}


@pytest.mark.parametrize(("openai_wire", "kimi_wire"), [
    ("openai-codex", "kimi-code"),  # forged: the prefixes themselves
    ("kimi-code", "openai-codex"),  # swapped
    ("openai-codex", "openai-codex"),
])
def test_forged_or_swapped_reviewer_prefix_wires_are_rejected(
    tmp_path, qualified_wires, agent_selectors, openai_wire, kimi_wire,
):
    reviewers = [r for s in load_policy().stages.values() for r in s.reviewers]
    # Every cited prefix and evidence string really exists in the policy.
    assert reviewers == ["openai-codex/gpt-5.6-sol:medium", "kimi-code/k3-256k"]
    policy = _mutated(tmp_path, lambda raw: _forge(
        raw["governance"]["lanes"], openai_wire, kimi_wire, [reviewers[0]], [reviewers[1]]))
    found = _qualify(policy, qualified_wires, agent_selectors)
    # Independent proxy config knows only gemini/ollama wires; association stays unsupported.
    assert {"unqualified_lane_wire:openai_codex", "unqualified_lane_wire:kimi",
            "unsupported_wire_association:openai_codex", "unsupported_wire_association:kimi"} <= set(found)
    assert "binding_value:lane-bindings:openai_codex:Wire" in found
    assert "binding_value:lane-bindings:kimi:Status" in found


def test_copied_metadata_cannot_stand_in_for_independent_wire_results(tmp_path, qualified_wires, agent_selectors):
    # Wire claim and "qualified" result agree only because both are copied from the policy.
    policy = _mutated(tmp_path, lambda raw: _forge(
        raw["governance"]["lanes"], "openai-codex", "kimi-code", ["openai-codex"], ["kimi-code"]))
    copied = {lane: record["wire"] for lane, record in policy["governance"]["lanes"].items() if record["wire"]}
    assert "unqualified_lane_wire:openai_codex" not in discrepancies(policy, copied)
    assert "unqualified_lane_wire:openai_codex" in discrepancies(policy, qualified_wires)
    assert set(qualified_wires) == {"gemini_flash", "ollama_cloud"}


@pytest.mark.parametrize("lane", ["anthropic_fable", "kimi", "chatgpt_astra", "openai_codex"])
def test_null_unresolved_lanes_stay_chris_owned_not_execution_proof(tmp_path, qualified_wires, agent_selectors, lane):
    policy = load_policy()
    assert policy["governance"]["lanes"][lane]["wire"] is None
    for field, value in (("status", "test_verified"), ("unresolved_owner", None)):
        mutated = _mutated(tmp_path, lambda raw: raw["governance"]["lanes"][lane].__setitem__(field, value))
        found = _qualify(mutated, qualified_wires, agent_selectors)
        assert f"pending_lane_owner:{lane}" in found

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from omp_work.routing import model_review
from omp_work.routing.model_review import (
    PolicyReviewError,
    check_policy_review,
    load_baseline,
    load_task_set,
    task_set_sha256,
)
from omp_work.routing.policy import RoutingPolicy, _validate_and_build, load_policy

REPO = Path(__file__).resolve().parents[3]
ANSWERS = {
    "plan": {"qual-plan-01": "Add qualification-tasks.v1.json, model_review.py and test_model_review_qualification.py; no runtime routing edits."},
    "review": {"qual-review-01": "Auth bypass removes PermissionError, allowing unauthorized access; restore verification."},
    "implement": {"qual-implement-01": "def discounted_price(price, percent):\n    return price * (1 - percent / 100)\n\nresults: 80, 60, 0"},
}
AXES = {axis: {"source": f"https://example.invalid/{axis}", "observed": "2026-10-14", "finding": "within threshold"}
        for axis in ("capability", "cost", "quota", "latency", "role_fit")}


def mutated(mutate) -> RoutingPolicy:
    raw = deepcopy(load_policy().raw)
    mutate(raw)
    return _validate_and_build(raw)


def failures(policy: RoutingPolicy, **kwargs) -> list[str]:
    with pytest.raises(PolicyReviewError) as info:
        check_policy_review(policy, **kwargs)
    return info.value.findings


def qualified_entry(policy: RoutingPolicy, key: str, roles: dict[str, dict]) -> dict:
    recipe = model_review.policy_recipes(policy)[0][key]
    return {
        "status": "qualified", "since": "2026-10-14",
        "qualification": {
            "date": "2026-10-14", "recipe": recipe, "task_digest": task_set_sha256(),
            "outputs": roles,
            "results": {r: {"outcome": "pass", "digest": task_set_sha256()} for r in roles},
            "evidence": deepcopy(AXES),
            "rubric": {"outcome": "pass", "reviewer": "chris", "reason": "meets thresholds"},
        },
    }


def with_recipe(mutate, key: str, roles: dict[str, dict]) -> RoutingPolicy:
    """Policy changed by ``mutate`` whose changed recipe carries a fresh qualification."""
    changed = mutated(mutate)
    entry = qualified_entry(changed, key, roles)

    def attach(raw):
        mutate(raw)
        raw["review"]["recipes"][key] = entry

    return mutated(attach)


def test_packaged_baseline_passes_and_pending_bindings_are_not_verified():
    summary = check_policy_review(load_policy(), repo_root=REPO)
    assert summary["qualified"] == []
    assert "binding:lane:kimi" in summary["pending"] and "binding:role:review" in summary["pending"]
    assert "binding:lane:kimi" not in summary["baseline"]
    assert {"binding:lane:gemini_flash", "binding:lane:ollama_cloud", "binding:role:implement"} <= set(summary["baseline"])
    assert {"reviewer:audit:0", "reviewer:acceptance_review:0"} <= set(summary["baseline"])
    review = load_policy().raw["review"]
    assert review["last_review"]["outcome"] == "baseline_retained"
    assert review["task_set_digest"] == task_set_sha256(load_task_set())


def test_new_stage_pair_marked_baseline_fails_even_with_since():
    def mutate(raw):
        raw["stages"]["triage"] = {**raw["stages"]["verify"], "rule": "triage"}
        raw["review"]["recipes"]["stage:triage"] = {"status": "baseline", "since": "2026-10-14"}

    assert "baseline_recipe_mismatch:stage:triage" in failures(mutated(mutate))


@pytest.mark.parametrize(("mutate", "code"), [
    (lambda r: r["stages"]["audit"]["reviewers"].append("kimi-code/k3-256k:high"), "unreviewed_recipe:reviewer:audit:1"),
    (lambda r: r["stages"]["audit"]["reviewers"].__setitem__(0, "openai-codex/gpt-5.6-sol:high"),
     "baseline_recipe_mismatch:reviewer:audit:0"),
    (lambda r: r["stages"]["audit"]["reviewers"].__setitem__(0, "openai-codex/gpt-5.6-sol"),
     "baseline_recipe_mismatch:reviewer:audit:0"),
    (lambda r: r["stages"]["acceptance_review"]["reviewers"].__setitem__(0, "other-wire/k3-256k"),
     "baseline_recipe_mismatch:reviewer:acceptance_review:0"),
    (lambda r: r["stages"]["plan"].__setitem__("effort", "E3"), "baseline_recipe_mismatch:stage:plan"),
    (lambda r: r["stages"]["implement"].__setitem__("model", "gemini-3.9-flash"),
     "baseline_recipe_mismatch:stage:implement"),
    (lambda r: r["tiers"]["E2"].__setitem__("provider", "kimi"), "baseline_recipe_mismatch:tier:E2"),
    (lambda r: r["alternates"][0]["to"].__setitem__("model", "other:cloud"),
     "baseline_recipe_mismatch:alternate:gemini-flash-overflow"),
    (lambda r: r["governance"]["roles"]["review"]["selectors"].pop(2),
     "baseline_recipe_mismatch:reviewer:audit:0"),
    (lambda r: r["governance"]["roles"]["plan"]["selectors"].clear(), "baseline_recipe_mismatch:stage:plan"),
])
def test_changed_or_added_recipe_needs_qualification(mutate, code):
    assert code in failures(mutated(mutate))


def test_resolving_pending_wire_without_evidence_is_refused():
    def mutate(raw):
        lane = raw["governance"]["lanes"]["kimi"]
        lane["wire"], lane["status"] = "kimi-code", "test_verified"

    found = failures(mutated(mutate))
    assert "resolved_but_marked_pending:binding:lane:kimi" in found


def test_pending_binding_cannot_be_marked_verified():
    def mutate(raw):
        raw["review"]["recipes"]["binding:lane:kimi"] = {"status": "baseline", "since": "2026-10-14"}

    assert "pending_counted_verified:binding:lane:kimi" in failures(mutated(mutate))


def test_tampered_baseline_snapshot_fails_pinned_digest():
    baseline = deepcopy(load_baseline())
    baseline["recipes"]["stage:plan"]["model"] = "gpt-9"
    assert "baseline_snapshot_tampered" in failures(load_policy(), baseline=baseline)
    baseline = deepcopy(load_baseline())
    baseline["pending"].remove("binding:lane:kimi")
    assert "baseline_snapshot_tampered" in failures(load_policy(), baseline=baseline)


def test_stale_digest_and_missing_review_metadata_fail():
    assert "stale_task_set_digest" in failures(mutated(lambda r: r["review"].__setitem__("task_set_digest", "0" * 64)))
    assert "last_review_missing" in failures(mutated(lambda r: r["review"].pop("last_review")))
    assert "schedule_missing:daily_check" in failures(mutated(lambda r: r["review"]["schedule"].pop("daily_check")))
    assert "rubric_path" in failures(mutated(lambda r: r["review"].__setitem__("rubric", "docs/models.md")))
    assert "rubric_missing" in failures(load_policy(), repo_root=Path("/nonexistent-root"))
    assert failures(mutated(lambda r: r.pop("review"))) == ["missing_review_metadata"]


def new_reviewer(raw):
    raw["stages"]["audit"]["reviewers"].append("kimi-code/k3-256k:high")
    raw["governance"]["roles"]["review"]["selectors"].append({"stage": "audit", "reviewer_index": 1})


def test_valid_complete_qualification_passes_for_reviewer_only_offering():
    policy = with_recipe(new_reviewer, "reviewer:audit:1", {"review": ANSWERS["review"]})
    summary = check_policy_review(policy)
    assert summary["qualified"] == ["reviewer:audit:1"]


def test_valid_qualification_passes_for_changed_stage_model():
    def change(raw):
        raw["stages"]["implement"]["model"] = "gemini-3.9-flash"

    policy = with_recipe(change, "stage:implement", {"implement": ANSWERS["implement"]})
    assert check_policy_review(policy)["qualified"] == ["stage:implement"]


def broken(edit):
    def build():
        base = mutated(new_reviewer)
        entry = qualified_entry(base, "reviewer:audit:1", {"review": ANSWERS["review"]})
        edit(entry["qualification"])

        def attach(raw):
            new_reviewer(raw)
            raw["review"]["recipes"]["reviewer:audit:1"] = entry

        return mutated(attach)

    return build()


@pytest.mark.parametrize(("edit", "code"), [
    (lambda q: q["outputs"]["review"].__setitem__("qual-review-01", "looks fine"),
     "qualification_outputs_fail:reviewer:audit:1:review"),
    (lambda q: q["outputs"]["review"].__setitem__("qual-review-99", "extra"),
     "qualification_outputs_fail:reviewer:audit:1:review"),
    (lambda q: q["outputs"].clear(), "missing_role_outputs:reviewer:audit:1:review"),
    (lambda q: q["outputs"].__setitem__("review", {}), "qualification_outputs_fail:reviewer:audit:1:review"),
    (lambda q: q["outputs"].__setitem__("plan", ANSWERS["plan"]) or q["outputs"].pop("review"),
     "missing_role_outputs:reviewer:audit:1:review"),
    (lambda q: q["evidence"].pop("quota"), "missing_rubric_evidence:reviewer:audit:1:quota"),
    (lambda q: q["evidence"]["latency"].pop("source"), "missing_rubric_evidence:reviewer:audit:1:latency"),
    (lambda q: q["evidence"]["cost"].__setitem__("observed", "yesterday"),
     "missing_rubric_evidence:reviewer:audit:1:cost"),
    (lambda q: q.pop("rubric"), "missing_rubric_verdict:reviewer:audit:1"),
    (lambda q: q.__setitem__("task_digest", "0" * 64), "qualification_stale_digest:reviewer:audit:1"),
    (lambda q: q["recipe"].__setitem__("identity", "kimi-code/k3-256k:low"),
     "qualification_recipe_mismatch:reviewer:audit:1"),
    (lambda q: q.__setitem__("date", "2026-10-15"), "qualification_after_last_review:reviewer:audit:1"),
    (lambda q: q.__setitem__("date", "soon"), "qualification_date:reviewer:audit:1"),
])
def test_incomplete_or_wrong_qualification_fails(edit, code):
    assert code in failures(broken(edit))


def test_forged_pass_with_wrong_outputs_fails_even_when_result_claims_pass():
    policy = broken(lambda q: q["outputs"]["review"].__setitem__("qual-review-01", "looks fine"))
    found = failures(policy)
    assert "forged_result:reviewer:audit:1:review" in found
    assert "qualification_outputs_fail:reviewer:audit:1:review" in found


def test_result_pass_without_regraded_digest_is_forged():
    policy = broken(lambda q: q["results"]["review"].__setitem__("digest", "0" * 64))
    assert "forged_result:reviewer:audit:1:review" in failures(policy)


def test_qualification_for_one_role_does_not_cover_another():
    def attach(raw):
        raw["stages"]["triage"] = {**raw["stages"]["verify"], "rule": "triage"}
        raw["governance"]["roles"]["verify"]["selectors"].append({"stage": "triage"})

    changed = mutated(attach)
    entry = qualified_entry(changed, "stage:triage", {"plan": ANSWERS["plan"]})

    def final(raw):
        attach(raw)
        raw["review"]["recipes"]["stage:triage"] = entry

    assert "missing_role_outputs:stage:triage:verify" in failures(mutated(final))

"""OMP-615-s09-s02: the daily offerings check reports evidence, never infers from gaps.

Failure mode defended: a retired, deprecated or failing in-use model goes
unreported (or a harmless new listing is flagged urgent), a missing/stale/empty
capture is read as "all clear" or as a retirement, an effort suffix splits a
``:cloud`` id, or the retained snapshot loses the last successful listing so a
later recovery compares against nothing. Captures are built through the frozen
parsers; the policy is the packaged routing policy.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest

from omp_work.routing.offerings_check import compare
from omp_work.routing.offerings_inputs import (
    OfferingsInputError,
    SourceGap,
    parse_listing,
    parse_notices,
    parse_sources,
)
from omp_work.routing.policy import load_policy

NOW = datetime(2026, 10, 15, 0, 0, tzinfo=timezone.utc)
FRESH = "2026-10-14T12:00:00+00:00"
FRESH_MS = int(datetime.fromisoformat(FRESH).timestamp() * 1000)

SOURCES = parse_sources(json.dumps({
    "version": "offerings-sources.v1",
    "notices": "notices.json",
    "lanes": {
        "gemini_flash": {"kind": "omp-cli", "catalog_provider": "gemini_flash"},
        "openai-codex": {"kind": "omp-cli", "catalog_provider": "openai-codex"},
        "ollama_cloud": {"kind": "listing-file", "catalog_provider": "ollama_cloud", "path": "ollama.json"},
        "kimi-code": {"kind": "listing-file", "catalog_provider": "kimi-code", "path": "kimi.json"},
    },
}))

IN_USE = {
    "gemini_flash": ["gemini-3.8-flash-high"],
    "openai-codex": ["gpt-5.6-sol"],
    "ollama_cloud": ["deepseek-v4.1-flash:cloud"],
    "kimi-code": ["k3-256k"],
}


def row(provider, model_id, **over):
    base = {
        "provider": provider, "id": model_id, "name": model_id, "contextWindow": 1000, "maxTokens": 100,
        "reasoning": True, "thinking": ["low"], "input": ["text"], "cost": {"input": 1, "output": 2},
    }
    return {**base, **over}


def listing(lane_name, ids, *, time=FRESH, cost=None, extra=()):
    lane = SOURCES.lanes[lane_name]
    over = {} if cost is None else {"cost": cost}
    models = [row(lane.catalog_provider, i, **over) for i in [*ids, *extra]]
    doc = {"models": models}
    if lane.kind == "omp-cli":
        doc["discovery"] = {lane.catalog_provider: {
            "provider": lane.catalog_provider, "status": "ok", "stale": False, "source": "provider",
            "fetchedAt": int(datetime.fromisoformat(time).timestamp() * 1000)}}
    else:
        doc["retrieval"] = {"provider": lane.catalog_provider, "source": "https://x", "retrieved_at": time,
                            "success": True}
    return parse_listing(json.dumps(doc), lane)


def all_listings(**changes):
    out = {name: listing(name, ids) for name, ids in IN_USE.items()}
    out.update(changes)
    return out


def dim(kind, retrieved=FRESH, **body):
    return {"coverage": "checked", "source": f"https://{kind}", "retrieved_at": retrieved, **body}


def lane_notices(items=(), status="operational", retrieved=FRESH):
    return {"notices": dim("n", retrieved, items=list(items)), "status": dim("s", retrieved, value=status)}


def notices(**per_lane):
    providers = {name: lane_notices() for name in IN_USE}
    providers.update(per_lane)
    return parse_notices(json.dumps({"version": "offerings-notices.v1", "providers": providers}))


def item(model, kind="deprecation", effective="2026-12-01"):
    return {"model": model, "type": kind, "url": "https://n/x", "published": "2026-10-01", "effective": effective}


_DEFAULT = object()


def run(listings=None, notes=_DEFAULT, previous=None, **kw):
    return compare(load_policy(), SOURCES, listings if listings is not None else all_listings(),
                   notices() if notes is _DEFAULT else notes, previous, NOW, **kw)


def of(result, kind, **match):
    return [f for f in result.findings if f.kind == kind
            and all(getattr(f, k) == v for k, v in match.items())]


def test_clean_first_run_reports_only_unmapped_recipes_and_baselines_snapshot():
    result = run()
    assert {f.kind for f in result.findings} == {"unchecked"}
    assert all(f.lane is None and f.in_use for f in result.findings)
    assert {r for f in result.findings for r in f.recipes} >= {"stage:plan", "tier:E2", "stage:audit"}
    assert set(result.snapshot) == set(IN_USE)
    entry = result.snapshot["openai-codex"]
    assert entry["catalog_provider"] == "openai-codex" and "gpt-5.6-sol" in entry["models"]
    assert entry["evidence"]["raw_sha256"] and entry["evidence"]["time"] == "2026-10-14T12:00:00Z"


def test_reviewer_prefix_resolves_via_catalog_provider_for_both_kinds_and_splits_effort():
    # openai-codex is an omp-cli lane, kimi-code a listing-file lane; both prefixes resolve.
    present = run()
    assert not of(present, "retired")
    gone = run(all_listings(**{
        "openai-codex": listing("openai-codex", ["other"]),
        "kimi-code": listing("kimi-code", ["other"]),
    }))
    codex = of(gone, "retired", lane="openai-codex", model="gpt-5.6-sol")
    assert [(f.effort, f.recipes, f.urgent, f.in_use) for f in codex] == [
        ("medium", ("reviewer:audit:0",), True, True)]
    kimi = of(gone, "retired", lane="kimi-code", model="k3-256k")
    assert kimi and all(f.urgent and f.effort is None for f in kimi)
    assert kimi[0].evidence["source"] == "kimi.json" and kimi[0].evidence["raw_sha256"]


def test_complete_id_with_colon_wins_over_effort_split():
    listings = all_listings(**{
        "openai-codex": listing("openai-codex", ["gpt-5.6-sol:medium"]),
        "ollama_cloud": listing("ollama_cloud", ["deepseek-v4.1-flash"]),
    })
    result = run(listings)
    # reviewer selector is itself a listed id: no split, nothing retired for it
    assert not of(result, "retired", lane="openai-codex")
    # ":cloud" is part of the stage/alternate id and is absent here: retired under its full id
    cloud = of(result, "retired", lane="ollama_cloud")
    assert {f.model for f in cloud} == {"deepseek-v4.1-flash:cloud"} and all(f.effort is None for f in cloud)
    assert set(cloud[0].recipes) == {"alternate:gemini-flash-overflow", "stage:research"}


def test_notices_make_in_use_models_urgent_and_unused_ones_not():
    notes = notices(
        gemini_flash=lane_notices([
            item("gemini-3.8-flash-high", "retirement", "2027-06-01"),  # future retirement: urgent now
            item("gemini-1.0", "retirement", "2026-11-01"),
        ]),
        **{
            "openai-codex": lane_notices([item("gpt-5.6-sol", "deprecation")]),
            "ollama_cloud": lane_notices([item("deepseek-v4.1-flash:cloud", "failure", None)]),
        },
    )
    result = run(notes=notes)
    (retiring,) = of(result, "retired", lane="gemini_flash", model="gemini-3.8-flash-high")
    assert retiring.urgent and retiring.in_use and "2027-06-01" in retiring.evidence["detail"]
    assert retiring.evidence["source"] == "https://n" and retiring.evidence["time"] == "2026-10-14T12:00:00Z"
    assert retiring.evidence["raw_sha256"] is None and retiring.evidence["cause"]
    assert retiring.evidence["listing_sha256"]
    (unused,) = of(result, "retired", model="gemini-1.0")
    assert not unused.in_use and not unused.urgent
    (dep,) = of(result, "deprecated", lane="openai-codex")
    assert dep.urgent and dep.effort == "medium"
    assert all(f.urgent for f in of(result, "failing", lane="ollama_cloud"))


def test_degraded_or_outage_status_flags_every_in_use_model_in_lane():
    result = run(notes=notices(ollama_cloud=lane_notices(status="outage")))
    failing = of(result, "failing", lane="ollama_cloud")
    assert {f.model for f in failing} == {"deepseek-v4.1-flash:cloud"}
    assert all(f.urgent and "outage" in f.evidence["detail"] for f in failing)
    assert not of(result, "failing", lane="gemini_flash")


def test_new_listing_is_harmless_and_first_run_flags_only_unused():
    first = run(all_listings(gemini_flash=listing("gemini_flash", IN_USE["gemini_flash"], extra=["brand-new"])))
    (fresh,) = of(first, "new")
    assert (fresh.model, fresh.in_use, fresh.urgent, fresh.recipes) == ("brand-new", False, False, ())
    prior = run().snapshot
    second = run(all_listings(
        gemini_flash=listing("gemini_flash", IN_USE["gemini_flash"], extra=["brand-new"])), previous=prior)
    (new,) = of(second, "new")
    assert not new.urgent and new.model == "brand-new"
    # an in-use model first seen after a prior baseline is new, still not urgent
    prior_missing = deepcopy(prior)
    del prior_missing["gemini_flash"]["models"]["gemini-3.8-flash-high"]
    third = run(previous=prior_missing)
    (inuse,) = of(third, "new", model="gemini-3.8-flash-high")
    assert inuse.in_use and not inuse.urgent and inuse.recipes


def test_changed_and_prior_retired_models_against_previous_snapshot():
    prior = run(all_listings(gemini_flash=listing("gemini_flash", [*IN_USE["gemini_flash"], "side"]))).snapshot
    result = run(all_listings(gemini_flash=listing("gemini_flash", IN_USE["gemini_flash"],
                                                    cost={"input": 5, "output": 2})), previous=prior)
    (changed,) = of(result, "changed", model="gemini-3.8-flash-high")
    assert changed.in_use and not changed.urgent and "cost" in changed.evidence["detail"]
    (gone,) = of(result, "retired", model="side")
    assert not gone.in_use and not gone.urgent


def test_provider_change_starts_baseline_without_changed_or_retired():
    prior = run().snapshot
    prior["gemini_flash"]["catalog_provider"] = "other_provider"
    prior["gemini_flash"]["models"] = {"old-model": prior["gemini_flash"]["models"]["gemini-3.8-flash-high"]}
    result = run(previous=prior)
    assert not of(result, "changed") and not of(result, "retired") and not of(result, "new")
    assert result.snapshot["gemini_flash"]["catalog_provider"] == "gemini_flash"


@pytest.mark.parametrize("label,make_notes,needle", [
    ("file", lambda: None, "file missing"),
    ("lane", lambda: parse_notices(json.dumps({"version": "offerings-notices.v1", "providers": {
        n: lane_notices() for n in IN_USE if n != "kimi-code"}})), "lane absent"),
    ("dimension", lambda: notices(**{"kimi-code": {"notices": dim("n", items=[])}}), "status dimension absent"),
    ("stale", lambda: notices(**{"kimi-code": lane_notices(retrieved="2026-10-01T00:00:00+00:00")}), "stale"),
    ("future", lambda: notices(**{"kimi-code": lane_notices(retrieved="2026-10-16T00:00:00+00:00")}), "future-dated"),
])
def test_each_notices_coverage_gap_is_unchecked_per_in_use_lane(label, make_notes, needle):
    result = run(notes=make_notes())
    gaps = [f for f in of(result, "unchecked") if f.lane == "kimi-code"]
    assert gaps and all(needle in f.evidence["detail"] and f.in_use and not f.urgent for f in gaps)
    assert not of(result, "retired") and not of(result, "failing")
    if label != "file":
        assert not [f for f in of(result, "unchecked") if f.lane == "gemini_flash"]


def test_not_published_is_covered_not_silence():
    published = {"coverage": "not_published", "source": "https://s", "retrieved_at": FRESH}
    result = run(notes=notices(**{"kimi-code": {"notices": published, "status": published}}))
    assert not [f for f in of(result, "unchecked") if f.lane == "kimi-code"]


@pytest.mark.parametrize("label,make", [
    ("missing", lambda: None),
    ("failed", lambda: OfferingsInputError("models", "boom")),
    ("unproven", lambda: SourceGap("kimi-code", "kimi-code", "no file retrieval proof")),
    ("empty", lambda: listing("kimi-code", [])),
    ("stale", lambda: listing("kimi-code", IN_USE["kimi-code"], time="2026-10-10T00:00:00+00:00")),
    ("future", lambda: listing("kimi-code", IN_USE["kimi-code"], time="2026-10-16T00:00:00+00:00")),
])
def test_bad_listing_is_unchecked_keeps_last_success_and_infers_no_retirement(label, make):
    prior = run().snapshot
    listings = all_listings()
    listings["kimi-code"] = make()
    if label == "missing":
        del listings["kimi-code"]
    result = run(listings, previous=prior)
    (gap,) = [f for f in of(result, "unchecked") if f.lane == "kimi-code" and f.model is None
              and "bytes" not in f.evidence["detail"]]
    assert gap.in_use and not gap.urgent and gap.evidence["cause"]
    assert not of(result, "retired", lane="kimi-code")
    assert result.snapshot["kimi-code"] == prior["kimi-code"]
    assert result.snapshot["gemini_flash"] == prior["gemini_flash"]


def test_success_then_gap_then_recovery_compares_against_last_success():
    day1 = run().snapshot
    day2 = run(all_listings(**{"kimi-code": listing("kimi-code", ["k3-256k"], time="2026-10-01T00:00:00+00:00")}),
               previous=day1)
    assert day2.snapshot["kimi-code"] == day1["kimi-code"]
    day3 = run(all_listings(**{"kimi-code": listing("kimi-code", ["k3-512k"], cost={"input": 9, "output": 9})}),
               previous=day2.snapshot)
    assert of(day3, "retired", lane="kimi-code", model="k3-256k")[0].urgent
    assert of(day3, "new", lane="kimi-code", model="k3-512k")
    day4 = run(all_listings(**{"kimi-code": listing("kimi-code", ["k3-256k"], cost={"input": 9, "output": 9})}),
               previous=day2.snapshot)
    (changed,) = of(day4, "changed", lane="kimi-code")
    assert "cost" in changed.evidence["detail"] and changed.in_use
    assert day4.snapshot["kimi-code"]["models"]["k3-256k"]["cost"] == {"input": 9, "output": 9}


def test_check_never_edits_routing_policy():
    policy = load_policy()
    before = deepcopy(policy.raw)
    compare(policy, SOURCES, all_listings(), notices(), None, NOW)
    assert policy.raw == before

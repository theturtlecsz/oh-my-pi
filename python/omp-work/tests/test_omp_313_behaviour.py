"""OMP-313 outcome behaviour spec: three behaviours, verbatim fence text, nothing satisfied."""
import json
import re
from pathlib import Path

import pytest

DOC = Path(__file__).resolve().parents[3] / "docs" / "programme" / "OMP-313-BEHAVIOUR.md"


def _spec():
    fences = re.findall(r"```json\n(.*?)\n```", DOC.read_text(encoding="utf-8"), re.S)
    assert len(fences) == 1
    return json.loads(fences[0])


def test_header_and_ids():
    s = _spec()
    assert s["item"] == "OMP-313"
    assert s["satisfied"] is False
    assert [b["id"] for b in s["behaviours"]] == [
        "adaptivity-vs-baseline", "held-out-transfer", "measured-improvement"]


def test_behaviour_fields_verbatim():
    expected = {
        "adaptivity-vs-baseline": (
            "Compare B3 with B2 on a matched budget.",
            "B3 changes its next selection when a trusted observation changes, on B2's budget.",
            "Untrusted score or equivalent repeat.",
            "Matched B2 and B3 arms are missing."),
        "held-out-transfer": (
            "confirmation outside development.",
            "Every outside-development confirmation delta is positive and the content is disjoint.",
            "Not disjoint, or an outside delta is non-positive.",
            "No outside-development delta is recorded."),
        "measured-improvement": (
            "unknown cost leaves the interval open.",
            "The interval lies entirely above zero and every cost axis is known.",
            "Untrusted, repeated, unknown-as-zero, or below zero.",
            "Zero is inside the interval, a bound or cost is unknown, or drift has no fresh window."),
    }
    for b in _spec()["behaviours"]:
        assert set(b) == {"id", "observation", "pass", "fail", "inconclusive", "rejects"}
        obs, pas, fail, inc = expected[b["id"]]
        assert (b["observation"], b["pass"], b["fail"], b["inconclusive"]) == (obs, pas, fail, inc)
        assert set(b["rejects"]) == {"code_presence", "fixture_only", "self_score", "unknown_as_zero"}
        assert len(b["rejects"]) == 4


def test_rules():
    s = _spec()
    assert s["rules_text"] == [
        "Unknown safety or authority is not permission.",
        "Noncritical uncertainty is a bounded investigation with the assumption recorded.",
        "Only a consequential unresolved decision goes to Chris.",
        "A null or negative comparison is not measured improvement.",
        "Five seeds are not programme qualification.",
        "No useful benefit leaves the capability off and offers Chris revise, defer, or retire.",
        "Technical delivery at merge is not product acceptance.",
    ]
    assert s["rules"] == {
        "q1_unknown_authority_is_permission": False,
        "q2_null_is_improvement": False,
        "q2_five_seeds_are_programme_qualification": False,
        "q3_merge_is_product_acceptance": False,
    }


def test_packages():
    assert _spec()["packages"] == {
        "prerequisite": ["R00", "R01", "R02", "R03", "R04", "R05", "R06", "R07", "R08", "R09",
                         "R12", "R14", "R15", "R16", "R17"],
        "adaptivity": ["R10", "R11", "R13"],
        "improvement": ["R18"],
        "all_three": ["R19"],
    }


def _oracle_text():
    text = DOC.read_text(encoding="utf-8")
    index = text.find("\n## Oracle\n")
    assert index != -1
    return text[index:]


def test_oracle_states_the_decision_procedure():
    oracle = _oracle_text()
    for token in ("arm_b2", "outside", "equivalent_repeat", "unknown_as_zero", "costs_known"):
        assert token in oracle
    assert all(word in oracle for word in ("Adaptivity", "Transfer", "Improvement"))


def test_oracle_programme_thresholds():
    oracle = _oracle_text()
    for threshold in ("20", "72", "30", "100"):
        assert threshold in oracle


def classify(record):
    """Apply the documented Oracle to an observation, without runtime services."""
    trusted = record.get("trusted", True)
    repeated = record.get("equivalent_repeat", False)
    if not trusted or repeated:
        adaptivity = "fail"
    elif all(record.get(key, False) for key in (
        "arm_b2", "arm_b3", "same_budget", "observation_changes_next"
    )):
        adaptivity = "pass"
    else:
        adaptivity = "inconclusive"

    outside = [delta for domain, delta in record.get("deltas", {}).items()
               if domain not in record.get("development", [])]
    if not record.get("disjoint", True) or any(delta <= 0 for delta in outside):
        transfer = "fail"
    elif outside:
        transfer = "pass"
    else:
        transfer = "inconclusive"

    costs_known = record.get("costs_known", True)
    lo, hi = record.get("lo"), record.get("hi")
    if (not trusted or repeated or record.get("unknown_as_zero", False)
            or (costs_known and hi is not None and hi < 0)):
        improvement = "fail"
    elif ((record.get("drift", False) and not record.get("new_window", True))
          or not costs_known or lo is None or hi is None or lo <= 0 <= hi):
        improvement = "inconclusive"
    elif lo > 0:
        improvement = "pass"
    else:
        improvement = "inconclusive"

    decisions = [adaptivity, transfer, improvement]
    if any(record.get(key, False) for key in ("code_presence", "fixture_only", "self_score")):
        decisions = ["fail", "fail", "fail"]
    programme = all(record.get(key, 0) >= threshold for key, threshold in (
        ("seeds", 20), ("hours", 72), ("items", 30), ("attempts", 100)
    ))
    return decisions, programme


EXAMPLE_DECISIONS = {
    "matched-gain": ["pass", "pass", "pass"],
    "null-interval": ["pass", "pass", "inconclusive"],
    "transfer-loss": ["pass", "fail", "fail"],
    "unknown-cost": ["pass", "pass", "inconclusive"],
    "code-presence": ["fail", "fail", "fail"],
    "untrusted": ["fail", "pass", "fail"],
    "cycle": ["fail", "pass", "fail"],
    "drift": ["pass", "pass", "inconclusive"],
    "same-domain": ["pass", "fail", "pass"],
    "unknown-as-zero": ["pass", "pass", "fail"],
    "self-score": ["fail", "fail", "fail"],
}


def test_examples_cover_the_eleven_comparisons():
    assert [example["id"] for example in _spec()["examples"]] == list(EXAMPLE_DECISIONS)


@pytest.mark.parametrize("example", _spec()["examples"], ids=lambda example: example["id"])
def test_examples_classify_outcomes_not_presence(example):
    expected = EXAMPLE_DECISIONS[example["id"]]
    assert example["decisions"] == expected
    assert example["programme"] is False
    assert classify(example["record"]) == (expected, False)


@pytest.mark.parametrize("seeds, qualified", [(19, False), (20, True)])
def test_programme_requires_twenty_seeds_at_matched_thresholds(seeds, qualified):
    _, programme = classify({"seeds": seeds, "hours": 72, "items": 30, "attempts": 100})
    assert programme is qualified

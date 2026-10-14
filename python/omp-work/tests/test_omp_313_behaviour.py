"""OMP-313 outcome behaviour spec: three behaviours, verbatim fence text, nothing satisfied."""
import json
import re
from pathlib import Path

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

"""OMP-582 outcome behaviour spec: R19 states three behaviours, rest verbatim from OMP-313, nothing satisfied."""
import importlib.util
import json
import re
from pathlib import Path

import pytest

PROGRAMME = Path(__file__).resolve().parents[3] / "docs" / "programme"
DOC = PROGRAMME / "OMP-582-BEHAVIOUR.md"
BASE = PROGRAMME / "OMP-313-BEHAVIOUR.md"


def _load(path):
    fences = re.findall(r"```json\n(.*?)\n```", path.read_text(encoding="utf-8"), re.S)
    assert len(fences) == 1
    return json.loads(fences[0])


def _oracle(path):
    text = path.read_text(encoding="utf-8")
    index = text.find("\n## Oracle\n")
    assert index != -1
    return text[index:]


_spec_313 = importlib.util.spec_from_file_location(
    "omp_313_behaviour_oracle", Path(__file__).with_name("test_omp_313_behaviour.py"))
_mod_313 = importlib.util.module_from_spec(_spec_313)
_spec_313.loader.exec_module(_mod_313)
classify = _mod_313.classify

SPEC = _load(DOC)
BASE_SPEC = _load(BASE)

TOKENS = {
    "adaptivity-vs-baseline": ("B2", "B3", "matched", "equivalent"),
    "held-out-transfer": ("development", "confirmation", "domain"),
    "measured-improvement": ("interval", "unknown", "maintenance", "drift"),
}


def test_header_and_unsatisfied():
    assert SPEC["item"] == "OMP-582"
    assert SPEC["satisfied"] is False
    assert "packages" not in SPEC
    assert [b["id"] for b in SPEC["behaviours"]] == list(TOKENS)


def test_observations_carry_the_behaviour_tokens():
    for b in SPEC["behaviours"]:
        for token in TOKENS[b["id"]]:
            assert token in b["observation"]


def test_everything_but_observations_is_verbatim():
    for mine, base in zip(SPEC["behaviours"], BASE_SPEC["behaviours"], strict=True):
        assert mine["id"] == base["id"]
        for key in ("pass", "fail", "inconclusive", "rejects"):
            assert mine[key] == base[key]
        assert mine["observation"] != base["observation"]
    assert SPEC["rules_text"] == BASE_SPEC["rules_text"]
    assert SPEC["rules"] == BASE_SPEC["rules"]
    assert SPEC["examples"] == BASE_SPEC["examples"]
    assert _oracle(DOC) == _oracle(BASE)


@pytest.mark.parametrize("example", SPEC["examples"], ids=lambda example: example["id"])
def test_examples_match_the_oracle(example):
    assert classify(example["record"]) == (example["decisions"], False)


@pytest.mark.parametrize("seeds, qualified", [(19, False), (20, True)])
def test_programme_thresholds(seeds, qualified):
    _, programme = classify({"seeds": seeds, "hours": 72, "items": 30, "attempts": 100})
    assert programme is qualified

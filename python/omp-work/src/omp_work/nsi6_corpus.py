"""Versioned NSI-6 replay data; labels never enter evaluator inputs.

Freeze/verify bind raw input, source snapshot, mutations and adjudications.
Validation checks corpus integrity only, never computes readiness.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from omp_work.frozen_corpus import FrozenCorpus, freeze, verify

REQUIRED_CLASSES = frozenset({
    "historical", "positive_ready", "omitted_rollback", "omitted_security",
    "omitted_failure", "contradiction", "ambiguous_scope", "stale_source",
    "stale_revision", "provenance_tampering", "prompt_injection", "paraphrase",
})
FILES = [("nsi6-cases-v1", "cases.json"),
         ("bounded-intake-history", "bounded_intake_golden.json")]


def validate_cases(directory: str | Path) -> dict:
    """Reject incomplete or internally inconsistent adjudicated replay data."""
    root = Path(directory)
    document = json.loads((root / "cases.json").read_bytes())
    if document["version"] != 1:
        raise ValueError("unsupported corpus version")
    rows = document["cases"]
    by_id = {row["id"]: row for row in rows}
    if len(by_id) != len(rows):
        raise ValueError("duplicate case ids")
    classes = {label for row in rows for label in row["classes"]}
    if missing := REQUIRED_CLASSES - classes:
        raise ValueError(f"missing classes: {sorted(missing)}")
    snapshot = (root / "bounded_intake_golden.json").read_bytes()
    history = {row["id"]: row for row in json.loads(snapshot)}
    if hashlib.sha256(snapshot).hexdigest() != document["history"]["sha256"]:
        raise ValueError("historical source byte drift")
    pairs: dict[str, list[dict]] = {}
    for row in rows:
        gold = row["gold"]
        if set(row["input"]) != {"bounded", "nsi2_proof_scenario"}:
            raise ValueError("evaluator input must be separate from gold")
        if gold["readiness"] not in {"READY_FOR_RATIFICATION", "NEEDS_SPEC", "CONFLICTED"}:
            raise ValueError("invalid gold readiness")
        criteria = {item["id"] for item in gold["corrected_draft"]["acceptance_criteria"]}
        if set(gold["criterion_ids"]) != criteria:
            raise ValueError("gold criteria do not match corrected draft")
        if not gold["critical_required_behaviors"] or not row["adjudication"]["rationale"]:
            raise ValueError("missing independent adjudication")
        origin = row["origin"]
        if origin["kind"] == "historical":
            original = history[origin["case_id"]]
            if (row["input"]["bounded"] != original["input"]
                    or origin["commit"] != document["history"]["commit"]
                    or origin["path"] != document["history"]["path"]
                    or origin["adjudication"] != original["adjudication"]):
                raise ValueError("historical attribution/input drift")
        if origin.get("base_case_id"):
            replay = deepcopy(by_id[origin["base_case_id"]]["input"])
            for mutation in origin["mutations"]:
                parent = replay
                for key in mutation["path"][:-1]:
                    parent = parent[key]
                key = mutation["path"][-1]
                if parent[key] != mutation["before"]:
                    raise ValueError("mutation preimage drift")
                parent[key] = deepcopy(mutation["after"])
            if replay != row["input"]:
                raise ValueError("mutation replay drift")
        if group := row["paraphrase_group"]:
            pairs.setdefault(group, []).append(row)
    for pair in pairs.values():
        semantics = [
            {key: row["gold"][key] for key in (
                "readiness", "critical_required_behaviors", "criterion_ids", "material_question_targets"
            )} for row in pair
        ]
        if len(pair) != 2 or semantics[0] != semantics[1]:
            raise ValueError("paraphrase gold semantics drift")
        if pair[0]["input"] == pair[1]["input"]:
            raise ValueError("paraphrases must differ")
    return document


def freeze_corpus(directory: str | Path, manifest: str | Path) -> FrozenCorpus:
    document = validate_cases(directory)
    return freeze(directory, FILES, document["history"]["commit"], manifest)


def load_corpus(directory: str | Path, manifest: str | Path) -> dict:
    verify(directory, manifest)
    return validate_cases(directory)


def evaluator_input(row: dict) -> dict:
    """A replay arm receives raw bounded input and proof recipe, never labels."""
    return deepcopy(row["input"])

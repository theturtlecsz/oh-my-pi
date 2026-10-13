"""Offline, label-blind NSI-6 shadow replay. No model or ledger clients."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import time
from typing import Callable

from pydantic import ValidationError

from omp_work.contracts.nsi.v1.compat import bounded_to_nsi, intake_contract, nsi_bounded_evaluation
from omp_work.frozen_corpus import verify
from omp_work.nsi6_corpus import evaluator_input, load_corpus
from omp_work.nsi_eval_postgres import disposable_provenance, implementation_hashes
from omp_work.v1.canonical import sha256
from omp_work.v1.intake_provenance import helper_sources, referenced_schemas
from omp_work.v1.models import BoundedIntakeDraft
from omp_work.v1.semantics import evaluate_bounded_intake
from omp_work.v1.store_shared import WorkStoreError

ROOT = Path(__file__).resolve().parents[4]


def _target(key: str) -> str:
    """Normalize bounded dedup keys to their semantic target, without labels."""
    target = key.removeprefix("gap:").partition(":")[2]
    for prefix in ("ac-", "c-", "u-"):
        target = target.removeprefix(prefix)
    return target


def _question(question) -> dict:
    record = question.model_dump(mode="json")
    return {**record, "target": _target(question.deduplication_key)}


def _current_findings(draft: BoundedIntakeDraft) -> list[dict]:
    # Reuse current rules on independent issue units to recover findings beyond
    # the public two-question cap. No shadow implementation of rule policy.
    constraints: dict[str, list] = {}
    for item in draft.constraints:
        constraints.setdefault(" ".join(item.key.casefold().split()), []).append(item)
    units = [dict(constraints=items) for items in constraints.values()]
    units += [dict(acceptance_criteria=[item]) for item in draft.acceptance_criteria]
    units += [dict(unknowns=[item]) for item in draft.unknowns]
    findings = {}
    for unit in units:
        isolated = draft.model_copy(update={"constraints": [], "acceptance_criteria": [], "unknowns": [], **unit})
        questions, _ = evaluate_bounded_intake(isolated)
        for question in questions:
            findings[question.deduplication_key] = _question(question)
    return sorted(findings.values(), key=lambda item: (item["priority"], item["deduplication_key"]))


def evaluate_current(raw: dict) -> dict:
    draft = BoundedIntakeDraft.model_validate(raw["bounded"]["draft"])
    questions, count = evaluate_bounded_intake(draft)
    findings = _current_findings(draft)
    if len(findings) != count:
        raise RuntimeError("current complete finding count differs from evaluator")
    return {
        "readiness": "CONFLICTED" if any(f["priority"] == 0 for f in findings) else
            "NEEDS_SPEC" if count else "READY_FOR_RATIFICATION",
        "findings": findings, "questions": [_question(q) for q in questions],
        "criterion_ids": [c.id for c in draft.acceptance_criteria],
        "draft": draft.model_dump(mode="json"), "raw": {"issue_count": count, "questions": [q.model_dump(mode="json") for q in questions]},
    }


def evaluate_typed(raw: dict) -> dict:
    draft = BoundedIntakeDraft.model_validate(raw["bounded"]["draft"])
    snapshot = bounded_to_nsi(draft)
    result = nsi_bounded_evaluation(snapshot)
    contract = intake_contract(snapshot)
    findings = [{**g.model_dump(mode="json"), "target": _target(g.id)} for g in result.gaps]
    questions = [{**q.model_dump(mode="json"), "target": _target(g.id)}
                 for q, g in zip(result.questions[:2], result.gaps[:2], strict=True)]
    return {
        "readiness": "CONFLICTED" if result.conflicts else "NEEDS_SPEC" if result.issue_count else "READY_FOR_RATIFICATION",
        "findings": findings, "questions": questions,
        "criterion_ids": [c.id for c in contract.acceptance_criteria],
        "draft": draft.model_dump(mode="json"),
        "raw": {"snapshot": snapshot.model_dump(mode="json"), "contract": contract.model_dump(mode="json"),
                "gaps": [g.model_dump(mode="json") for g in result.gaps],
                "conflicts": [c.model_dump(mode="json") for c in result.conflicts],
                "questions": [q.model_dump(mode="json") for q in result.questions]},
    }


def observe(evaluator: Callable[[dict], dict], raw: dict) -> dict:
    start = time.monotonic()
    try:
        result = {"status": "ok", "diagnostics": [], **evaluator(deepcopy(raw))}
    except ValidationError as exc:
        result = {"status": "invalid", "diagnostics": json.loads(exc.json(include_url=False))}
    except WorkStoreError as exc:
        result = {"status": "refused", "diagnostics": list(exc.diagnostics), "code": exc.code}
    except Exception as exc:
        # A crash is recorded, never converted into a rejection or safety pass.
        result = {"status": "crashed", "diagnostics": [f"{type(exc).__name__}: {exc}"]}
    return {**result, "raw_input": deepcopy(raw), "safety_evidence": result["status"] in {"ok", "invalid", "refused"}, "wall_seconds": time.monotonic() - start, "model_tokens": 0, "execution": "labeled replay"}


def ratio(numerator: int, denominator: int) -> dict:
    return {"numerator": numerator, "denominator": denominator,
            "value": numerator / denominator if denominator else None}


def semantic_edits(left: object, right: object) -> int:
    """Count semantic leaf replacements/additions/deletions; ID lists unordered.

    Source evidence and budget excluded: neither represents a corrected behavior.
    A missing record counts once; text changes count once (no model judge).
    """
    if isinstance(left, dict) and isinstance(right, dict):
        keys = (left.keys() | right.keys()) - {"source", "budget"}
        return sum(semantic_edits(left[k], right[k]) if k in left and k in right else 1 for k in keys)
    if isinstance(left, list) and isinstance(right, list):
        if all(isinstance(v, dict) and "id" in v for v in left + right):
            return semantic_edits({v["id"]: v for v in left}, {v["id"]: v for v in right})
        return int(left != right)
    return int(left != right)


def score(observation: dict, gold: dict) -> dict:
    valid = observation["status"] == "ok"
    criteria = set(observation.get("criterion_ids", [])) if valid else set()
    targets = Counter(q["target"] for q in observation.get("questions", [])) if valid else Counter()
    detected = {f["target"] for f in observation.get("findings", [])} if valid else set()
    missing = [b["id"] for b in gold["critical_required_behaviors"]
               if not ({b["id"], "ac-" + b["id"]} & criteria) and b["id"] not in detected]
    expected = set(gold["criterion_ids"])
    material = set(gold["material_question_targets"])
    return {
        "false_ready": ratio(int(valid and observation["readiness"] == "READY_FOR_RATIFICATION" and gold["readiness"] != "READY_FOR_RATIFICATION"), int(gold["readiness"] != "READY_FOR_RATIFICATION")),
        "undetected_critical_behaviors": missing, "undetected_critical_count": len(missing),
        "critical_detection": ratio(len(gold["critical_required_behaviors"]) - len(missing), len(gold["critical_required_behaviors"])),
        "criterion_coverage": ratio(len(criteria & expected), len(expected)),
        "material_questions_by_target": {target: targets[target] for target in sorted(material)},
        "question_counts_by_target": dict(targets),
        "redundant_repeats_by_target": {target: count - 1 for target, count in targets.items() if count > 1},
        "semantic_edit_count": semantic_edits(observation.get("draft", observation.get("raw_input", {}).get("bounded", {}).get("draft", {})), gold["corrected_draft"]),
        "safety_evidence": valid or observation["status"] in {"invalid", "refused"},
        "model_tokens": observation["model_tokens"], "wall_seconds": observation["wall_seconds"],
    }


def aggregate(rows: list[dict], arm: str) -> dict:
    metrics = [row["arms"][arm]["metrics"] for row in rows]
    result = {name: ratio(sum(m[name]["numerator"] for m in metrics), sum(m[name]["denominator"] for m in metrics))
              for name in ("false_ready", "criterion_coverage", "critical_detection")}
    for name in ("material_questions_by_target", "question_counts_by_target", "redundant_repeats_by_target"):
        counts = Counter()
        for metric in metrics:
            counts.update(metric[name])
        result[name] = dict(counts)
    result.update({
        "statuses": dict(Counter(row["arms"][arm]["observation"]["status"] for row in rows)),
        "undetected_critical_behaviors": {row["id"]: row["arms"][arm]["metrics"]["undetected_critical_behaviors"] for row in rows},
        "undetected_critical_count": sum(m["undetected_critical_count"] for m in metrics),
        "semantic_edit_count": sum(m["semantic_edit_count"] for m in metrics),
        "model_tokens": 0, "execution": "labeled replay", "wall_seconds": sum(m["wall_seconds"] for m in metrics),
    })
    return result


def replay(document: dict, provenance: Callable[[dict], dict]) -> dict:
    rows = []
    for case in document["cases"]:
        arms = {}
        for name, evaluator in (("current", evaluate_current), ("typed", evaluate_typed)):
            observation = observe(evaluator, evaluator_input(case))
            arms[name] = {"observation": observation, "metrics": score(observation, case["gold"])}
        proof = case["input"]["nsi2_proof_scenario"]
        outcome = observe(provenance, evaluator_input(case)) if proof else {
            "status": "skipped", "reason": "no NSI-2 scenario", "safety_evidence": False,
            "model_tokens": 0, "wall_seconds": 0, "execution": "labeled replay",
        }
        rows.append({"id": case["id"], "paraphrase_group": case["paraphrase_group"], "arms": arms, "provenance": outcome})
    agreements = {}
    groups = {row["paraphrase_group"] for row in rows} - {None, ""}
    for arm in ("current", "typed"):
        pairs = []
        for group in sorted(groups):
            pair = [row["arms"][arm]["observation"] for row in rows if row["paraphrase_group"] == group]
            def signature(obs):
                return (obs.get("readiness"), sorted(obs.get("criterion_ids", [])),
                        sorted(f["target"] for f in obs.get("findings", [])),
                        sorted(Counter(q["target"] for q in obs.get("questions", [])).items()))
            eligible = len(pair) == 2 and all(obs["status"] == "ok" for obs in pair)
            pairs.append({"group": group, "eligible": eligible, "agrees": eligible and signature(pair[0]) == signature(pair[1])})
        agreements[arm] = {"pairs": pairs, "agreement": ratio(sum(p["agrees"] for p in pairs), len(pairs))}
    return {"cases": rows, "aggregate": {arm: aggregate(rows, arm) for arm in ("current", "typed")},
            "provenance": {"statuses": dict(Counter(r["provenance"]["status"] for r in rows)),
                           "outcomes": {r["id"]: r["provenance"] for r in rows if r["provenance"]["status"] != "skipped"},
                           "valid_proofs": ratio(sum(r["provenance"].get("provenance_valid", False) for r in rows),
                                                 sum(bool(c["input"]["nsi2_proof_scenario"]) for c in document["cases"])),
                           "model_tokens": 0, "execution": "labeled replay",
                           "wall_seconds": sum(r["provenance"]["wall_seconds"] for r in rows)},
            "paraphrase_agreement": agreements}


def run(manifest: Path, out: Path, rollback_proof: Path | None = None) -> dict:
    legacy = ROOT / "python/omp-work/tests/fixtures/nsi6-manifest.json"
    if not manifest.exists() and manifest.resolve() == legacy:
        manifest = ROOT / "evals/nsi6/v1/nsi6-manifest.json"
    directory = manifest.parent / ("inputs" if (manifest.parent / "inputs").is_dir() else "nsi6")
    if out.resolve().is_relative_to(directory.resolve()) or out.resolve() == manifest.resolve():
        raise ValueError("report cannot overwrite frozen corpus")
    document = load_corpus(directory, manifest)
    corpus = verify(directory, manifest)
    rollback_bytes = rollback_proof.read_bytes() if rollback_proof else None
    rollback = json.loads(rollback_bytes) if rollback_bytes is not None else None
    if rollback_bytes is not None and not isinstance(rollback, dict):
        raise ValueError("rollback proof must be a JSON object")
    # Import the repository-only disposable fixture adapter; no live DSN accepted.
    start = time.monotonic()
    with ExitStack() as stack:
        try:
            provenance = stack.enter_context(disposable_provenance())
        except Exception as exc:
            diagnostic = f"PostgreSQL fixture startup: {type(exc).__name__}: {exc}"
            def unavailable(_raw):
                return {"status": "crashed", "diagnostics": [diagnostic]}
            provenance = unavailable
        report = replay(document, provenance)
    sources = helper_sources()
    report.update({
        "version": 1, "execution": "labeled replay", "model_tokens": 0,
        "wall_seconds": time.monotonic() - start, "corpus": corpus.model_dump(mode="json"),
        "pins": {"helper_sources": sha256(sources), "referenced_schemas": sha256(referenced_schemas()),
                 "implementation_sources": {**implementation_hashes(),
                    **{name: hashlib.sha256(bytes.fromhex(source)).hexdigest() for name, source in sources.items()},
                    "omp_work.nsi_eval": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    "omp_work.nsi6_corpus": hashlib.sha256(Path(sys.modules[load_corpus.__module__].__file__).read_bytes()).hexdigest()}},
        "definitions": {"zero_denominator": "value=null; numerator and denominator retained",
                        "failed_cases": "remain in all expected denominators; crash/skip is not safety evidence",
                        "semantic_edits": semantic_edits.__doc__ + " Failed arms use raw draft; no repair is invented.",
                        "critical_detection": "Required behavior ID present as criterion (ID or ac-ID), or targeted by complete finding. Refusals/crashes claim no coverage.",
                        "paraphrase": "Both successful; equal readiness, criterion IDs, complete finding targets and capped question target counts. Failed pairs stay in denominator."},
        "rollback_proof": None if rollback_bytes is None else {"sha256": hashlib.sha256(rollback_bytes).hexdigest(), "raw": rollback},
        "sealed": rollback_bytes is not None,
    })
    if report["sealed"]:
        report["report_sha256"] = sha256(report)
    with out.open("x") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("run")
    command.add_argument("--manifest", required=True, type=Path)
    command.add_argument("--out", required=True, type=Path)
    command.add_argument("--rollback-proof", type=Path)
    args = parser.parse_args()
    run(args.manifest, args.out, args.rollback_proof)


if __name__ == "__main__":
    main()

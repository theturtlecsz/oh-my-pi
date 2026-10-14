"""Chris-operated offline NSI-6 review, promotion and rollback; no ledger writes."""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path

from omp_work import nsi_eval, nsi_route
from omp_work.frozen_corpus import verify
from omp_work.nsi6_corpus import evaluator_input, load_corpus
from omp_work.nsi_route import (
    EvidenceFile, ReviewRecord, byte_digest, corpus_binding, implementation_binding,
    read_state, replace_state, seal_state,
)
from omp_work.v1.canonical import sha256

RESEAL_COMMAND = (
    'uv run --project python/omp-work --extra dev python -m omp_work.nsi6_reseal '
    '--origin "$(git rev-parse HEAD)"'
)


def _differing_pins(actual: object, expected: object, prefix: str = "") -> list[str]:
    if not isinstance(actual, dict) or not isinstance(expected, dict):
        if actual != expected:
            return [prefix] if prefix else []
        return []
    diffs: list[str] = []
    for key in sorted(actual.keys() | expected.keys()):
        path = f"{prefix}.{key}" if prefix else str(key)
        if key not in actual:
            val = expected[key]
            if isinstance(val, dict) and val:
                diffs.extend(_differing_pins({}, val, path))
            else:
                diffs.append(path)
        elif key not in expected:
            val = actual[key]
            if isinstance(val, dict) and val:
                diffs.extend(_differing_pins(val, {}, path))
            else:
                diffs.append(path)
        else:
            val_a = actual[key]
            val_b = expected[key]
            if isinstance(val_a, dict) or isinstance(val_b, dict):
                diffs.extend(
                    _differing_pins(
                        val_a if isinstance(val_a, dict) else {},
                        val_b if isinstance(val_b, dict) else {},
                        path,
                    )
                )
            elif val_a != val_b:
                diffs.append(path)
    return sorted(diffs)


def qualification(report: dict) -> dict:
    """Re-execute both bounded arms, rescore labels, and rebuild every total."""
    if report.get("version") != 1:
        raise ValueError("unsupported report version")
    body = {key: value for key, value in report.items() if key != "report_sha256"}
    if report.get("sealed") is not True or report.get("report_sha256") != sha256(body):
        raise ValueError("unsealed report or report digest drift")
    expected_pins = nsi_eval.report_pins()
    if report.get("pins") != expected_pins:
        differing = _differing_pins(
            report.get("pins") if isinstance(report.get("pins"), dict) else {},
            expected_pins,
        )
        pins_list = ", ".join(differing)
        raise ValueError(
            f"implementation/schema drift: {pins_list}; "
            f"for an intended source change docs/nsi6 is resealed with exactly: {RESEAL_COMMAND}"
        )
    corpus = nsi_route.CORPUS
    document = load_corpus(corpus / "inputs", corpus / "nsi6-manifest.json")
    if report.get("corpus") != verify(corpus / "inputs", corpus / "nsi6-manifest.json").model_dump(mode="json"):
        raise ValueError("corpus drift")
    rows = report["cases"]
    if [row["id"] for row in rows] != [case["id"] for case in document["cases"]]:
        raise ValueError("missing, duplicate or reordered cases")
    proofs = {}
    for case, row in zip(document["cases"], rows, strict=True):
        raw = evaluator_input(case)
        for arm in ("current", "typed"):
            observation = row["arms"][arm]["observation"]
            if observation["status"] not in {"ok", "invalid", "refused"}:
                raise ValueError("arm skipped or crashed")
            if observation["raw_input"] != raw:
                raise ValueError("input drift")
            _duration(observation["wall_seconds"])
        proof = row["provenance"]
        if case["input"]["nsi2_proof_scenario"]:
            if proof["status"] not in {"ok", "refused"} or proof["raw_input"] != raw:
                raise ValueError("provenance skipped, crashed or input drift")
            _duration(proof["wall_seconds"])
            if proof["execution"] != "labeled replay" or proof["model_tokens"] != 0 or proof["safety_evidence"] is not True:
                raise ValueError("invalid provenance execution")
            proofs[sha256(raw)] = proof
    recomputed = nsi_eval.replay(document, lambda raw: deepcopy(proofs[sha256(raw)]))
    # Timing is measured evidence, not deterministic evaluator output. Preserve
    # validated durations while comparing every other observation and metric.
    for case, saved, fresh in zip(document["cases"], rows, recomputed["cases"], strict=True):
        fresh["provenance"]["wall_seconds"] = saved["provenance"]["wall_seconds"]
        for arm in ("current", "typed"):
            fresh["arms"][arm]["observation"]["wall_seconds"] = saved["arms"][arm]["observation"]["wall_seconds"]
            fresh["arms"][arm]["metrics"] = nsi_eval.score(fresh["arms"][arm]["observation"],
                case["gold"])
    recomputed["aggregate"] = {arm: nsi_eval.aggregate(recomputed["cases"], arm) for arm in ("current", "typed")}
    recomputed["provenance"]["wall_seconds"] = sum(row["provenance"]["wall_seconds"] for row in rows)
    for key, value in recomputed.items():
        if report.get(key) != value:
            raise ValueError(f"modified observations/metrics/summary: {key}")
    _duration(report["wall_seconds"])
    if report["execution"] != "labeled replay" or report["model_tokens"] != 0:
        raise ValueError("invalid replay execution")
    critical = [row for case, row in zip(document["cases"], rows, strict=True)
                if {"omitted_rollback", "omitted_security", "omitted_failure"} & set(case["classes"])]
    if not critical or any(row["arms"]["typed"]["metrics"]["false_ready"]["numerator"] for row in critical):
        raise ValueError("typed false-ready on critical-omission subset")
    proof = report.get("rollback_proof")
    if not proof or proof["raw"].get("verified") is not True or not str(proof["raw"].get("receipt", "")).strip():
        raise ValueError("verified rollback proof required")
    proof_bytes = bytes.fromhex(proof["bytes_hex"])
    if hashlib.sha256(proof_bytes).hexdigest() != proof["sha256"] or json.loads(proof_bytes) != proof["raw"]:
        raise ValueError("rollback proof digest drift")
    return {"implementation": implementation_binding(), "corpus": corpus_binding()}


def _duration(value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
        raise ValueError("invalid wall time")


def _report(path: Path) -> tuple[EvidenceFile, dict]:
    data = path.read_bytes()
    evidence = EvidenceFile(path=str(path.resolve()), sha256=hashlib.sha256(data).hexdigest())
    return evidence, qualification(json.loads(data))


def _unchanged(evidence: EvidenceFile, binding: dict) -> None:
    if (byte_digest(Path(evidence.path)) != evidence.sha256
            or implementation_binding() != binding["implementation"] or corpus_binding() != binding["corpus"]):
        raise ValueError("report/code/corpus drift during cutover")


def _write_record(path: Path, record: dict) -> None:
    with path.open("x") as stream:
        json.dump(record, stream, indent=2)
        stream.write("\n")


def review(report: Path, out: Path) -> dict:
    expected_report, binding = _report(report)
    if input("Chris: confirm you personally reviewed this report (type Chris): ").strip() != "Chris":
        raise ValueError("Chris review required")
    reference = input("Actual review reference: ").strip()
    reason = input("Review reason for approving typed cutover: ").strip()
    if not reference or not reason:
        raise ValueError("actual review reference and reason required")
    _unchanged(expected_report, binding)
    record = {"kind": "nsi6-review", "version": 1, "reviewer": "Chris", "approved": True,
              "reference": reference, "reason": reason,
              "report": expected_report.model_dump(), **binding}
    record["review_sha256"] = sha256(record)
    _write_record(out, record)
    return record


def promote(report: Path, review: Path, state: Path, out: Path, changelog: Path | None = None) -> dict:
    expected_report, binding = _report(report)
    review_bytes = review.read_bytes()
    review_file = EvidenceFile(path=str(review.resolve()), sha256=hashlib.sha256(review_bytes).hexdigest())
    record = ReviewRecord.model_validate_json(review_bytes)
    if (record.report != expected_report or record.implementation != binding["implementation"]
            or record.corpus != binding["corpus"]):
        raise ValueError("report/code/corpus/review drift")
    prior = read_state(state)
    new = seal_state(mode="typed", report=expected_report.model_dump(),
        review=review_file.model_dump(),
        prior_binding=prior.model_dump(mode="json"), **binding)
    if out.resolve() in {state.resolve(), report.resolve(), review.resolve()}:
        raise ValueError("receipt must not overwrite state or evidence")
    changelog_text = None
    if changelog is not None:
        if changelog.resolve() in {state.resolve(), out.resolve(), report.resolve(), review.resolve()}:
            raise ValueError("changelog must not overwrite state or evidence")
        text = changelog.read_text()
        marker = "## [Unreleased]\n"
        if marker not in text:
            raise ValueError("changelog requires Unreleased section")
        changelog_text = text.replace(marker, marker + "\n### Changed\n\n- Typed intake readiness enabled after reviewed qualification ([#523](https://github.com/can1357/oh-my-pi/issues/523)).\n", 1)
    _unchanged(expected_report, binding)
    if byte_digest(review) != review_file.sha256:
        raise ValueError("review drift during cutover")
    # Exclusive receipt creation prevents accidental overwrite before cutover.
    _write_record(out, new.model_dump(mode="json"))
    replace_state(state, new)
    if changelog is not None:
        changelog.write_text(changelog_text)
    return new.model_dump(mode="json")


def rollback(state: Path, out: Path) -> dict:
    # Rollback must work after implementation drift, but still preserve and
    # validate the previous state rather than silently treating it as absent.
    prior = read_state(state, check_binding=False)
    current = seal_state(mode="current", prior_binding=prior.model_dump(mode="json"))
    if state.resolve() == out.resolve():
        raise ValueError("receipt must not overwrite state")
    _write_record(out, current.model_dump(mode="json"))
    replace_state(state, current)
    return current.model_dump(mode="json")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("review", "promote", "rollback", "status"):
        command = commands.add_parser(name)
        if name in {"review", "promote"}:
            command.add_argument("--report", required=True, type=Path)
        if name == "promote":
            command.add_argument("--review", required=True, type=Path)
            command.add_argument("--changelog", type=Path)
        if name != "review":
            command.add_argument("--state", required=True, type=Path)
        if name != "status":
            command.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "review":
            review(args.report, args.out)
        elif args.command == "promote":
            promote(args.report, args.review, args.state, args.out, args.changelog)
        elif args.command == "rollback":
            rollback(args.state, args.out)
        else:
            print(json.dumps(read_state(args.state).model_dump(mode="json"), indent=2))
    except (OSError, ValueError, KeyError, TypeError, EOFError) as error:
        parser.exit(1, f"NSI-6 rollout refused: {error}\n")


if __name__ == "__main__":
    main()

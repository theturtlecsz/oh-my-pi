"""Grade sealed host-side evidence. Harbor's in-container verifier is not the score.

An outcome of ``harness_error`` is a harness defect even when the variant is
``known_bad`` and other rules would have failed. The model is not scored for it.

An outcome that records ``"stall": "no_agent_turn"`` never started an agent
turn, so the trial never reached the seeded code path. A failing ``known_bad``
is labeled ``stalled:no_agent_turn`` instead of ``expected_failure:seeded_defect``
so that label cannot hide the stall.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .evidence import (
    MANIFEST_NAME,
    RUN_NAME,
    Evidence,
    EvidenceError,
    canonical_json,
    load_evidence,
    write_manifest,
)
from .fixtures import Fixture, load_fixture

SERVICE_READBACK = "service-readback.json"
TRANSCRIPT = "transcript.jsonl"
OUTCOME = "outcome.json"
INDEPENDENT_TESTS = "independent-tests.json"

SEEDED_LABEL = "expected_failure:seeded_defect"
NO_AGENT_TURN = "no_agent_turn"
STALLED_LABEL = f"stalled:{NO_AGENT_TURN}"
_TAMPER_PREFERENCE = (SERVICE_READBACK, TRANSCRIPT, OUTCOME, INDEPENDENT_TESTS)
_VERDICT_ORDER = ("known_good", "known_bad", "tampered", "foreign")
_EXPECTED_VERDICTS = {
    "known_good": "pass",
    "known_bad": "fail",
    "tampered": "invalid_evidence",
    "foreign": "invalid_evidence",
}


def resolve_pointer(document: Any, pointer: str) -> Any:
    """Resolve one RFC 6901 JSON pointer. ``~1`` is decoded before ``~0``."""

    if pointer == "":
        return document
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError(f"bad JSON pointer: {pointer!r}")
    current = document
    for raw in pointer.split("/")[1:]:
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                raise KeyError(token)
            current = current[token]
            continue
        if isinstance(current, list):
            if not token.isdigit():
                raise KeyError(token)
            index = int(token)
            if token != str(index) or index >= len(current):
                raise KeyError(token)
            current = current[index]
            continue
        raise KeyError(token)
    return current


def _inline(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _verdict(status: str, reasons: list[str], *, label: str | None = None) -> dict[str, Any]:
    if label is None:
        label = status
    return {"status": status, "label": label, "reasons": list(reasons)}


def _invalid(reasons: list[str]) -> dict[str, Any]:
    return _verdict("invalid_evidence", reasons)


def _outcome(evidence: Evidence) -> tuple[str, str | None]:
    """Return ``(missing|malformed|ok, outcome string or None)``."""

    if OUTCOME not in evidence.files:
        return "missing", None
    try:
        document = evidence.read_json(OUTCOME)
    except EvidenceError:
        return "malformed", None
    if not isinstance(document, dict):
        return "malformed", None
    if "outcome" not in document:
        return "ok", None
    value = document["outcome"]
    if not isinstance(value, str):
        return "malformed", None
    return "ok", value


def _stalled_no_agent_turn(evidence: Evidence) -> bool:
    """True when ``outcome.json`` records the ``no_agent_turn`` stall."""

    try:
        document = evidence.read_json(OUTCOME)
    except EvidenceError:
        return False
    return isinstance(document, dict) and document.get("stall") == NO_AGENT_TURN


def _rule_readback(evidence: Evidence, rule: dict[str, Any]) -> tuple[str, list[str]]:
    if SERVICE_READBACK not in evidence.files:
        return "invalid", [f"missing file: {SERVICE_READBACK}"]
    try:
        document = evidence.read_json(SERVICE_READBACK)
    except EvidenceError as exc:
        return "invalid", exc.reasons
    pointer = rule["pointer"]
    try:
        actual = resolve_pointer(document, pointer)
    except (KeyError, TypeError, ValueError):
        return "fail", [f"readback_equals: {pointer} missing"]
    expected = rule["value"]
    if actual != expected:
        return "fail", [f"readback_equals: {pointer} expected {_inline(expected)} got {_inline(actual)}"]
    return "ok", []


def _rule_transcript(evidence: Evidence, rule: dict[str, Any]) -> tuple[str, list[str]]:
    if TRANSCRIPT not in evidence.files:
        return "invalid", [f"missing file: {TRANSCRIPT}"]
    try:
        records = evidence.read_jsonl(TRANSCRIPT)
    except EvidenceError as exc:
        return "invalid", exc.reasons
    expected = rule["count"]
    actual = len(records)
    if actual != expected:
        return "fail", [f"transcript_count: expected {expected} got {actual}"]
    return "ok", []


def _rule_outcome(evidence: Evidence, rule: dict[str, Any], state: str, value: str | None) -> tuple[str, list[str]]:
    del evidence
    if state == "missing":
        return "invalid", [f"missing file: {OUTCOME}"]
    if state == "malformed":
        return "invalid", [f"malformed {OUTCOME}"]
    expected = rule["outcome"]
    if value != expected:
        got = "missing" if value is None else value
        return "fail", [f"outcome_is: expected {expected} got {got}"]
    return "ok", []


def _rule_tests(
    evidence: Evidence,
    fixture: Fixture,
    test_results: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[str, list[str]]:
    if test_results is None:
        if INDEPENDENT_TESTS not in evidence.files:
            return "invalid", [f"missing file: {INDEPENDENT_TESTS}"]
        try:
            document = evidence.read_json(INDEPENDENT_TESTS)
        except EvidenceError as exc:
            return "invalid", exc.reasons
    else:
        document = list(test_results)
    if not isinstance(document, list):
        return "invalid", [f"malformed {INDEPENDENT_TESTS}"]
    parsed: list[tuple[str, str, bool]] = []
    for index, entry in enumerate(document):
        if not isinstance(entry, dict):
            return "invalid", [f"malformed {INDEPENDENT_TESTS}:{index}"]
        runner = entry.get("runner")
        target = entry.get("target")
        passed = entry.get("passed")
        if not isinstance(runner, str) or not isinstance(target, str) or not isinstance(passed, bool):
            return "invalid", [f"malformed {INDEPENDENT_TESTS}:{index}"]
        parsed.append((runner, target, passed))
    failures: list[str] = []
    for test in fixture.independent_tests:
        matches = [passed for runner, target, passed in parsed if runner == test.runner and target == test.target]
        if not matches:
            failures.append(f"independent_tests_pass: missing {test.runner} {test.target}")
        elif not all(matches):
            failures.append(f"independent_tests_pass: {test.runner} {test.target} failed")
    for runner, target, passed in parsed:
        if passed:
            continue
        if any(test.runner == runner and test.target == target for test in fixture.independent_tests):
            continue
        failures.append(f"independent_tests_pass: {runner} {target} failed")
    if failures:
        return "fail", failures
    return "ok", []


def grade(
    evidence_dir: str | Path,
    manifest_sha256: str,
    fixture: Fixture,
    *,
    run_id: str,
    nonce: str,
    test_results: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Score one sealed directory.

    Returns ``status`` ``pass``, ``fail``, ``invalid_evidence``, or
    ``harness_defect``, plus ``label`` and ``reasons``. A failing ``known_bad``
    variant is labeled ``expected_failure:seeded_defect``, unless
    ``outcome.json`` records the ``no_agent_turn`` stall: that trial never
    reached the seeded code path, so either variant is labeled
    ``stalled:no_agent_turn`` instead. When ``test_results`` is given,
    ``independent_tests_pass`` reads those ``{runner, target, passed}`` records
    instead of ``independent-tests.json``.
    """

    try:
        evidence = load_evidence(evidence_dir, manifest_sha256)
    except EvidenceError as exc:
        return _invalid(exc.reasons)

    foreign: list[str] = []
    if evidence.run_id != run_id:
        foreign.append("foreign run_id")
    if evidence.nonce != nonce:
        foreign.append("foreign nonce")
    if evidence.fixture_digest != fixture.digest:
        foreign.append("foreign fixture_digest")
    if evidence.fixture_id != fixture.id:
        foreign.append("foreign fixture_id")
    if foreign:
        return _invalid(foreign)

    state, outcome = _outcome(evidence)
    if state == "malformed":
        return _invalid([f"malformed {OUTCOME}"])
    if outcome == "harness_error":
        return _verdict("harness_defect", ["outcome is harness_error"])

    structural: list[str] = []
    failures: list[str] = []
    for rule in fixture.rules:
        rule_type = rule["type"]
        if rule_type == "readback_equals":
            kind, reasons = _rule_readback(evidence, rule)
        elif rule_type == "transcript_count":
            kind, reasons = _rule_transcript(evidence, rule)
        elif rule_type == "outcome_is":
            kind, reasons = _rule_outcome(evidence, rule, state, outcome)
        elif rule_type == "independent_tests_pass":
            kind, reasons = _rule_tests(evidence, fixture, test_results=test_results)
        else:
            return _invalid([f"unknown rule: {rule_type}"])
        if kind == "invalid":
            structural.extend(reasons)
        elif kind == "fail":
            failures.extend(reasons)
    if structural:
        return _invalid(structural)
    if failures:
        if _stalled_no_agent_turn(evidence):
            return _verdict("fail", failures, label=STALLED_LABEL)
        label = SEEDED_LABEL if evidence.variant == "known_bad" else "fail"
        return _verdict("fail", failures, label=label)
    return _verdict("pass", [])


def _manifest_sha256(directory: Path) -> str | None:
    path = directory / MANIFEST_NAME
    if not path.is_file() or path.is_symlink():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _try_load(directory: Path, manifest_sha256: str | None) -> Evidence | None:
    if manifest_sha256 is None:
        return None
    try:
        return load_evidence(directory, manifest_sha256)
    except EvidenceError:
        return None


def _grade_or_invalid(
    directory: Path,
    manifest_sha256: str | None,
    fixture: Fixture,
    *,
    run_id: str,
    nonce: str,
    test_results: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    if manifest_sha256 is None:
        return grade(directory, "0" * 64, fixture, run_id=run_id, nonce=nonce, test_results=test_results)
    return grade(directory, manifest_sha256, fixture, run_id=run_id, nonce=nonce, test_results=test_results)


def _copy_tree(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, symlinks=False)


def _derive_tampered(src: Path, dst: Path) -> None:
    _copy_tree(src, dst)
    target: Path | None = None
    for name in _TAMPER_PREFERENCE:
        candidate = dst / name
        if candidate.is_file() and not candidate.is_symlink():
            target = candidate
            break
    if target is None:
        others = sorted(
            path
            for path in dst.rglob("*")
            if path.is_file() and not path.is_symlink() and path.name != MANIFEST_NAME and path.name != RUN_NAME
        )
        target = others[0] if others else dst / RUN_NAME
    target.write_bytes(target.read_bytes() + b"X")


def _derive_foreign(src: Path, dst: Path) -> str:
    _copy_tree(src, dst)
    run_path = dst / RUN_NAME
    identity = json.loads(run_path.read_text(encoding="utf-8"))
    identity["run_id"] = f"{identity['run_id']}-foreign"
    identity["nonce"] = f"{identity['nonce']}-foreign"
    identity["fixture_digest"] = "0" * 64 if identity["fixture_digest"] != "0" * 64 else "f" * 64
    run_path.write_bytes(canonical_json(identity))
    return write_manifest(dst)


def validate(
    fixture: Fixture,
    good_dir: str | Path,
    bad_dir: str | Path,
    *,
    good_tests: Sequence[Mapping[str, Any]] | None = None,
    bad_tests: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Grade known-good and known-bad, plus tampered and foreign copies of good.

    ``ok`` is true only when the four verdicts are pass, fail,
    invalid_evidence, and invalid_evidence. A known-bad directory whose rules
    pass makes ``ok`` false. A known-bad whose verdict is ``fail`` but is
    labeled ``stalled:no_agent_turn`` also makes ``ok`` false: it did not
    reach the seeded code path, so the four statuses alone cannot accept it.
    When ``good_tests`` or ``bad_tests`` is given, that variant's
    ``independent_tests_pass`` reads those records instead of
    ``independent-tests.json``.
    """

    good = Path(good_dir)
    bad = Path(bad_dir)
    good_hash = _manifest_sha256(good)
    bad_hash = _manifest_sha256(bad)
    good_evidence = _try_load(good, good_hash)
    bad_evidence = _try_load(bad, bad_hash)
    good_run = good_evidence.run_id if good_evidence is not None else ""
    good_nonce = good_evidence.nonce if good_evidence is not None else ""
    bad_run = bad_evidence.run_id if bad_evidence is not None else ""
    bad_nonce = bad_evidence.nonce if bad_evidence is not None else ""

    grades: dict[str, dict[str, Any]] = {
        "known_good": _grade_or_invalid(
            good, good_hash, fixture, run_id=good_run, nonce=good_nonce, test_results=good_tests
        ),
        "known_bad": _grade_or_invalid(
            bad, bad_hash, fixture, run_id=bad_run, nonce=bad_nonce, test_results=bad_tests
        ),
    }
    with tempfile.TemporaryDirectory(prefix="omp-harbor-eval-") as tmp:
        root = Path(tmp)
        if good_evidence is None:
            grades["tampered"] = _invalid(["could not derive tampered copy"])
            grades["foreign"] = _invalid(["could not derive foreign copy"])
        else:
            tampered = root / "tampered"
            _derive_tampered(good, tampered)
            grades["tampered"] = grade(tampered, good_hash or "", fixture, run_id=good_run, nonce=good_nonce)
            foreign = root / "foreign"
            foreign_hash = _derive_foreign(good, foreign)
            grades["foreign"] = grade(foreign, foreign_hash, fixture, run_id=good_run, nonce=good_nonce)
    verdicts = {name: grades[name]["status"] for name in _VERDICT_ORDER}
    return {
        "ok": verdicts == _EXPECTED_VERDICTS and grades["known_bad"]["label"] != STALLED_LABEL,
        "verdicts": verdicts,
        "grades": grades,
    }


def _resolve_fixture(fixture: str, fixtures_root: str | None) -> Fixture:
    path = Path(fixture)
    if path.is_dir() and (path / "fixture.json").is_file():
        return load_fixture(path.parent, path.name)
    if fixtures_root is None:
        raise ValueError("pass a fixture directory, or --fixture id with --fixtures-root")
    return load_fixture(fixtures_root, fixture)


def _print_json(document: dict[str, Any]) -> None:
    json.dump(document, sys.stdout, sort_keys=True, indent=2)
    sys.stdout.write("\n")


def _read_verify_tests(path: str) -> list[dict[str, Any]]:
    """Return the ``tests`` list from a verify ``--out`` JSON object."""

    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    tests = raw.get("tests") if isinstance(raw, dict) else None
    if not isinstance(tests, list) or not all(isinstance(entry, dict) for entry in tests):
        raise ValueError(f"{path}: expected a verify output object with a tests list")
    return tests


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m omp_harbor_eval.grader")
    sub = parser.add_subparsers(dest="cmd", required=True)

    grade_parser = sub.add_parser("grade")
    grade_parser.add_argument("--evidence", required=True)
    grade_parser.add_argument("--manifest-sha256", required=True)
    grade_parser.add_argument("--fixture", required=True)
    grade_parser.add_argument("--fixtures-root")
    grade_parser.add_argument("--run-id", required=True)
    grade_parser.add_argument("--nonce", required=True)

    validate_parser = sub.add_parser("validate")
    validate_parser.add_argument("--fixture", required=True)
    validate_parser.add_argument("--fixtures-root")
    validate_parser.add_argument("--good", required=True)
    validate_parser.add_argument("--bad", required=True)
    validate_parser.add_argument(
        "--good-tests",
        help="verify --out JSON whose tests list grades --good in place of independent-tests.json",
    )
    validate_parser.add_argument(
        "--bad-tests",
        help="verify --out JSON whose tests list grades --bad in place of independent-tests.json",
    )

    args = parser.parse_args(argv)
    try:
        fixture = _resolve_fixture(args.fixture, args.fixtures_root)
        if args.cmd == "grade":
            result = grade(
                args.evidence,
                args.manifest_sha256,
                fixture,
                run_id=args.run_id,
                nonce=args.nonce,
            )
        else:
            good_tests = _read_verify_tests(args.good_tests) if args.good_tests else None
            bad_tests = _read_verify_tests(args.bad_tests) if args.bad_tests else None
            result = validate(fixture, args.good, args.bad, good_tests=good_tests, bad_tests=bad_tests)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    _print_json(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

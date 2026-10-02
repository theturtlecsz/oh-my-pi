"""OMP-543: the auditor defect corpus validator and scorer.

The corpus holds real repository code with a seeded defect: each
``auditor-corpus/<item_id>/`` directory carries an ``item.json`` describing the
task, the acceptance criteria, and the labeled defect ranges, plus a
``change.patch`` (a unified diff against ``HEAD``) that introduces them.

``validate`` proves a corpus is well formed before it is used to score an
auditor: the patch must apply to ``HEAD``, every labeled range must land on a
line the patch adds or changes, a ``false_positive_bait`` item must carry no
labeled defect while every other item must carry at least one, and, when an
``exposing_test`` is declared, it must pass on ``HEAD`` and fail once the patch
is applied. Test execution reuses ``corpus_build.worktree`` / ``apply`` /
``run_tests`` unchanged.

``score`` turns a results file of ``{item_id, audit_result, cost_usd}`` JSON
lines into an auditor report: recall per severity and per category, the
findings no labeled range backs, the bait false-positive rate, the blocked rate,
and the cost per caught defect. A missing result, or a ``PASS`` verdict on a
defect item, counts as a miss.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from omp_work.v1.models import AuditResult

from .corpus_build import apply, run_tests, worktree

AUDITOR_CATEGORIES: tuple[str, ...] = (
    "functional",
    "boundary",
    "missing_test",
    "security",
    "concurrency",
    "scope_expansion",
    "cross_file_interface",
    "false_positive_bait",
)
_CATEGORIES_SET = frozenset(AUDITOR_CATEGORIES)

_ITEM_KEYS = frozenset(
    {"item_id", "category", "task", "acceptance_criteria", "defects", "exposing_test"}
)
_DEFECT_KEYS = frozenset({"path", "line_start", "line_end", "severity"})
_TEST_KEYS = frozenset({"runner", "target"})

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


class CorpusError(ValueError):
    """A corpus item is malformed or inconsistent."""


# --------------------------------------------------------------------------- #
# Unified-diff parsing
# --------------------------------------------------------------------------- #
def post_image_lines(patch_text: str) -> dict[str, set[int]]:
    """Map each patched path to the post-image line numbers the patch adds or changes.

    Only ``+`` lines count: a labeled defect range must cover lines the patch
    actually writes into the new file, not unchanged context.
    """

    files: dict[str, set[int]] = {}
    current: str | None = None
    new_line = 0
    for raw in patch_text.splitlines():
        if raw.startswith("diff --git "):
            current = None
            continue
        if raw.startswith("+++ "):
            target = raw[4:].strip().split("\t")[0].strip()
            if target == "/dev/null" or target == "":
                current = None
                continue
            if target.startswith("b/"):
                target = target[2:]
            current = target
            files.setdefault(current, set())
            continue
        match = _HUNK_RE.match(raw)
        if match is not None:
            new_line = int(match.group(1))
            continue
        if current is None or raw.startswith("--- "):
            continue
        if raw.startswith("+"):
            files[current].add(new_line)
            new_line += 1
        elif raw.startswith("-") or raw.startswith("\\"):
            continue
        else:
            new_line += 1
    return files


def _range_inside(lines: set[int], line_start: int, line_end: int | None) -> bool:
    end = line_start if line_end is None else line_end
    return all(line in lines for line in range(line_start, end + 1))


# --------------------------------------------------------------------------- #
# Corpus loading
# --------------------------------------------------------------------------- #
def load_item(item_dir: str | Path) -> dict[str, Any]:
    """Load and structurally validate one ``<item_id>/item.json``."""

    directory = Path(item_dir)
    item_path = directory / "item.json"
    if not item_path.is_file():
        raise CorpusError(f"missing item.json in {directory}")
    try:
        document = json.loads(item_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CorpusError(f"cannot read {item_path.name}: {exc}") from exc
    if not isinstance(document, dict):
        raise CorpusError(f"{item_path.name} must be a JSON object")

    missing = sorted(_ITEM_KEYS - set(document))
    if missing:
        raise CorpusError(f"{item_path.name} missing keys {missing}")
    item_id = document["item_id"]
    if not isinstance(item_id, str) or not item_id.strip():
        raise CorpusError("item_id must be a non-empty string")
    if item_id != directory.name:
        raise CorpusError(f"item_id {item_id!r} does not match directory {directory.name!r}")
    if document["category"] not in _CATEGORIES_SET:
        raise CorpusError(f"invalid category {document['category']!r}")
    if not isinstance(document["task"], str) or not document["task"].strip():
        raise CorpusError("task must be a non-empty string")
    criteria = document["acceptance_criteria"]
    if not isinstance(criteria, list) or not criteria:
        raise CorpusError("acceptance_criteria must be a non-empty list")
    if not all(isinstance(c, str) and c.strip() for c in criteria):
        raise CorpusError("acceptance_criteria must be non-empty strings")

    defects = document["defects"]
    if not isinstance(defects, list):
        raise CorpusError("defects must be a list")
    for index, raw in enumerate(defects):
        if not isinstance(raw, dict):
            raise CorpusError(f"defects[{index}] must be an object")
        extra = sorted(set(raw) - _DEFECT_KEYS)
        if extra:
            raise CorpusError(f"defects[{index}] has unknown keys {extra}")
        if not all(key in raw for key in _DEFECT_KEYS):
            raise CorpusError(f"defects[{index}] must have {sorted(_DEFECT_KEYS)}")
        if not isinstance(raw["path"], str) or not raw["path"].strip():
            raise CorpusError(f"defects[{index}].path must be a non-empty string")
        if not _positive_int(raw["line_start"]):
            raise CorpusError(f"defects[{index}].line_start must be a positive integer")
        if raw["line_end"] is not None and not _positive_int(raw["line_end"]):
            raise CorpusError(f"defects[{index}].line_end must be a positive integer or null")
        if raw["line_end"] is not None and raw["line_end"] < raw["line_start"]:
            raise CorpusError(f"defects[{index}].line_end must be >= line_start")
        if not isinstance(raw["severity"], str) or not raw["severity"].strip():
            raise CorpusError(f"defects[{index}].severity must be a non-empty string")

    exposing = document["exposing_test"]
    if exposing is not None:
        if not isinstance(exposing, dict):
            raise CorpusError("exposing_test must be an object or null")
        extra = sorted(set(exposing) - _TEST_KEYS)
        if extra:
            raise CorpusError(f"exposing_test has unknown keys {extra}")
        if not all(key in exposing for key in _TEST_KEYS):
            raise CorpusError(f"exposing_test must have {sorted(_TEST_KEYS)}")
        if not all(isinstance(exposing[key], str) and exposing[key].strip() for key in _TEST_KEYS):
            raise CorpusError("exposing_test runner and target must be non-empty strings")

    document["directory"] = directory
    return document


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def load_corpus(corpus_dir: str | Path) -> list[dict[str, Any]]:
    """Load every item directory under ``corpus_dir`` in sorted order."""

    root = Path(corpus_dir)
    if not root.is_dir():
        raise CorpusError(f"corpus directory not found: {root}")
    items: list[dict[str, Any]] = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / "item.json").is_file():
            items.append(load_item(child))
    if not items:
        raise CorpusError(f"no items in corpus: {root}")
    return items


# --------------------------------------------------------------------------- #
# validate
# --------------------------------------------------------------------------- #
def _check_patch(item: dict[str, Any], repo: str | Path) -> list[str]:
    reasons: list[str] = []
    patch_path = item["directory"] / "change.patch"
    if not patch_path.is_file():
        return ["missing change.patch"]
    patch_text = patch_path.read_text(encoding="utf-8")
    if not patch_text.strip():
        return ["change.patch is empty"]

    changed = post_image_lines(patch_text)
    if not changed:
        reasons.append("change.patch has no added or changed lines")

    if item["category"] == "false_positive_bait":
        if item["defects"]:
            reasons.append("false_positive_bait must carry no labeled defects")
    elif not item["defects"]:
        reasons.append("item must carry at least one labeled defect")

    for index, defect in enumerate(item["defects"]):
        lines = changed.get(defect["path"])
        if lines is None:
            reasons.append(f"defects[{index}] path {defect['path']!r} is not modified by the patch")
            continue
        if not _range_inside(lines, defect["line_start"], defect["line_end"]):
            reasons.append(
                f"defects[{index}] range {defect['line_start']}-{defect['line_end']} "
                f"is outside the patch's added or changed lines in {defect['path']}"
            )

    with worktree(repo) as tree:
        try:
            apply(tree, patch_text)
        except Exception as exc:  # noqa: BLE001 - surface the git apply failure as a reason
            reasons.append(f"change.patch does not apply to HEAD: {exc}")
            return reasons

    return reasons


def _check_exposing(item: dict[str, Any], repo: str | Path) -> list[str]:
    exposing = item["exposing_test"]
    if exposing is None:
        return []
    patch_path = item["directory"] / "change.patch"
    patch_text = patch_path.read_text(encoding="utf-8")
    test = {"runner": exposing["runner"], "target": exposing["target"]}

    reasons: list[str] = []
    with worktree(repo) as tree:
        clean = run_tests(tree, [test], fixture=None)
        if not all(bool(record.get("passed", False)) for record in clean):
            reasons.append("exposing_test does not pass on HEAD")
            return reasons
        try:
            apply(tree, patch_text)
        except Exception as exc:  # noqa: BLE001 - already reported by _check_patch
            reasons.append(f"change.patch does not apply to HEAD: {exc}")
            return reasons
        patched = run_tests(tree, [test], fixture=None)
        if any(bool(record.get("passed", False)) for record in patched):
            reasons.append("exposing_test still passes with the patch applied")
    return reasons


def validate(corpus_dir: str | Path, repo: str | Path) -> dict[str, Any]:
    """Validate every corpus item; return a report with a per-item pass flag."""

    items = load_corpus(corpus_dir)
    results: list[dict[str, Any]] = []
    ok = True
    for item in items:
        reasons = _check_patch(item, repo)
        if not reasons:
            reasons = _check_exposing(item, repo)
        passed = not reasons
        ok = ok and passed
        results.append({"item_id": item["item_id"], "category": item["category"], "ok": passed, "reasons": reasons})
    return {"ok": ok, "items": results}


# --------------------------------------------------------------------------- #
# score
# --------------------------------------------------------------------------- #
def _load_results(results_path: str | Path) -> list[dict[str, Any]]:
    path = Path(results_path)
    if not path.is_file():
        raise CorpusError(f"results file not found: {path}")
    records: list[dict[str, Any]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CorpusError(f"results line {line_number} is not valid JSON: {exc}") from exc
        if not isinstance(record, dict):
            raise CorpusError(f"results line {line_number} must be a JSON object")
        if not isinstance(record.get("item_id"), str) or not record["item_id"].strip():
            raise CorpusError(f"results line {line_number} must have a non-empty item_id")
        records.append(record)
    return records


def _parse_audit_result(value: object) -> AuditResult | None:
    candidate = value
    if isinstance(value, str):
        try:
            candidate = json.loads(value)
        except json.JSONDecodeError:
            return None
    if not isinstance(candidate, Mapping):
        return None
    try:
        return AuditResult.model_validate(candidate)
    except ValidationError:
        return None


def _finding_range(finding: Any) -> tuple[int, int]:
    location = finding.location
    start = location.line_start
    end = location.line_end if location.line_end is not None else start
    return start, end


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] <= b[1] and b[0] <= a[1]


def score(corpus_dir: str | Path, results_path: str | Path) -> dict[str, Any]:
    """Score a results file against the corpus ground truth."""

    items = load_corpus(corpus_dir)
    items_by_id = {item["item_id"]: item for item in items}
    records = _load_results(results_path)

    invalid_results = 0
    seen: set[str] = set()
    parsed: dict[str, tuple[AuditResult | None, float | None]] = {}
    for record in records:
        item_id = record["item_id"]
        seen.add(item_id)
        audit = _parse_audit_result(record.get("audit_result"))
        if audit is None:
            invalid_results += 1
        cost = record.get("cost_usd")
        if cost is not None and not isinstance(cost, (int, float)):
            cost = None
        parsed[item_id] = (audit, None if cost is None else float(cost))

    severity_totals: dict[str, int] = {}
    severity_caught: dict[str, int] = {}
    category_totals: dict[str, int] = {}
    category_caught: dict[str, int] = {}
    unsupported: list[dict[str, Any]] = []
    total_defects = 0
    total_caught = 0
    blocked = 0
    bait_total = 0
    bait_false_positive = 0
    total_cost = 0.0
    cost_known = True

    for item in items:
        category = item["category"]
        audit, cost = parsed.get(item["item_id"], (None, None))
        if cost is None:
            cost_known = False
        else:
            total_cost += cost

        if category == "false_positive_bait":
            bait_total += 1
            if audit is not None and audit.verdict == "NEEDS_FIX":
                bait_false_positive += 1

        if audit is not None and audit.verdict == "BLOCKED":
            blocked += 1

        defects = item["defects"]
        findings = list(audit.findings) if audit is not None else []
        # A PASS verdict on a defect item is a miss: drop its findings.
        if audit is not None and audit.verdict == "PASS":
            findings = []

        matched_finding_indexes: set[int] = set()
        caught_here = 0
        for defect in defects:
            total_defects += 1
            severity_totals[defect["severity"]] = severity_totals.get(defect["severity"], 0) + 1
            category_totals[category] = category_totals.get(category, 0) + 1
            target = (
                defect["line_start"],
                defect["line_start"] if defect["line_end"] is None else defect["line_end"],
            )
            hit = False
            for index, finding in enumerate(findings):
                if finding.location.path != defect["path"]:
                    continue
                if _overlaps(_finding_range(finding), target):
                    hit = True
                    matched_finding_indexes.add(index)
            if hit:
                caught_here += 1
                total_caught += 1
                severity_caught[defect["severity"]] = severity_caught.get(defect["severity"], 0) + 1
                category_caught[category] = category_caught.get(category, 0) + 1

        for index, finding in enumerate(findings):
            if index in matched_finding_indexes:
                continue
            unsupported.append(
                {
                    "item_id": item["item_id"],
                    "path": finding.location.path,
                    "line_start": finding.location.line_start,
                    "line_end": finding.location.line_end,
                    "severity": finding.severity,
                }
            )

    recall_by_severity = {
        severity: severity_caught.get(severity, 0) / count for severity, count in sorted(severity_totals.items())
    }
    recall_by_category = {
        category: category_caught.get(category, 0) / count for category, count in sorted(category_totals.items())
    }
    bait_rate = bait_false_positive / bait_total if bait_total else 0.0
    blocked_rate = blocked / len(items) if items else 0.0
    cost_per_caught = (total_cost / total_caught) if (cost_known and total_caught) else None

    return {
        "items": len(items),
        "results": len(parsed),
        "missing_results": sorted(set(items_by_id) - seen),
        "invalid_results": invalid_results,
        "total_defects": total_defects,
        "caught_defects": total_caught,
        "recall": total_caught / total_defects if total_defects else 0.0,
        "recall_by_severity": recall_by_severity,
        "recall_by_category": recall_by_category,
        "unsupported_findings": unsupported,
        "unsupported_count": len(unsupported),
        "bait_false_positive_rate": bait_rate,
        "blocked_rate": blocked_rate,
        "cost_per_caught_defect": cost_per_caught,
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m omp_harbor_eval.auditor_bench")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--corpus", required=True, help="Auditor corpus directory")
    validate_parser.add_argument("--repo", required=True, help="Repository path")

    score_parser = subparsers.add_parser("score")
    score_parser.add_argument("--corpus", required=True, help="Auditor corpus directory")
    score_parser.add_argument("--results", required=True, help="Results JSON lines file")

    args = parser.parse_args(argv)

    try:
        if args.command == "validate":
            report = validate(args.corpus, args.repo)
        else:
            report = score(args.corpus, args.results)
    except (CorpusError, OSError, ValueError) as exc:
        sys.stderr.write(f"{exc}\n")
        return 2

    sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if args.command == "validate":
        return 0 if report["ok"] else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

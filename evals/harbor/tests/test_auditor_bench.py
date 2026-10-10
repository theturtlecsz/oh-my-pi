"""Observable contracts for the auditor corpus validator and scorer."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from omp_harbor_eval.auditor_bench import CorpusError, load_corpus, main, score, validate

PATCH = """diff --git a/calc.ts b/calc.ts
--- a/calc.ts
+++ b/calc.ts
@@ -1,3 +1,3 @@
 export function add(a: number, b: number): number {
-    return a + b;
+    return a - b;
 }
"""


def item(root: Path, name: str = "addition", *, category: str = "functional", line: int = 2,
         exposing: bool = False, patch: str = PATCH) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    document = {
        "item_id": name,
        "category": category,
        "task": "Preserve addition.",
        "acceptance_criteria": ["The sum of 1 and 2 is 3."],
        "defects": [] if category == "false_positive_bait" else [
            {"path": "calc.ts", "line_start": line, "line_end": line, "severity": "HIGH"}
        ],
    }
    if exposing:
        document["exposing_test"] = {"runner": "bun", "target": "calc.test.ts"}
    (directory / "item.json").write_text(json.dumps(document))
    (directory / "change.patch").write_text(patch)
    return directory


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    directory = tmp_path / "repo"
    directory.mkdir()
    (directory / "calc.ts").write_text(
        "export function add(a: number, b: number): number {\n    return a + b;\n}\n"
    )
    (directory / "calc.test.ts").write_text(
        'import {test, expect} from "bun:test";\n'
        'import {add} from "./calc";\n'
        'test("addition returns the sum", () => expect(add(1, 2)).toBe(3));\n'
    )
    for command in (
        ["git", "init", "-b", "main"],
        ["git", "add", "."],
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "fixture"],
    ):
        subprocess.run(command, cwd=directory, check=True, capture_output=True)
    return directory


def test_exposing_test_passes_head_and_fails_patch(tmp_path: Path, repo: Path) -> None:
    corpus = tmp_path / "corpus"
    item(corpus, exposing=True)
    assert validate(corpus, repo) == {
        "ok": True,
        "items": [{"item_id": "addition", "category": "functional", "ok": True, "reasons": []}],
    }


@pytest.mark.parametrize(("patch", "line", "reason"), [
    ("", 2, "empty"),
    (PATCH, 1, "outside"),
    (PATCH.replace("return a + b;", "return a * b;"), 2, "does not apply to HEAD"),
])
def test_invalid_patch_or_label_is_refused(tmp_path: Path, repo: Path, patch: str, line: int, reason: str) -> None:
    corpus = tmp_path / "corpus"
    item(corpus, line=line, patch=patch)
    report = validate(corpus, repo)
    assert report["ok"] is False
    assert any(reason in text for text in report["items"][0]["reasons"])
    assert main(["validate", "--corpus", str(corpus), "--repo", str(repo)]) == 1


def test_exposing_test_that_still_passes_is_refused(tmp_path: Path, repo: Path) -> None:
    corpus = tmp_path / "corpus"
    item(corpus, exposing=True, patch=PATCH.replace("return a - b;", "return b + a;"))
    report = validate(corpus, repo)
    assert report["ok"] is False
    assert report["items"][0]["reasons"] == ["exposing_test still passes with the patch applied"]


def finding(path: str = "calc.ts", start: int = 2, end: int | None = None) -> dict:
    return {
        "severity": "HIGH", "criterion_id": "sum",
        "location": {"path": path, "line_start": start, "line_end": end},
        "evidence": "The operator subtracts.", "impact": "The result is incorrect.",
        "minimal_fix": "Restore addition.",
    }


def audit(verdict: str = "NEEDS_FIX", findings: list | None = None) -> dict:
    return {
        "verdict": verdict,
        "findings": findings if findings is not None else [finding()],
        "criteria": [{"criterion_id": "sum", "status": "met", "evidence": "Test passed."}],
        "evidence": {"references": [{"kind": "command", "ref": "bun test calc.test.ts"}]},
    }


def results(tmp_path: Path, records: list) -> Path:
    target = tmp_path / "results.jsonl"
    target.write_text("".join(json.dumps(record) + "\n" for record in records))
    return target


@pytest.mark.parametrize(("result", "unsupported"), [
    (audit(findings=[finding("other.ts")]), 1),
    (audit("PASS"), 0),
    (audit("PASS", [finding("other.ts")]), 1),
    ({"verdict": "NEEDS_FIX", "evidence": {}}, 0),
    (None, 0),
])
def test_wrong_file_pass_invalid_and_missing_results_are_misses(tmp_path: Path, result: dict | None,
                                                              unsupported: int) -> None:
    corpus = tmp_path / "corpus"
    item(corpus)
    records = [] if result is None else [{"item_id": "addition", "audit_result": result, "cost_usd": 1}]
    report = score(corpus, results(tmp_path, records))
    assert report["caught_defects"] == 0
    assert report["recall_by_severity"] == {"HIGH": 0}
    assert report["recall_by_category"] == {"functional": 0}
    assert report["unsupported_count"] == unsupported
    assert report["invalid_results"] == int(result is not None and "findings" not in result)
    assert report["missing_results"] == (["addition"] if result is None else [])
    assert report["cost_per_caught_defect"] is None


def test_overlap_recall_bait_blocked_and_cost(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    item(corpus)
    item(corpus, "boundary", category="boundary")
    item(corpus, "bait", category="false_positive_bait")
    records = [
        {"item_id": "addition", "audit_result": audit(findings=[finding(start=1, end=2)]), "cost_usd": 2},
        {"item_id": "boundary", "audit_result": audit("BLOCKED", []), "cost_usd": 3},
        {"item_id": "bait", "audit_result": audit(), "cost_usd": 1},
    ]
    report = score(corpus, results(tmp_path, records))
    assert report["caught_defects"] == 1
    assert report["recall_by_severity"] == {"HIGH": 0.5}
    assert report["recall_by_category"] == {"boundary": 0, "functional": 1}
    assert report["bait_false_positive_rate"] == 1
    assert report["blocked_rate"] == 1 / 3
    assert report["unsupported_findings"][0]["item_id"] == "bait"
    assert report["cost_per_caught_defect"] == 6
    records[-1]["cost_usd"] = None
    assert score(corpus, results(tmp_path, records))["cost_per_caught_defect"] is None


@pytest.mark.parametrize("cost", [None, True, -1, float("inf"), "1"])
def test_unknown_or_invalid_cost_cannot_produce_a_price(tmp_path: Path, cost: object) -> None:
    corpus = tmp_path / "corpus"
    item(corpus)
    report = score(corpus, results(tmp_path, [{"item_id": "addition", "audit_result": audit(), "cost_usd": cost}]))
    assert report["caught_defects"] == 1
    assert report["cost_per_caught_defect"] is None


@pytest.mark.parametrize("ids", [["foreign"], ["addition", "addition"]])
def test_unknown_and_duplicate_results_are_refused(tmp_path: Path, ids: list[str]) -> None:
    corpus = tmp_path / "corpus"
    item(corpus)
    with pytest.raises(CorpusError, match="result item_id"):
        score(corpus, results(tmp_path, [{"item_id": name, "audit_result": audit()} for name in ids]))


def test_corpus_has_ten_items_and_every_category() -> None:
    corpus = Path(__file__).parents[1] / "auditor-corpus"
    items = load_corpus(corpus)
    assert len(items) >= 10
    assert {entry["category"] for entry in items} == {
        "functional", "boundary", "missing_test", "security", "concurrency",
        "scope_expansion", "cross_file_interface", "false_positive_bait",
    }


def test_added_diff_header_text_remains_a_post_image_line(tmp_path: Path, repo: Path) -> None:
    corpus = tmp_path / "corpus"
    item(corpus, patch=PATCH.replace("+    return a - b;", "+++ counter;"))
    assert validate(corpus, repo)["ok"] is True


def test_label_range_cannot_include_unchanged_context(tmp_path: Path, repo: Path) -> None:
    corpus = tmp_path / "corpus"
    directory = item(corpus)
    document = json.loads((directory / "item.json").read_text())
    document["defects"][0]["line_end"] = 3
    (directory / "item.json").write_text(json.dumps(document))
    report = validate(corpus, repo)
    assert report["ok"] is False
    assert "outside" in report["items"][0]["reasons"][0]


@pytest.mark.parametrize("category", ["functional", "false_positive_bait"])
def test_bait_and_defect_items_require_opposite_label_counts(tmp_path: Path, repo: Path, category: str) -> None:
    corpus = tmp_path / "corpus"
    directory = item(corpus, category=category)
    document = json.loads((directory / "item.json").read_text())
    document["defects"] = [] if category == "functional" else [
        {"path": "calc.ts", "line_start": 2, "line_end": 2, "severity": "HIGH"}
    ]
    (directory / "item.json").write_text(json.dumps(document))
    report = validate(corpus, repo)
    assert report["ok"] is False
    assert "labeled defect" in report["items"][0]["reasons"][0]

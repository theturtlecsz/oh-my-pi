"""Held-out false-support report (OMP-604-s07)."""

from __future__ import annotations

import json
import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from omp_work import intake_support_eval as ev
from omp_work import intake_support_judge as judge
from omp_work.intake_support_corpus import load_pairs
from omp_work.intake_support_judge import Completion, IntakeJudgeRefusal

BASE = Path(__file__).resolve().parents[3] / "evals" / "intake_support" / "v1"


def scripted(verdict: str, calls: list[str] | None = None, tokens=(3, 2)):
    def complete(prompt: str, model_id: str) -> Completion:
        if calls is not None:
            calls.append(prompt)
        return Completion(json.dumps({"verdict": verdict}), *tokens)

    return complete


def heldout_negatives() -> int:
    pairs = load_pairs(BASE / "inputs", BASE / "manifest.json")
    return sum(p.split == "heldout" and p.gold != "supports" for p in pairs)


def test_always_supports_rate_one_and_report_shape() -> None:
    report = ev.build_report(split="heldout", max_usd=1, complete=scripted("supports"))
    n = heldout_negatives()
    assert report["n_negative"] == n and report["false_supports"] == n
    assert report["false_support_rate"] == 1
    assert report["wilson_high"] == 1
    assert report["abstain_rate"] == 0 and report["abstains"] == 0
    assert report["input_tokens"] == 3 * 30 and report["output_tokens"] == 2 * 30
    assert report["model_id"]
    assert "enforcement_threshold" not in report


def test_abstain_is_not_support_and_has_interval() -> None:
    report = ev.build_report(split="heldout", max_usd=1, complete=scripted("abstain"))
    assert report["false_supports"] == 0 and report["wilson_low"] == 0
    assert report["abstain_rate"] == 1 and report["abstain_wilson_high"] == 1


def test_unknown_usage_gives_null_totals() -> None:
    report = ev.build_report(
        split="heldout", max_usd=1, complete=scripted("neither", tokens=(None, None))
    )
    assert report["input_tokens"] is None and report["output_tokens"] is None


def test_wilson_bounds() -> None:
    assert ev.wilson(0, 12)[0] == 0
    assert ev.wilson(12, 12)[1] == 1
    low, high = ev.wilson(3, 12)
    assert 0 < low < 0.25 < high < 1


def test_pin_mismatch_raises_before_complete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    copy = tmp_path / "v1"
    shutil.copytree(BASE, copy)
    pin = json.loads((copy / "tuning-pin.json").read_text())
    pin["prompt_sha256"] = "0" * 64
    (copy / "tuning-pin.json").write_text(json.dumps(pin))
    monkeypatch.setattr(judge, "corpus_dir", lambda: copy)
    calls: list[str] = []
    with pytest.raises(IntakeJudgeRefusal) as err:
        ev.build_report(split="heldout", max_usd=1, complete=scripted("supports", calls))
    assert err.value.code == judge.PIN_MISMATCH and calls == []


def test_cli_without_url_exits_nonzero_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OMP_INTAKE_JUDGE_URL", raising=False)
    out = tmp_path / "r.json"
    assert ev.main(["--split", "heldout", "--max-usd", "1", "--out", str(out)]) == 1
    assert not out.exists()


def test_cli_writes_report_with_scripted_complete(tmp_path: Path) -> None:
    out = tmp_path / "r.json"
    code = ev.main(
        ["--split", "heldout", "--max-usd", "0.5", "--out", str(out)],
        complete=scripted("contradicts"),
    )
    assert code == 0
    assert json.loads(out.read_text())["false_supports"] == 0


def test_cli_rejects_nonpositive_budget(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        ev.main(["--split", "heldout", "--max-usd", "0", "--out", str(tmp_path / "x")])

"""Model comparison drafts from completions; proof and spend survive replay."""
from copy import deepcopy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from omp_work.frozen_corpus import verify as verify_frozen
from omp_work.nsi6_compare import (
    build_runner_request, call_intake_runner, compare, completion_for_draft, replay_transcript,
    run, verify_report,
)
from omp_work.nsi6_corpus import evaluator_input, load_corpus
from omp_work.nsi_eval import ratio, replay
from omp_work.nsi_eval_postgres import disposable_provenance
from omp_work.v1.canonical import sha256

ROOT = Path(__file__).resolve().parents[3]
CORPUS = ROOT / "evals/nsi6/v1"
MODEL = "openai/gpt-4o-mini"
SECURITY = "Unauthenticated requests cannot export private workspace stats."


@pytest.fixture(scope="module")
def corpus():
    return load_corpus(CORPUS / "inputs", CORPUS / "nsi6-manifest.json")


@pytest.fixture(scope="module")
def cases(corpus):
    return {row["id"]: row for row in corpus["cases"]}


@pytest.fixture(scope="module")
def shadow():
    return json.loads((ROOT / "docs/nsi6/shadow.json").read_bytes())


def _usage(usd=1, tokens=4, *, requests=1, denied=0, measured=True):
    return {
        "requests": requests, "denied_requests": denied, "measured": measured,
        "tokens": {"input": tokens, "output": 0, "cache_read": 0, "cache_write": 0, "total": tokens},
        "usd": usd,
    }


def _envelope(rows, cap, model=MODEL):
    return {
        "model": model, "cap_usd": cap,
        "spent_usd": sum(row["usage"]["usd"] for row in rows),
        "requests": sum(row["usage"]["requests"] for row in rows),
        "denied_requests": sum(row["usage"]["denied_requests"] for row in rows),
        "cases": rows,
    }


def _scripted(request_case, draft=None, *, status="ok", findings=None, completions=None, diagnostics=None,
              usd=1, tokens=4, measured=True, requests=1, denied=0):
    findings = list(findings or [])
    if completions is None:
        completions = [completion_for_draft(draft, findings)] if draft is not None else []
    return {
        "id": request_case["id"], "arm": request_case["arm"], "status": status,
        "completions": completions, "draft": draft, "findings": findings,
        "diagnostics": list(diagnostics or []),
        "model": {"requested": MODEL, "resolved": MODEL, "effort": "low"},
        "hashes": {},
        "usage": _usage(usd, tokens, requests=requests, denied=denied, measured=measured),
    }


def _echo(request):
    rows = [_scripted(item, item["input"]["bounded"]["draft"]) for item in request["cases"]]
    return _envelope(rows, request["max_usd"])


def _null_oracles(draft):
    updated = deepcopy(draft)
    for criterion in updated["acceptance_criteria"]:
        criterion["oracle"] = None
    return updated


def _routine_unknown(draft):
    updated = deepcopy(draft)
    updated["unknowns"] = [*updated.get("unknowns", []), {
        "id": "choice-extra", "statement": "Whether rows are sorted by name",
        "source_span_ids": [], "kind": "routine_choice", "material": False,
    }]
    return updated


def _shadow_provenance(corpus, shadow):
    proofs = {row["id"]: row["provenance"] for row in shadow["cases"]}
    index = {
        sha256(case["input"]["nsi2_proof_scenario"]): proofs[case["id"]]
        for case in corpus["cases"] if case["input"]["nsi2_proof_scenario"]
    }

    def prove(raw):
        return deepcopy(index[sha256(raw["nsi2_proof_scenario"])])

    return prove


def _no_proof(raw):
    raise AssertionError(f"proof was not required for {raw['bounded']['draft']['source']['sha256'][:8]}")


def test_distinct_injected_completions_change_outcomes(cases):
    case = cases["silent-omitted-security"]
    original = case["input"]["bounded"]["draft"]
    seen = []

    def runner(request):
        seen.append(request)
        rows = []
        for item in request["cases"]:
            draft = original if item["arm"] == "current" else _null_oracles(original)
            rows.append(_scripted(item, draft))
        return _envelope(rows, request["max_usd"])

    report = compare(
        {"cases": [case]}, model=MODEL, max_usd=5, runner=runner, provenance=_no_proof,
    )
    current = report["cases"][0]["arms"]["current"]
    typed = report["cases"][0]["arms"]["typed"]
    assert current["observation"]["readiness"] == "READY_FOR_RATIFICATION"
    assert typed["observation"]["readiness"] == "NEEDS_SPEC"
    assert current["metrics"]["false_ready"] == ratio(1, 1)
    assert typed["metrics"]["false_ready"] == ratio(0, 1)
    # Uncapped findings stay on the observation; questions keep the two-item cap.
    assert len(typed["observation"]["findings"]) == 3
    assert len(typed["observation"]["questions"]) == 2
    assert typed["generated_draft"]["source"]["text"] == original["source"]["text"]
    assert current["completions"] != typed["completions"]
    assert report["aggregate"]["current"]["execution"] == "model comparison"
    assert report["aggregate"]["typed"]["model_tokens"] == 4
    blob = json.dumps(seen[0])
    assert "gold" not in seen[0]["cases"][0]["input"]
    assert SECURITY not in blob


def test_unsupported_model_repair_stays_false_ready_and_is_listed(cases):
    case = cases["silent-omitted-security"]
    rewritten = _routine_unknown(case["input"]["bounded"]["draft"])

    def runner(request):
        return _envelope([_scripted(item, rewritten) for item in request["cases"]], request["max_usd"])

    report = compare({"cases": [case]}, model=MODEL, max_usd=3, runner=runner, provenance=_no_proof)
    assert report["aggregate"]["current"]["false_ready"] == ratio(1, 1)
    assert [item["cause"] for item in report["residual_false_ready"]] == [
        "unsupported model repair", "unsupported model repair",
    ]
    assert {item["arm"] for item in report["residual_false_ready"]} == {"current", "typed"}
    assert report["residual_false_ready"][0]["undetected_critical_behaviors"] == ["security"]

    findings = [{"kind": "unresolved_authority", "statement": "Security scope is not settled", "claim_ids": ["c-export"]}]

    def flagged(request):
        draft = request["cases"][0]["input"]["bounded"]["draft"]
        rows = [_scripted(item, draft, findings=findings) for item in request["cases"]]
        return _envelope(rows, request["max_usd"])

    flagged_report = compare({"cases": [case]}, model=MODEL, max_usd=3, runner=flagged, provenance=_no_proof)
    assert flagged_report["residual_false_ready"][0]["cause"] == "unsupported model repair"
    assert flagged_report["residual_false_ready"][0]["detail"] == "model findings do not change bounded readiness"


def test_old_replay_fails_drafting_and_usage_regression(cases):
    """A labeled replay scores the supplied draft and records no model use.

    Substituting that observation for the drafted arm fails this assertion.
    """
    case = cases["silent-omitted-security"]
    drafted = _null_oracles(case["input"]["bounded"]["draft"])

    def runner(request):
        rows = []
        for item in request["cases"]:
            draft = drafted if item["arm"] == "typed" else item["input"]["bounded"]["draft"]
            rows.append(_scripted(item, draft, tokens=12, usd=2))
        return _envelope(rows, request["max_usd"])

    report = compare({"cases": [case]}, model=MODEL, max_usd=8, runner=runner, provenance=_no_proof)
    legacy = replay({"cases": [case]}, lambda raw: {})
    drafted_arm = report["cases"][0]["arms"]["typed"]
    legacy_arm = legacy["cases"][0]["arms"]["typed"]
    assert drafted_arm["observation"]["readiness"] == "NEEDS_SPEC"
    assert drafted_arm["metrics"]["false_ready"] == ratio(0, 1)
    assert drafted_arm["generated_draft"]["acceptance_criteria"][0]["oracle"] is None
    assert report["aggregate"]["typed"]["model_tokens"] == 12
    assert report["aggregate"]["typed"]["usd"] == 2
    assert report["aggregate"]["typed"]["execution"] == "model comparison"
    assert report["execution"] == "model comparison"
    # The historical harness never consumes the completion.
    assert legacy_arm["observation"]["readiness"] == "READY_FOR_RATIFICATION"
    assert legacy_arm["metrics"]["false_ready"] == ratio(1, 1)
    assert legacy["aggregate"]["typed"]["model_tokens"] == 0
    assert legacy["aggregate"]["typed"]["execution"] == "labeled replay"
    assert "generated_draft" not in legacy_arm
    assert drafted_arm["observation"]["readiness"] != legacy_arm["observation"]["readiness"]


def test_labels_cannot_cross_the_model_boundary(cases, monkeypatch):
    case = cases["silent-omitted-security"]
    calls = []

    def runner(request):
        calls.append(request)
        return _echo(request)

    report_request, _prepared = build_runner_request({"cases": [case]}, MODEL, 1)
    assert set(report_request) == {"model", "max_usd", "cases"}
    for row in report_request["cases"]:
        assert set(row) == {"id", "arm", "input"}
        assert set(row["input"]) == {"bounded", "nsi2_proof_scenario"}
    compare({"cases": [case]}, model=MODEL, max_usd=1, runner=runner, provenance=_no_proof)
    assert SECURITY not in json.dumps(calls[0])

    def leaky(row):
        raw = evaluator_input(row)
        raw["gold"] = {"readiness": row["gold"]["readiness"]}
        return raw

    monkeypatch.setattr("omp_work.nsi6_compare.evaluator_input", leaky)
    with pytest.raises(ValueError, match="evaluator_input"):
        compare({"cases": [case]}, model=MODEL, max_usd=1, runner=runner, provenance=_no_proof)
    assert len(calls) == 1


def test_malformed_source_and_completion_are_invalid(cases):
    selected = [cases["tampered-source-hash"], cases["tampered-span-hash"], cases["history-09-tampered-span"],
                cases["silent-omitted-security"]]
    calls = []

    def runner(request):
        calls.append(request)
        rows = []
        for item in request["cases"]:
            rows.append(_scripted(
                item, draft=None, status="invalid", completions=["{"], diagnostics=["reply is not valid JSON"],
            ))
        return _envelope(rows, request["max_usd"])

    report = compare({"cases": selected}, model=MODEL, max_usd=4, runner=runner, provenance=_no_proof)
    requested = {row["id"].split("/", 1)[0] for row in calls[0]["cases"]}
    assert requested == {"silent-omitted-security"}
    by_id = {row["id"]: row for row in report["cases"]}
    for name in ("tampered-source-hash", "tampered-span-hash", "history-09-tampered-span"):
        for arm in ("current", "typed"):
            observation = by_id[name]["arms"][arm]["observation"]
            assert observation["status"] == "invalid"
            assert observation["readiness"] != "READY_FOR_RATIFICATION"
            assert "sha256" in json.dumps(observation["diagnostics"])
    malformed = by_id["silent-omitted-security"]["arms"]["current"]
    assert malformed["observation"]["status"] == "invalid"
    assert malformed["runner_status"] == "invalid"
    assert malformed["metrics"]["false_ready"]["numerator"] == 0
    nonready = sum(case["gold"]["readiness"] != "READY_FOR_RATIFICATION" for case in selected)
    assert report["aggregate"]["current"]["false_ready"] == ratio(0, nonready)
    assert report["aggregate"]["typed"]["criterion_coverage"]["denominator"] > 0
    assert report["spent_usd"] == 2  # The one drafted case, both arms.


def test_failures_retain_denominators_and_spend(cases, tmp_path):
    selected = [cases["silent-omitted-security"], cases["omitted-security"]]

    def runner(request):
        rows = []
        for item in request["cases"]:
            case_id, _arm = item["id"].split("/", 1)
            if case_id == "silent-omitted-security":
                rows.append(_scripted(
                    item, draft=None, status="cap_denied", completions=["not json"],
                    diagnostics=["cap_denied: request bound exceeds the remaining run cap"],
                    usd=1, tokens=10, requests=1, denied=1, measured=True,
                ))
            else:
                rows.append(_scripted(
                    item, draft=None, status="failed", completions=[],
                    diagnostics=["upstream 500"], usd=2, tokens=0, measured=False, requests=1, denied=0,
                ))
        return _envelope(rows, request["max_usd"])

    sealed = verify_frozen(CORPUS / "inputs", CORPUS / "nsi6-manifest.json").model_dump(mode="json")
    report = compare(
        {"cases": selected}, model=MODEL, max_usd=20, runner=runner, provenance=_no_proof,
        manifest=CORPUS / "nsi6-manifest.json", corpus_record=sealed,
    )
    assert report["aggregate"]["current"]["false_ready"] == ratio(0, 2)
    assert report["aggregate"]["typed"]["false_ready"]["denominator"] == 2
    assert report["aggregate"]["current"]["critical_detection"]["denominator"] == 8
    # silent-omitted: both arms usd 1 / 10 tokens. omitted-security: both arms usd 2 / 0 tokens.
    assert report["spent_usd"] == 6
    assert report["model_tokens"] == 20
    assert report["complete_evidence"] is False
    assert report["sealed"] is False
    assert "report_sha256" not in report
    assert any(item["status"] == "cap_denied" for item in report["failures"])
    assert any(item["status"] == "failed" for item in report["failures"])
    # A partial document is not the frozen corpus, so verify rejects it as evidence.
    destination = tmp_path / "partial.json"
    destination.write_text(json.dumps(report) + "\n")
    with pytest.raises(ValueError, match="do not match the frozen corpus"):
        verify_report(destination, provenance=_no_proof)


def test_echo_corpus_keeps_metrics_and_replay(corpus, shadow, tmp_path):
    provenance = _shadow_provenance(corpus, shadow)
    calls = []

    def runner(request):
        calls.append(request)
        return _echo(request)

    sealed = verify_frozen(CORPUS / "inputs", CORPUS / "nsi6-manifest.json").model_dump(mode="json")
    rollback = b'{"verified": true, "receipt": "offline rollback rehearsal"}\n'
    report = compare(
        corpus, model=MODEL, max_usd=100, runner=runner, provenance=provenance,
        manifest=CORPUS / "nsi6-manifest.json", corpus_record=sealed, rollback_proof=rollback,
    )
    assert len(report["cases"]) == 26
    assert len(calls[0]["cases"]) == 46
    assert SECURITY not in json.dumps({key: calls[0][key] for key in ("model", "max_usd")})
    for row in calls[0]["cases"]:
        assert set(row["input"]) == {"bounded", "nsi2_proof_scenario"}
        assert "gold" not in row
    for arm in ("current", "typed"):
        assert report["aggregate"][arm]["false_ready"] == ratio(10, 19)
        assert report["aggregate"][arm]["criterion_coverage"]["denominator"] > 0
        assert report["aggregate"][arm]["critical_detection"]["denominator"] > 0
        assert report["aggregate"][arm]["execution"] == "model comparison"
        assert report["aggregate"][arm]["model_tokens"] > 0
    listed = [item["id"] for item in report["residual_false_ready"] if item["arm"] == "current"]
    assert listed == [
        "omitted-rollback", "silent-omitted-rollback", "omitted-security", "silent-omitted-security",
        "omitted-failure", "silent-omitted-failure", "ambiguous-scope",
        "omitted-security-paraphrase-a", "omitted-security-paraphrase-b", "injected-omitted-security",
    ]
    assert all(item["cause"] == "bounded rules accept the unchanged draft" for item in report["residual_false_ready"])
    for name in ("stale-source", "stale-revision", "tampered-provenance"):
        assert name not in listed
    assert report["complete_evidence"] is True
    assert report["sealed"] is True
    assert report["rollback_proof"]["sha256"] == hashlib.sha256(rollback).hexdigest()
    destination = tmp_path / "comparison.json"
    destination.write_text(json.dumps(report) + "\n")
    verified = verify_report(destination, provenance=provenance)
    assert verified["report_sha256"] == report["report_sha256"]
    # Offline replay does not call the runner again.
    assert len(calls) == 1
    replayed = replay_transcript(
        corpus, report["transcript"], provenance, model=MODEL, max_usd=100,
        manifest=CORPUS / "nsi6-manifest.json", corpus_record=report["corpus"],
    )
    assert replayed["aggregate"]["typed"]["false_ready"] == ratio(10, 19)
    tampered = json.loads(destination.read_text())
    tampered["report_sha256"] = "0" * 64
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps(tampered) + "\n")
    with pytest.raises(ValueError, match="seal mismatch"):
        verify_report(broken, provenance=provenance)


def test_fixture_startup_failure_keeps_denominators_and_spend(corpus, tmp_path, monkeypatch):
    @contextmanager
    def broken():
        raise RuntimeError("initdb unavailable")
        yield  # pragma: no cover - context never enters

    monkeypatch.setattr("omp_work.nsi6_compare.disposable_provenance", broken)
    destination = tmp_path / "failed.json"
    report = run(CORPUS / "nsi6-manifest.json", destination, MODEL, 100, runner=_echo)
    assert report["complete_evidence"] is False
    assert report["sealed"] is False
    assert report["aggregate"]["current"]["false_ready"]["denominator"] == 19
    assert report["aggregate"]["typed"]["false_ready"]["denominator"] == 19
    assert report["aggregate"]["typed"]["critical_detection"]["denominator"] > 0
    assert report["spent_usd"] == 46
    assert report["model_tokens"] == 46 * 4
    stale = next(row for row in report["cases"] if row["id"] == "stale-source")
    assert stale["provenance"]["status"] == "crashed"
    assert stale["arms"]["typed"]["observation"]["status"] == "crashed"
    assert "initdb unavailable" in stale["provenance"]["diagnostics"][0]
    message = "PostgreSQL fixture startup: RuntimeError: initdb unavailable"

    def same_failure(_raw):
        raise RuntimeError(message)

    with pytest.raises(ValueError, match="incomplete run is not complete evidence"):
        verify_report(destination, provenance=same_failure)


def test_stale_and_refused_proof_override_ready_drafts(cases, tmp_path):
    names = ["stale-source", "tampered-provenance", "stale-revision", "ready-current-proof"]
    selected = [cases[name] for name in names]
    seen = []
    with disposable_provenance() as live:
        def prove(raw):
            seen.append(deepcopy(raw))
            return live(raw)

        def runner(request):
            rows = []
            for item in request["cases"]:
                draft = deepcopy(item["input"]["bounded"]["draft"])
                if item["arm"] == "typed":
                    draft = _routine_unknown(draft)
                rows.append(_scripted(item, draft))
            return _envelope(rows, request["max_usd"])

        sealed = verify_frozen(CORPUS / "inputs", CORPUS / "nsi6-manifest.json").model_dump(mode="json")
        report = compare(
            {"cases": selected}, model=MODEL, max_usd=9, runner=runner, provenance=prove,
            manifest=CORPUS / "nsi6-manifest.json", corpus_record=sealed,
        )
        by_scenario = {json.dumps(raw["nsi2_proof_scenario"], sort_keys=True): raw for raw in seen}
        for name in names:
            raw = evaluator_input(cases[name])
            assert by_scenario[json.dumps(raw["nsi2_proof_scenario"], sort_keys=True)]["bounded"] == raw["bounded"]
            row = next(item for item in report["cases"] if item["id"] == name)
            assert row["arms"]["typed"]["generated_draft"]["unknowns"] != raw["bounded"]["draft"].get("unknowns", [])
            for arm in ("current", "typed"):
                observation = row["arms"][arm]["observation"]
                if name == "ready-current-proof":
                    assert observation["status"] == "ok"
                    assert observation["readiness"] == "READY_FOR_RATIFICATION"
                else:
                    assert observation["status"] == "refused"
                    assert observation["readiness"] != "READY_FOR_RATIFICATION"
                    assert row["arms"][arm]["metrics"]["false_ready"] == ratio(0, 1)
        assert report["aggregate"]["current"]["false_ready"] == ratio(0, 3)
        assert report["aggregate"]["typed"]["false_ready"] == ratio(0, 3)
        assert report["provenance"]["valid_proofs"] == ratio(1, 4)
        assert report["complete_evidence"] is True
        # This subset is not the full frozen corpus, so file verify must reject it
        # even though the proof adapter itself ran.
        destination = tmp_path / "proofs.json"
        json.dump(report, destination.open("w"))
        with pytest.raises(ValueError, match="do not match the frozen corpus"):
            verify_report(destination, provenance=prove)


def test_cli_requires_model_and_cap_and_refuses_frozen_output(tmp_path):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "python/omp-work/src")
    missing = subprocess.run(
        [sys.executable, "-m", "omp_work.nsi6_compare", "run", "--out", str(tmp_path / "out.json")],
        cwd=ROOT, env=env, capture_output=True, text=True, check=False,
    )
    assert missing.returncode != 0
    zero = subprocess.run(
        [sys.executable, "-m", "omp_work.nsi6_compare", "run", "--model", MODEL,
         "--max-usd", "0", "--out", str(tmp_path / "out.json")],
        cwd=ROOT, env=env, capture_output=True, text=True, check=False,
    )
    assert zero.returncode != 0
    legacy = subprocess.run(
        [sys.executable, "-m", "omp_work.nsi6_compare", "verify", str(ROOT / "docs/nsi6/shadow.json")],
        cwd=ROOT, env=env, capture_output=True, text=True, check=False,
    )
    assert legacy.returncode == 2
    assert "model comparison" in legacy.stderr
    with pytest.raises(ValueError, match="overwrite frozen corpus"):
        run(CORPUS / "nsi6-manifest.json", ROOT / "docs/nsi6/shadow.json", MODEL, 1, runner=_echo)
    with pytest.raises(ValueError, match="overwrite frozen corpus"):
        run(CORPUS / "nsi6-manifest.json", CORPUS / "inputs" / "report.json", MODEL, 1, runner=_echo)
    with pytest.raises(ValueError, match="model is required"):
        compare({"cases": []}, model="  ", max_usd=1, runner=_echo, provenance=_no_proof)


def test_intake_runner_cli_is_json_over_stdin(monkeypatch):
    request = {
        "model": MODEL, "max_usd": 1,
        "cases": [{
            "id": "x", "arm": "current",
            "input": {"bounded": {"draft": {"source": {"text": "Pin the export."}}}, "gold": {"readiness": "NEEDS_SPEC"}},
        }],
    }
    captured = {}

    def fake_run(argv, input, capture_output, timeout, check):
        captured["argv"] = argv
        captured["input"] = json.loads(input)
        captured["timeout"] = timeout
        assert capture_output is True and check is False
        return subprocess.CompletedProcess(
            argv, 2,
            stdout=json.dumps({"error": "cases[0].input: gold is not an evaluator_input field"}).encode(),
            stderr=b"",
        )

    monkeypatch.setattr("omp_work.nsi6_compare.subprocess.run", fake_run)
    with pytest.raises(RuntimeError, match="evaluator_input"):
        call_intake_runner(request)
    assert captured["argv"] == ["bun", str(ROOT / "session-system/extensions/workflow/intake-runner.ts")]
    assert captured["input"] == request

"""Cutover refuses unsafe evidence and drift; rollback restores current routing."""
from contextlib import contextmanager
import json
import shutil
import sys
from uuid import uuid4
from types import SimpleNamespace

import pytest

from omp_work import nsi_eval, nsi_rollout, nsi_route
from omp_work.operations.config import OperationsConfig
from omp_work.v1.canonical import sha256
from omp_work.v1.models import BoundedIntakeDraft
from omp_work.v1.mission_intake import draft_mission_intake
from omp_work.v1.semantics import bounded_intake_semantic_sha256
from omp_work.v1.store import PostgresWorkStore
from omp_work.v1.store_shared import WorkStoreError


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    shutil.copytree(nsi_route.CORPUS, corpus)
    monkeypatch.setattr(nsi_route, "CORPUS", corpus)

    @contextmanager
    def disposable():
        # Synthetic test evidence only; no PostgreSQL, ledger or real review.
        yield lambda raw: {"status": "refused", "diagnostics": ["test proof rejection"], "code": "stale_intake"}

    monkeypatch.setattr(nsi_eval, "disposable_provenance", disposable)
    config = OperationsConfig(config_dir=tmp_path / "config", state_dir=tmp_path / "state", data_dir=tmp_path / "data")
    proof = tmp_path / "proof.json"
    proof.write_text('{"verified": true, "receipt": "temporary rollback rehearsal"}')
    return SimpleNamespace(config=config, corpus=corpus, proof=proof, report=tmp_path / "report.json",
        review=tmp_path / "review.json", state=config.config_dir / "nsi-route.json",
        out=tmp_path / "promotion.json", changelog=tmp_path / "CHANGELOG.md")


def report(evidence, monkeypatch, *, qualified=True):
    if qualified:
        original = nsi_eval.evaluate_typed

        def improved(raw):
            # Hypothetical fixed typed implementation, still label blind.
            result = original(raw)
            if result["readiness"] == "READY_FOR_RATIFICATION":
                result["readiness"] = "NEEDS_SPEC"
            return result

        monkeypatch.setattr(nsi_eval, "evaluate_typed", improved)
    return nsi_eval.run(evidence.corpus / "nsi6-manifest.json", evidence.report, evidence.proof)


def record_review(evidence, monkeypatch):
    answers = iter(["Chris", "test-only review reference", "Synthetic cutover qualification reviewed"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr(sys, "argv", ["nsi_rollout", "review", "--report", str(evidence.report), "--out", str(evidence.review)])
    nsi_rollout.main()
    return json.loads(evidence.review.read_bytes())


def reseal_report(path, mutate):
    document = json.loads(path.read_bytes())
    mutate(document)
    document.pop("report_sha256")
    document["report_sha256"] = sha256(document)
    path.write_text(json.dumps(document))


def test_unpromoted_route_uses_current_and_unsafe_report_cannot_promote(evidence, monkeypatch):
    assert nsi_route.route_mode(evidence.config) == "current"
    report(evidence, monkeypatch, qualified=False)
    with pytest.raises(ValueError, match="critical-omission"):
        nsi_rollout.promote(evidence.report, evidence.review, evidence.state, evidence.out)
    assert not evidence.state.exists()
    assert not evidence.out.exists()
    assert nsi_route.route_mode(evidence.config) == "current"


def test_qualified_review_promotes_and_rollback_restores_current(evidence, monkeypatch):
    report(evidence, monkeypatch)
    record_review(evidence, monkeypatch)
    evidence.changelog.write_text("# Changelog\n\n## [Unreleased]\n\n## [1.0]\nold release\n")
    monkeypatch.setattr(sys, "argv", ["nsi_rollout", "promote", "--report", str(evidence.report),
        "--review", str(evidence.review), "--state", str(evidence.state), "--out", str(evidence.out),
        "--changelog", str(evidence.changelog)])
    nsi_rollout.main()
    promoted = json.loads(evidence.state.read_bytes())
    assert nsi_route.route_mode(evidence.config) == "typed"
    assert "[#523](https://github.com/can1357/oh-my-pi/issues/523)" in evidence.changelog.read_text()
    assert evidence.changelog.read_text().endswith("## [1.0]\nold release\n")
    assert json.loads(evidence.out.read_bytes()) == json.loads(evidence.state.read_bytes())
    rollback_receipt = evidence.out.with_name("rollback.json")
    monkeypatch.setattr(sys, "argv", ["nsi_rollout", "rollback", "--state", str(evidence.state), "--out", str(rollback_receipt)])
    nsi_rollout.main()
    rolled_back = json.loads(rollback_receipt.read_bytes())
    assert rolled_back["prior_binding"] == promoted
    assert nsi_route.route_mode(evidence.config) == "current"
    assert not list(evidence.state.parent.glob(".*"))


def test_missing_or_changed_review_refuses_promotion_without_changelog(evidence, monkeypatch):
    report(evidence, monkeypatch)
    evidence.changelog.write_text("## [Unreleased]\n")
    with pytest.raises(FileNotFoundError):
        nsi_rollout.promote(evidence.report, evidence.review, evidence.state, evidence.out, evidence.changelog)
    record_review(evidence, monkeypatch)
    review = json.loads(evidence.review.read_bytes())
    review["reason"] = "changed after review"
    evidence.review.write_text(json.dumps(review))
    with pytest.raises(ValueError, match="review.*drift"):
        nsi_rollout.promote(evidence.report, evidence.review, evidence.state, evidence.out, evidence.changelog)
    assert evidence.changelog.read_text() == "## [Unreleased]\n"
    assert not evidence.state.exists()


@pytest.mark.parametrize("mutation", [
    lambda r: r["aggregate"]["typed"].update(semantic_edit_count=0),
    lambda r: r["cases"][0]["arms"]["typed"]["metrics"].pop("criterion_coverage"),
    lambda r: r["cases"][0]["arms"]["typed"]["observation"].update(status="skipped"),
    lambda r: r["cases"][0]["arms"]["current"]["observation"].update(status="crashed"),
    lambda r: r["cases"].pop(),
    lambda r: r.update(sealed=False),
    lambda r: next(row for row in r["cases"] if row["provenance"]["status"] != "skipped")["provenance"].update(status="crashed"),
    lambda r: r["cases"][0]["arms"]["typed"]["observation"]["raw_input"]["bounded"]["draft"].update(unknowns=[]),
    lambda r: r["rollback_proof"]["raw"].update(receipt="forged receipt"),
    lambda r: r["rollback_proof"]["raw"].update(verified=False),
    lambda r: r["pins"]["route_sources"].update({"packages/work-client/src/intake-nsi.ts": "0" * 64}),
])
def test_resealed_modified_evidence_cannot_promote(evidence, monkeypatch, mutation):
    report(evidence, monkeypatch)
    reseal_report(evidence.report, mutation)
    with pytest.raises((ValueError, KeyError)):
        nsi_rollout.promote(evidence.report, evidence.review, evidence.state, evidence.out)
    assert not evidence.state.exists()


@pytest.mark.parametrize("drift", ["code", "corpus", "report", "review", "malformed"])
def test_stale_typed_state_blocks_runtime_readiness(evidence, monkeypatch, drift):
    report(evidence, monkeypatch)
    record_review(evidence, monkeypatch)
    nsi_rollout.promote(evidence.report, evidence.review, evidence.state, evidence.out)
    if drift == "code":
        binding = nsi_route.implementation_binding()
        binding["packages/work-client/src/intake-nsi.ts"] = "0" * 64
        monkeypatch.setattr(nsi_route, "implementation_binding", lambda: binding)
    elif drift == "corpus":
        path = evidence.corpus / "inputs/cases.json"
        path.write_bytes(path.read_bytes() + b"\n")
    else:
        path = getattr(evidence, "state" if drift == "malformed" else drift)
        path.write_text("{}" if drift == "malformed" else path.read_text() + "\n")
    store = PostgresWorkStore(evidence.config)
    envelope = SimpleNamespace(command=SimpleNamespace(payload=SimpleNamespace(draft=None)))
    with pytest.raises(WorkStoreError, match="intake_not_ready"):
        store._assess_bounded_intake(envelope)
    draft = BoundedIntakeDraft.model_validate(json.loads((evidence.corpus / "inputs/cases.json").read_bytes())["cases"][0]["input"]["bounded"]["draft"])
    payload = SimpleNamespace(draft=draft, ratified_semantic_sha256=bounded_intake_semantic_sha256(draft))
    with pytest.raises(WorkStoreError, match="intake_not_ready"):
        store._publish_bounded_intake(None, SimpleNamespace(command=SimpleNamespace(payload=payload)), uuid4(), "owner")
    payload = SimpleNamespace(mission_id=uuid4(), intake=draft)
    with pytest.raises(WorkStoreError, match="intake_not_ready"):
        draft_mission_intake(None, SimpleNamespace(command=SimpleNamespace(payload=payload)), uuid4(), "owner", None, None, config=evidence.config)
    if drift != "malformed":
        nsi_rollout.rollback(evidence.state, evidence.out.with_name("rollback.json"))
        assert nsi_route.route_mode(evidence.config) == "current"


def test_review_binds_code_and_report_until_promotion(evidence, monkeypatch):
    report(evidence, monkeypatch)
    record_review(evidence, monkeypatch)
    evidence.report.write_bytes(evidence.report.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="review.*drift"):
        nsi_rollout.promote(evidence.report, evidence.review, evidence.state, evidence.out)
    assert not evidence.state.exists()


def test_review_requires_chris_reference_and_reason(evidence, monkeypatch):
    report(evidence, monkeypatch)
    answers = iter(["automation"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    with pytest.raises(ValueError, match="Chris review"):
        nsi_rollout.review(evidence.report, evidence.review)
    assert not evidence.review.exists()


def test_runtime_assessment_selects_typed_then_current(evidence, monkeypatch):
    draft = BoundedIntakeDraft.model_validate(json.loads((evidence.corpus / "inputs/cases.json").read_bytes())["cases"][0]["input"]["bounded"]["draft"])
    envelope = SimpleNamespace(command=SimpleNamespace(payload=SimpleNamespace(draft=draft)))
    store = PostgresWorkStore(evidence.config)
    report(evidence, monkeypatch)
    record_review(evidence, monkeypatch)
    nsi_rollout.promote(evidence.report, evidence.review, evidence.state, evidence.out)
    monkeypatch.setattr("omp_work.nsi_route.evaluate_typed_bounded_intake", lambda draft: ((), 1))
    monkeypatch.setattr("omp_work.nsi_route.evaluate_bounded_intake", lambda draft: ((), 0))
    assert store._assess_bounded_intake(envelope)["ready_for_ratification"] is False
    nsi_rollout.rollback(evidence.state, evidence.out.with_name("rollback.json"))
    assert store._assess_bounded_intake(envelope)["ready_for_ratification"] is True


def test_failed_atomic_replace_leaves_previous_route(evidence, monkeypatch):
    nsi_route.replace_state(evidence.state, nsi_route.seal_state(mode="current"))
    previous = evidence.state.read_bytes()

    def fail(source, destination):
        raise OSError("replace failed")

    monkeypatch.setattr(nsi_route.os, "replace", fail)
    with pytest.raises(OSError, match="replace failed"):
        nsi_route.replace_state(evidence.state, nsi_route.seal_state(mode="current", prior_binding={"test": True}))
    assert evidence.state.read_bytes() == previous
    assert not list(evidence.state.parent.glob(".*"))


def test_typed_runtime_preserves_uncapped_issue_count_and_capped_questions(evidence, monkeypatch):
    report(evidence, monkeypatch)
    record_review(evidence, monkeypatch)
    nsi_rollout.promote(evidence.report, evidence.review, evidence.state, evidence.out)
    case = next(case for case in json.loads((evidence.corpus / "inputs/cases.json").read_bytes())["cases"] if case["id"] == "ready-complete")
    raw = case["input"]["bounded"]["draft"]
    for criterion in raw["acceptance_criteria"]:
        criterion["oracle"] = None
    draft = BoundedIntakeDraft.model_validate(raw)
    envelope = SimpleNamespace(command=SimpleNamespace(payload=SimpleNamespace(draft=draft)))
    result = PostgresWorkStore(evidence.config)._assess_bounded_intake(envelope)
    assert result["ready_for_ratification"] is False
    assert result["issue_count"] == 4
    assert [question["claim_ids"] for question in result["questions"]] == [["ac-export"], ["ac-failure"]]


def test_status_cli_reports_unpromoted_and_refuses_malformed(evidence, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["nsi_rollout", "status", "--state", str(evidence.state)])
    nsi_rollout.main()
    assert json.loads(capsys.readouterr().out)["mode"] == "current"
    evidence.state.parent.mkdir()
    evidence.state.write_text("{broken")
    with pytest.raises(SystemExit) as refusal:
        nsi_rollout.main()
    assert refusal.value.code == 1
    assert "rollout refused" in capsys.readouterr().err


def test_report_drift_during_interactive_review_cannot_mint_approval(evidence, monkeypatch):
    report(evidence, monkeypatch)
    answers = iter(["Chris", "temporary reference", "temporary reason"])

    def answer(prompt):
        evidence.report.write_bytes(evidence.report.read_bytes() + b"\n")
        return next(answers)

    monkeypatch.setattr("builtins.input", answer)
    with pytest.raises(ValueError, match="drift during cutover"):
        nsi_rollout.review(evidence.report, evidence.review)
    assert not evidence.review.exists()

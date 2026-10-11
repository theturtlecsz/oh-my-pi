"""Transfer admission rejects altered methods before any search proposal."""

import hashlib
from dataclasses import replace

import pytest

from omp_work.research import mechanism, recursive_method
from omp_work.research.harness_release import package_candidate
from omp_work.research.recursive import seal_json
from omp_work.research.recursive_method import GeneratedPolicy, SOURCE_PATH, run_method
from omp_work.research.recursive_transfer_methods import FrozenMethod, freeze_pair
from test_research_recursive_method import generated, inputs, observation, tree


def pair():
    baseline, _, baseline_study, baseline_context, protocol = generated(0.8)
    candidate, _, study, context, _ = generated(0.5)
    return baseline, candidate, baseline_study, study, baseline_context, context, protocol


@pytest.mark.parametrize("arm", ["baseline", "candidate"])
@pytest.mark.parametrize("change,reason", [
    ("source", "changed_source"),
    ("recipe", "changed_recipe"),
    ("spec", "spec_changed"),
    ("recipe_sha256", "changed_digest"),
    ("source_sha256", "changed_digest"),
    ("artifact_sha256", "changed_digest"),
    ("failure", "generation_crashed"),
    ("missing", "changed_source"),
])
def test_altered_arm_refused_before_proposal(monkeypatch, arm, change, reason):
    baseline, candidate, _, study, _, _, _ = pair()
    result = baseline if arm == "baseline" else candidate
    if change == "source":
        # Even self-consistent attacker digests cannot admit another executable.
        source = b"def propose(steps, opened, *, seed=0, width=1):\n    return 'b', seed\n"
        result = replace(result, source_bytes=source,
                         source_sha256=hashlib.sha256(source).hexdigest(),
                         artifact_sha256=package_candidate({SOURCE_PATH: source})["bundle_sha256"])
    elif change == "recipe":
        result = replace(result, recipe_bytes=seal_json({"min_metric": 0.9}))
        if arm == "baseline":
            reason = "baseline_study_not_unique"
    elif change == "spec":
        result = replace(result, spec_bytes=seal_json({"replacement": True}))
    elif change == "failure":
        result = replace(result, failures=("generation_crashed",))
    elif change == "missing":
        result = replace(result, source_bytes=None)
    else:
        result = replace(result, **{change: "0" * 64})

    def proposal(*args, **kwargs):
        pytest.fail("invalid pair reached a proposal")

    monkeypatch.setattr(recursive_method.strategy_protocol, "run_candidate", proposal)
    with pytest.raises(ValueError, match=reason):
        freeze_pair(study, result if arm == "baseline" else baseline,
                    result if arm == "candidate" else candidate)


@pytest.mark.parametrize("arm", ["baseline", "candidate"])
@pytest.mark.parametrize("field,reason", [
    ("method_sha256", "method_changed"),
    ("lower_researcher_sha256", "lower_researcher_changed"),
    ("evaluator_component_sha256", "evaluator_component_changed"),
    ("evaluator_artifact_sha256", "evaluator_artifact_changed"),
    ("protocol_sha256", "protocol_changed"),
])
def test_changed_study_identity_refused(monkeypatch, arm, field, reason):
    baseline, candidate, baseline_study, study, _, _, _ = pair()
    if arm == "baseline":
        changed = baseline_study.model_copy(update={field: "0" * 64})
        baseline = replace(baseline, state=baseline.state.model_copy(update={"studies": (changed,)}))
    else:
        study = study.model_copy(update={field: "0" * 64})
    monkeypatch.setattr(recursive_method.strategy_protocol, "run_candidate",
                        lambda *args, **kwargs: pytest.fail("invalid pair reached a proposal"))
    with pytest.raises(ValueError, match=reason):
        freeze_pair(study, baseline, candidate)


def test_failed_generation_cannot_be_frozen(monkeypatch):
    baseline, _, _, _, _, _, _ = pair()
    study, recipe, spec, dev, context, _ = inputs()

    def crash(*args):
        raise RuntimeError("generator crashed")

    with monkeypatch.context() as patch:
        patch.setattr(mechanism, "generation_job", crash)
        failed = run_method(study, recipe, spec, dev, context=context)
    with pytest.raises(ValueError, match="generation_crashed"):
        freeze_pair(study, baseline, failed)


def test_valid_pair_preserves_studies_and_policy_selection(monkeypatch):
    baseline, candidate, baseline_study, study, baseline_context, context, protocol = pair()
    monkeypatch.setattr(recursive_method, "run_method",
                        lambda *args, **kwargs: pytest.fail("freezing regenerated a method"))
    frozen_baseline, frozen_candidate = freeze_pair(study, baseline, candidate)
    assert frozen_baseline.study == baseline_study
    assert frozen_baseline.study is baseline.state.studies[0]
    assert frozen_candidate.study is study
    for frozen, result, ctx, expected in (
        (frozen_baseline, baseline, baseline_context, ("b", 0)),
        (frozen_candidate, candidate, context, ("a1", 0)),
    ):
        observations = {"a": observation(frozen.study, protocol)}
        wrapped = GeneratedPolicy(frozen.result, frozen.study, context=ctx,
                                  protocol=protocol, observations=observations)
        plain = GeneratedPolicy(result, frozen.study, context=ctx,
                                protocol=protocol, observations=observations)
        assert wrapped(tree(), {"a"}) == plain(tree(), {"a"}) == expected


def test_direct_frozen_method_rejects_forged_source():
    result, _, study, _, _ = generated()
    with pytest.raises(ValueError, match="changed_source"):
        FrozenMethod(replace(result, source_bytes=b"forged"), study)

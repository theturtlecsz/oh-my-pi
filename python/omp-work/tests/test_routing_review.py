from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from omp_work.routing.policy import RoutingRefused, load_policy
from omp_work.routing.review import check_reviewer


def _write_policy(tmp_path: Path, data: dict) -> Path:
    target = tmp_path / "routing-policy.v1.json"
    target.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return target


def test_audit_accepts_its_listed_reviewer():
    assert check_reviewer(load_policy(), stage="audit", reviewer="openai-codex/gpt-5.6-sol:medium") is None


def test_acceptance_review_accepts_its_listed_reviewer():
    assert check_reviewer(load_policy(), stage="acceptance_review", reviewer="kimi-code/k3-256k") is None


def test_reviewer_absent_from_stage_list_is_refused():
    policy = load_policy()

    with pytest.raises(RoutingRefused) as unlisted:
        check_reviewer(policy, stage="audit", reviewer="gemini_flash/gemini-3.8-flash-high")
    assert unlisted.value.code == "reviewer_not_independent"

    with pytest.raises(RoutingRefused) as other_stage:
        check_reviewer(policy, stage="audit", reviewer="kimi-code/k3-256k")
    assert other_stage.value.code == "reviewer_not_independent"


def test_stage_without_reviewers_is_not_a_review_stage():
    policy = load_policy()

    with pytest.raises(RoutingRefused) as not_review:
        check_reviewer(policy, stage="implement", reviewer="openai-codex/gpt-5.6-sol:medium")
    assert not_review.value.code == "not_a_review_stage"

    with pytest.raises(RoutingRefused) as unknown:
        check_reviewer(policy, stage="no_such_stage", reviewer="openai-codex/gpt-5.6-sol:medium")
    assert unknown.value.code == "not_a_review_stage"


def test_reviewer_equal_to_maker_is_refused(tmp_path: Path):
    data = copy.deepcopy(load_policy().raw)
    data["stages"]["audit"]["reviewers"] = ["kimi-code/k3-256k"]
    policy = load_policy(_write_policy(tmp_path, data))

    assert check_reviewer(policy, stage="audit", reviewer="kimi-code/k3-256k", maker="openai-codex/gpt-5.6-sol") is None

    with pytest.raises(RoutingRefused) as exc:
        check_reviewer(policy, stage="audit", reviewer="kimi-code/k3-256k", maker="kimi-code/k3-256k")
    assert exc.value.code == "reviewer_is_maker"

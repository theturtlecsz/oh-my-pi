import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(ROOT))

from omp_work.contracts.r02.validate import (  # noqa: E402
    assert_execution_status_not_scientific_success,
    validate_instance,
)


def test_valid_campaign_trial_policy():
    campaign_payload = {
        "id": "camp-1",
        "revision": 0,
        "status": "draft",
        "hypothesis_ids": ["h1", "h2"],
        "resource_vector": {"cpu": 2.0, "mem_gb": 4.0, "gpu": 1.0},
        "policy_id": "pol-1",
    }
    validate_instance("campaign.schema.json", campaign_payload)

    trial_payload = {
        "id": "t1",
        "campaign_id": "camp-1",
        "revision": 1,
        "status": "queued",
        "evidence_ids": ["e1"],
        "native_candidate_id": "cand-99",
    }
    validate_instance("trial.schema.json", trial_payload)

    policy_payload = {
        "id": "p1",
        "revision": 0,
        "actions": ["execute", "observe"],
        "capabilities": ["gpu", "large-mem"],
        "compatibility": {"min_schema": "1.0", "max_schema": "2.0"},
    }
    validate_instance("policy.schema.json", policy_payload)


def test_valid_evidence_with_finite_metrics():
    evidence_payload = {
        "id": "e1",
        "kind": "metric",
        "content_digest": "sha256:deadbeef",
        "trust": "verified",
        "metrics": {"loss": 1.0},
    }
    validate_instance("evidence.schema.json", evidence_payload)


def test_missing_required_field():
    payload = {
        "id": "camp-1",
        # "revision" missing
        "status": "draft",
        "hypothesis_ids": ["h1"],
        "resource_vector": {"cpu": 1.0, "mem_gb": 2.0},
    }
    with pytest.raises(ValueError, match="revision"):
        validate_instance("campaign.schema.json", payload)


def test_nan_inf_metric():
    base_evidence = {
        "id": "e1",
        "kind": "metric",
        "content_digest": "sha256:deadbeef",
        "trust": "untrusted",
    }

    with pytest.raises(ValueError, match="loss"):
        validate_instance(
            "evidence.schema.json",
            {**base_evidence, "metrics": {"loss": math.nan}},
        )

    with pytest.raises(ValueError, match="loss"):
        validate_instance(
            "evidence.schema.json",
            {**base_evidence, "metrics": {"loss": math.inf}},
        )

    with pytest.raises(ValueError, match="loss"):
        validate_instance(
            "evidence.schema.json",
            {**base_evidence, "metrics": {"loss": -math.inf}},
        )


def test_negative_revision():
    payload = {
        "id": "camp-1",
        "revision": -1,
        "status": "draft",
        "hypothesis_ids": ["h1"],
        "resource_vector": {"cpu": 1.0, "mem_gb": 2.0},
    }
    with pytest.raises(ValueError, match="revision"):
        validate_instance("campaign.schema.json", payload)


def test_bad_status_enum():
    payload = {
        "id": "camp-1",
        "revision": 0,
        "status": "invalid_status",
        "hypothesis_ids": ["h1"],
        "resource_vector": {"cpu": 1.0, "mem_gb": 2.0},
    }
    with pytest.raises(ValueError, match="status"):
        validate_instance("campaign.schema.json", payload)


def test_wrong_type():
    payload = {
        "id": 12345,  # expected string
        "revision": 0,
        "status": "draft",
        "hypothesis_ids": ["h1"],
        "resource_vector": {"cpu": 1.0, "mem_gb": 2.0},
    }
    with pytest.raises(ValueError, match="id"):
        validate_instance("campaign.schema.json", payload)


def test_bool_as_integer():
    payload = {
        "id": "camp-1",
        "revision": True,  # bool not allowed as integer
        "status": "draft",
        "hypothesis_ids": ["h1"],
        "resource_vector": {"cpu": 1.0, "mem_gb": 2.0},
    }
    with pytest.raises(ValueError, match="revision"):
        validate_instance("campaign.schema.json", payload)


def test_forbidden_extra_field():
    payload = {
        "id": "camp-1",
        "revision": 0,
        "status": "draft",
        "hypothesis_ids": ["h1"],
        "resource_vector": {"cpu": 1.0, "mem_gb": 2.0},
        "extra_forbidden_field": "disallowed",
    }
    with pytest.raises(ValueError, match="extra_forbidden_field"):
        validate_instance("campaign.schema.json", payload)


def test_assert_execution_status_not_scientific_success():
    with pytest.raises(ValueError, match="scientific success"):
        assert_execution_status_not_scientific_success("succeeded", "untrusted")

    assert_execution_status_not_scientific_success("succeeded", "trusted")
    assert_execution_status_not_scientific_success("failed", "untrusted")

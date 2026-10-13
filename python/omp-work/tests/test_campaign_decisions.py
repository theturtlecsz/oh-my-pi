"""Tests for campaign_decisions pure codec (OMP-605-s02)."""

from __future__ import annotations

import json
import math
from uuid import UUID, uuid4

import pytest

from omp_work.jobs.campaign_decisions import (
    MAX_RATIONALE_LENGTH,
    MAX_REASON_LENGTH,
    MAX_STEP_ID_LENGTH,
    Decision,
    decision_id,
    decode,
    encode,
    format_rationale,
    observations_digest,
)


def _valid_hash() -> str:
    return observations_digest([])


def test_round_trip_stays_within_2048_with_max_length_id_and_rationale() -> None:
    max_id = "s" * MAX_STEP_ID_LENGTH
    max_rationale = "r" * MAX_RATIONALE_LENGTH
    d_max = Decision(
        index=0,
        policy="greedy",
        step_id=max_id,
        observations_count=0,
        observations_sha256=_valid_hash(),
        rationale=max_rationale,
    )

    encoded = encode(d_max)
    assert len(encoded) <= MAX_REASON_LENGTH
    decoded = decode(encoded)
    assert decoded == d_max

    # High index and count
    d_large = Decision(
        index=999999,
        policy="fixed",
        step_id=max_id,
        observations_count=999999,
        observations_sha256=_valid_hash(),
        rationale=max_rationale,
    )
    encoded_large = encode(d_large)
    assert len(encoded_large) <= MAX_REASON_LENGTH
    decoded_large = decode(encoded_large)
    assert decoded_large == d_large


def test_compact_sorted_json_adds_v1() -> None:
    d = Decision(
        index=0,
        policy="fixed",
        step_id="t0",
        observations_count=0,
        observations_sha256=_valid_hash(),
        rationale="node=t0;parent=none;metric=none",
    )
    encoded = encode(d)
    raw = json.loads(encoded)
    assert raw["v"] == 1
    assert d.v == 1

    # Verify keys are alphabetically sorted in JSON serialization
    keys = list(json.loads(encoded).keys())
    assert keys == sorted(keys)
    assert keys == [
        "index",
        "observations_count",
        "observations_sha256",
        "policy",
        "rationale",
        "step_id",
        "v",
    ]

    # Verify no unnecessary whitespace
    assert " " not in encoded
    assert "\n" not in encoded


def test_observations_digest_recomputes_identically() -> None:
    t1 = uuid4()
    t2 = uuid4()
    t3 = "trial_custom_3"
    pairs = [(t1, 0.85), (t2, None), (t3, 10)]

    digest_1 = observations_digest(pairs)
    digest_2 = observations_digest(pairs)
    assert digest_1 == digest_2
    assert len(digest_1) == 64
    assert all(c in "0123456789abcdef" for c in digest_1)

    # String version of UUIDs produces identical digest
    pairs_as_strings = [[str(t1), 0.85], [str(t2), None], [t3, 10]]
    assert observations_digest(pairs_as_strings) == digest_1


def test_observations_digest_changes_on_metric_order_and_trial_identity() -> None:
    t1 = uuid4()
    t2 = uuid4()
    t3 = uuid4()

    base_pairs = [(t1, 1.0), (t2, 2.0)]
    base_digest = observations_digest(base_pairs)

    # Changed trusted metric changes digest
    pairs_metric_changed = [(t1, 1.0), (t2, 2.1)]
    assert observations_digest(pairs_metric_changed) != base_digest

    pairs_metric_null = [(t1, 1.0), (t2, None)]
    assert observations_digest(pairs_metric_null) != base_digest

    # Changed order changes digest
    pairs_order_changed = [(t2, 2.0), (t1, 1.0)]
    assert observations_digest(pairs_order_changed) != base_digest

    # Changed trial identity changes digest
    pairs_trial_changed = [(t1, 1.0), (t3, 2.0)]
    assert observations_digest(pairs_trial_changed) != base_digest


def test_observations_digest_validation() -> None:
    t1 = uuid4()

    # Non-finite metrics
    for bad_metric in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError, match="finite"):
            observations_digest([(t1, bad_metric)])

    # Wrong metric types
    with pytest.raises(TypeError):
        observations_digest([(t1, True)])
    with pytest.raises(TypeError):
        observations_digest([(t1, "0.5")])

    # Wrong trial_id types or bounds
    with pytest.raises(ValueError, match="must have length 1.."):
        observations_digest([("", 1.0)])
    with pytest.raises(ValueError, match="must have length 1.."):
        observations_digest([("a" * 65, 1.0)])
    with pytest.raises(TypeError):
        observations_digest([(12345, 1.0)])

    # Wrong pairs structure
    with pytest.raises(TypeError):
        observations_digest(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        observations_digest("not-a-list")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        observations_digest([(t1,)])  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        observations_digest([(t1, 1.0, "extra")])  # type: ignore[arg-type]


def test_decode_legacy_text_returns_none() -> None:
    assert decode(None) is None
    assert decode("") is None
    assert decode("   ") is None
    assert decode("manual exploratory trial") is None
    assert decode("greedy search step picking node a1") is None
    assert decode("{broken json string") is None
    assert decode("42") is None
    assert decode('["array", "of", "values"]') is None
    # JSON dict without "v"
    assert decode('{"policy": "fixed", "step_id": "t0"}') is None
    assert decode('{"notes": "legacy reason format"}') is None


def test_decode_malformed_v1_fails() -> None:
    valid_hash = _valid_hash()
    base_dict = {
        "v": 1,
        "index": 0,
        "policy": "fixed",
        "step_id": "t0",
        "observations_count": 0,
        "observations_sha256": valid_hash,
        "rationale": "node=t0;parent=none;metric=none",
    }

    # Missing fields
    for field in ("index", "policy", "step_id", "observations_count", "observations_sha256", "rationale"):
        bad = dict(base_dict)
        del bad[field]
        with pytest.raises(ValueError):
            decode(json.dumps(bad))

    # Unsupported version
    bad_v = dict(base_dict, v=2)
    with pytest.raises(ValueError, match="unsupported decision record version"):
        decode(json.dumps(bad_v))

    bad_v_type = dict(base_dict, v="1")
    with pytest.raises(ValueError, match="unsupported decision record version"):
        decode(json.dumps(bad_v_type))

    # Extra fields
    bad_extra = dict(base_dict, extra="unexpected")
    with pytest.raises(ValueError, match="keys mismatch"):
        decode(json.dumps(bad_extra))

    # Negative index
    bad_neg_index = dict(base_dict, index=-1, observations_count=-1)
    with pytest.raises(ValueError, match="non-negative"):
        decode(json.dumps(bad_neg_index))

    # observations_count != index
    bad_count = dict(base_dict, observations_count=1)
    with pytest.raises(ValueError, match="must equal index"):
        decode(json.dumps(bad_count))

    # Invalid policy
    bad_policy = dict(base_dict, policy="beam")
    with pytest.raises(ValueError, match="policy must be 'fixed' or 'greedy'"):
        decode(json.dumps(bad_policy))

    # Invalid step_id
    bad_step_empty = dict(base_dict, step_id="")
    with pytest.raises(ValueError, match="step_id"):
        decode(json.dumps(bad_step_empty))

    bad_step_chars = dict(base_dict, step_id="invalid/step")
    with pytest.raises(ValueError, match="step_id"):
        decode(json.dumps(bad_step_chars))

    bad_step_len = dict(base_dict, step_id="s" * (MAX_STEP_ID_LENGTH + 1))
    with pytest.raises(ValueError, match="step_id"):
        decode(json.dumps(bad_step_len))

    # Invalid hash
    bad_hash = dict(base_dict, observations_sha256="not_a_64_hex_hash")
    with pytest.raises(ValueError, match="64-hex"):
        decode(json.dumps(bad_hash))

    bad_hash_case = dict(base_dict, observations_sha256="A" * 64)
    with pytest.raises(ValueError, match="64-hex"):
        decode(json.dumps(bad_hash_case))

    # Invalid rationale
    bad_rationale_empty = dict(base_dict, rationale="")
    with pytest.raises(ValueError, match="rationale must not be empty"):
        decode(json.dumps(bad_rationale_empty))

    bad_rationale_len = dict(base_dict, rationale="r" * (MAX_RATIONALE_LENGTH + 1))
    with pytest.raises(ValueError, match="rationale exceeds"):
        decode(json.dumps(bad_rationale_len))

    # Types validation
    bad_type_index = dict(base_dict, index="zero")
    with pytest.raises(TypeError):
        decode(json.dumps(bad_type_index))

    bad_bool_index = dict(base_dict, index=True, observations_count=1)
    with pytest.raises(TypeError):
        decode(json.dumps(bad_bool_index))


def test_decision_id_replays_per_campaign_and_no_collision_across_indices() -> None:
    c1 = uuid4()
    c2 = uuid4()

    # Replay for same campaign and index
    id_c1_0_a = decision_id(c1, 0)
    id_c1_0_b = decision_id(c1, 0)
    assert id_c1_0_a == id_c1_0_b
    assert isinstance(id_c1_0_a, UUID)

    # Accepts string campaign ID
    assert decision_id(str(c1), 0) == id_c1_0_a

    # No collision across indices
    indices = range(100)
    c1_ids = [decision_id(c1, i) for i in indices]
    assert len(set(c1_ids)) == 100

    # Different campaign has different decision IDs
    c2_ids = [decision_id(c2, i) for i in indices]
    assert set(c1_ids).isdisjoint(set(c2_ids))

    # Bounds validation
    with pytest.raises(ValueError, match="non-negative"):
        decision_id(c1, -1)
    with pytest.raises(TypeError):
        decision_id(c1, "0")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="must not be empty"):
        decision_id("", 0)
    with pytest.raises(TypeError):
        decision_id(1234, 0)  # type: ignore[arg-type]


def test_format_rationale() -> None:
    r1 = format_rationale("node1", parent="root", metric_basis=0.95)
    assert r1 == "node=node1;parent=root;metric=0.95"
    assert len(r1) <= MAX_RATIONALE_LENGTH

    r2 = format_rationale("root")
    assert r2 == "node=root;parent=none;metric=none"

    with pytest.raises(ValueError):
        format_rationale("")


def test_decision_frozen_immutability() -> None:
    d = Decision(
        index=0,
        policy="fixed",
        step_id="t0",
        observations_count=0,
        observations_sha256=_valid_hash(),
        rationale="node=t0;parent=none;metric=none",
    )
    with pytest.raises(AttributeError):
        d.index = 1  # type: ignore[misc]
    with pytest.raises(AttributeError):
        d.step_id = "t1"  # type: ignore[misc]


def test_encode_refuses_over_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    d = Decision(
        index=0,
        policy="fixed",
        step_id="t0",
        observations_count=0,
        observations_sha256=_valid_hash(),
        rationale="node=t0;parent=none;metric=none",
    )
    import omp_work.jobs.campaign_decisions as cd

    monkeypatch.setattr(cd, "MAX_REASON_LENGTH", 10)
    with pytest.raises(ValueError, match="exceeds 10 characters"):
        encode(d)


def test_observations_digest_excludes_observation_uuids_and_durations() -> None:
    trial_id = uuid4()
    trusted_metric = 42.5

    # Simulate two receipts with different observation UUIDs, durations, candidate-authored values
    receipt_a = {
        "observation_id": uuid4(),
        "trial_id": trial_id,
        "duration_ms": 1234,
        "candidate_authored_score": 999.0,
        "trusted_metric": trusted_metric,
    }
    receipt_b = {
        "observation_id": uuid4(),
        "trial_id": trial_id,
        "duration_ms": 5678,
        "candidate_authored_score": 0.0,
        "trusted_metric": trusted_metric,
    }

    pairs_a = [(receipt_a["trial_id"], receipt_a["trusted_metric"])]
    pairs_b = [(receipt_b["trial_id"], receipt_b["trusted_metric"])]

    assert observations_digest(pairs_a) == observations_digest(pairs_b)


def test_direct_decision_construction_validation() -> None:
    valid_hash = _valid_hash()

    with pytest.raises(ValueError, match="non-negative"):
        Decision(
            index=-1,
            policy="fixed",
            step_id="t0",
            observations_count=-1,
            observations_sha256=valid_hash,
            rationale="node=t0",
        )

    with pytest.raises(ValueError, match="must equal index"):
        Decision(
            index=0,
            policy="fixed",
            step_id="t0",
            observations_count=1,
            observations_sha256=valid_hash,
            rationale="node=t0",
        )

    with pytest.raises(ValueError, match="policy"):
        Decision(
            index=0,
            policy="invalid",  # type: ignore[arg-type]
            step_id="t0",
            observations_count=0,
            observations_sha256=valid_hash,
            rationale="node=t0",
        )

    with pytest.raises(ValueError, match="step_id"):
        Decision(
            index=0,
            policy="fixed",
            step_id="invalid/id",
            observations_count=0,
            observations_sha256=valid_hash,
            rationale="node=t0",
        )

    with pytest.raises(ValueError, match="64-hex"):
        Decision(
            index=0,
            policy="fixed",
            step_id="t0",
            observations_count=0,
            observations_sha256="bad-hash",
            rationale="node=t0",
        )

    with pytest.raises(ValueError, match="rationale"):
        Decision(
            index=0,
            policy="fixed",
            step_id="t0",
            observations_count=0,
            observations_sha256=valid_hash,
            rationale="r" * (MAX_RATIONALE_LENGTH + 1),
        )


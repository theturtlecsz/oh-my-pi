"""OMP-315-s03: R06 typed receipt, evaluation protocol, and verification.

These tests defend the observable contract the managed experiment path relies
on: the protocol is sealed by its canonical bytes, a receipt is only sound when
its bytes are canonical and every declared binding matches, and a qualified
receipt must carry its declared metrics, outputs, manifests, and a rejected
negative control. Each test names the failure mode a consumer would see.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from omp_work.research.receipt import (
    MANAGED_CAPABILITY,
    MANAGED_POLICY,
    TRIAL_RECEIPT_SCHEMA_VERSION,
    EvaluationProtocol,
    MetricSpec,
    ReceiptExpectation,
    TrialReceipt,
    load_protocol,
    managed_job_id,
    output_digest,
    qualifies,
    verify_receipt,
)

A = "a" * 64
B = "b" * 64
C = "c" * 64
D = "d" * 64
E = "e" * 64
F = "f" * 64
CONFIRMATION = "ab" * 32
OTHER = "cd" * 32


def protocol_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "contract_version": "research-protocol.v1",
        "metrics": [
            {"name": "score", "direction": "higher"},
            {"name": "loss", "direction": "lower"},
        ],
        "primary": "score",
        "tolerance": {"score": 0.05},
        "candidate_command": ["python", "candidate.py"],
        "evaluator_command": ["python", "evaluator.py"],
        "outputs": ["out/score.json", "out/log.txt"],
        "requires": ["python3"],
        "confirmation_sha256": CONFIRMATION,
        "timeout_seconds": 60,
    }
    payload.update(overrides)
    return payload


def default_protocol() -> EvaluationProtocol:
    return EvaluationProtocol.model_validate(protocol_payload())


def protocol_sha256(protocol: EvaluationProtocol) -> str:
    return hashlib.sha256(protocol.protocol_bytes()).hexdigest()


def receipt_payload(
    protocol: EvaluationProtocol | None = None, **overrides: object
) -> dict[str, object]:
    sealed = protocol or default_protocol()
    payload: dict[str, object] = {
        "schema_version": TRIAL_RECEIPT_SCHEMA_VERSION,
        "trial_id": "trial-1",
        "campaign_id": "camp-1",
        "job_id": managed_job_id("trial-1"),
        "candidate_digest": A,
        "evaluator_component_sha256": B,
        "evaluator_artifact_sha256": C,
        "protocol_sha256": protocol_sha256(sealed),
        "confirmation_sha256": sealed.confirmation_sha256,
        "candidate_manifest": D,
        "evaluator_manifest": E,
        "output_sha256": F,
        "negative_control": "rejected",
        "metrics": {"score": 0.9, "loss": 0.1},
        "verdict": "qualified",
        "failure_reason": None,
        "evidence": {
            "references": [
                {
                    "kind": "receipt",
                    "ref": "managed-trial:trial-1",
                    "result": "qualified",
                    "sha256": "11" * 32,
                }
            ]
        },
    }
    payload.update(overrides)
    return payload


def receipt_of(**overrides: object) -> TrialReceipt:
    return TrialReceipt.model_validate(receipt_payload(**overrides))


def receipt_bytes(receipt: TrialReceipt) -> bytes:
    return receipt.receipt_bytes()


def bytes_digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def expectation_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "trial_id": "trial-1",
        "campaign_id": "camp-1",
        "job_id": managed_job_id("trial-1"),
        "candidate_digest": A,
        "evaluator_component_sha256": B,
        "evaluator_artifact_sha256": C,
    }
    payload.update(overrides)
    return payload


def expectations(**overrides: object) -> ReceiptExpectation:
    return ReceiptExpectation.model_validate(expectation_payload(**overrides))


def verify_variant(
    overrides: dict[str, object],
    *,
    expected: ReceiptExpectation | None = None,
    protocol: EvaluationProtocol | None = None,
    with_protocol_sha256: bool = True,
) -> list[str]:
    """Verify a canonical receipt changed by ``overrides`` against its own digest."""
    sealed = protocol or default_protocol()
    receipt = TrialReceipt.model_validate(receipt_payload(sealed, **overrides))
    raw = receipt_bytes(receipt)
    kwargs: dict[str, object] = {
        "expected_sha256": bytes_digest(raw),
        "expected": expected if expected is not None else expectations(),
        "protocol": sealed,
    }
    if with_protocol_sha256:
        kwargs["protocol_sha256"] = protocol_sha256(sealed)
    return verify_receipt(raw, **kwargs)


def test_managed_constants_are_pinned() -> None:
    assert MANAGED_POLICY == "managed-experiment.v1"
    assert MANAGED_CAPABILITY == "research.managed_trial"
    assert managed_job_id("trial-1") == "managed-trial:trial-1"


def test_protocol_bytes_are_canonical_and_stable() -> None:
    protocol = default_protocol()
    raw = protocol.protocol_bytes()
    assert raw == protocol.protocol_bytes()
    assert json.loads(raw) == protocol.model_dump(mode="json")
    loaded = load_protocol(raw, protocol_sha256(protocol))
    assert loaded.protocol_bytes() == raw


def test_load_protocol_refuses_digest_mismatch_and_invalid() -> None:
    raw = default_protocol().protocol_bytes()
    with pytest.raises(ValueError, match="protocol digest mismatch"):
        load_protocol(raw, "0" * 64)

    invalid = protocol_payload(primary="missing")
    with pytest.raises(ValidationError):
        load_protocol(invalid)

    # NaN is not JSON; it must be refused rather than silently coerced.
    nan_tolerance = b'{"contract_version":"research-protocol.v1","metrics":[{"name":"score","direction":"higher"}],"primary":"score","tolerance":{"score":NaN},"candidate_command":["x"],"evaluator_command":["y"],"timeout_seconds":1}'
    with pytest.raises(ValueError):
        load_protocol(nan_tolerance)


@pytest.mark.parametrize(
    "overrides",
    [
        {"metrics": []},
        {"primary": "absent"},
        {"metrics": [{"name": "score", "direction": "higher"}, {"name": "score", "direction": "lower"}]},
        {"tolerance": {"score": 1.5}},
        {"tolerance": {"score": -0.1}},
        {"tolerance": {"absent": 0.5}},
        {"outputs": ["out/score.json", "out/score.json"]},
        {"outputs": ["../escape"]},
        {"outputs": ["/absolute"]},
        {"outputs": ["out//double"]},
        {"timeout_seconds": 0},
        {"candidate_command": []},
        {"evaluator_command": []},
    ],
)
def test_protocol_validation_refusals(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        EvaluationProtocol.model_validate(protocol_payload(**overrides))


def test_metric_spec_direction_is_closed() -> None:
    assert MetricSpec(name="score", direction="higher").direction == "higher"
    with pytest.raises(ValidationError):
        MetricSpec(name="score", direction="sideways")


def test_output_digest_is_order_independent_over_declared_paths() -> None:
    expected = hashlib.sha256(
        json.dumps(
            {
                "out/log.txt": hashlib.sha256(b"log").hexdigest(),
                "out/score.json": hashlib.sha256(b"payload").hexdigest(),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    assert output_digest({"out/score.json": b"payload", "out/log.txt": b"log"}) == expected
    assert output_digest({"out/log.txt": b"log", "out/score.json": b"payload"}) == expected


def test_receipt_expectation_is_the_first_six_receipt_fields() -> None:
    assert list(ReceiptExpectation.model_fields) == [
        "trial_id",
        "campaign_id",
        "job_id",
        "candidate_digest",
        "evaluator_component_sha256",
        "evaluator_artifact_sha256",
    ]


def test_valid_receipt_is_sound_and_qualifies() -> None:
    protocol = default_protocol()
    raw = receipt_bytes(receipt_of())
    reasons = verify_receipt(
        raw,
        expected_sha256=bytes_digest(raw),
        expected=expectations(),
        protocol=protocol,
        protocol_sha256=protocol_sha256(protocol),
    )
    assert reasons == []
    assert qualifies(TrialReceipt.model_validate(json.loads(raw)), reasons) is True


@pytest.mark.parametrize(
    ("reason", "overrides"),
    [
        ("stale_receipt", {"schema_version": 2}),
        ("wrong_trial", {"trial_id": "trial-2"}),
        ("wrong_campaign", {"campaign_id": "camp-2"}),
        ("wrong_job", {"job_id": "managed-trial:trial-2"}),
        ("wrong_candidate", {"candidate_digest": OTHER}),
        ("changed_evaluator", {"evaluator_component_sha256": OTHER}),
        ("changed_evaluator", {"evaluator_artifact_sha256": OTHER}),
        ("stale_protocol", {"protocol_sha256": "12" * 32}),
        ("wrong_confirmation", {"confirmation_sha256": OTHER}),
        ("undeclared_metric", {"metrics": {"score": 0.9, "loss": 0.1, "hidden": 1.0}}),
        ("missing_metric", {"metrics": {"score": 0.9}}),
        ("missing_outputs", {"output_sha256": None}),
        ("missing_manifest", {"candidate_manifest": None}),
        ("missing_manifest", {"evaluator_manifest": None}),
        ("negative_control_accepted", {"negative_control": "accepted"}),
    ],
)
def test_one_field_change_produces_exactly_that_reason(
    reason: str, overrides: dict[str, object]
) -> None:
    assert verify_variant(overrides) == [reason]


def test_receipt_digest_mismatch_is_that_reason_alone() -> None:
    protocol = default_protocol()
    raw = receipt_bytes(receipt_of())
    reasons = verify_receipt(
        raw,
        expected_sha256="0" * 64,
        expected=expectations(),
        protocol=protocol,
        protocol_sha256=protocol_sha256(protocol),
    )
    assert reasons == ["receipt_digest_mismatch"]


def test_failed_verdicts_verify_without_reasons_and_do_not_qualify() -> None:
    for verdict, reason in (
        ("candidate_failed", "candidate regressed the primary metric"),
        ("evaluator_failed", "evaluator harness crashed"),
    ):
        overrides = {
            "verdict": verdict,
            "failure_reason": reason,
            "schema_version": TRIAL_RECEIPT_SCHEMA_VERSION,
            "candidate_manifest": None,
            "evaluator_manifest": None,
            "output_sha256": None,
            "negative_control": "accepted",
            "metrics": {},
        }
        protocol = default_protocol()
        receipt = TrialReceipt.model_validate(receipt_payload(protocol, **overrides))
        raw = receipt_bytes(receipt)
        reasons = verify_receipt(
            raw,
            expected_sha256=bytes_digest(raw),
            expected=expectations(),
            protocol=protocol,
            protocol_sha256=protocol_sha256(protocol),
        )
        assert reasons == [], (verdict, reasons)
        assert qualifies(receipt, reasons) is False


def test_non_canonical_bytes_are_malformed() -> None:
    receipt = receipt_of()
    canonical = receipt_bytes(receipt)
    spaced = json.dumps(receipt.model_dump(mode="json"), indent=2).encode()
    assert spaced != canonical
    assert verify_receipt(spaced) == ["receipt_malformed"]
    assert verify_receipt(b"{not json") == ["receipt_malformed"]


@pytest.mark.parametrize("literal", [b"NaN", b"Infinity", b"-Infinity"])
def test_non_finite_metric_bytes_are_malformed(literal: bytes) -> None:
    payload = receipt_payload()
    payload["metrics"] = {"score": 0.9, "loss": 0.1}
    body = json.dumps(payload).encode()
    poisoned = body.replace(b"0.1", literal)
    assert literal in poisoned
    assert verify_receipt(poisoned) == ["receipt_malformed"]


def test_receipt_model_refuses_non_finite_and_malformed_fields() -> None:
    with pytest.raises(ValidationError):
        TrialReceipt.model_validate(receipt_payload(metrics={"score": float("nan")}))
    with pytest.raises(ValidationError):
        TrialReceipt.model_validate(receipt_payload(metrics={"score": float("inf")}))
    with pytest.raises(ValidationError):
        TrialReceipt.model_validate(receipt_payload(candidate_digest="nothex"))
    with pytest.raises(ValidationError):
        TrialReceipt.model_validate(receipt_payload(unknown_field="x"))
    with pytest.raises(ValidationError):
        TrialReceipt.model_validate(
            receipt_payload(verdict="qualified", failure_reason="should not be here")
        )
    with pytest.raises(ValidationError):
        TrialReceipt.model_validate(receipt_payload(verdict="maybe"))


def test_protocol_sha256_is_optional_but_checked_when_given() -> None:
    protocol = default_protocol()
    raw = receipt_bytes(receipt_of())
    assert (
        verify_receipt(raw, expected_sha256=bytes_digest(raw), protocol=protocol) == []
    )
    assert verify_receipt(
        raw,
        expected_sha256=bytes_digest(raw),
        protocol=protocol,
        protocol_sha256="0" * 64,
    ) == ["stale_protocol"]

"""Tests for the independent-verifier verdict signature (OMP-417, lock 6)."""

from __future__ import annotations

import pytest
from omp_work.orchestrator.verifier import accept_candidate, sign_verdict

KEY = b"control-plane-and-verifier-key"
OTHER_KEY = b"a-different-verifier-key"
CANDIDATE = "a" * 64
ATTEMPT = "attempt-001"
PRODUCER = "producer-worker-0"
VERIFIER = "verifier-worker-1"


def test_signed_pass_accepted() -> None:
    record = sign_verdict(
        KEY,
        candidate_sha256=CANDIDATE,
        attempt_id=ATTEMPT,
        verdict="pass",
        verifier_id=VERIFIER,
    )
    assert record["candidate_sha256"] == CANDIDATE
    assert record["attempt_id"] == ATTEMPT
    assert record["verdict"] == "pass"
    assert record["verifier_id"] == VERIFIER
    assert isinstance(record["signature"], str) and len(record["signature"]) == 64

    assert accept_candidate(
        KEY,
        record,
        candidate_sha256=CANDIDATE,
        attempt_id=ATTEMPT,
        producer_id=PRODUCER,
    ) is True


def test_record_none_refused() -> None:
    assert accept_candidate(
        KEY,
        None,
        candidate_sha256=CANDIDATE,
        attempt_id=ATTEMPT,
        producer_id=PRODUCER,
    ) is False


@pytest.mark.parametrize(
    "missing_field",
    ["candidate_sha256", "attempt_id", "verdict", "verifier_id", "signature"],
)
def test_missing_field_refused(missing_field: str) -> None:
    record = sign_verdict(
        KEY,
        candidate_sha256=CANDIDATE,
        attempt_id=ATTEMPT,
        verdict="pass",
        verifier_id=VERIFIER,
    )
    del record[missing_field]
    assert accept_candidate(
        KEY,
        record,
        candidate_sha256=CANDIDATE,
        attempt_id=ATTEMPT,
        producer_id=PRODUCER,
    ) is False


@pytest.mark.parametrize(
    "bad_field",
    ["candidate_sha256", "attempt_id", "verdict", "verifier_id", "signature"],
)
def test_non_string_field_refused(bad_field: str) -> None:
    record: dict[str, object] = sign_verdict(
        KEY,
        candidate_sha256=CANDIDATE,
        attempt_id=ATTEMPT,
        verdict="pass",
        verifier_id=VERIFIER,
    )
    record[bad_field] = 12345
    assert accept_candidate(
        KEY,
        record,
        candidate_sha256=CANDIDATE,
        attempt_id=ATTEMPT,
        producer_id=PRODUCER,
    ) is False


def test_signed_fail_verdict_refused() -> None:
    record = sign_verdict(
        KEY,
        candidate_sha256=CANDIDATE,
        attempt_id=ATTEMPT,
        verdict="fail",
        verifier_id=VERIFIER,
    )
    assert accept_candidate(
        KEY,
        record,
        candidate_sha256=CANDIDATE,
        attempt_id=ATTEMPT,
        producer_id=PRODUCER,
    ) is False


def test_signed_with_another_key_refused() -> None:
    record = sign_verdict(
        OTHER_KEY,
        candidate_sha256=CANDIDATE,
        attempt_id=ATTEMPT,
        verdict="pass",
        verifier_id=VERIFIER,
    )
    assert accept_candidate(
        KEY,
        record,
        candidate_sha256=CANDIDATE,
        attempt_id=ATTEMPT,
        producer_id=PRODUCER,
    ) is False


def test_same_verifier_and_producer_refused() -> None:
    record = sign_verdict(
        KEY,
        candidate_sha256=CANDIDATE,
        attempt_id=ATTEMPT,
        verdict="pass",
        verifier_id=PRODUCER,
    )
    assert accept_candidate(
        KEY,
        record,
        candidate_sha256=CANDIDATE,
        attempt_id=ATTEMPT,
        producer_id=PRODUCER,
    ) is False


def test_presented_for_another_candidate_sha256_refused() -> None:
    record = sign_verdict(
        KEY,
        candidate_sha256=CANDIDATE,
        attempt_id=ATTEMPT,
        verdict="pass",
        verifier_id=VERIFIER,
    )
    assert accept_candidate(
        KEY,
        record,
        candidate_sha256="b" * 64,
        attempt_id=ATTEMPT,
        producer_id=PRODUCER,
    ) is False


def test_presented_for_another_attempt_id_refused() -> None:
    record = sign_verdict(
        KEY,
        candidate_sha256=CANDIDATE,
        attempt_id=ATTEMPT,
        verdict="pass",
        verifier_id=VERIFIER,
    )
    assert accept_candidate(
        KEY,
        record,
        candidate_sha256=CANDIDATE,
        attempt_id="attempt-999",
        producer_id=PRODUCER,
    ) is False


def test_tampered_verdict_field_refused() -> None:
    record = sign_verdict(
        KEY,
        candidate_sha256=CANDIDATE,
        attempt_id=ATTEMPT,
        verdict="pass",
        verifier_id=VERIFIER,
    )
    record["verdict"] = "tampered"
    assert accept_candidate(
        KEY,
        record,
        candidate_sha256=CANDIDATE,
        attempt_id=ATTEMPT,
        producer_id=PRODUCER,
    ) is False


def test_tampered_verifier_id_field_refused() -> None:
    record = sign_verdict(
        KEY,
        candidate_sha256=CANDIDATE,
        attempt_id=ATTEMPT,
        verdict="pass",
        verifier_id=VERIFIER,
    )
    record["verifier_id"] = "impostor-verifier"
    assert accept_candidate(
        KEY,
        record,
        candidate_sha256=CANDIDATE,
        attempt_id=ATTEMPT,
        producer_id=PRODUCER,
    ) is False


def test_tampered_signature_refused() -> None:
    record = sign_verdict(
        KEY,
        candidate_sha256=CANDIDATE,
        attempt_id=ATTEMPT,
        verdict="pass",
        verifier_id=VERIFIER,
    )
    record["signature"] = "0" * 64
    assert accept_candidate(
        KEY,
        record,
        candidate_sha256=CANDIDATE,
        attempt_id=ATTEMPT,
        producer_id=PRODUCER,
    ) is False


def test_non_ascii_signature_refused() -> None:
    record = sign_verdict(
        KEY,
        candidate_sha256=CANDIDATE,
        attempt_id=ATTEMPT,
        verdict="pass",
        verifier_id=VERIFIER,
    )
    record["signature"] = "invalid-\u1234"
    assert accept_candidate(
        KEY,
        record,
        candidate_sha256=CANDIDATE,
        attempt_id=ATTEMPT,
        producer_id=PRODUCER,
    ) is False


def test_empty_key_refused_by_accept() -> None:
    record = sign_verdict(
        KEY,
        candidate_sha256=CANDIDATE,
        attempt_id=ATTEMPT,
        verdict="pass",
        verifier_id=VERIFIER,
    )
    assert accept_candidate(
        b"",
        record,
        candidate_sha256=CANDIDATE,
        attempt_id=ATTEMPT,
        producer_id=PRODUCER,
    ) is False


@pytest.mark.parametrize(
    "malformed",
    [42, "not a mapping", [], object(), {}],
)
def test_malformed_record_never_raises(malformed: object) -> None:
    assert accept_candidate(
        KEY,
        malformed,  # type: ignore[arg-type]
        candidate_sha256=CANDIDATE,
        attempt_id=ATTEMPT,
        producer_id=PRODUCER,
    ) is False


def test_sign_verdict_rejects_empty_key() -> None:
    with pytest.raises(ValueError, match="key must not be empty"):
        sign_verdict(
            b"",
            candidate_sha256=CANDIDATE,
            attempt_id=ATTEMPT,
            verdict="pass",
            verifier_id=VERIFIER,
        )


@pytest.mark.parametrize("invalid_verdict", ["", "PASS", "FAIL", "pending", "unknown"])
def test_sign_verdict_rejects_unknown_verdict(invalid_verdict: str) -> None:
    with pytest.raises(ValueError, match="verdict must be 'pass' or 'fail'"):
        sign_verdict(
            KEY,
            candidate_sha256=CANDIDATE,
            attempt_id=ATTEMPT,
            verdict=invalid_verdict,
            verifier_id=VERIFIER,
        )

"""Independent-verifier verdict signature (OMP-417, lock 6).

The verifier signs its verdict with an HMAC key only it and the control plane
hold, and records the candidate and attempt it judged. ``accept_candidate``
accepts a candidate only when the record is a verified pass for exactly this
candidate and attempt, signed by a verifier that is not the producer.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import hmac
import json

__all__ = [
    "accept_candidate",
    "sign_verdict",
]

_FOUR_FIELDS = ("candidate_sha256", "attempt_id", "verdict", "verifier_id")
_FIVE_FIELDS = (*_FOUR_FIELDS, "signature")


def _compute_signature(
    key: bytes,
    *,
    candidate_sha256: str,
    attempt_id: str,
    verdict: str,
    verifier_id: str,
) -> str:
    payload = {
        "candidate_sha256": candidate_sha256,
        "attempt_id": attempt_id,
        "verdict": verdict,
        "verifier_id": verifier_id,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(key, raw, hashlib.sha256).hexdigest()


def sign_verdict(
    key: bytes,
    *,
    candidate_sha256: str,
    attempt_id: str,
    verdict: str,
    verifier_id: str,
) -> dict[str, str]:
    """Sign one verdict, binding candidate, attempt, verifier, and verdict."""
    if not key:
        raise ValueError("key must not be empty")
    if verdict not in ("pass", "fail"):
        raise ValueError(f"verdict must be 'pass' or 'fail', got {verdict!r}")
    signature = _compute_signature(
        key,
        candidate_sha256=candidate_sha256,
        attempt_id=attempt_id,
        verdict=verdict,
        verifier_id=verifier_id,
    )
    return {
        "candidate_sha256": candidate_sha256,
        "attempt_id": attempt_id,
        "verdict": verdict,
        "verifier_id": verifier_id,
        "signature": signature,
    }


def accept_candidate(
    key: bytes,
    record: Mapping[str, object] | None,
    *,
    candidate_sha256: str,
    attempt_id: str,
    producer_id: str,
) -> bool:
    """True only when ``record`` is a valid signed pass for this candidate and attempt."""
    if not isinstance(key, (bytes, bytearray)) or not key:
        return False
    if not isinstance(record, Mapping):
        return False
    for field in _FIVE_FIELDS:
        if not isinstance(record.get(field), str):
            return False
    if record["verdict"] != "pass":
        return False
    if record["candidate_sha256"] != candidate_sha256:
        return False
    if record["attempt_id"] != attempt_id:
        return False
    if record["verifier_id"] == producer_id:
        return False
    expected = _compute_signature(
        key,
        candidate_sha256=record["candidate_sha256"],
        attempt_id=record["attempt_id"],
        verdict=record["verdict"],
        verifier_id=record["verifier_id"],
    )
    try:
        return hmac.compare_digest(record["signature"], expected)
    except TypeError:
        return False

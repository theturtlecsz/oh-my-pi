"""Decision codec for research campaign trial proposals (OMP-605).

Provides a pure codec for encoding and decoding ProposeResearchTrialPayload.reason,
bounded by the contract's 2048-character limit. Compact sorted JSON format adds v=1.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
import json
import math
import re
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from omp_work.v1.canonical import sha256

__all__ = [
    "Decision",
    "Policy",
    "decision_id",
    "decode",
    "encode",
    "format_rationale",
    "observations_digest",
]

Policy = Literal["fixed", "greedy"]

MAX_REASON_LENGTH = 2048
MAX_RATIONALE_LENGTH = 256
MAX_STEP_ID_LENGTH = 64
MAX_TRIAL_ID_LENGTH = 64

_VALID_POLICIES = frozenset({"fixed", "greedy"})
_STEP_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_V1_KEYS = frozenset(
    {
        "index",
        "observations_count",
        "observations_sha256",
        "policy",
        "rationale",
        "step_id",
        "v",
    }
)


def _validate_decision_fields(
    *,
    index: int,
    policy: str,
    step_id: str,
    observations_count: int,
    observations_sha256: str,
    rationale: str,
) -> None:
    if isinstance(index, bool) or not isinstance(index, int):
        raise TypeError(f"index must be an int, got {type(index).__name__}")
    if index < 0:
        raise ValueError(f"index must be non-negative, got {index}")

    if not isinstance(policy, str):
        raise TypeError(f"policy must be a str, got {type(policy).__name__}")
    if policy not in _VALID_POLICIES:
        raise ValueError(f"policy must be 'fixed' or 'greedy', got {policy!r}")

    if not isinstance(step_id, str):
        raise TypeError(f"step_id must be a str, got {type(step_id).__name__}")
    if not _STEP_ID_PATTERN.fullmatch(step_id):
        raise ValueError(f"step_id does not match bounds or allowed characters: {step_id!r}")

    if isinstance(observations_count, bool) or not isinstance(observations_count, int):
        raise TypeError(f"observations_count must be an int, got {type(observations_count).__name__}")
    if observations_count < 0:
        raise ValueError(f"observations_count must be non-negative, got {observations_count}")
    if observations_count != index:
        raise ValueError(f"observations_count must equal index: {observations_count} != {index}")

    if not isinstance(observations_sha256, str):
        raise TypeError(f"observations_sha256 must be a str, got {type(observations_sha256).__name__}")
    if not _SHA256_PATTERN.fullmatch(observations_sha256):
        raise ValueError(f"observations_sha256 must be a 64-hex string, got {observations_sha256!r}")

    if not isinstance(rationale, str):
        raise TypeError(f"rationale must be a str, got {type(rationale).__name__}")
    if len(rationale) == 0:
        raise ValueError("rationale must not be empty")
    if len(rationale) > MAX_RATIONALE_LENGTH:
        raise ValueError(f"rationale exceeds {MAX_RATIONALE_LENGTH} characters: {len(rationale)}")


@dataclass(frozen=True, slots=True)
class Decision:
    """Frozen decision record representing one trial selection in an adaptive campaign."""

    index: int
    policy: Policy
    step_id: str
    observations_count: int
    observations_sha256: str
    rationale: str

    def __post_init__(self) -> None:
        _validate_decision_fields(
            index=self.index,
            policy=self.policy,
            step_id=self.step_id,
            observations_count=self.observations_count,
            observations_sha256=self.observations_sha256,
            rationale=self.rationale,
        )

    @property
    def v(self) -> int:
        return 1


def format_rationale(
    node: str,
    *,
    parent: str | None = None,
    metric_basis: object = None,
) -> str:
    """Format a rationale string naming chosen node, parent, and metric basis."""
    if not isinstance(node, str) or not node:
        raise ValueError("node must be a non-empty string")
    p_str = "none" if parent is None else parent
    m_str = "none" if metric_basis is None else str(metric_basis)
    res = f"node={node};parent={p_str};metric={m_str}"
    if len(res) > MAX_RATIONALE_LENGTH:
        raise ValueError(f"rationale exceeds {MAX_RATIONALE_LENGTH} characters: {len(res)}")
    return res


def encode(decision: Decision) -> str:
    """Encode a Decision into a compact sorted JSON string adding v=1.

    Refuses over the 2048-character limit.
    """
    if not isinstance(decision, Decision):
        raise TypeError(f"expected Decision instance, got {type(decision).__name__}")

    payload = {
        "index": decision.index,
        "observations_count": decision.observations_count,
        "observations_sha256": decision.observations_sha256,
        "policy": decision.policy,
        "rationale": decision.rationale,
        "step_id": decision.step_id,
        "v": 1,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if len(encoded) > MAX_REASON_LENGTH:
        raise ValueError(f"encoded decision exceeds {MAX_REASON_LENGTH} characters ({len(encoded)} > {MAX_REASON_LENGTH})")
    return encoded


def decode(reason: str | None) -> Decision | None:
    """Decode a reason string into a Decision.

    Returns None for absent records or legacy text.
    Raises ValueError / TypeError for malformed v1 records.
    """
    if reason is None or not isinstance(reason, str) or not reason.strip():
        return None

    try:
        data = json.loads(reason)
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(data, dict):
        return None

    if "v" not in data:
        return None

    if data["v"] != 1 or isinstance(data["v"], bool):
        raise ValueError(f"unsupported decision record version: {data['v']!r}")

    if len(reason) > MAX_REASON_LENGTH:
        raise ValueError(f"v1 decision record exceeds {MAX_REASON_LENGTH} characters ({len(reason)} > {MAX_REASON_LENGTH})")

    if set(data.keys()) != _V1_KEYS:
        raise ValueError(f"malformed v1 decision record: keys mismatch: {set(data.keys())}")

    return Decision(
        index=data["index"],
        policy=data["policy"],
        step_id=data["step_id"],
        observations_count=data["observations_count"],
        observations_sha256=data["observations_sha256"],
        rationale=data["rationale"],
    )


def observations_digest(pairs: Iterable[Sequence[object]] | Iterable[tuple[object, object]]) -> str:
    """Compute canonical sha256 of ordered [[str(trial_id), trusted_metric_or_null], ...].

    Inputs come from settled verified receipts; excludes observation UUIDs,
    durations and candidate-authored values. Digest is campaign-local.
    """
    if pairs is None:
        raise TypeError("pairs must not be None")
    if not hasattr(pairs, "__iter__"):
        raise TypeError("pairs must be iterable")

    canonical_pairs: list[list[Any]] = []
    for idx, item in enumerate(pairs):
        if not isinstance(item, (tuple, list)):
            raise TypeError(f"item at index {idx} must be a (trial_id, metric) sequence")
        if len(item) != 2:
            raise ValueError(f"item at index {idx} must have length 2 (trial_id, metric)")

        trial_id, metric = item

        if not isinstance(trial_id, (str, UUID)):
            raise TypeError(f"trial_id at index {idx} must be a str or UUID, got {type(trial_id).__name__}")
        trial_id_str = str(trial_id).strip()
        if not trial_id_str or len(trial_id_str) > MAX_TRIAL_ID_LENGTH:
            raise ValueError(f"trial_id at index {idx} must have length 1..{MAX_TRIAL_ID_LENGTH}, got {len(trial_id_str)}")

        if metric is not None:
            if isinstance(metric, bool) or not isinstance(metric, (int, float)):
                raise TypeError(f"metric at index {idx} must be int, float, or None, got {type(metric).__name__}")
            if not math.isfinite(metric):
                raise ValueError(f"metric at index {idx} must be finite, got {metric}")

        canonical_pairs.append([trial_id_str, metric])

    return sha256(canonical_pairs)


def decision_id(campaign_id: UUID | str, index: int) -> UUID:
    """Derive deterministic decision UUID using uuid5(NAMESPACE_URL, f"omp-campaign:{campaign_id}:decision:{index}")."""
    if isinstance(index, bool) or not isinstance(index, int):
        raise TypeError(f"index must be an int, got {type(index).__name__}")
    if index < 0:
        raise ValueError(f"index must be non-negative, got {index}")
    if not isinstance(campaign_id, (str, UUID)):
        raise TypeError(f"campaign_id must be a UUID or str, got {type(campaign_id).__name__}")
    cid_str = str(campaign_id).strip()
    if not cid_str:
        raise ValueError("campaign_id must not be empty")

    return uuid5(NAMESPACE_URL, f"omp-campaign:{cid_str}:decision:{index}")

"""R06 typed evaluation protocol and trusted trial receipt (OMP-315).

Pure, database-free models for the managed experiment path: a sealed
evaluation protocol, the trial receipt an evaluator issues, and the
verification that rejects a receipt which does not bind the expected trial,
candidate, evaluator, protocol, confirmation data, and outputs.

Canonical bytes use the repository-wide canonical JSON (`v1.canonical`), so a
receipt digest is reproducible across the writer and the verifier. Metrics are
finite floats only; NaN and infinity are refused at parse time and by the
model. ``verify_receipt`` returns an ordered, de-duplicated reason list; an
empty list is a sound receipt.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import Field, ValidationError, field_validator, model_validator

from omp_work.v1.canonical import canonical_json, sha256
from omp_work.v1.models import EvidenceManifest, StrictModel

__all__ = [
    "MANAGED_CAPABILITY",
    "MANAGED_POLICY",
    "TRIAL_RECEIPT_SCHEMA_VERSION",
    "EvaluationProtocol",
    "MetricSpec",
    "ReceiptExpectation",
    "TrialReceipt",
    "load_protocol",
    "managed_job_id",
    "output_digest",
    "qualifies",
    "verify_receipt",
]

MANAGED_POLICY = "managed-experiment.v1"
MANAGED_CAPABILITY = "research.managed_trial"
TRIAL_RECEIPT_SCHEMA_VERSION = 1

_Hex64 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
_Direction = Literal["lower", "higher"]
_Verdict = Literal["qualified", "candidate_failed", "evaluator_failed"]
_NegativeControl = Literal["rejected", "accepted"]


def managed_job_id(trial_id: object) -> str:
    return f"managed-trial:{trial_id}"


def _reject_constant(token: str) -> None:
    raise ValueError(f"non-finite JSON constant not allowed: {token}")


def _no_control(value: str) -> str:
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ValueError("value contains control characters")
    return value


def _argv(value: tuple[str, ...]) -> tuple[str, ...]:
    if not value:
        raise ValueError("command argv must not be empty")
    for argument in value:
        if not argument:
            raise ValueError("command argv entries must not be empty")
        _no_control(argument)
    return value


def _relative_output(value: str) -> str:
    if not value or value.startswith(("/", "\\")) or "\\" in value:
        raise ValueError(f"output path must be relative: {value!r}")
    if value.startswith("./") or value.endswith("/") or "//" in value:
        raise ValueError(f"output path is not canonical: {value!r}")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"output path must not contain . or .. segments: {value!r}")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ValueError(f"output path contains control characters: {value!r}")
    return value


class MetricSpec(StrictModel):
    name: str = Field(min_length=1, max_length=255)
    direction: _Direction

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("metric name must not have surrounding whitespace")
        return _no_control(value)


class EvaluationProtocol(StrictModel):
    """Sealed evaluation protocol. Its canonical bytes are its identity."""

    contract_version: Literal["research-protocol.v1"] = "research-protocol.v1"
    metrics: tuple[MetricSpec, ...]
    primary: str = Field(min_length=1)
    tolerance: dict[str, float] = Field(default_factory=dict)
    candidate_command: tuple[str, ...]
    evaluator_command: tuple[str, ...]
    outputs: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    confirmation_sha256: _Hex64 | None = None
    timeout_seconds: int = Field(gt=0)

    @field_validator("candidate_command")
    @classmethod
    def validate_candidate_command(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _argv(value)

    @field_validator("evaluator_command")
    @classmethod
    def validate_evaluator_command(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _argv(value)

    @field_validator("requires")
    @classmethod
    def validate_requires(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for name in value:
            if not name or name != name.strip():
                raise ValueError("required executable must be a non-empty name")
            _no_control(name)
        return value

    @model_validator(mode="after")
    def validate_protocol(self) -> EvaluationProtocol:
        if not self.metrics:
            raise ValueError("protocol requires at least one metric")
        names: list[str] = []
        for metric in self.metrics:
            if metric.name in names:
                raise ValueError(f"duplicate metric name: {metric.name}")
            names.append(metric.name)
        if self.primary not in names:
            raise ValueError("primary metric must be declared in metrics")
        for metric, bound in self.tolerance.items():
            if metric not in names:
                raise ValueError(f"tolerance names undeclared metric: {metric}")
            if not math.isfinite(bound) or not 0.0 <= bound <= 1.0:
                raise ValueError(f"tolerance for {metric} must be within 0..1")
        seen: set[str] = set()
        for output in self.outputs:
            _relative_output(output)
            if output in seen:
                raise ValueError(f"duplicate output path: {output}")
            seen.add(output)
        return self

    def protocol_bytes(self) -> bytes:
        return canonical_json(self.model_dump(mode="json")).encode("utf-8")


class ReceiptExpectation(StrictModel):
    """The six receipt fields an accepted trial is expected to bind."""

    trial_id: str = Field(min_length=1)
    campaign_id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    candidate_digest: _Hex64
    evaluator_component_sha256: _Hex64
    evaluator_artifact_sha256: _Hex64


class TrialReceipt(StrictModel):
    """Trusted evaluation receipt for one managed trial."""

    schema_version: int = Field(default=TRIAL_RECEIPT_SCHEMA_VERSION, ge=1)
    trial_id: str = Field(min_length=1)
    campaign_id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    candidate_digest: _Hex64
    evaluator_component_sha256: _Hex64
    evaluator_artifact_sha256: _Hex64
    protocol_sha256: _Hex64
    confirmation_sha256: _Hex64 | None = None
    candidate_manifest: _Hex64 | None = None
    evaluator_manifest: _Hex64 | None = None
    output_sha256: _Hex64 | None = None
    negative_control: _NegativeControl
    metrics: dict[str, float] = Field(default_factory=dict)
    verdict: _Verdict
    failure_reason: str | None = None
    evidence: EvidenceManifest = Field(default_factory=EvidenceManifest)

    @field_validator("metrics")
    @classmethod
    def validate_metrics(cls, value: dict[str, float]) -> dict[str, float]:
        for name, metric in value.items():
            if not name:
                raise ValueError("metric name must not be empty")
            _no_control(name)
            if isinstance(metric, bool) or not math.isfinite(metric):
                raise ValueError(f"metric {name} must be a finite number")
        return value

    @field_validator("failure_reason")
    @classmethod
    def validate_failure_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value:
            raise ValueError("failure_reason must not be empty")
        return _no_control(value)

    @model_validator(mode="after")
    def validate_receipt(self) -> TrialReceipt:
        if self.verdict == "qualified" and self.failure_reason is not None:
            raise ValueError("a qualified receipt cannot carry a failure_reason")
        return self

    def receipt_bytes(self) -> bytes:
        return canonical_json(self.model_dump(mode="json")).encode("utf-8")


def output_digest(files: Mapping[str, bytes]) -> str:
    """Digest of a declared output set: sha256 over {relative path: sha256}."""
    return sha256(
        {relative: hashlib.sha256(data).hexdigest() for relative, data in files.items()}
    )


def load_protocol(
    data: bytes | str | Mapping[str, Any] | EvaluationProtocol,
    expected_sha256: str | None = None,
) -> EvaluationProtocol:
    """Parse and validate a protocol; refuse a digest mismatch or invalid data."""
    if isinstance(data, EvaluationProtocol):
        protocol = data
    else:
        if isinstance(data, (bytes, bytearray)):
            try:
                payload = json.loads(
                    bytes(data).decode("utf-8"), parse_constant=_reject_constant
                )
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                raise ValueError("invalid protocol") from None
        elif isinstance(data, str):
            try:
                payload = json.loads(data, parse_constant=_reject_constant)
            except (json.JSONDecodeError, ValueError):
                raise ValueError("invalid protocol") from None
        else:
            payload = data
        protocol = EvaluationProtocol.model_validate(payload)
    if expected_sha256 is not None:
        if hashlib.sha256(protocol.protocol_bytes()).hexdigest() != expected_sha256:
            raise ValueError("protocol digest mismatch")
    return protocol


def verify_receipt(
    data: bytes | str,
    *,
    expected_sha256: str | None = None,
    expected: ReceiptExpectation | None = None,
    protocol: EvaluationProtocol | None = None,
    protocol_sha256: str | None = None,
) -> list[str]:
    """Verify a serialized receipt; an empty list means sound.

    Every reason identifies one broken binding: the raw bytes digest, canonical
    form, receipt schema, trial, campaign, job, candidate, evaluator, protocol,
    confirmation, or metric declaration. The trailing reasons apply only to a
    ``qualified`` verdict.
    """
    raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    reasons: list[str] = []

    if expected_sha256 is not None and hashlib.sha256(raw).hexdigest() != expected_sha256:
        reasons.append("receipt_digest_mismatch")

    try:
        payload = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
        receipt = TrialReceipt.model_validate(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, ValidationError):
        reasons.append("receipt_malformed")
        return reasons

    if raw != receipt.receipt_bytes():
        reasons.append("receipt_malformed")
        return reasons

    if receipt.schema_version != TRIAL_RECEIPT_SCHEMA_VERSION:
        reasons.append("stale_receipt")

    if expected is not None:
        if receipt.trial_id != expected.trial_id:
            reasons.append("wrong_trial")
        if receipt.campaign_id != expected.campaign_id:
            reasons.append("wrong_campaign")
        if receipt.job_id != expected.job_id:
            reasons.append("wrong_job")
        if receipt.candidate_digest != expected.candidate_digest:
            reasons.append("wrong_candidate")
        if (
            receipt.evaluator_component_sha256 != expected.evaluator_component_sha256
            or receipt.evaluator_artifact_sha256 != expected.evaluator_artifact_sha256
        ):
            reasons.append("changed_evaluator")

    if protocol is not None:
        computed = hashlib.sha256(protocol.protocol_bytes()).hexdigest()
        expected_protocol = protocol_sha256 if protocol_sha256 is not None else computed
        if expected_protocol != computed or receipt.protocol_sha256 != expected_protocol:
            reasons.append("stale_protocol")
        if receipt.confirmation_sha256 != protocol.confirmation_sha256:
            reasons.append("wrong_confirmation")
        declared = {metric.name for metric in protocol.metrics}
        if any(name not in declared for name in receipt.metrics):
            reasons.append("undeclared_metric")
        if receipt.verdict == "qualified":
            if any(name not in receipt.metrics for name in declared):
                reasons.append("missing_metric")
            if receipt.output_sha256 is None:
                reasons.append("missing_outputs")
            if receipt.candidate_manifest is None or receipt.evaluator_manifest is None:
                reasons.append("missing_manifest")
            if receipt.negative_control == "accepted":
                reasons.append("negative_control_accepted")

    seen: set[str] = set()
    ordered: list[str] = []
    for reason in reasons:
        if reason not in seen:
            seen.add(reason)
            ordered.append(reason)
    return ordered


def qualifies(receipt: TrialReceipt, reasons: list[str]) -> bool:
    """A receipt qualifies only when its verdict is qualified and it is sound."""
    return receipt.verdict == "qualified" and not reasons

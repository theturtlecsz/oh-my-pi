"""Labelling-rule document for intake-support corpus v2.

``rule_block`` is the only parser of the markdown document. ``load`` and
``seal`` both call it. The rules object keeps its digest slots null so the
document does not hash itself. An approval file's ``rule_sha256`` is the
SHA-256 of the document's exact bytes, and its ``approval_sha256`` is null.
Sealing records the SHA-256 of those exact approval bytes on a separate
sealed approval.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import ConfigDict, Field

from omp_work.v1.canonical import canonical_json
from omp_work.v1.models import StrictModel, hex64

_FENCE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")


class _Strict(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Labels(_Strict):
    supports: str = Field(min_length=1)
    contradicts: str = Field(min_length=1)
    neither: str = Field(min_length=1)


class NearMissKinds(_Strict):
    topic_wrong_claim: str = Field(min_length=1)
    negation: str = Field(min_length=1)
    scope_mismatch: str = Field(min_length=1)
    quote_out_of_context: str = Field(min_length=1)
    adjacent_inference: str = Field(min_length=1)


class Rules(_Strict):
    version: Literal[2]
    labels: Labels
    near_miss_kinds: NearMissKinds
    context_rule: str = Field(min_length=1)
    rule_sha256: None
    approval_sha256: None


class Approval(_Strict):
    version: Literal[2]
    rule_sha256: hex64
    approval_sha256: None


class SealedApproval(_Strict):
    version: Literal[2]
    rule_sha256: hex64
    approval_sha256: hex64


def _opening(line: str) -> tuple[str, str] | None:
    match = _FENCE.match(line)
    if match is None:
        return None
    marker, info = match.group(2), match.group(3)
    if marker[0] == "`" and "`" in info:
        return None
    stripped = info.strip()
    language = stripped.split(None, 1)[0] if stripped else ""
    return marker, language


def _json_bodies(text: str) -> list[str]:
    lines = text.splitlines()
    bodies: list[str] = []
    index = 0
    total = len(lines)
    while index < total:
        opened = _opening(lines[index])
        if opened is None:
            index += 1
            continue
        marker, language = opened
        index += 1
        body: list[str] = []
        closed = False
        while index < total:
            close = _FENCE.match(lines[index])
            if (
                close is not None
                and close.group(2)[0] == marker[0]
                and len(close.group(2)) >= len(marker)
                and close.group(3).strip() == ""
            ):
                closed = True
                index += 1
                break
            body.append(lines[index])
            index += 1
        if not closed:
            raise ValueError("unclosed code fence")
        if language == "json":
            bodies.append("\n".join(body))
    return bodies


def _rules_object(text: str) -> dict[str, Any]:
    bodies = _json_bodies(text)
    if not bodies:
        raise ValueError("no JSON block")
    if len(bodies) > 1:
        raise ValueError("multiple JSON blocks")
    try:
        value = json.loads(bodies[0])
    except json.JSONDecodeError as error:
        raise ValueError("malformed JSON") from error
    return _as_object(value, "JSON block is not an object")


def _as_object(value: object, message: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    raise ValueError(message)


def rule_block(path: str | Path) -> dict[str, Any]:
    """Return the single JSON object in a labelling-rule document.

    Reject a document with no JSON block, more than one JSON block, a block
    that is not JSON, or a JSON value that is not an object. A JSON block is
    a closed fence whose info string's first word is ``json``.
    """
    data = Path(path).read_bytes()
    return _rules_object(data.decode("utf-8"))


def load(path: str | Path) -> Rules:
    """Load the v2 rules object. Digest slots stay null."""
    return Rules.model_validate(rule_block(path))


def _bound_approval(document: Path, approval: Path) -> tuple[Approval, bytes, str]:
    load(document)
    digest = hashlib.sha256(document.read_bytes()).hexdigest()
    approval_bytes = approval.read_bytes()
    try:
        parsed = json.loads(approval_bytes)
    except json.JSONDecodeError as error:
        raise ValueError("approval JSON is malformed") from error
    record = Approval.model_validate(_as_object(parsed, "approval JSON must be an object"))
    if record.rule_sha256 != digest:
        raise ValueError("rule_sha256 must hash the exact document bytes")
    return record, approval_bytes, digest


def seal(document: str | Path, approval: str | Path, destination: str | Path) -> SealedApproval:
    """Seal an approval of a labelling-rule document.

    ``approval`` is a JSON file whose ``rule_sha256`` equals the SHA-256 of
    the document's exact bytes and whose ``approval_sha256`` is null. The
    sealed record copies that approval and sets ``approval_sha256`` to the
    SHA-256 of the approval file's exact bytes.
    """
    _record, approval_bytes, digest = _bound_approval(Path(document), Path(approval))
    sealed = SealedApproval(version=2, rule_sha256=digest, approval_sha256=hashlib.sha256(approval_bytes).hexdigest())
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(canonical_json(sealed.model_dump(mode="json")).encode("utf-8"))
    return sealed


def verify(document: str | Path, approval: str | Path, sealed: str | Path) -> SealedApproval:
    """Return the sealed approval when both digests match the exact input bytes."""
    _record, approval_bytes, digest = _bound_approval(Path(document), Path(approval))
    found = SealedApproval.model_validate_json(Path(sealed).read_bytes())
    if found.rule_sha256 != digest:
        raise ValueError("rule digest drift")
    if found.approval_sha256 != hashlib.sha256(approval_bytes).hexdigest():
        raise ValueError("approval digest drift")
    return found

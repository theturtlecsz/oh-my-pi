"""Pure candidate packaging and independent release acceptance gates (R14)."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import TypedDict
from unicodedata import category

from omp_work.v1.canonical import canonical_json, sha256

CORE_PREFIXES = (
    "python/omp-work/src/omp_work/contracts/",
    "python/omp-work/src/omp_work/v1/",
)
GATES = ("code_review", "audit", "ci", "native_acceptance", "live_activation")


class CandidateBundle(TypedDict):
    bundle_sha256: str
    files: dict[str, str]
    parent_release: str | None
    touches_core_authority: bool


class ReleaseManifest(TypedDict):
    release_id: str
    bundle_sha256: str
    evaluated_bundle_sha256: str | None
    parent_release: str | None
    evaluator: dict[str, str]
    versions: dict[str, str]
    schema_read: tuple[int, ...]
    schema_write: int
    gates: dict[str, dict[str, str]]
    status: str


def package_candidate(
    files: Mapping[str, bytes], *, parent_release: str | None = None
) -> CandidateBundle:
    """Hash canonical relative paths and their supplied contents without I/O."""
    if not files:
        raise ValueError("empty_bundle")
    digests: dict[str, str] = {}
    for path, content in files.items():
        if (
            not path
            or path.startswith("/")
            or "\\" in path
            or any(segment in ("", ".", "..") for segment in path.split("/"))
            or any(category(char) == "Cc" for char in path)
        ):
            raise ValueError("bad_path")
        digests[path] = sha256(content)
    return {
        "bundle_sha256": sha256(digests),
        "files": digests,
        "parent_release": parent_release,
        "touches_core_authority": any(path.startswith(CORE_PREFIXES) for path in digests),
    }


def new_manifest(
    release_id: str,
    bundle: CandidateBundle,
    *,
    evaluator: Mapping[str, str],
    versions: Mapping[str, str],
    schema_read: tuple[int, ...],
    schema_write: int,
) -> ReleaseManifest:
    """Describe a packaged release, including evaluator and schema compatibility."""
    return {
        "release_id": release_id,
        "bundle_sha256": bundle["bundle_sha256"],
        "evaluated_bundle_sha256": None,
        "parent_release": bundle["parent_release"],
        "evaluator": dict(evaluator),
        "versions": dict(versions),
        "schema_read": tuple(schema_read),
        "schema_write": schema_write,
        "gates": {},
        "status": "packaged",
    }


def manifest_bytes(manifest: ReleaseManifest) -> bytes:
    """Encode the manifest as canonical UTF-8 JSON."""
    return canonical_json(manifest).encode("utf-8")


def bind_evaluated_bytes(
    manifest: ReleaseManifest, evaluated_sha256: str
) -> ReleaseManifest:
    """Bind evaluation only to the exact packaged candidate."""
    if evaluated_sha256 != manifest["bundle_sha256"]:
        raise ValueError("evaluated_bytes_mismatch")
    result = deepcopy(manifest)
    result["evaluated_bundle_sha256"] = evaluated_sha256
    return result


def record_gate(
    manifest: ReleaseManifest,
    bundle: CandidateBundle,
    gate: str,
    *,
    actor: str,
    evidence_sha256: str,
) -> ReleaseManifest:
    """Record one independent gate, enforcing owner authority where required."""
    if gate not in GATES:
        raise ValueError("unknown_gate")
    if gate in manifest["gates"]:
        raise ValueError("gate_already_recorded")
    if gate == "live_activation" and actor != "owner":
        raise ValueError("activation_requires_owner")
    if (
        gate in ("native_acceptance", "live_activation")
        and bundle["touches_core_authority"]
        and actor != "owner"
    ):
        raise ValueError("core_authority_requires_owner")
    result = deepcopy(manifest)
    result["gates"][gate] = {"actor": actor, "evidence_sha256": evidence_sha256}
    return result


def gate_status(manifest: ReleaseManifest) -> dict[str, bool]:
    return {gate: gate in manifest["gates"] for gate in GATES}


def propose_release(manifest: ReleaseManifest) -> ReleaseManifest:
    """Propose accepted evaluated bytes without applying live activation."""
    if (
        manifest["evaluated_bundle_sha256"] != manifest["bundle_sha256"]
        or not all(gate in manifest["gates"] for gate in GATES[:-1])
    ):
        raise ValueError("incomplete_acceptance")
    result = deepcopy(manifest)
    result["status"] = "proposed_release"
    return result

"""R04 custody rules that do not need PostgreSQL.

Bytes verify, path and archive escapes refuse, retention and project ACL are
decided before a read, and a cache hit cannot be claimed as an independent
replicate. Installed research bytes stay plaintext under the inline ceiling.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from omp_work.operations.artifacts import install_bytes_artifact, read_verified_bytes
from omp_work.research.custody import (
    RESEARCH_ARTIFACT_MAX_BYTES,
    contained_path,
    load_collected_bytes,
    replicate_masquerade,
    retention_expired,
    retrieval_denied,
    unsafe_archive_member,
    validate_declared_location,
    zip_member_bytes,
)
from omp_work.v1.canonical import sha256
from omp_work.v1.models import RESEARCH_ARTIFACT_MAX_BYTES as MODEL_MAX
from omp_work.v1.models import ResearchArtifactManifest
from pydantic import ValidationError


def test_artifact_hash_vectors_match_manifest_and_bytes() -> None:
    fixture = json.loads(
        (
            Path(__file__).parents[1]
            / "src/omp_work/contracts/v1/research-artifact-hash.json"
        ).read_text(encoding="utf-8")
    )
    assert fixture["algorithm"] == "research-artifact.v1"
    for vector in fixture["vectors"]:
        model = ResearchArtifactManifest.model_validate(vector["manifest"])
        assert sha256(model.model_dump(mode="json")) == vector["manifest_sha256"]
        raw = __import__("base64").b64decode(vector["content_base64"])
        assert hashlib.sha256(raw).hexdigest() == vector["artifact_sha256"]
        assert len(raw) == vector["manifest"]["size_bytes"]


def test_inline_ceiling_rejects_oversize_manifest() -> None:
    assert MODEL_MAX == RESEARCH_ARTIFACT_MAX_BYTES
    digest = "a" * 64
    with pytest.raises(ValidationError):
        ResearchArtifactManifest.model_validate(
            {
                "contract_version": "research-artifact.v1",
                "artifact_sha256": digest,
                "size_bytes": RESEARCH_ARTIFACT_MAX_BYTES + 1,
                "media_type": "application/octet-stream",
                "name": "big.bin",
                "access_class": "workspace",
                "issuer_kind": "candidate_authored",
                "source_ref": "experiments/001/big.bin",
                "valid_until": None,
            }
        )


def test_contained_paths_and_source_locations_refuse_escapes(tmp_path: Path) -> None:
    root = tmp_path / "root"
    kept = contained_path(root, "experiments/001/sample.txt")
    assert kept == root.resolve() / "experiments/001/sample.txt"
    for relative in (
        "/etc/passwd",
        "../secret",
        "foo/../bar",
        "foo/../../etc",
        "C:\\windows\\system32",
        "x\\y",
    ):
        with pytest.raises(ValueError, match="path escapes containment"):
            contained_path(root, relative)
        with pytest.raises(ValueError, match="path escapes containment"):
            validate_declared_location(relative)
    validate_declared_location("source://weights@v3")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"nope")
    link = root / "alias"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="path escapes containment"):
        contained_path(root, "alias/secret")


def test_malicious_archive_is_rejected_and_safe_member_is_read(tmp_path: Path) -> None:
    assert unsafe_archive_member("../evil.txt")
    assert unsafe_archive_member("/etc/passwd")
    assert unsafe_archive_member("C:/windows/system32/x")
    malicious = io.BytesIO()
    with zipfile.ZipFile(malicious, "w") as archive:
        archive.writestr("../evil.txt", b"nope")
        archive.writestr("ok.txt", b"yes")
    with pytest.raises(ValueError, match="archive member escapes containment"):
        zip_member_bytes(malicious.getvalue(), "ok.txt")
    safe = io.BytesIO()
    with zipfile.ZipFile(safe, "w") as archive:
        archive.writestr("nested/ok.txt", b"member-bytes")
    assert zip_member_bytes(safe.getvalue(), "nested/ok.txt") == b"member-bytes"
    root = tmp_path / "collections"
    destination = contained_path(root, "drop/bad.zip")
    destination.parent.mkdir(parents=True)
    destination.write_bytes(malicious.getvalue())
    with pytest.raises(ValueError, match="archive member escapes containment"):
        load_collected_bytes(root, "drop/bad.zip", None)


def test_installed_bytes_match_plaintext_and_refuse_mismatch(tmp_path: Path) -> None:
    data = b"verified research bytes"
    digest = hashlib.sha256(data).hexdigest()
    path = tmp_path / digest[:2] / digest
    install_bytes_artifact(path, data, digest)
    assert path.read_bytes() == data
    assert oct(path.stat().st_mode & 0o777) == "0o400"
    assert read_verified_bytes(path, digest, len(data)) == data
    backup = tmp_path / "backup-copy"
    backup.write_bytes(data)
    assert read_verified_bytes(backup, digest, len(data)) == data
    backup.write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="artifact_unavailable"):
        read_verified_bytes(backup, digest, len(data))
    path.chmod(0o600)
    path.write_bytes(b"tampered-installed")
    path.chmod(0o400)
    with pytest.raises(RuntimeError, match="artifact_unavailable"):
        read_verified_bytes(path, digest, len(data))
    with pytest.raises(RuntimeError, match="artifact_unavailable"):
        install_bytes_artifact(path, data, digest)
    missing = tmp_path / "missing"
    with pytest.raises(RuntimeError, match="artifact_unavailable"):
        read_verified_bytes(missing, digest, len(data))


def test_retention_acl_and_cache_rules() -> None:
    past = datetime.now(UTC) - timedelta(days=1)
    future = datetime.now(UTC) + timedelta(days=1)
    assert retention_expired(past)
    assert retention_expired(past.isoformat())
    assert not retention_expired(future)
    assert not retention_expired(None)
    assert (
        retrieval_denied(
            "project",
            "deny",
            project_id="p-deny",
            declared_project="p-deny",
        )
        == "project permissions deny retrieval"
    )
    assert (
        retrieval_denied(
            "project",
            "allow",
            project_id=None,
            declared_project="p-ok",
        )
        == "project permissions deny retrieval"
    )
    assert (
        retrieval_denied(
            "project",
            "allow",
            project_id="p-ok",
            declared_project="p-ok",
        )
        is None
    )
    assert retrieval_denied("workspace", None, project_id=None) is None
    assert (
        replicate_masquerade(cached=True)
        == "cached result cannot masquerade as new independent replicate"
    )
    assert replicate_masquerade(cached=False) is None

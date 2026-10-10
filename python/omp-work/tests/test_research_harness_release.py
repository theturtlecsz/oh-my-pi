import json
from copy import deepcopy

import pytest

from omp_work.research.harness_release import (
    CandidateBundle,
    ReleaseManifest,
    bind_evaluated_bytes,
    gate_status,
    manifest_bytes,
    new_manifest,
    package_candidate,
    propose_release,
    record_gate,
)
from omp_work.v1.canonical import canonical_json, sha256


def make_manifest(bundle: CandidateBundle) -> ReleaseManifest:
    return new_manifest(
        "release-1",
        bundle,
        evaluator={"component_sha256": "a" * 64, "artifact_sha256": "b" * 64},
        versions={"policy": "p1", "model": "m1", "skill": "s1"},
        schema_read=(1, 2),
        schema_write=2,
    )


def test_bundle_identity_is_canonical_and_tracks_paths_and_contents():
    files = {"z.py": b"last\x00", "a.py": b"first\xff"}
    snapshot = files.copy()
    bundle = package_candidate(files, parent_release="release-0")
    expected_files = {path: sha256(content) for path, content in files.items()}
    assert bundle == {
        "bundle_sha256": sha256(expected_files),
        "files": expected_files,
        "parent_release": "release-0",
        "touches_core_authority": False,
    }
    reordered = dict(reversed(list(files.items())))
    assert package_candidate(reordered, parent_release="release-0") == bundle
    changed = package_candidate({**files, "a.py": b"changed"})
    renamed = package_candidate({"z.py": files["z.py"], "renamed.py": files["a.py"]})
    assert changed["bundle_sha256"] != bundle["bundle_sha256"]
    assert renamed["bundle_sha256"] != bundle["bundle_sha256"]
    assert files == snapshot


@pytest.mark.parametrize(
    "path",
    [
        "", "/abs", "./a", "a\\b", "a/", "a//b", "a/./b", "a/../b",
        "a\x00b", "a\x7fb", "a\x85b",
    ],
)
def test_noncanonical_bundle_paths_are_rejected(path):
    with pytest.raises(ValueError, match="^bad_path$"):
        package_candidate({path: b"content"})


def test_empty_bundle_is_rejected():
    with pytest.raises(ValueError, match="^empty_bundle$"):
        package_candidate({})


@pytest.mark.parametrize(
    ("path", "core"),
    [
        ("python/omp-work/src/omp_work/contracts/v1/manifest.json", True),
        ("python/omp-work/src/omp_work/v1/canonical.py", True),
        ("python/omp-work/src/omp_work/v10/module.py", False),
    ],
)
def test_core_authority_detection_uses_complete_prefix(path, core):
    bundle = package_candidate({"lab.py": b"lab", path: b"change"})
    assert bundle["touches_core_authority"] is core


def test_manifest_serializes_compatibility_and_copies_input_metadata():
    bundle = package_candidate({"lab.py": b"content"}, parent_release="release-0")
    evaluator = {"component_sha256": "a" * 64, "artifact_sha256": "b" * 64}
    versions = {"policy": "p1", "model": "m1", "skill": "s1"}
    manifest = new_manifest(
        "release-é", bundle, evaluator=evaluator, versions=versions,
        schema_read=(1, 2), schema_write=2,
    )
    expected = {
        "release_id": "release-é",
        "bundle_sha256": bundle["bundle_sha256"],
        "evaluated_bundle_sha256": None,
        "parent_release": "release-0",
        "evaluator": evaluator.copy(),
        "versions": versions.copy(),
        "schema_read": [1, 2],
        "schema_write": 2,
        "gates": {},
        "status": "packaged",
    }
    evaluator["artifact_sha256"] = "changed"
    versions["policy"] = "changed"
    bundle["parent_release"] = "changed"
    encoded = manifest_bytes(manifest)
    assert json.loads(encoded) == expected
    assert encoded == canonical_json(expected).encode("utf-8")
    assert manifest["schema_read"] == (1, 2)
    assert manifest_bytes(dict(reversed(list(manifest.items())))) == encoded


def test_evaluation_binds_only_packaged_bytes_without_mutating_manifest():
    bundle = package_candidate({"lab.py": b"content"})
    manifest = make_manifest(bundle)
    snapshot = deepcopy(manifest)
    with pytest.raises(ValueError, match="^evaluated_bytes_mismatch$"):
        bind_evaluated_bytes(manifest, sha256(b"foreign"))
    assert manifest == snapshot
    bound = bind_evaluated_bytes(manifest, bundle["bundle_sha256"])
    assert bound["evaluated_bundle_sha256"] == bundle["bundle_sha256"]
    bound["evaluator"]["artifact_sha256"] = "changed"
    assert manifest == snapshot


def test_acceptance_gates_record_evidence_independently_and_cannot_repeat():
    bundle = package_candidate({"lab.py": b"content"})
    manifest = make_manifest(bundle)
    statuses = {
        "code_review": False, "audit": False, "ci": False,
        "native_acceptance": False, "live_activation": False,
    }
    assert gate_status(manifest) == statuses
    for gate, actor in (
        ("code_review", "reviewer"), ("audit", "auditor"),
        ("ci", "ci"), ("native_acceptance", "owner"),
    ):
        previous = manifest
        snapshot = deepcopy(manifest)
        evidence = sha256({"gate": gate})
        recorded = record_gate(
            manifest, bundle, gate, actor=actor, evidence_sha256=evidence,
        )
        assert manifest == snapshot
        assert recorded["gates"][gate] == {"actor": actor, "evidence_sha256": evidence}
        statuses[gate] = True
        assert gate_status(recorded) == statuses
        manifest = recorded
    snapshot = deepcopy(manifest)
    with pytest.raises(ValueError, match="^gate_already_recorded$"):
        record_gate(manifest, bundle, "code_review", actor="owner", evidence_sha256="replacement")
    with pytest.raises(ValueError, match="^unknown_gate$"):
        record_gate(manifest, bundle, "release", actor="owner", evidence_sha256="evidence")
    assert manifest == snapshot
    recorded["gates"]["code_review"]["actor"] = "changed"
    assert previous["gates"]["code_review"]["actor"] == "reviewer"


@pytest.mark.parametrize("core", [False, True])
def test_live_activation_requires_owner_even_for_noncore_bundles(core):
    path = "python/omp-work/src/omp_work/contracts/v1/schema.json" if core else "lab.py"
    bundle = package_candidate({path: b"content"})
    manifest = make_manifest(bundle)
    snapshot = deepcopy(manifest)
    with pytest.raises(ValueError, match="^activation_requires_owner$"):
        record_gate(manifest, bundle, "live_activation", actor="lab", evidence_sha256="evidence")
    activated = record_gate(manifest, bundle, "live_activation", actor="owner", evidence_sha256="evidence")
    assert gate_status(activated) == {
        "code_review": False, "audit": False, "ci": False,
        "native_acceptance": False, "live_activation": True,
    }
    assert activated["status"] == "packaged"
    assert manifest == snapshot


@pytest.mark.parametrize("core", [False, True])
def test_native_acceptance_requires_owner_only_for_core_authority(core):
    path = "python/omp-work/src/omp_work/v1/canonical.py" if core else "lab.py"
    bundle = package_candidate({path: b"content"})
    manifest = make_manifest(bundle)
    snapshot = deepcopy(manifest)
    if core:
        with pytest.raises(ValueError, match="^core_authority_requires_owner$"):
            record_gate(manifest, bundle, "native_acceptance", actor="lab", evidence_sha256="evidence")
    accepted = record_gate(manifest, bundle, "native_acceptance", actor="owner" if core else "lab", evidence_sha256="evidence")
    assert gate_status(accepted)["native_acceptance"] is True
    assert manifest == snapshot


@pytest.mark.parametrize("missing", ["code_review", "audit", "ci", "native_acceptance", "binding", "matching_binding", None])
def test_proposal_requires_all_four_gates_and_exact_evaluated_bytes(missing):
    bundle = package_candidate({"lab.py": b"content"})
    manifest = make_manifest(bundle)
    if missing != "binding":
        manifest = bind_evaluated_bytes(manifest, bundle["bundle_sha256"])
    if missing == "matching_binding":
        manifest["evaluated_bundle_sha256"] = sha256(b"foreign")
    for gate in ("code_review", "audit", "ci", "native_acceptance"):
        if gate != missing:
            manifest = record_gate(manifest, bundle, gate, actor="owner", evidence_sha256=sha256(gate))
    snapshot = deepcopy(manifest)
    if missing is not None:
        with pytest.raises(ValueError, match="^incomplete_acceptance$"):
            propose_release(manifest)
    else:
        proposed = propose_release(manifest)
        assert proposed["status"] == "proposed_release"
        assert proposed["evaluated_bundle_sha256"] == bundle["bundle_sha256"]
        assert gate_status(proposed)["live_activation"] is False
        proposed["gates"]["audit"]["actor"] = "changed"
    assert manifest == snapshot

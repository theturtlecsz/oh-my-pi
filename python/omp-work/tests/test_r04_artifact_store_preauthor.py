"""R04 artifact store process tests — mechanical seal C24 (after C23 empty-soft FAIL)."""
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(ROOT))

from omp_work.contracts.v1.artifact_store import (  # noqa: E402
    ArtifactStore,
    ArtifactStoreError,
)

FLAG = Path(__file__).with_name("R04_ARTIFACT_STORE_IMPLEMENTED.flag")


def test_implementation_flag_present():
    assert FLAG.is_file(), "R04_ARTIFACT_STORE_IMPLEMENTED.flag required after real store"


def test_bytes_and_manifest_verify():
    store = ArtifactStore()
    o1 = store.put_text("hello-artifact")
    o2 = store.put_text("other")
    store.verify_bytes(o1.digest, b"hello-artifact")
    man = store.put_manifest(
        {
            "python/omp-work/a.txt": o1.digest,
            "python/omp-work/b.txt": o2.digest,
        }
    )
    store.verify_manifest(man)
    try:
        store.verify_bytes(o1.digest, b"tampered")
        assert False, "tamper must fail"
    except ArtifactStoreError:
        pass


def test_path_and_archive_escapes_refused():
    store = ArtifactStore()
    bad = [
        "/etc/passwd",
        "../secret",
        "foo/../bar",
        "C:\\windows\\system32",
        "./relative",
        "dir/",
        "a//b",
        "x\\y",
        "glob*.py",
    ]
    for path in bad:
        try:
            store.put_text("x", relative_path=path)
            assert False, f"must refuse {path!r}"
        except ValueError:
            pass
    store.put_text(
        "ok", relative_path="python/omp-work/src/omp_work/operations/artifacts.py"
    )


def test_inaccessible_sources_remain_marked():
    store = ArtifactStore()
    store.mark_source("s1", "inaccessible")
    assert store.source_status("s1") == "inaccessible"
    store.mark_source("s2", "ok")
    assert store.source_status("s2") == "ok"
    assert store.source_status("unknown") == "inaccessible"


def test_project_permissions_before_retrieval():
    store = ArtifactStore()
    obj = store.put_text("secret-bytes")
    store.set_project_access("p-deny", "deny")
    store.set_project_access("p-ok", "allow")
    try:
        store.retrieve(obj.digest, project_id="p-deny")
        assert False, "deny must block"
    except ArtifactStoreError as e:
        assert "permissions" in str(e).lower() or "deny" in str(e).lower()
    assert store.retrieve(obj.digest, project_id="p-ok") == b"secret-bytes"


def test_cached_result_cannot_masquerade_as_independent_replicate():
    store = ArtifactStore()
    obj = store.put_text("cached-payload")
    store.remember_cache("job-1", obj.digest)
    try:
        store.claim_independent_replicate(
            cache_hit=True, digest=obj.digest, claimed_independent=True
        )
        assert False, "cache masquerade must refuse"
    except ArtifactStoreError:
        pass
    # Fresh independent claim without cache_hit and not only-via-cache-index path:
    # clear cache index semantics — claim with cache_hit False is allowed for new work.
    store2 = ArtifactStore()
    obj2 = store2.put_text("fresh-payload")
    store2.claim_independent_replicate(
        cache_hit=False, digest=obj2.digest, claimed_independent=True
    )

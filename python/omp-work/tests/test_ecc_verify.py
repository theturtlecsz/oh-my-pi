"""ecc_verify acceptance."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from omp_work.ecc_verify import verify_pack_pin, compute_manifest_sha256

MANIFEST_CONTENT = (
    b'{\n'
    b'  "adapter": "omp-v1",\n'
    b'  "ecc_pack_id": "eng-methods",\n'
    b'  "instructions": [\n'
    b'    "maker!=checker when reviewer_required",\n'
    b'    "write-first",\n'
    b'    "stop-on-green"\n'
    b'  ],\n'
    b'  "version": 1\n'
    b'}\n'
)
PIN = "6f029ca3d3d0436013fa56bf4c3d4e1c041734c08756129a38a26eda82125c05"


def test_verify_ok(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_bytes(MANIFEST_CONTENT)
    r = verify_pack_pin(manifest_path=manifest, ecc_pack_id="eng-methods", ecc_pin=PIN)
    assert r.ok
    assert r.ecc_pin_computed == PIN


def test_verify_bad_pin(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_bytes(MANIFEST_CONTENT)
    r = verify_pack_pin(manifest_path=manifest, ecc_pack_id="eng-methods", ecc_pin="0"*64)
    assert not r.ok

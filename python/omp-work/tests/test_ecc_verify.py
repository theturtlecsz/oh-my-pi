"""ecc_verify acceptance."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from omp_work.ecc_verify import verify_pack_pin, compute_manifest_sha256

MANIFEST = Path("/home/thetu/grokbot/ecc/eng-methods/manifest.json")
PIN = "6f029ca3d3d0436013fa56bf4c3d4e1c041734c08756129a38a26eda82125c05"

def test_verify_ok():
    r = verify_pack_pin(manifest_path=MANIFEST, ecc_pack_id="eng-methods", ecc_pin=PIN)
    assert r.ok
    assert r.ecc_pin_computed == PIN

def test_verify_bad_pin():
    r = verify_pack_pin(manifest_path=MANIFEST, ecc_pack_id="eng-methods", ecc_pin="0"*64)
    assert not r.ok

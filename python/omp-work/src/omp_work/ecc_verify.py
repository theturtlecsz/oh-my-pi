"""Verify ecc_pack_id + ecc_pin against a pack manifest (FULL-PROGRAM v1.1)."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import re


@dataclass(frozen=True)
class EccVerifyResult:
    ok: bool
    ecc_pack_id: str
    ecc_pin_expected: str
    ecc_pin_computed: str
    message: str

    def provenance_line(self) -> str:
        return (
            f"ECC provenance: pack={self.ecc_pack_id} pin={self.ecc_pin_computed} "
            f"adapter=omp-v1 — verify={'PASS' if self.ok else 'FAIL'}"
        )


_PIN_OK = re.compile(r"^[0-9a-f]{7,40}$|^[0-9a-f]{64}$", re.I)


def compute_manifest_sha256(manifest_path: Path) -> str:
    return hashlib.sha256(manifest_path.read_bytes()).hexdigest()


def verify_pack_pin(
    *,
    manifest_path: Path | str,
    ecc_pack_id: str,
    ecc_pin: str,
) -> EccVerifyResult:
    path = Path(manifest_path)
    if not path.is_file():
        return EccVerifyResult(False, ecc_pack_id, ecc_pin, "", f"manifest missing: {path}")
    if not _PIN_OK.match(ecc_pin.strip()):
        return EccVerifyResult(False, ecc_pack_id, ecc_pin, "", "ecc_pin not git sha or sha256")
    data = json.loads(path.read_text())
    mid = str(data.get("ecc_pack_id") or "")
    computed = compute_manifest_sha256(path)
    if mid and mid != ecc_pack_id:
        return EccVerifyResult(False, ecc_pack_id, ecc_pin, computed, f"pack id mismatch manifest={mid}")
    # Accept either exact sha256 match OR (legacy) git-length pin equality only if equal to computed prefix/full
    ok = ecc_pin.lower() == computed.lower() or (
        len(ecc_pin) <= 40 and computed.lower().startswith(ecc_pin.lower())
    )
    # Prefer exact sha256; prefix match only for short git pins documenting upstream — for grokbot we use full sha256
    if len(ecc_pin) == 64:
        ok = ecc_pin.lower() == computed.lower()
    msg = "pin matches manifest sha256" if ok else "pin does not match manifest sha256"
    return EccVerifyResult(ok, ecc_pack_id, ecc_pin, computed, msg)

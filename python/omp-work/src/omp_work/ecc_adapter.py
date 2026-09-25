"""OMP ECC adapter stub (W2 demo) — loads eng content-pack metadata for workers.

Not a Cursor agent. Pack instructions are applied by ephemeral eng workers under WorkService.
Baseline upstream pin from ACTIVE/ECC-IN-PIVOT.md.
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class EccPackLoad:
    ecc_pack_id: str
    ecc_pin: str
    ecc_adapter: str
    provenance: str

    def evidence_line(self) -> str:
        return (
            f"ECC provenance: pack={self.ecc_pack_id} pin={self.ecc_pin} "
            f"adapter={self.ecc_adapter} — {self.provenance}"
        )


def load_pack(*, ecc_pack_id: str, ecc_pin: str, ecc_adapter: str = "omp-v1") -> EccPackLoad:
    if not ecc_pack_id.strip():
        raise ValueError("ecc_pack_id required")
    if not ecc_pin.strip():
        raise ValueError("ecc_pin required")
    if len(ecc_pin) < 7:
        raise ValueError("ecc_pin must be a git revision")
    # Demo pack: eng-methods at programme baseline
    if ecc_pack_id == "eng-methods" and ecc_pin.startswith("8321021"):
        prov = "eng-methods instructions active: maker≠checker, write-first, stop-on-green"
    else:
        prov = f"pack {ecc_pack_id} loaded at pin {ecc_pin} (unqualified catalog entry)"
    return EccPackLoad(ecc_pack_id=ecc_pack_id, ecc_pin=ecc_pin, ecc_adapter=ecc_adapter, provenance=prov)

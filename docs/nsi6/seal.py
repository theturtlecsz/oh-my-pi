"""Package actual rehearsal outputs without discarding either original receipt.

Run from repository root after the four rehearsal test commands in README.md.
No route/review writes; this packages evidence, never qualifies or promotes it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from omp_work.frozen_corpus import freeze

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs/nsi6/rollback.json"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze-evidence", action="store_true")
    parser.add_argument("--origin", help="full source commit for generated evidence")
    args = parser.parse_args()
    if args.freeze_evidence:
        if not args.origin:
            parser.error("--freeze-evidence requires --origin")
        with TemporaryDirectory(prefix=".nsi6-evidence-", dir=ROOT) as temp:
            inputs = Path(temp) / "inputs"
            inputs.mkdir()
            files = []
            for name in ("shadow", "rollback", "service", "host"):
                filename = f"{name}.json"
                (inputs / filename).write_bytes((ROOT / "docs/nsi6" / filename).read_bytes())
                files.append((name, filename))
            freeze(inputs, files, args.origin, ROOT / "docs/nsi6/evidence-manifest.json")
        return
    original = OUT.read_bytes()
    decoder = json.JSONDecoder()
    pending = original.decode().strip()
    records = []
    while pending:
        record, end = decoder.raw_decode(pending)
        records.append(record)
        pending = pending[end:].strip()
    if len(records) != 2 or any(record.get("verified") is not True for record in records):
        raise ValueError("expected original service JSON followed by original host JSONL receipt")
    artifacts = {}
    for name in ("service", "host"):
        data = (ROOT / f"docs/nsi6/{name}.json").read_bytes()
        raw = json.loads(data)
        if raw.get("verified") is not True or raw.get("owner_approval") is not False:
            raise ValueError("rehearsal missing or misrepresented as owner approval")
        artifacts[name] = {"sha256": hashlib.sha256(data).hexdigest(), "raw": raw}
    paths = (
        "docs/nsi6/seal.py",
        "python/omp-work/tests/test_nsi6_evidence.py",
        "python/omp-work/tests/test_nsi6_service_route.py",
        "python/omp-work/tests/test_nsi6_rollout.py",
        "python/omp-work/tests/test_bounded_intake_publish.py",
        "python/omp-work/tests/test_mission_intake_e2e.py",
        "session-system/tests/nsi6-intake-host.test.ts",
        "session-system/tests/nsi6-evidence-host.test.ts",
    )
    proof = {
        "version": 1, "verified": True,
        "receipt": "Actual disposable service and fresh workflow-host current->typed->current rehearsals; not owner approval",
        "owner_approval": False, "synthetic_review": True,
        "original_receipts": {"bytes_hex": original.hex(), "sha256": hashlib.sha256(original).hexdigest(), "records": records},
        "artifacts": artifacts,
        "source_sha256": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in paths},
        "limits": ["Synthetic test-only typed qualification and Chris input stand-in, not Chris review",
                   "Host copies sealed temporary service states but validates only kind/version/mode, not evidence binding",
                   "Host backend assessment/writes mocked; service handlers use disposable real PostgreSQL",
                   "Re-created applications/hosts, not installed process restart or deployment"],
    }
    OUT.write_text(json.dumps(proof, indent=2) + "\n")


if __name__ == "__main__":
    main()

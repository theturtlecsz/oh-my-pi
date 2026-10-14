"""Reseal NSI-6 shadow pins and refreeze the evidence manifest. No PostgreSQL rerun."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from omp_work import nsi_eval, nsi_rollout
from omp_work.frozen_corpus import freeze
from omp_work.v1.canonical import sha256

ROOT = Path(__file__).resolve().parents[4]
_ACCEPTED = "typed false-ready on critical-omission subset"


def reseal(directory: str | Path = ROOT / "docs/nsi6", *, origin: str) -> dict:
    """Rewrite shadow pins and report_sha256, then replace the evidence manifest.

    Qualification must succeed or raise the known critical-omission rejection.
    Any other error leaves the directory untouched.
    """
    directory = Path(directory)
    shadow_path = directory / "shadow.json"
    report = json.loads(shadow_path.read_bytes())
    body = {key: value for key, value in report.items() if key != "report_sha256"}
    if report.get("sealed") is not True or report.get("report_sha256") != sha256(body):
        raise ValueError("unsealed report or report digest drift")
    # Keep report_pins() key order; canonical sealing does not sort this copy.
    report["pins"] = nsi_eval.report_pins()
    body = {key: value for key, value in report.items() if key != "report_sha256"}
    report["report_sha256"] = sha256(body)
    try:
        nsi_rollout.qualification(report)
    except ValueError as error:
        if str(error) != _ACCEPTED:
            raise
    _write_report(shadow_path, report)
    _refreeze(directory, origin)
    return report


def _write_report(path: Path, report: dict) -> None:
    temporary = path.with_name(f".{path.name}.reseal")
    try:
        with temporary.open("w") as stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _refreeze(directory: Path, origin: str) -> None:
    """Seal shadow/rollback/service/host the way docs/nsi6/seal.py does.

    frozen_corpus.freeze will not replace a different payload, so the read-only
    manifest is swapped for a newly sealed sibling.
    """
    manifest = directory / "evidence-manifest.json"
    staged = directory / ".evidence-manifest.json.reseal"
    try:
        with TemporaryDirectory(prefix=".nsi6-evidence-", dir=directory.parent) as temp:
            inputs = Path(temp) / "inputs"
            inputs.mkdir()
            files = []
            for name in ("shadow", "rollback", "service", "host"):
                filename = f"{name}.json"
                (inputs / filename).write_bytes((directory / filename).read_bytes())
                files.append((name, filename))
            staged.unlink(missing_ok=True)
            freeze(inputs, files, origin, staged)
        staged.replace(manifest)
    finally:
        staged.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True, help="full source commit for the refrozen manifest")
    parser.add_argument("--directory", type=Path, default=ROOT / "docs/nsi6")
    args = parser.parse_args()
    try:
        reseal(args.directory, origin=args.origin)
    except (OSError, ValueError) as error:
        parser.exit(1, f"NSI-6 reseal refused: {error}\n")


if __name__ == "__main__":
    main()

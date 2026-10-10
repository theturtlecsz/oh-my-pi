"""Research dossier: durable report, results table, and sources (R17, OMP-316).

``write_dossier`` stores a caller-supplied record as ``dossier.json`` plus a
rendered ``report.md`` and ``tables/results.csv``. ``verify_dossier`` rebuilds
both renderings and re-hashes the sources. ``reproduce`` reports, per
experiment, whether its private inputs are present and its metrics recur.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
from pathlib import Path, PurePosixPath
from typing import Any

__all__ = ["main", "measurements", "reproduce", "verify_dossier", "write_dossier"]

_CSV_COLUMNS = ["name", "metric", "value", "receipt_sha256"]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _check_path(rel: str) -> str:
    p = PurePosixPath(rel)
    if not rel or p.is_absolute() or ".." in p.parts or "\\" in rel:
        raise ValueError("bad_path")
    return rel


def _render_csv(experiments: list[dict[str, Any]]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(_CSV_COLUMNS)
    for exp in experiments:
        for metric, value in exp.get("metrics", {}).items():
            writer.writerow([exp["name"], metric, repr(float(value)), exp.get("receipt_sha256", "")])
    return out.getvalue()


def _render_report(dossier: dict[str, Any]) -> str:
    lines = [f"# {dossier['objective']}", ""]
    for key in ("scope", "protocol", "decision", "limits"):
        if dossier.get(key) not in (None, "", [], {}):
            lines += [f"- {key}: {dossier[key]}"]
    lines += ["", "## Sources", ""]
    for src in dossier.get("sources", []):
        lines.append(f"- {src['id']} sha256={src['sha256']} coverage={src.get('coverage', '')}")
    lines += ["", "## Experiments", ""]
    for exp in dossier.get("experiments", []):
        lines.append(f"### {exp['name']}")
        lines.append(f"- receipt_sha256: {exp.get('receipt_sha256', '')}")
        for metric, value in exp.get("metrics", {}).items():
            lines.append(f"- {metric}: {value}")
        lines.append("")
    for key in ("hypotheses", "negatives", "contradictions", "questions"):
        items = dossier.get(key) or []
        if items:
            lines += [f"## {key.capitalize()}", ""]
            lines += [f"- {item}" for item in items]
            lines.append("")
    return "\n".join(lines) + "\n"


def write_dossier(
    directory: str | Path,
    record: dict[str, Any],
    *,
    source_bytes: dict[str, bytes] | None = None,
    plot_bytes: dict[str, bytes] | None = None,
    manuscript_bytes: bytes | None = None,
) -> dict[str, Any]:
    """Write dossier.json, report.md, tables/results.csv, sources, plots, manuscript."""
    root = Path(directory)
    source_bytes = source_bytes or {}
    plot_bytes = plot_bytes or {}
    dossier = json.loads(json.dumps(record))

    # Validate everything before touching the filesystem.
    for src in dossier.get("sources", []):
        _check_path(src["id"])
        if src["id"] in source_bytes and _sha(source_bytes[src["id"]]) != src["sha256"]:
            raise ValueError("artifact_hash_mismatch")
    plots = []
    for plot in dossier.get("plots", []):
        entry = {"path": plot} if isinstance(plot, str) else dict(plot)
        _check_path(entry["path"])
        if entry["path"] in plot_bytes:
            entry["sha256"] = _sha(plot_bytes[entry["path"]])
        plots.append(entry)
    dossier["plots"] = plots
    manuscript = dict(dossier.get("manuscript") or {})
    manuscript.setdefault("status", "not_generated")
    if manuscript.get("path"):
        _check_path(manuscript["path"])
        if manuscript_bytes is not None:
            manuscript["sha256"] = _sha(manuscript_bytes)
    dossier["manuscript"] = manuscript

    for sid, data in source_bytes.items():
        _check_path(sid)
        (root / "sources").mkdir(parents=True, exist_ok=True)
        (root / "sources" / sid).write_bytes(data)
    for rel, data in plot_bytes.items():
        _check_path(rel)
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    if manuscript_bytes is not None and manuscript.get("path"):
        target = root / manuscript["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(manuscript_bytes)

    root.mkdir(parents=True, exist_ok=True)
    (root / "tables").mkdir(exist_ok=True)
    (root / "dossier.json").write_text(json.dumps(dossier, indent=2, sort_keys=True) + "\n")
    (root / "report.md").write_text(_render_report(dossier))
    (root / "tables" / "results.csv").write_text(_render_csv(dossier.get("experiments", [])))
    return dossier


def _load(directory: str | Path) -> dict[str, Any]:
    return json.loads((Path(directory) / "dossier.json").read_text())


def measurements(directory: str | Path) -> dict[str, dict[str, float]]:
    """Read tables/results.csv as ``{name: {metric: value}}``."""
    out: dict[str, dict[str, float]] = {}
    with (Path(directory) / "tables" / "results.csv").open(newline="") as fh:
        for row in csv.DictReader(fh):
            out.setdefault(row["name"], {})[row["metric"]] = float(row["value"])
    return out


def verify_dossier(directory: str | Path) -> list[str]:
    """Return reasons the stored files disagree with dossier.json."""
    root = Path(directory)
    dossier = _load(root)
    reasons: list[str] = []
    try:
        report_ok = (root / "report.md").read_text() == _render_report(dossier)
        csv_ok = (root / "tables" / "results.csv").read_text() == _render_csv(dossier.get("experiments", []))
    except FileNotFoundError:
        report_ok = csv_ok = False
    if not (report_ok and csv_ok):
        reasons.append("report_altered")
    for src in dossier.get("sources", []):
        path = root / "sources" / src["id"]
        if not path.is_file():
            reasons.append(f"missing_artifact:{src['id']}")
        elif _sha(path.read_bytes()) != src["sha256"]:
            reasons.append(f"artifact_hash_mismatch:{src['id']}")
    return reasons


def reproduce(
    directory: str | Path,
    *,
    private_dir: str | Path | None = None,
    rerun: Any = None,
    tolerance: float = 0.0,
) -> dict[str, Any]:
    """Report source and per-experiment reproduction status."""
    root = Path(directory)
    dossier = _load(root)
    sources_ok = all(
        (root / "sources" / s["id"]).is_file() and _sha((root / "sources" / s["id"]).read_bytes()) == s["sha256"]
        for s in dossier.get("sources", [])
    )
    result: dict[str, Any] = {"sources": "reproduced" if sources_ok else "not_reproduced"}
    for exp in dossier.get("experiments", []):
        name = exp["name"]
        missing = [
            sha
            for sha in dossier.get("private_dependencies", [])
            if private_dir is None or not (Path(private_dir) / sha).is_file()
        ]
        if missing:
            result[name] = f"missing_private_dependency:{missing[0]}"
            continue
        stored = exp.get("metrics", {})
        got = stored if rerun is None else rerun(name)
        same = set(got) == set(stored) and all(abs(float(got[k]) - float(v)) <= tolerance for k, v in stored.items())
        result[name] = "reproduced" if same else "not_reproduced"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omp_work.research.dossier")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("verify").add_argument("directory")
    rep = sub.add_parser("reproduce")
    rep.add_argument("directory")
    rep.add_argument("--private-dir")
    args = parser.parse_args(argv)
    if args.cmd == "verify":
        reasons = verify_dossier(args.directory)
        for reason in reasons:
            print(reason)
        return 1 if reasons else 0
    result = reproduce(args.directory, private_dir=args.private_dir)
    for name, status in result.items():
        print(f"{name} {status}")
    return 0 if all(s == "reproduced" for s in result.values()) else 1


if __name__ == "__main__":
    sys.exit(main())

"""Reproducible bundle for one managed investigation (R17, OMP-315).

``build_bundle`` copies the candidate, evaluator, protocol, and receipt of
every trusted trial. Confirmation bytes stay out of the bundle. Their digests
are listed under ``private_dependencies``. ``verify_bundle`` checks those
bytes and the rendered results. ``reproduce`` reruns each qualified trial on
this host, with confirmation bytes supplied from outside the bundle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from pydantic import ValidationError

from omp_work.jobs.managed_trial import evaluate_candidate
from omp_work.jobs.runner import (
    LocalJailBackend,
    RunRequest,
    _environment_manifest,
    check_environment,
)
from omp_work.jobs.store import NativeJobStore
from omp_work.operations.artifacts import read_verified_bytes
from omp_work.operations.config import OperationsConfig
from omp_work.research.custody import artifact_path
from omp_work.research.managed import managed_trials
from omp_work.research.receipt import EvaluationProtocol, TrialReceipt, load_protocol
from omp_work.v1.canonical import sha256

__all__ = [
    "build_bundle",
    "main",
    "reproduce",
    "verify_bundle",
]

BUNDLE_FORMAT = "omp-repro-bundle.v1"
RESULT_FORMAT = "omp-managed-result.v1"
_RESULT_KEYS = (
    "format",
    "trial_id",
    "campaign_id",
    "receipt_sha256",
    "candidate_digest",
    "verdict",
    "primary_metric",
    "direction",
    "metrics",
    "description",
)


def _json_object(value: object) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _raw_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _content_bytes(content_dir: Path, digest: str) -> bytes:
    path = content_dir / digest
    try:
        data = path.read_bytes()
    except OSError:
        raise ValueError(f"missing_artifact_source:{digest}") from None
    if _raw_sha256(data) != digest:
        raise ValueError(f"artifact_hash_mismatch:{digest}")
    return data


def _custody_bytes(
    cur: psycopg.Cursor[dict[str, object]],
    workspace_id: UUID,
    digest: str,
    data_dir: Path,
) -> bytes:
    cur.execute(
        "SELECT manifest FROM omp_research.artifacts WHERE workspace_id=%s AND artifact_sha256=%s",
        (workspace_id, digest),
    )
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"missing_artifact_source:{digest}")
    manifest = _json_object(row["manifest"])
    size = None if manifest is None else manifest.get("size_bytes")
    if isinstance(size, bool) or not isinstance(size, int):
        raise ValueError(f"missing_artifact_source:{digest}")
    try:
        return read_verified_bytes(artifact_path(data_dir, workspace_id, digest), digest, size)
    except (RuntimeError, OSError):
        raise ValueError(f"missing_artifact_source:{digest}") from None


def _evaluator_artifact(
    cur: psycopg.Cursor[dict[str, object]], workspace_id: UUID, component_sha256: str
) -> str:
    cur.execute(
        "SELECT descriptor FROM omp_research.components WHERE workspace_id=%s AND component_sha256=%s",
        (workspace_id, component_sha256),
    )
    row = cur.fetchone()
    descriptor = None if row is None else _json_object(row["descriptor"])
    artifact = None if descriptor is None else descriptor.get("artifact_sha256")
    if not isinstance(artifact, str) or not artifact:
        raise ValueError(f"missing_artifact_source:{component_sha256}")
    return artifact


def _description(
    cur: psycopg.Cursor[dict[str, object]], workspace_id: UUID, trial_id: str
) -> str:
    cur.execute(
        "SELECT reason FROM omp_research.trials WHERE workspace_id=%s AND trial_id=%s",
        (workspace_id, UUID(trial_id)),
    )
    row = cur.fetchone()
    reason = None if row is None else row["reason"]
    if isinstance(reason, str) and reason.strip():
        return reason.strip()
    return f"trial {trial_id[:8]}"


def _direction(protocol: EvaluationProtocol) -> str:
    for metric in protocol.metrics:
        if metric.name == protocol.primary:
            return str(metric.direction)
    raise ValueError("primary metric is not declared")


def _result_record(
    trial: dict[str, Any], protocol: EvaluationProtocol, receipt: TrialReceipt
) -> dict[str, Any]:
    metrics = {name: receipt.metrics[name] for name in sorted(receipt.metrics)}
    return {
        "format": RESULT_FORMAT,
        "trial_id": trial["trial_id"],
        "campaign_id": trial["campaign_id"],
        "receipt_sha256": trial["receipt_sha256"],
        "candidate_digest": trial["candidate_digest"],
        "verdict": receipt.verdict,
        "primary_metric": protocol.primary,
        "direction": _direction(protocol),
        "metrics": metrics,
        "description": trial["description"],
    }


def _result_line(record: dict[str, Any]) -> str:
    ordered = {key: record[key] for key in _RESULT_KEYS}
    return json.dumps(ordered, separators=(",", ":"), ensure_ascii=False)


def _metrics_text(metrics: dict[str, float]) -> str:
    if not metrics:
        return ""
    return ",".join(
        f"{name}={json.dumps(metrics[name])}" for name in sorted(metrics)
    )


def _render(directory: Path, manifest: dict[str, Any]) -> tuple[bytes, bytes]:
    """Rebuild results.jsonl and results.md from artifact bytes and descriptions."""
    lines: list[str] = []
    blocks: list[str] = [f"# {BUNDLE_FORMAT}", ""]
    artifact_dir = directory / "artifacts"
    for trial in manifest["trials"]:
        if not isinstance(trial, dict):
            raise ValueError("report_altered")
        protocol = load_protocol((artifact_dir / str(trial["protocol_sha256"])).read_bytes())
        receipt = TrialReceipt.model_validate(
            json.loads((artifact_dir / str(trial["receipt_sha256"])).read_bytes())
        )
        record = _result_record(trial, protocol, receipt)
        lines.append(_result_line(record))
        blocks.extend(
            [
                f"trial_id: {record['trial_id']}",
                f"campaign_id: {record['campaign_id']}",
                f"verdict: {record['verdict']}",
                f"primary_metric: {record['primary_metric']}",
                f"direction: {record['direction']}",
                f"metrics: {_metrics_text(record['metrics'])}",
                f"receipt_sha256: {record['receipt_sha256']}",
                f"candidate_digest: {record['candidate_digest']}",
                f"description: {record['description']}",
                "",
            ]
        )
    jsonl = ("".join(f"{line}\n" for line in lines)).encode("utf-8")
    markdown = ("\n".join(blocks)).encode("utf-8")
    if not markdown.endswith(b"\n"):
        markdown += b"\n"
    return jsonl, markdown


def _load_manifest(directory: Path) -> dict[str, Any] | None:
    path = directory / "manifest.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("format") != BUNDLE_FORMAT:
        return None
    trials = payload.get("trials")
    if not isinstance(trials, list):
        return None
    for trial in trials:
        if not isinstance(trial, dict):
            return None
        for key in (
            "trial_id",
            "campaign_id",
            "candidate_digest",
            "protocol_sha256",
            "receipt_sha256",
            "evaluator_artifact_sha256",
            "description",
        ):
            if not isinstance(trial.get(key), str):
                return None
    return payload


def _trial_shas(trial: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(trial["candidate_digest"]),
        str(trial["evaluator_artifact_sha256"]),
        str(trial["protocol_sha256"]),
        str(trial["receipt_sha256"]),
    )


def build_bundle(
    cur: psycopg.Cursor[dict[str, object]],
    workspace_id: UUID | str,
    work_id: UUID | str,
    *,
    content_dir: Path | str,
    data_dir: Path | str,
    out: Path | str,
) -> Path:
    """Write one bundle for every managed trial of ``work_id`` that has no reasons.

    Candidate and evaluator bytes come from ``content_dir``. Protocol and
    receipt bytes come from custody. A missing or mismatched source raises
    ``ValueError``. Confirmation bytes are not copied.
    """
    workspace = UUID(str(workspace_id))
    work = UUID(str(work_id))
    content = Path(content_dir)
    data = Path(data_dir)
    destination = Path(out)
    rows = managed_trials(cur, workspace, work, data_dir=data)
    trials: list[dict[str, Any]] = []
    blobs: dict[str, bytes] = {}
    environments: dict[str, dict[str, object]] = {}
    confirmations: set[str] = set()

    for trial, receipt, reasons in rows:
        if reasons or receipt is None:
            continue
        trial_id = str(trial["trial_id"])
        candidate_digest = str(trial["candidate_digest"])
        evaluator_artifact = _evaluator_artifact(
            cur, workspace, str(trial["evaluator_sha256"])
        )
        spec = _json_object(trial.get("spec")) or {}
        protocol_sha = spec.get("evaluation_protocol_sha256")
        if not isinstance(protocol_sha, str) or not protocol_sha:
            raise ValueError(f"missing_artifact_source:{trial_id}")
        receipt_sha = _raw_sha256(receipt.receipt_bytes())
        blobs[candidate_digest] = _content_bytes(content, candidate_digest)
        blobs[evaluator_artifact] = _content_bytes(content, evaluator_artifact)
        protocol_bytes = _custody_bytes(cur, workspace, protocol_sha, data)
        blobs[protocol_sha] = protocol_bytes
        blobs[receipt_sha] = _custody_bytes(cur, workspace, receipt_sha, data)
        protocol = load_protocol(protocol_bytes, expected_sha256=protocol_sha)
        if protocol.confirmation_sha256 is not None:
            confirmations.add(protocol.confirmation_sha256)
        # The receipt stores only the environment hash. Keep the dict when it
        # is the same hash, so a later host can compare it without the database.
        if receipt.evaluator_manifest is not None:
            environment = _environment_manifest(
                RunRequest(
                    protocol.evaluator_command,
                    Path("."),
                    requires=protocol.requires,
                )
            )
            if sha256(environment) == receipt.evaluator_manifest:
                environments[receipt.evaluator_manifest] = environment
        trials.append(
            {
                "trial_id": trial_id,
                "campaign_id": str(trial["campaign_id"]),
                "candidate_digest": candidate_digest,
                "evaluator_artifact_sha256": evaluator_artifact,
                "protocol_sha256": protocol_sha,
                "receipt_sha256": receipt_sha,
                "confirmation_sha256": protocol.confirmation_sha256,
                "description": _description(cur, workspace, trial_id),
            }
        )

    manifest = {
        "format": BUNDLE_FORMAT,
        "workspace_id": str(workspace),
        "work_id": str(work),
        "private_dependencies": sorted(confirmations),
        "environments": environments,
        "trials": trials,
    }
    destination.mkdir(parents=True, exist_ok=True)
    artifact_dir = destination / "artifacts"
    artifact_dir.mkdir(mode=0o700, exist_ok=True)
    for digest, data in blobs.items():
        (artifact_dir / digest).write_bytes(data)
    (destination / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    jsonl, markdown = _render(destination, manifest)
    (destination / "results.jsonl").write_bytes(jsonl)
    (destination / "results.md").write_bytes(markdown)
    return destination


def verify_bundle(directory: Path | str) -> list[str]:
    """Reasons the bundle is not the bytes it claims.

    ``missing_artifact:<sha>`` and ``artifact_hash_mismatch:<sha>`` name one
    artifact each. ``report_altered`` means ``results.jsonl`` or ``results.md``
    is not the text regenerated from the artifact bytes. An unreadable manifest
    is ``report_altered``.
    """
    root = Path(directory)
    manifest = _load_manifest(root)
    if manifest is None:
        return ["report_altered"]
    reasons: list[str] = []
    seen: set[str] = set()
    for trial in manifest["trials"]:
        for digest in _trial_shas(trial):
            if digest in seen:
                continue
            seen.add(digest)
            path = root / "artifacts" / digest
            if not path.is_file():
                reasons.append(f"missing_artifact:{digest}")
                continue
            if _raw_sha256(path.read_bytes()) != digest:
                reasons.append(f"artifact_hash_mismatch:{digest}")
    try:
        rendered = _render(root, manifest)
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError, ValueError, KeyError, TypeError):
        rendered = None
    if rendered is not None:
        jsonl, markdown = rendered

        def _read(path: Path) -> bytes:
            try:
                return path.read_bytes()
            except OSError:
                return b""

        if _read(root / "results.jsonl") != jsonl or _read(root / "results.md") != markdown:
            reasons.append("report_altered")
    return reasons


def _within_tolerance(new: float, old: float, tolerance: float) -> bool:
    return abs(new - old) <= tolerance * abs(old)


def _reproduce_one(
    directory: Path,
    manifest: dict[str, Any],
    trial: dict[str, Any],
    *,
    private_dir: Path | None,
) -> str:
    artifact_dir = directory / "artifacts"
    protocol = load_protocol((artifact_dir / trial["protocol_sha256"]).read_bytes())
    receipt = TrialReceipt.model_validate(
        json.loads((artifact_dir / trial["receipt_sha256"]).read_bytes())
    )
    if receipt.verdict != "qualified":
        raise ValueError("reproduce called for a trial that is not qualified")
    environments = manifest.get("environments")
    stored = environments.get(receipt.evaluator_manifest) if isinstance(environments, dict) else None
    if (
        not isinstance(stored, dict)
        or receipt.evaluator_manifest is None
        or sha256(stored) != receipt.evaluator_manifest
    ):
        return "environment_mismatch:evaluator_manifest"
    diffs = check_environment(stored)
    if diffs:
        return "environment_mismatch:" + ",".join(diffs)
    confirmation: bytes | None = None
    confirmation_sha = trial.get("confirmation_sha256")
    if isinstance(confirmation_sha, str) and confirmation_sha:
        path = None if private_dir is None else private_dir / confirmation_sha
        try:
            data = None if path is None else path.read_bytes()
        except OSError:
            data = None
        if data is None or _raw_sha256(data) != confirmation_sha:
            return f"missing_private_dependency:{confirmation_sha}"
        confirmation = data
    evaluation = evaluate_candidate(
        LocalJailBackend(),
        protocol,
        candidate_tar=(artifact_dir / trial["candidate_digest"]).read_bytes(),
        candidate_digest=str(trial["candidate_digest"]),
        evaluator_script=(artifact_dir / trial["evaluator_artifact_sha256"]).read_bytes(),
        confirmation=confirmation,
        cancel=None,
        scratch_dir=None,
    )
    if evaluation.canceled or evaluation.verdict != receipt.verdict:
        return "not_reproduced"
    if set(evaluation.metrics) != set(receipt.metrics):
        return "not_reproduced"
    for name, old in receipt.metrics.items():
        tolerance = float(protocol.tolerance.get(name, 0.0))
        if not _within_tolerance(float(evaluation.metrics[name]), float(old), tolerance):
            return "not_reproduced"
    return "reproduced"


def reproduce(directory: Path | str, private_dir: Path | str | None = None) -> dict[str, str]:
    """Status of each qualified trial, in bundle order.

    A different host environment is ``environment_mismatch:<keys>``. Missing
    confirmation bytes are ``missing_private_dependency:<sha>``. A rerun with
    the same verdict and metrics inside tolerance is ``reproduced``. Anything
    else is ``not_reproduced``.
    """
    root = Path(directory)
    manifest = _load_manifest(root)
    if manifest is None:
        raise ValueError("report_altered")
    private = None if private_dir is None else Path(private_dir)
    statuses: dict[str, str] = {}
    artifact_dir = root / "artifacts"
    for trial in manifest["trials"]:
        receipt = TrialReceipt.model_validate(
            json.loads((artifact_dir / str(trial["receipt_sha256"])).read_bytes())
        )
        if receipt.verdict != "qualified":
            continue
        statuses[str(trial["trial_id"])] = _reproduce_one(
            root, manifest, trial, private_dir=private
        )
    return statuses


def _print_reasons(reasons: list[str]) -> int:
    for reason in reasons:
        print(reason)
    return 1 if reasons else 0


def main(argv: list[str] | None = None) -> int:
    """``build``, ``verify``, or ``reproduce``. Exit 1 on any reason or miss."""
    parser = argparse.ArgumentParser(prog="python -m omp_work.research.bundle")
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build")
    build.add_argument("--workspace", required=True)
    build.add_argument("--work", required=True)
    build.add_argument("--actor", required=True)
    build.add_argument("--content-dir", required=True, type=Path)
    build.add_argument("--data-dir", required=True, type=Path)
    build.add_argument("--out", required=True, type=Path)

    verify = commands.add_parser("verify")
    verify.add_argument("directory", type=Path)

    replay = commands.add_parser("reproduce")
    replay.add_argument("directory", type=Path)
    replay.add_argument("--private-dir", type=Path, default=None)

    args = parser.parse_args(argv)
    if args.command == "build":
        store = NativeJobStore(OperationsConfig.defaults())
        try:
            with store.transaction(UUID(args.workspace), UUID(args.actor)) as cur:
                build_bundle(
                    cur,
                    args.workspace,
                    args.work,
                    content_dir=args.content_dir,
                    data_dir=args.data_dir,
                    out=args.out,
                )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return _print_reasons(verify_bundle(args.out))
    if args.command == "verify":
        return _print_reasons(verify_bundle(args.directory))
    statuses = reproduce(args.directory, args.private_dir)
    print(json.dumps(statuses))
    if any(status != "reproduced" for status in statuses.values()):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

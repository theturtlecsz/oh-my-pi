"""Verifier module entry: judge one frozen candidate and publish its verdict (OMP-417).

``python -m omp_work.orchestrator.verify_main`` (and the argument-free
``omp-work orchestrator verify`` dispatch) runs inside a verifier worktree. It
reads ``.omp/verify-task.json`` exactly once into a :class:`VerifyTask`, freezes
the candidate through :func:`prepare_candidate`, runs the task's test command on
that snapshot with the task timeout, and only after the snapshot is gone signs
the verdict with the task's HMAC key.

Success is the preflight plus a zero test exit and a clean snapshot: child
stdout and any pre-existing ``.omp`` artifact can never establish a pass. The
``audit-report.txt`` is published atomically before ``verdict.json`` and a pass
verdict is written only when both persist; every other outcome publishes a
BLOCKED report that states the reason and leaves no usable pass. The HMAC key is
read only in the signer, and the signed record binds the exact task digest,
attempt, and verifier identity.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess  # nosec B404 - verifier argv lists only; no shell is used
import tempfile
from contextlib import suppress

from pydantic import ValidationError

from omp_work.egress_sandbox import SandboxUnavailable
from omp_work.orchestrator.candidate_git import CandidateGitError
from omp_work.orchestrator.verify_candidate import VerifyTask, prepare_candidate
from omp_work.orchestrator.verify_tests import run_tests
from omp_work.orchestrator.verifier import sign_verdict
from omp_work.v1.audit_result import render_audit_result
from omp_work.v1.models import (
    AuditCriterionCheck,
    AuditFinding,
    AuditFindingLocation,
    AuditResult,
    EvidenceManifest,
    EvidenceReference,
)

__all__ = ["main"]

_TASK = "verify-task.json"
_VERDICT = "verdict.json"
_REPORT = "audit-report.txt"
_OUTPUTS = (_VERDICT, _REPORT)
_LINE_LIMIT = 2000


def _oneline(text: str, limit: int = _LINE_LIMIT) -> str:
    """One non-empty single-line field, bounded for the contract model."""
    flat = " ".join(text.split())
    if not flat:
        return "unspecified"
    return flat[:limit]


def _atomic_write(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` through a same-directory temp file and rename."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".omp-tmp-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp)
        raise


def _discard(path: Path) -> None:
    """Remove one output without following a symlink."""
    with suppress(OSError):
        if path.is_symlink() or path.is_file():
            path.unlink()


def _clear_outputs(omp_dir: Path) -> str | None:
    """Clear stale regular outputs; refuse (but remove) a symlinked one.

    Returns the refusal code, or None when both outputs were absent or regular.
    """
    refused: str | None = None
    for name in _OUTPUTS:
        path = omp_dir / name
        if path.is_symlink():
            # Never write through a stale output link; remove the link itself.
            with suppress(OSError):
                path.unlink()
            refused = refused or f"{name}_symlink"
        elif path.is_dir():
            refused = refused or f"{name}_not_regular"
        elif path.exists():
            path.unlink()
    return refused


def _read_task(omp_dir: Path) -> tuple[VerifyTask | None, str]:
    """Read ``.omp/verify-task.json`` once, into a validated ``VerifyTask``."""
    try:
        raw = (omp_dir / _TASK).read_text(encoding="utf-8")
    except OSError:
        return None, "task_missing"
    try:
        document = json.loads(raw)
    except (ValueError, TypeError):
        return None, "task_invalid"
    try:
        return VerifyTask.model_validate(document), ""
    except ValidationError:
        return None, "task_invalid"


def _pass_result(task: VerifyTask, cwd: Path, exit_code: int) -> AuditResult:
    """The typed PASS: the digest, the prepared worktree, and the test checks."""
    command = shlex.join(task.test_command)
    return AuditResult(
        verdict="PASS",
        criteria=(
            AuditCriterionCheck(
                criterion_id="candidate-digest",
                status="met",
                evidence=(
                    f"the base..candidate diff hashed to the task digest "
                    f"{task.candidate_sha256}"
                ),
            ),
            AuditCriterionCheck(
                criterion_id="worktree-path",
                status="met",
                evidence=(
                    f"{cwd} was prepared from the candidate commit "
                    f"{task.candidate_commit}"
                ),
            ),
            AuditCriterionCheck(
                criterion_id="tests",
                status="met",
                evidence=f"{command} exited {exit_code} in the frozen snapshot",
            ),
        ),
        evidence=EvidenceManifest(
            references=(
                EvidenceReference(
                    kind="file",
                    ref=str(cwd),
                    sha256=task.candidate_sha256,
                ),
                EvidenceReference(kind="command", ref=command, result="exit 0"),
            )
        ),
    )


def _blocked_result(cwd: Path, code: str, detail: str) -> AuditResult:
    """The typed BLOCKED: the deterministic reason the candidate was not passed."""
    return AuditResult(
        verdict="BLOCKED",
        findings=(
            AuditFinding(
                severity="BLOCKER",
                criterion_id="verification",
                location=AuditFindingLocation(path=str(cwd), line_start=1),
                evidence=_oneline(f"{code}: {detail}" if detail else code),
                impact="the candidate was not verified, so no pass may be accepted",
                minimal_fix="fix the verifier input or the candidate and rerun",
            ),
        ),
        evidence=EvidenceManifest(),
    )


def _publish_failure(omp_dir: Path, cwd: Path, code: str, detail: str = "") -> bool:
    """Leave no usable pass and, when possible, publish the BLOCKED report."""
    _discard(omp_dir / _VERDICT)
    try:
        report = render_audit_result(_blocked_result(cwd, code, detail))
        _atomic_write(omp_dir / _REPORT, report.encode("utf-8"))
    except OSError:
        return False
    return True


def _judge(task: VerifyTask, cwd: Path) -> tuple[int | None, str, str]:
    """Preflight and test the candidate, then clean the snapshot before returning.

    Returns ``(exit_code, failure_code, detail)``; ``failure_code`` is empty only
    when the preflight, the test exit, and the snapshot cleanup all succeeded.
    """
    exit_code: int | None = None
    failure = ""
    detail = ""
    snapshot: Path | None = None
    try:
        with prepare_candidate(task, cwd) as prepared:
            snapshot = prepared
            try:
                result = run_tests(task.test_command, snapshot, task.timeout_seconds)
            except subprocess.TimeoutExpired:
                failure = "timeout"
                detail = f"the test command exceeded {task.timeout_seconds}s"
            except SandboxUnavailable as error:
                failure = "sandbox_unavailable"
                detail = str(error)
            except Exception as error:  # noqa: BLE001 - any verifier fault is a refusal
                failure = "verifier_error"
                detail = type(error).__name__
            else:
                exit_code = result.exit_code
    except CandidateGitError as error:
        failure = f"preflight_{error.code}"
        detail = " ".join(error.violations)
    except Exception as error:  # noqa: BLE001 - any verifier fault is a refusal
        failure = "verifier_error"
        detail = type(error).__name__

    if not failure and exit_code != 0:
        failure = f"tests_exit_{exit_code}"
        detail = "the test command did not exit zero"
    if not failure and (snapshot is None or snapshot.exists()):
        failure = "cleanup_failed"
        detail = "the frozen snapshot was not removed"
    return exit_code, failure, detail


def main(argv: list[str] | None = None) -> int:
    """Judge the candidate in the current worktree; 0 only for a persisted pass."""
    del argv  # the verifier dispatch takes no arguments
    cwd = Path.cwd()
    omp_dir = cwd / ".omp"

    if omp_dir.is_symlink():
        return 2
    try:
        omp_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return 2
    try:
        refused = _clear_outputs(omp_dir)
    except OSError:
        return 2
    if refused is not None or (omp_dir / _TASK).is_symlink():
        return 2

    task, reason = _read_task(omp_dir)
    if task is None:
        return 1 if _publish_failure(omp_dir, cwd, reason) else 2

    exit_code, failure, detail = _judge(task, cwd)
    if failure:
        return 1 if _publish_failure(omp_dir, cwd, failure, detail) else 2

    # The signer is the only place the HMAC key is read.
    try:
        key = Path(task.key_path).read_bytes()
    except OSError:
        return 1 if _publish_failure(omp_dir, cwd, "key_unreadable") else 2
    if not key:
        return 1 if _publish_failure(omp_dir, cwd, "key_empty") else 2
    try:
        record = sign_verdict(
            key,
            candidate_sha256=task.candidate_sha256,
            attempt_id=task.attempt_id,
            verdict="pass",
            verifier_id=task.verifier_id,
        )
    except ValueError:
        return 1 if _publish_failure(omp_dir, cwd, "sign_failed") else 2

    report = render_audit_result(_pass_result(task, cwd, int(exit_code)))
    try:
        _atomic_write(omp_dir / _REPORT, report.encode("utf-8"))
        _atomic_write(
            omp_dir / _VERDICT,
            (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"),
        )
    except OSError:
        _discard(omp_dir / _VERDICT)
        if not _publish_failure(omp_dir, cwd, "output_failure"):
            return 2
        return 1
    if not (omp_dir / _REPORT).is_file() or not (omp_dir / _VERDICT).is_file():
        _discard(omp_dir / _VERDICT)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Verifier-side candidate preparation (OMP-417).

``VerifyTask`` is the strict task a verifier reads before judging a candidate:
the pinned base and candidate commits, the digest the candidate must hash to,
the allowed change envelope, the acceptance criteria, the test command, and the
verifier identity and timeout. ``prepare_candidate`` is a context manager that
verifies a worktree against that task and yields a frozen snapshot of the
candidate commit, cleaning the snapshot on every exit.

Nothing here runs the test command or signs a verdict: the context manager only
prepares the tree the verifier later judges.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
import io
import math
from pathlib import Path
import shutil
import tarfile
import tempfile
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from omp_work.orchestrator import candidate_git

__all__ = [
    "VerifyTask",
    "prepare_candidate",
]

# The only files the orchestrator transports into a verifier worktree. They are
# untracked by construction; anything else under .omp is a candidate change.
TRANSPORT_PATHS = (
    ".omp/verify-task.json",
    ".omp/verdict.json",
    ".omp/audit-report.txt",
)

_SHA1 = r"^[0-9a-f]{40}$"
_SHA256 = r"^[0-9a-f]{64}$"


class VerifyTask(BaseModel):
    """The strict task one verifier judges. Every field is validated, not coerced."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    candidate_sha256: str = Field(pattern=_SHA256)
    attempt_id: str
    verifier_id: Literal["verifier"]
    key_path: str
    test_command: list[str] = Field(min_length=1)
    criteria: list[dict[str, Any]]
    candidate_commit: str = Field(pattern=_SHA1)
    base_commit: str = Field(pattern=_SHA1)
    allowed_paths: list[str]
    timeout_seconds: float

    @field_validator("attempt_id")
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        try:
            parsed = UUID(value)
        except (ValueError, AttributeError, TypeError) as err:
            raise ValueError("attempt_id must be a UUID") from err
        if str(parsed) != value:
            raise ValueError("attempt_id must be a canonical lowercase UUID")
        return value

    @field_validator("key_path")
    @classmethod
    def _absolute_key_path(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("key_path must be absolute")
        return value

    @field_validator("timeout_seconds")
    @classmethod
    def _positive_finite(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            raise ValueError("timeout_seconds must be positive and finite")
        return value


def _validate_task(task: VerifyTask | Mapping[str, Any]) -> VerifyTask:
    """Return ``task`` as a :class:`VerifyTask` or refuse with ``invalid_task``."""
    if isinstance(task, VerifyTask):
        return task
    if not isinstance(task, Mapping):
        raise candidate_git.CandidateGitError(
            "invalid_task", ["task must be a mapping or VerifyTask"]
        )
    try:
        return VerifyTask.model_validate(dict(task))
    except ValidationError as err:
        return _invalid_task(err)


def _invalid_task(err: ValidationError) -> VerifyTask:
    violations = [
        f"{'/'.join(str(part) for part in error['loc'])}:{error['type']}"
        for error in err.errors()
    ]
    raise candidate_git.CandidateGitError("invalid_task", violations)


def _head(worktree: Path) -> str:
    return (
        candidate_git.git(worktree, "--no-optional-locks", "rev-parse", "HEAD")
        .decode("ascii")
        .strip()
    )


def _committed_transport(worktree: Path, commit: str) -> list[str]:
    out = candidate_git.git(
        worktree,
        "--no-optional-locks",
        "ls-tree",
        "-r",
        "--name-only",
        "-z",
        commit,
    )
    committed: set[str] = set()
    for raw in out.split(b"\0"):
        if not raw:
            continue
        name = raw.decode("utf-8", errors="surrogateescape")
        if name in TRANSPORT_PATHS:
            committed.add(name)
    return sorted(committed)


def _worktree_violations(worktree: Path) -> list[str]:
    """Tracked/index dirt and untracked/ignored files that are not transport."""
    out = candidate_git.git(
        worktree,
        "--no-optional-locks",
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignored=traditional",
    )
    violations: list[str] = []
    for raw in out.split(b"\0"):
        if len(raw) < 3:
            continue
        code = raw[:2].decode("ascii", errors="replace")
        path = raw[3:].decode("utf-8", errors="surrogateescape")
        if code in ("??", "!!"):
            if path not in TRANSPORT_PATHS:
                violations.append(f"unexpected:{path}")
        else:
            violations.append(f"tracked_change:{path}")
    return violations


def _envelope_violations(worktree: Path, commit: str) -> list[str]:
    """Filesystem differences from ``commit`` under an empty envelope.

    The worktree holds the pinned commit plus at most the untracked transport
    files, so a faithful tree differs from ``commit`` only by those files. Every
    other difference is a violation: an unexpected or ignored file, a tracked
    edit, git metadata, an escaping symlink, a hardlink, or an unsupported node.
    """
    found = candidate_git.check_envelope(
        worktree,
        commit,
        (),
        repository=worktree,
    )
    violations: list[str] = []
    for violation in found:
        if violation.startswith("outside_allowed:"):
            path = violation.split(":", 1)[1]
            if path in TRANSPORT_PATHS:
                continue
        violations.append(violation)
    return violations


def _snapshot_violations(snapshot: Path, worktree: Path, commit: str) -> list[str]:
    """The archive's transforms of ``commit``: a faithful export differs by nothing."""
    return candidate_git.check_envelope(
        snapshot,
        commit,
        (),
        repository=worktree,
    )


@contextmanager
def prepare_candidate(
    task: VerifyTask | Mapping[str, Any],
    worktree: str | Path,
) -> Iterator[Path]:
    """Verify ``worktree`` against ``task`` and yield a frozen candidate snapshot.

    The worktree must have ``HEAD`` at the task's candidate commit, hash to the
    task's digest, change only paths inside the task's envelope, and be otherwise
    clean: no tracked or index edits and no untracked or ignored files except the
    three transport files. The candidate commit must not itself carry a transport
    file. A tar export of the commit is extracted with the strict data filter,
    and any export-attribute transform is refused. The snapshot directory is
    removed on every exit. No test command runs and no verdict is signed.
    """
    resolved = _validate_task(task)

    wt = Path(worktree).resolve()

    head = _head(wt)
    if head != resolved.candidate_commit:
        raise candidate_git.CandidateGitError("head_mismatch", [head])

    audited = candidate_git.audit_inputs(wt, resolved.base_commit, resolved.candidate_commit)
    digest = str(audited["diff_sha256"])
    if digest != resolved.candidate_sha256:
        raise candidate_git.CandidateGitError("digest_mismatch", [digest])

    outside = [
        f"outside_allowed:{path}"
        for path in audited["changed_paths"]
        if not candidate_git.path_allowed(path, resolved.allowed_paths)
    ]
    if outside:
        raise candidate_git.CandidateGitError("envelope_violation", outside)

    committed = _committed_transport(wt, resolved.candidate_commit)
    if committed:
        raise candidate_git.CandidateGitError("committed_transport", committed)

    violations = _worktree_violations(wt)
    if violations:
        raise candidate_git.CandidateGitError("dirty_worktree", violations)

    envelope = _envelope_violations(wt, resolved.candidate_commit)
    if envelope:
        raise candidate_git.CandidateGitError("envelope_violation", envelope)

    archive = candidate_git.git(
        wt,
        "--no-optional-locks",
        "archive",
        "--format=tar",
        resolved.candidate_commit,
    )

    snapshot = Path(tempfile.mkdtemp(prefix="omp-verify-snapshot-"))
    try:
        try:
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
                tar.extractall(snapshot, filter="data")
        except tarfile.TarError as err:
            raise candidate_git.CandidateGitError(
                "snapshot_failed", [type(err).__name__]
            ) from err

        transformed = _snapshot_violations(snapshot, wt, resolved.candidate_commit)
        if transformed:
            raise candidate_git.CandidateGitError("envelope_violation", transformed)

        yield snapshot
    finally:
        shutil.rmtree(snapshot, ignore_errors=True)

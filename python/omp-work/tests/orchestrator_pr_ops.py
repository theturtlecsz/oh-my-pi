"""Push stage operation that commits on base and pushes candidate_ref (OMP-518-s05)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from omp_work.orchestrator.candidate_git import git
from omp_work.orchestrator.service import (
    Outcome,
    StageContext,
    register_stage_operation,
)

__all__ = ["register", "run_push"]

_ENV = {
    "GIT_AUTHOR_NAME": "Orchestrator",
    "GIT_AUTHOR_EMAIL": "orchestrator@omp.local",
    "GIT_COMMITTER_NAME": "Orchestrator",
    "GIT_COMMITTER_EMAIL": "orchestrator@omp.local",
}


def register() -> None:
    """Register the pr_ops push stage operation."""
    register_stage_operation("push", run_push)


def run_push(ctx: StageContext) -> Outcome:
    """Commit on base and push candidate_ref to remote."""
    req = ctx.request
    if isinstance(req, Mapping):
        base = str(req.get("base_commit") or "")
        ref = str(req.get("candidate_ref") or "")
        remote = str(req.get("remote") or "")
    else:
        base = str(getattr(req, "base_commit", "") or "")
        ref = str(getattr(req, "candidate_ref", "") or "")
        remote = str(getattr(req, "remote", "") or "")

    repo = Path(ctx.config.control_repo)
    tree = git(repo, "rev-parse", "--verify", f"{base}^{{tree}}").decode("ascii").strip()
    commit = git(
        repo,
        "-c",
        "commit.gpgSign=false",
        "commit-tree",
        tree,
        "-p",
        base,
        "-m",
        f"Candidate commit for {ctx.mission_id}",
        extra_env=_ENV,
    ).decode("ascii").strip()

    git(repo, "update-ref", ref, commit)
    git(repo, "push", remote, f"{commit}:{ref}")
    if ref.startswith("refs/heads/"):
        git(repo, "push", remote, f"{commit}:refs/heads/{ref}")

    return Outcome(
        outcome="succeeded",
        data={
            "stage": "push",
            "candidate_commit": commit,
            "candidate_ref": ref,
        },
    )

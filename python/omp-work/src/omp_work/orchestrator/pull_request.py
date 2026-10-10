"""Typed pull-request operations with intent logging (OMP-518-s03).

Provides open, check, read, checks-state, and merge operations for GitHub pull
requests, integrating with ``candidate_git.IntentLog`` and ``GitHubPulls``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path

from omp_work.orchestrator.candidate_git import IntentLog, _intent_key
from omp_work.orchestrator.github_pulls import (
    GitHubPulls,
    PullRequest,
    PullRequestError,
)

__all__ = [
    "check_pull",
    "merge_pull_request",
    "open_pull_request",
    "read_pull_request",
    "required_checks_state",
]

PASSED_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})


def check_pull(
    pull: PullRequest,
    *,
    head: str,
    base: str,
    commit: str,
) -> PullRequest:
    """Validate that ``pull`` targets ``head`` and ``base`` for ``commit``.

    Raises ``PullRequestError("target_mismatch")`` when ``head_ref`` or ``base_ref``
    does not match ``head`` or ``base``. Otherwise raises
    ``PullRequestError("head_mismatch")`` when ``head_sha`` does not match
    ``commit`` and the pull is not merged.
    """
    if pull.head_ref != head or pull.base_ref != base:
        raise PullRequestError("target_mismatch")
    if pull.head_sha != commit and not pull.merged:
        raise PullRequestError("head_mismatch")
    return pull


def read_pull_request(
    api: GitHubPulls,
    n: int,
    *,
    head: str,
    base: str,
    commit: str,
) -> PullRequest:
    """Fetch pull request ``n`` and validate it against ``head``, ``base``, and ``commit``."""
    pull = api.read(n)
    return check_pull(pull, head=head, base=base, commit=commit)


def open_pull_request(
    api: GitHubPulls,
    *,
    commit: str,
    head: str,
    base: str,
    title: str,
    body: str,
    intents: IntentLog,
    lease_ok: Callable[[], bool],
) -> PullRequest:
    """Open a pull request for ``head`` onto ``base`` or return an existing one."""
    if not lease_ok():
        raise PullRequestError("lease_dead")

    key = _intent_key("open", commit, api.repository, f"{head}->{base}")
    number = api.find(head)
    if number is not None:
        pull = api.read(number)
        check_pull(pull, head=head, base=base, commit=commit)
        if intents.open(key) is not None:
            intents.mark_done(key, str(number))
        return pull

    if intents.open(key) is None:
        intents.record_intent(key, "open", f"{api.repository} {head}->{base}")

    try:
        pull = api.create(head=head, base=base, title=title, body=body)
    except PullRequestError as exc:
        if exc.code == "unprocessable":
            number = api.find(head)
            if number is None:
                raise
            pull = api.read(number)
        else:
            raise

    check_pull(pull, head=head, base=base, commit=commit)
    if intents.open(key) is not None:
        intents.mark_done(key, str(pull.number))
    return pull


def required_checks_state(
    api: GitHubPulls,
    pull: PullRequest,
    required: Iterable[str],
) -> str:
    """Return 'passed', 'failed', or 'pending' for ``pull``'s required checks.

    Raises ``PullRequestError("required_checks_missing")`` when ``required`` is empty.
    """
    req_list = list(required)
    if not req_list:
        raise PullRequestError("required_checks_missing")

    conclusions = api.check_conclusions(pull.head_sha)
    has_pending = False
    for name in req_list:
        if name not in conclusions or conclusions[name] is None:
            has_pending = True
        elif conclusions[name] not in PASSED_CONCLUSIONS:
            return "failed"
    if has_pending:
        return "pending"
    return "passed"


def merge_pull_request(
    api: GitHubPulls,
    *,
    pull_number: int,
    commit: str,
    head: str,
    base: str,
    intents: IntentLog,
    lease_ok: Callable[[], bool],
    credential_path: Path | str | None,
) -> str:
    """Merge pull request ``pull_number`` and return the merge commit sha."""
    if credential_path is None:
        raise PullRequestError("credential_required")
    if not lease_ok():
        raise PullRequestError("lease_dead")

    pull = api.read(pull_number)
    check_pull(pull, head=head, base=base, commit=commit)

    key = _intent_key("merge", commit, api.repository, f"{head}->{base}")

    if pull.merged:
        merge_sha = pull.merge_commit_sha or ""
        if intents.open(key) is not None:
            intents.mark_done(key, merge_sha)
        return merge_sha

    if pull.state == "closed":
        raise PullRequestError("pull_request_closed")

    if intents.open(key) is None:
        intents.record_intent(key, "merge", f"{api.repository} {head}->{base}")

    merge_sha = api.merge(pull_number, commit, credential_path)
    intents.mark_done(key, merge_sha)
    return merge_sha

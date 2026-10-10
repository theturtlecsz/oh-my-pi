"""Merge stage operation via GitHub pull requests (OMP-518-s05).

Provides `run_merge(ctx)` bound to the `merge` stage. Validates candidate commit
and repository configuration, opens or reads the pull request under standing
policy, gates on required checks, and merges through `d41-merge-approval` with
an owner-released merge credential.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import UUID

from omp_work.action_classify import ResolvedTarget, classify, parse_submission
from omp_work.orchestrator.github_pulls import (
    GitHubPulls,
    PullRequest,
    PullRequestError,
)
from omp_work.orchestrator.merge_gate import (
    load_answered_decision,
    release_merge_credential,
)
from omp_work.orchestrator.pull_request import (
    merge_pull_request,
    open_pull_request,
    read_pull_request,
    required_checks_state,
)
from omp_work.orchestrator.service import (
    Outcome,
    StageContext,
    register_stage_operation,
)
from omp_work.orchestrator.stages import STAGE_ORDER, ActionVerdict
from omp_work.project_store import ProjectNotFound
from omp_work.standing_policy import RepositoryRecord

__all__ = ["register", "run_merge"]


def register() -> None:
    """Register the merge stage operation."""
    register_stage_operation("merge", run_merge)


def _find_candidate_commit(prior: Mapping[str, Any]) -> str | None:
    for stage in reversed(STAGE_ORDER):
        bucket = prior.get(stage)
        if isinstance(bucket, Mapping):
            data = bucket.get("data")
            if isinstance(data, Mapping) and data.get("candidate_commit"):
                return str(data["candidate_commit"])
    for bucket in reversed(list(prior.values())):
        if isinstance(bucket, Mapping):
            data = bucket.get("data")
            if isinstance(data, Mapping) and data.get("candidate_commit"):
                return str(data["candidate_commit"])
    return None


def _req_val(ctx: StageContext, key: str) -> str:
    req = ctx.request
    if isinstance(req, Mapping):
        return str(req.get(key) or "")
    return str(getattr(req, key, "") or "")


def run_merge(ctx: StageContext) -> Outcome:
    """Execute the merge stage: open/read pull request, check checks, and merge."""
    commit = _find_candidate_commit(ctx.prior)
    if not commit:
        return Outcome(
            outcome="failed",
            blocker="candidate_missing",
            data={"stage": "merge", "code": "candidate_missing"},
        )

    candidate_ref = _req_val(ctx, "candidate_ref")
    target_ref = _req_val(ctx, "target_ref")
    repository_key = _req_val(ctx, "repository")

    if not repository_key:
        return Outcome(
            outcome="failed",
            blocker="repository_missing",
            data={"stage": "merge", "code": "repository_missing"},
        )

    try:
        project = ctx.projects.read_project(
            UUID(str(ctx.workspace_id)),
            UUID(str(ctx.principal.actor_id)),
            UUID(str(ctx.project_id)),
        )
    except (ProjectNotFound, ValueError):
        return Outcome(
            outcome="failed",
            blocker="repository_missing",
            data={"stage": "merge", "code": "repository_missing"},
        )

    repos = project.get("repositories") or ()
    repo_dict = next(
        (
            item
            for item in repos
            if isinstance(item, Mapping) and str(item.get("key")) == repository_key
        ),
        None,
    )
    if repo_dict is None:
        return Outcome(
            outcome="failed",
            blocker="repository_missing",
            data={"stage": "merge", "code": "repository_missing"},
        )

    record = RepositoryRecord(
        key=str(repo_dict["key"]),
        default_branch=str(repo_dict.get("default_branch") or "main"),
        protected_branches=tuple(
            str(item) for item in (repo_dict.get("protected_branches") or ())
        ),
        automation_ci_secret_free=bool(repo_dict.get("automation_ci_secret_free")),
    )

    github_repo = ctx.config.github_repositories.get(repository_key)
    if not github_repo or ctx.config.github_token_path is None:
        return Outcome(
            outcome="failed",
            blocker="pull_request_config_missing",
            data={"stage": "merge", "code": "pull_request_config_missing"},
        )

    api = GitHubPulls(
        api_url=ctx.config.github_api_url,
        repository=github_repo,
        token_path=ctx.config.github_token_path,
    )

    pull_number: int | None = None
    try:
        # 2. Pull
        merge_bucket = ctx.prior.get("merge")
        merge_data = merge_bucket.get("data") if isinstance(merge_bucket, Mapping) else None
        prior_num = merge_data.get("pull_number") if isinstance(merge_data, Mapping) else None

        pull: PullRequest
        if prior_num is not None:
            pull = read_pull_request(
                api,
                int(prior_num),
                head=candidate_ref,
                base=target_ref,
                commit=commit,
            )
            pull_number = pull.number
        else:
            pr_operation = {
                "kind": "create_pull_request",
                "repository": repository_key,
                "branch": candidate_ref,
                "base_branch": target_ref,
                "commit": commit,
            }
            pr_resolved = ResolvedTarget(
                repository=record,
                commit=commit,
                changed_paths=(),
            )
            kept_pull: list[PullRequest] = []

            work = (
                ctx.request.get("work")
                if isinstance(ctx.request, Mapping)
                else getattr(ctx.request, "work", None)
            )
            if hasattr(work, "model_dump"):
                work_dict = work.model_dump(mode="json")
            elif isinstance(work, Mapping):
                work_dict = work
            else:
                work_dict = {}
            title = str(work_dict.get("title") or "Pull request")
            body = str(work_dict.get("description") or "")

            def open_executor(_op: Any, _target: Any) -> None:
                p = open_pull_request(
                    api,
                    commit=commit,
                    head=candidate_ref,
                    base=target_ref,
                    title=title,
                    body=body,
                    intents=ctx.intents,
                    lease_ok=ctx.lease_ok,
                )
                kept_pull.append(p)

            act_result = ctx.act(
                pr_operation, pr_resolved, open_executor, rule_id="create-pull-request"
            )
            if act_result.status != "done":
                classified = classify(parse_submission(pr_operation), pr_resolved)
                action_verdict = ActionVerdict(
                    tier=classified.tier,
                    action_class=classified.action_class,
                    target_sha256=classified.target_sha256,
                    allowed=False,
                    code=act_result.code or act_result.status,
                )
                return Outcome(
                    outcome="failed",
                    data={
                        "stage": "merge",
                        "status": act_result.status,
                        "code": act_result.code or act_result.status,
                    },
                    action=action_verdict,
                )

            if kept_pull:
                pull = kept_pull[0]
            else:
                found_num = api.find(candidate_ref)
                if found_num is None:
                    return Outcome(
                        outcome="failed",
                        blocker="pull_request_missing",
                        data={"stage": "merge", "code": "pull_request_missing"},
                    )
                pull = read_pull_request(
                    api,
                    found_num,
                    head=candidate_ref,
                    base=target_ref,
                    commit=commit,
                )
            pull_number = pull.number

        # 3. Required checks
        checks_state = required_checks_state(api, pull, ctx.config.required_checks)
        if checks_state == "pending":
            return Outcome(
                outcome="failed",
                data={
                    "stage": "merge",
                    "code": "checks_pending",
                    "pull_number": pull.number,
                },
            )
        if checks_state != "passed":
            return Outcome(
                outcome="failed",
                blocker="required_checks_failed",
                data={
                    "stage": "merge",
                    "code": "required_checks_failed",
                    "pull_number": pull.number,
                },
            )

        # 4. Merge
        merge_operation = {
            "kind": "merge",
            "repository": repository_key,
            "branch": target_ref,
            "commit": commit,
        }
        merge_resolved = ResolvedTarget(
            repository=record,
            commit=commit,
            changed_paths=(),
        )
        classified_merge = classify(parse_submission(merge_operation), merge_resolved)

        kept_merge_sha: list[str] = []

        def merge_executor(_op: Any, _target: Any) -> None:
            decision_id = ctx.answered_decision("d41-merge-approval")
            decision: dict[str, object] | None = None
            if decision_id is not None:
                with ctx._handler.store.transaction(
                    ctx.workspace_id, ctx.principal.actor_id
                ) as cur:
                    decision = load_answered_decision(cur, ctx.workspace_id, UUID(decision_id))

            cred_path: Path | None = None
            if ctx.config.merge_credential_path is not None:
                cred_path = release_merge_credential(
                    decision=decision,
                    workspace_id=ctx.workspace_id,
                    target_sha256=classified_merge.target_sha256,
                    allowed_signers=ctx.config.allowed_signers,
                    credential_path=ctx.config.merge_credential_path,
                )

            if cred_path is None:
                raise PullRequestError("credential_refused")

            merge_sha = merge_pull_request(
                api,
                pull_number=pull.number,
                commit=commit,
                head=candidate_ref,
                base=target_ref,
                intents=ctx.intents,
                lease_ok=ctx.lease_ok,
                credential_path=cred_path,
            )
            kept_merge_sha.append(merge_sha)

        merge_result = ctx.act(
            merge_operation,
            merge_resolved,
            merge_executor,
            rule_id="d41-merge-approval",
        )

        if merge_result.status != "done":
            action_verdict = ActionVerdict(
                tier=classified_merge.tier,
                action_class=classified_merge.action_class,
                target_sha256=classified_merge.target_sha256,
                allowed=False,
                code=merge_result.code or merge_result.status,
            )
            return Outcome(
                outcome="failed",
                data={
                    "stage": "merge",
                    "candidate_commit": commit,
                    "target_ref": target_ref,
                    "pull_number": pull.number,
                    "status": merge_result.status,
                    "code": merge_result.code or merge_result.status,
                },
                action=action_verdict,
            )

        if kept_merge_sha:
            merge_commit_sha = kept_merge_sha[0]
        else:
            re_pull = read_pull_request(
                api,
                pull.number,
                head=candidate_ref,
                base=target_ref,
                commit=commit,
            )
            merge_commit_sha = re_pull.merge_commit_sha or ""

        return Outcome(
            outcome="succeeded",
            data={
                "stage": "merge",
                "candidate_commit": commit,
                "target_ref": target_ref,
                "pull_number": pull.number,
                "merge_commit_sha": merge_commit_sha,
            },
        )

    except PullRequestError as exc:
        if exc.code == "outcome_unknown":
            raise
        err_data: dict[str, Any] = {"stage": "merge", "code": exc.code}
        if pull_number is not None:
            err_data["pull_number"] = pull_number
        return Outcome(outcome="failed", blocker=exc.code, data=err_data)

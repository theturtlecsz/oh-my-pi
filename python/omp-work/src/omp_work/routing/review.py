from __future__ import annotations

from .policy import RoutingPolicy, RoutingRefused


def check_reviewer(
    policy: RoutingPolicy,
    *,
    stage: str,
    reviewer: str,
    maker: str | None = None,
) -> None:
    """Refuse a reviewer that a stage does not admit independently.

    Identities are ``provider/model[:effort]`` strings exactly as written in the
    policy's ``reviewers`` lists. Accepted identities exist only in the policy
    file; none are compiled in here.
    """
    rule = policy.stages.get(stage)
    if rule is None or not rule.reviewers:
        raise RoutingRefused("not_a_review_stage")
    if reviewer not in rule.reviewers:
        raise RoutingRefused("reviewer_not_independent")
    if maker is not None and reviewer == maker:
        raise RoutingRefused("reviewer_is_maker")

"""Issue prefiltering before worktree and session creation."""

from __future__ import annotations

import json
import logging
import tomllib
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import TYPE_CHECKING, Any

from robomp.jev_client import (
    extract_bool_probability,
    extract_choice_probabilities,
)

if TYPE_CHECKING:
    from robomp.config import Settings
    from robomp.db import Database
    from robomp.jev_client import JevClient

log = logging.getLogger(__name__)

_PRIMARY_TYPES = (
    "bug",
    "enhancement",
    "question",
    "proposal",
    "documentation",
    "wontfix",
    "invalid",
    "duplicate",
)


@dataclass(slots=True, frozen=True)
class PrefilterResult:
    route: str
    label: str | None = None


@cache
def load_prefilter_questions() -> dict[str, Any]:
    """Load prefilter questions configuration from prompts/prefilter_questions.toml."""
    text = resources.files("robomp.prompts").joinpath("prefilter_questions.toml").read_text(encoding="utf-8")
    data = tomllib.loads(text)
    if not isinstance(data, dict):
        raise ValueError("prefilter_questions.toml must contain a TOML table")
    return data


async def run_prefilter(
    settings: Settings,
    db: Database,
    client: JevClient | None,
    key: str,
    title: str,
    body: str,
) -> PrefilterResult:
    """Evaluate an issue against Jev typed decisions before session creation.

    When both jev_enabled and prefilter_enabled are active, queries Jev with
    choice over primary types and yes/no gate questions.
    Returns:
      - route='answered', label='batch-audit' if batch_audit >= threshold
      - route='answered', label='invalid' | 'question' if top primary type in ('invalid', 'question') >= threshold
      - route='session', label=None otherwise (including failure, disabled, or client=None).
    Always records an issue_prefilter row in the database.
    """
    if client is None or not settings.jev_enabled or not settings.prefilter_enabled:
        db.record_issue_prefilter(
            key=key,
            route="session",
            label=None,
            probabilities_json="{}",
            request_id=None,
        )
        return PrefilterResult(route="session", label=None)

    state = f"{title}\n\n{body}" if body else title
    questions = load_prefilter_questions()

    decision = await client.decide(
        state=state,
        questions=questions,
        feature="robomp_prefilter",
        db=db,
    )

    if decision is None:
        db.record_issue_prefilter(
            key=key,
            route="session",
            label=None,
            probabilities_json="{}",
            request_id=None,
        )
        return PrefilterResult(route="session", label=None)

    answers = decision.answers
    primary_ans = answers.get("primary_type") or answers.get("primary")
    primary_probs = extract_choice_probabilities(primary_ans)

    top_label: str | None = None
    top_prob = 0.0
    if primary_probs:
        top_label = max(primary_probs, key=primary_probs.get)
        top_prob = primary_probs[top_label]

    batch_audit_prob = extract_bool_probability(answers.get("batch_audit"))
    first_person_failure_prob = extract_bool_probability(answers.get("first_person_failure"))
    wanted_different_behavior_prob = extract_bool_probability(answers.get("wanted_different_behavior"))
    upstream_cause_prob = extract_bool_probability(answers.get("upstream_cause"))
    nondefault_exotic_env_prob = extract_bool_probability(answers.get("nondefault_exotic_env"))

    threshold = settings.prefilter_threshold
    if batch_audit_prob >= threshold:
        route = "answered"
        label = "batch-audit"
    elif top_label in ("invalid", "question") and top_prob >= threshold:
        route = "answered"
        label = top_label
    else:
        route = "session"
        label = None

    probabilities = {
        "primary_type": primary_probs,
        "batch_audit": batch_audit_prob,
        "first_person_failure": first_person_failure_prob,
        "wanted_different_behavior": wanted_different_behavior_prob,
        "upstream_cause": upstream_cause_prob,
        "nondefault_exotic_env": nondefault_exotic_env_prob,
    }

    db.record_issue_prefilter(
        key=key,
        route=route,
        label=label,
        probabilities_json=json.dumps(probabilities),
        request_id=decision.request_id,
    )

    return PrefilterResult(route=route, label=label)

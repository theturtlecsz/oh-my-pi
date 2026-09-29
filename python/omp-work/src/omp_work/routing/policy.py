from __future__ import annotations

from dataclasses import dataclass, field
import importlib.resources
import json
from pathlib import Path
from typing import Any, Mapping

ALLOWED_VERSIONS = frozenset({"routing-policy.v1"})
EXPECTED_LADDER = ("E0", "E1", "E2", "E3", "E4")
VALID_TIER_EFFORTS = frozenset({"E1", "E2", "E3", "E4"})
VALID_ESCALATION_ON = frozenset({"failed_attempts", "review_rejected"})


class RoutingRefused(Exception):
    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


class RoutingPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class Tier:
    provider: str
    model: str
    est_tokens: int


@dataclass(frozen=True)
class BestOfN:
    n: int
    metric: str

    def __getitem__(self, item: str) -> Any:
        if item == "n":
            return self.n
        if item == "metric":
            return self.metric
        raise KeyError(item)


@dataclass(frozen=True)
class StageRule:
    rule: str
    effort: str
    provider: str
    model: str
    max_retries: int = 0
    best_of_n: BestOfN | None = None
    reviewers: tuple[str, ...] = ()


@dataclass(frozen=True)
class AlternateTarget:
    provider: str
    model: str

    def __getitem__(self, item: str) -> Any:
        if item == "provider":
            return self.provider
        if item == "model":
            return self.model
        raise KeyError(item)


@dataclass(frozen=True)
class AlternateRule:
    rule: str
    from_provider: str
    to: AlternateTarget

    @property
    def from_(self) -> str:
        return self.from_provider

    def __getitem__(self, item: str) -> Any:
        if item == "rule":
            return self.rule
        if item in ("from", "from_provider"):
            return self.from_provider
        if item == "to":
            return self.to
        raise KeyError(item)


@dataclass(frozen=True)
class ConcurrencyConfig:
    in_flight_max: int
    providers: Mapping[str, int]

    def __getitem__(self, item: str) -> Any:
        if item == "in_flight_max":
            return self.in_flight_max
        if item in ("providers", "provider_partitions"):
            return self.providers
        raise KeyError(item)

    def get(self, item: str, default: Any = None) -> Any:
        try:
            return self[item]
        except KeyError:
            return default


@dataclass(frozen=True)
class RetryRule:
    rule: str


@dataclass(frozen=True)
class EscalationRule:
    rule: str
    on: str
    step: int = 1


@dataclass(frozen=True)
class StageRoute:
    stage: str
    rule: str
    provider: str
    model: str
    effort: str
    attempt: int = 1
    applied: tuple[str, ...] = ()
    effort_reason: str | None = None

    def record(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "rule": self.rule,
            "provider": self.provider,
            "model": self.model,
            "effort": self.effort,
            "attempt": self.attempt,
            "applied": list(self.applied),
            "effort_reason": self.effort_reason,
        }


@dataclass(frozen=True)
class RoutingPolicy:
    version: str
    caller_model_fields: str
    ladder: tuple[str, ...]
    tiers: Mapping[str, Tier]
    concurrency: ConcurrencyConfig
    alternates: tuple[AlternateRule, ...]
    stages: Mapping[str, StageRule]
    missions: Mapping[str, tuple[str, ...]]
    retry: RetryRule
    escalation: tuple[EscalationRule, ...]
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def __getitem__(self, item: str) -> Any:
        return self.raw[item]

    def get(self, item: str, default: Any = None) -> Any:
        return self.raw.get(item, default)


def load_policy(path: str | Path | None = None) -> RoutingPolicy:
    if path is None:
        try:
            ref = importlib.resources.files("omp_work.routing").joinpath("routing-policy.v1.json")
            text = ref.read_text(encoding="utf-8")
        except Exception as exc:
            raise RoutingPolicyError(f"Failed to read packaged routing policy: {exc}") from exc
    else:
        p = Path(path)
        if not p.is_file():
            raise RoutingPolicyError(f"Routing policy file not found: {p}")
        text = p.read_text(encoding="utf-8")

    try:
        data = json.loads(text)
    except Exception as exc:
        raise RoutingPolicyError(f"Invalid JSON in routing policy: {exc}") from exc

    if not isinstance(data, dict):
        raise RoutingPolicyError("Routing policy must be a JSON object")

    return _validate_and_build(data)


def _validate_and_build(data: dict[str, Any]) -> RoutingPolicy:
    version = data.get("version")
    if version not in ALLOWED_VERSIONS:
        raise RoutingPolicyError(f"Invalid policy version: {version!r}, expected one of {sorted(ALLOWED_VERSIONS)}")

    ladder = data.get("ladder")
    if not isinstance(ladder, (list, tuple)) or tuple(ladder) != EXPECTED_LADDER:
        raise RoutingPolicyError(f"Invalid ladder: {ladder!r}, expected {list(EXPECTED_LADDER)}")

    caller_model_fields = data.get("caller_model_fields", "refuse")
    if not isinstance(caller_model_fields, str):
        raise RoutingPolicyError("caller_model_fields must be a string")

    concurrency_raw = data.get("concurrency")
    if not isinstance(concurrency_raw, dict):
        raise RoutingPolicyError("Missing or invalid concurrency section")
    in_flight_max = concurrency_raw.get("in_flight_max")
    if not isinstance(in_flight_max, int) or in_flight_max < 0:
        raise RoutingPolicyError("concurrency.in_flight_max must be a non-negative integer")
    providers_raw = concurrency_raw.get("providers")
    if not isinstance(providers_raw, dict):
        raise RoutingPolicyError("concurrency.providers must be an object")
    providers: dict[str, int] = {}
    for prov, cap in providers_raw.items():
        if not isinstance(cap, int) or cap < 0:
            raise RoutingPolicyError(f"Concurrency cap for provider {prov!r} must be a non-negative integer")
        providers[prov] = cap
    concurrency = ConcurrencyConfig(in_flight_max=in_flight_max, providers=providers)

    tiers_raw = data.get("tiers")
    if not isinstance(tiers_raw, dict):
        raise RoutingPolicyError("Missing or invalid tiers section")
    tiers: dict[str, Tier] = {}
    for tier_name, tier_info in tiers_raw.items():
        if tier_name not in VALID_TIER_EFFORTS:
            raise RoutingPolicyError(f"Tier effort outside E1..E4: {tier_name!r}")
        if not isinstance(tier_info, dict):
            raise RoutingPolicyError(f"Tier {tier_name} must be an object")
        provider = tier_info.get("provider")
        model = tier_info.get("model")
        est_tokens = tier_info.get("est_tokens")
        if not isinstance(provider, str) or not provider:
            raise RoutingPolicyError(f"Tier {tier_name} missing provider")
        if not isinstance(model, str) or not model:
            raise RoutingPolicyError(f"Tier {tier_name} missing model")
        if not isinstance(est_tokens, int) or est_tokens <= 0:
            raise RoutingPolicyError(f"Tier {tier_name} est_tokens must be a positive integer")
        if provider not in concurrency.providers:
            raise RoutingPolicyError(f"Provider {provider!r} in tier {tier_name} has no concurrency cap")
        tiers[tier_name] = Tier(provider=provider, model=model, est_tokens=est_tokens)

    alternates_raw = data.get("alternates", [])
    if not isinstance(alternates_raw, list):
        raise RoutingPolicyError("alternates must be a list")
    alternates: list[AlternateRule] = []
    for alt_entry in alternates_raw:
        if not isinstance(alt_entry, dict):
            raise RoutingPolicyError("Each alternate must be an object")
        rule = alt_entry.get("rule")
        from_prov = alt_entry.get("from")
        to_info = alt_entry.get("to")
        if not isinstance(rule, str) or not rule:
            raise RoutingPolicyError("Alternate missing rule")
        if not isinstance(from_prov, str) or not from_prov:
            raise RoutingPolicyError("Alternate missing from")
        if not isinstance(to_info, dict):
            raise RoutingPolicyError("Alternate missing to")
        to_prov = to_info.get("provider")
        to_model = to_info.get("model")
        if not isinstance(to_prov, str) or not to_prov:
            raise RoutingPolicyError("Alternate to missing provider")
        if not isinstance(to_model, str) or not to_model:
            raise RoutingPolicyError("Alternate to missing model")
        if from_prov not in concurrency.providers:
            raise RoutingPolicyError(f"Provider {from_prov!r} in alternate rule {rule!r} has no concurrency cap")
        if to_prov not in concurrency.providers:
            raise RoutingPolicyError(f"Provider {to_prov!r} in alternate rule {rule!r} has no concurrency cap")
        alternates.append(
            AlternateRule(
                rule=rule,
                from_provider=from_prov,
                to=AlternateTarget(provider=to_prov, model=to_model),
            )
        )

    stages_raw = data.get("stages")
    if not isinstance(stages_raw, dict):
        raise RoutingPolicyError("Missing or invalid stages section")
    stages: dict[str, StageRule] = {}
    for stage_name, stage_info in stages_raw.items():
        if not isinstance(stage_info, dict):
            raise RoutingPolicyError(f"Stage {stage_name} must be an object")
        rule = stage_info.get("rule")
        effort = stage_info.get("effort")
        provider = stage_info.get("provider")
        model = stage_info.get("model")
        max_retries = stage_info.get("max_retries", 0)
        best_of_n_raw = stage_info.get("best_of_n")
        reviewers_raw = stage_info.get("reviewers", ())

        if not isinstance(rule, str) or not rule:
            raise RoutingPolicyError(f"Stage {stage_name} missing rule")
        if effort not in VALID_TIER_EFFORTS:
            raise RoutingPolicyError(f"Stage {stage_name} effort outside E1..E4: {effort!r}")
        if not isinstance(provider, str) or not provider:
            raise RoutingPolicyError(f"Stage {stage_name} missing provider")
        if not isinstance(model, str) or not model:
            raise RoutingPolicyError(f"Stage {stage_name} missing model")
        if not isinstance(max_retries, int) or max_retries < 0:
            raise RoutingPolicyError(f"Stage {stage_name} max_retries must be a non-negative integer")
        if provider not in concurrency.providers:
            raise RoutingPolicyError(f"Provider {provider!r} in stage {stage_name} has no concurrency cap")

        bon: BestOfN | None = None
        if best_of_n_raw is not None:
            if not isinstance(best_of_n_raw, dict):
                raise RoutingPolicyError(f"Stage {stage_name} best_of_n must be an object")
            n = best_of_n_raw.get("n")
            metric = best_of_n_raw.get("metric")
            if not isinstance(n, int) or n < 2:
                raise RoutingPolicyError(f"best_of_n.n below 2 in stage {stage_name}: {n!r}")
            if not isinstance(metric, str) or not metric:
                raise RoutingPolicyError(f"Stage {stage_name} best_of_n missing metric")
            bon = BestOfN(n=n, metric=metric)

        if not isinstance(reviewers_raw, (list, tuple)):
            raise RoutingPolicyError(f"Stage {stage_name} reviewers must be a list or tuple")
        for r in reviewers_raw:
            if not isinstance(r, str) or not r:
                raise RoutingPolicyError(f"Stage {stage_name} invalid reviewer: {r!r}")

        stages[stage_name] = StageRule(
            rule=rule,
            effort=effort,
            provider=provider,
            model=model,
            max_retries=max_retries,
            best_of_n=bon,
            reviewers=tuple(reviewers_raw),
        )

    missions_raw = data.get("missions")
    if not isinstance(missions_raw, dict):
        raise RoutingPolicyError("Missing or invalid missions section")
    missions: dict[str, tuple[str, ...]] = {}
    for mission_name, stage_list in missions_raw.items():
        if not isinstance(stage_list, (list, tuple)):
            raise RoutingPolicyError(f"Mission {mission_name} stages must be a list")
        for stg in stage_list:
            if not isinstance(stg, str) or stg not in stages:
                raise RoutingPolicyError(f"Unknown mission stage {stg!r} in mission {mission_name}")
        missions[mission_name] = tuple(stage_list)

    retry_raw = data.get("retry")
    if not isinstance(retry_raw, dict):
        raise RoutingPolicyError("Missing or invalid retry section")
    retry_rule_str = retry_raw.get("rule")
    if not isinstance(retry_rule_str, str) or not retry_rule_str:
        raise RoutingPolicyError("retry missing rule")
    retry = RetryRule(rule=retry_rule_str)

    escalation_raw = data.get("escalation")
    if not isinstance(escalation_raw, list):
        raise RoutingPolicyError("Missing or invalid escalation section")
    escalations: list[EscalationRule] = []
    for esc_entry in escalation_raw:
        if not isinstance(esc_entry, dict):
            raise RoutingPolicyError("Each escalation rule must be an object")
        rule = esc_entry.get("rule")
        on_trigger = esc_entry.get("on")
        step = esc_entry.get("step", 1)
        if not isinstance(rule, str) or not rule:
            raise RoutingPolicyError("Escalation rule missing rule")
        if on_trigger not in VALID_ESCALATION_ON:
            raise RoutingPolicyError(f"Unknown escalation 'on' trigger: {on_trigger!r}")
        if not isinstance(step, int) or step < 1:
            raise RoutingPolicyError(f"Escalation step must be a positive integer: {step!r}")
        escalations.append(EscalationRule(rule=rule, on=on_trigger, step=step))

    rule_ids: list[str] = []
    for stg_rule in stages.values():
        rule_ids.append(stg_rule.rule)
    for alt_rule in alternates:
        rule_ids.append(alt_rule.rule)
    rule_ids.append(retry.rule)
    for esc_rule in escalations:
        rule_ids.append(esc_rule.rule)

    seen_rules: set[str] = set()
    for rid in rule_ids:
        if rid in seen_rules:
            raise RoutingPolicyError(f"Duplicate rule id: {rid!r}")
        seen_rules.add(rid)

    return RoutingPolicy(
        version=version,
        caller_model_fields=caller_model_fields,
        ladder=tuple(ladder),
        tiers=tiers,
        concurrency=concurrency,
        alternates=tuple(alternates),
        stages=stages,
        missions=missions,
        retry=retry,
        escalation=tuple(escalations),
        raw=dict(data),
    )

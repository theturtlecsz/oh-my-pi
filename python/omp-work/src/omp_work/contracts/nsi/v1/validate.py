"""Offline, fail-closed NSI schema validation and semantic helpers."""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Mapping

from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from jsonschema.validators import extend
from pydantic import BaseModel
from referencing import Registry, Resource

if TYPE_CHECKING:
    from .models import Gap, IntakeClaim, WaiverDefinition

_StrictValidator = extend(
    Draft202012Validator,
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine(
        "integer", lambda checker, value: type(value) is int,
    ),
)

SCHEMA_DIR = Path(__file__).parent
_SCHEMA_NAMES = frozenset({"authority", "taxonomy", "claim", "status", "contract"})


def load_schema(name: str) -> dict[str, object]:
    """Load a bundled document only; never resolve paths or fetch remote schemas."""
    document = name.removesuffix(".schema.json")
    if document not in _SCHEMA_NAMES:
        raise ValueError(f"Unknown NSI schema: {name}")
    return json.loads((SCHEMA_DIR / f"{document}.schema.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _registry() -> Registry:
    return Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema))
        for schema in (load_schema(name) for name in sorted(_SCHEMA_NAMES))
    )


def _wire(instance: object) -> object:
    if isinstance(instance, BaseModel):
        return _wire(instance.model_dump(exclude_unset=True))
    if instance is None or type(instance) in (str, bool, int):
        return instance
    if type(instance) is float:
        # JSON has no NaN/Infinity, despite Python's permissive JSON encoder.
        json.dumps(instance, allow_nan=False)
        return instance
    if type(instance) is list:
        return [_wire(value) for value in instance]
    if type(instance) is dict and all(type(key) is str for key in instance):
        return {key: _wire(value) for key, value in instance.items()}
    raise ValueError(f"Not a JSON value: {type(instance).__name__}")


@lru_cache(maxsize=None)
def _validator(schema_name: str) -> Draft202012Validator:
    document, separator, fragment = schema_name.partition("#")
    schema = load_schema(document)
    if separator:
        if not fragment.startswith("/$defs/"):
            raise ValueError(f"Unknown NSI definition: {schema_name}")
        # Resolve only known definitions, including Binding's composed fields.
        node = schema
        for part in fragment.removeprefix("/").split("/"):
            if not isinstance(node, dict) or part not in node:
                raise ValueError(f"Unknown NSI definition: {schema_name}")
            node = node[part]
        schema = {"$ref": f"{schema['$id']}#{fragment}"}
    return _StrictValidator(schema, registry=_registry(), format_checker=FormatChecker())


def validate_instance(schema_name: str, instance: object) -> None:
    """Validate untrusted JSON without coercion; errors raise ValueError.

    Use document names (``claim``) or definition selectors
    (``contract#/$defs/FormalCheckResult``). References resolve offline.
    """
    try:
        _validator(schema_name).validate(_wire(instance))
    except ValidationError as exc:
        path = "/".join(str(part) for part in exc.absolute_path)
        raise ValueError(f"NSI validation at {path or 'root'}: {exc.message}") from exc


# Only operational timestamps and explicit presentation fields are omitted.
# Source text, evidence hashes, revisions, and nested binding hashes remain semantic.
_NON_SEMANTIC = frozenset({"created_at", "updated_at", "layout", "presentation"})
_SELF_HASHES = {
    "content": frozenset({"contentHash", "content_hash"}),
    "closure": frozenset({"closureHash", "closure_hash"}),
    "contract": frozenset({"contractHash", "contract_hash"}),
}
_SET_FIELDS = frozenset({"claims", "entities", "artifacts", "predicates", "dimensions",
                        "requirements", "acceptance_criteria", "formalizations", "waivers",
                        "claim_ids", "dimension_ids", "requirement_ids", "premise_ids",
                        "conclusion_ids", "evidence_ids", "assumptions"})


def _canonical(value: object, *, omitted: frozenset[str] = frozenset(), field: str = "") -> object:
    if isinstance(value, dict):
        return {key: _canonical(item, field=key) for key, item in value.items()
                if key not in _NON_SEMANTIC and key not in omitted}
    if isinstance(value, list):
        items = [_canonical(item) for item in value]
        return sorted(items, key=_json) if field in _SET_FIELDS else items
    return value


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _hash(domain: str, instance: object) -> str:
    payload = _json(_canonical(_wire(instance), omitted=_SELF_HASHES[domain]))
    return hashlib.sha256(f"nsi/v1/{domain}\n{payload}".encode("utf-8")).hexdigest()


def content_hash(instance: object) -> str:
    """Hash semantic content, excluding its own stored digest and presentation."""
    return _hash("content", instance)


def closure_hash(instance: object) -> str:
    """Hash complete supplied closure, including source/rule/schema bindings."""
    return _hash("closure", instance)


def contract_hash(instance: object) -> str:
    """Hash requirements, criteria, formalizations, and exact revision waivers."""
    return _hash("contract", instance)


# Public wire spelling aliases.
contentHash = content_hash
closureHash = closure_hash
contractHash = contract_hash


def premise_truth(claims: Mapping[str, IntakeClaim], premise_id: str) -> Literal["true", "false", "unknown"]:
    """Absence or an unadmitted proposal supplies no truth evidence."""
    claim = claims.get(premise_id)
    if claim is None or claim.state != "admitted":
        return "unknown"
    return claim.truth


def validate_unknown_semantics(
    premises: Mapping[str, IntakeClaim], results: Mapping[str, str],
) -> None:
    """Reject treating an absent/unknown premise as true or refuted."""
    for premise_id, result in results.items():
        if result not in ("true", "false", "unknown"):
            raise ValueError(f"Invalid truth value for {premise_id}")
        if result != premise_truth(premises, premise_id):
            raise ValueError(f"Truth evidence mismatch for {premise_id}")


def validate_waiver(waiver: WaiverDefinition, gap: Gap, *, run_id: str, revision: int) -> None:
    """Apply owner waiver only to matching, permitted gap and record revision."""
    validate_instance("authority#/$defs/WaiverDefinition", waiver)
    validate_instance("status#/$defs/Gap", gap)
    if gap.materiality == "critical" or gap.waiver_policy != "owner_with_reason":
        raise ValueError("Gap cannot be waived")
    if waiver.waiver_policy != gap.waiver_policy or waiver.materiality != gap.materiality:
        raise ValueError("Waiver does not match gap policy")
    if (waiver.dimension_id, waiver.premise_id) != (gap.dimension_id, gap.premise_id):
        raise ValueError("Waiver does not match exact premise")
    if (waiver.run_id, waiver.revision) != (run_id, revision):
        raise ValueError("Waiver is stale or belongs to another run")
    if not waiver.reason.strip() or not waiver.decision_id.strip():
        raise ValueError("Waiver requires explicit reason and decision")


__all__ = ["load_schema", "validate_instance", "content_hash", "closure_hash", "contract_hash",
           "contentHash", "closureHash", "contractHash", "premise_truth",
           "validate_unknown_semantics", "validate_waiver"]

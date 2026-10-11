"""Fail-closed gate that admits only trusted NSI state as canonical."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from typing import NoReturn, TypeVar

from pydantic import ValidationError

from . import models
from .validate import validate_instance

T = TypeVar("T")


class AuthorityRefusal(ValueError):
    """Content was refused as NSI canonical state."""


def _exported_record_models() -> tuple[type[models.NSIModel], ...]:
    found: list[type[models.NSIModel]] = []
    for name in models.__all__:
        value = getattr(models, name)
        if isinstance(value, type) and issubclass(value, models.NSIModel):
            found.append(value)
    return tuple(found)


# Exported shapes, including snapshots, contracts, ontologies, and id-less receipts.
_RECORD_MODELS = _exported_record_models()
_RECORD_MODEL_TYPES = frozenset(_RECORD_MODELS)


def _refuse_content() -> NoReturn:
    raise AuthorityRefusal("markdown and other non-records cannot be canonical state")


def _json_wire(value: object) -> bool:
    """True for a JSON tree. Nested models and other Python values are not wire.

    Model validation accepts an already-built model and coerces ``Decimal`` into
    a number field. ``validate_instance`` also rewrites models into dicts before
    checking constraints. Either path would admit the original non-wire object,
    so the tree has to be JSON before a model is asked.
    """
    if value is None or type(value) in (str, bool, int):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is list:
        return all(_json_wire(item) for item in value)
    if type(value) is dict:
        return all(type(key) is str and _json_wire(item) for key, item in value.items())
    return False


def _accepts_model(model: type[models.NSIModel], value: dict) -> bool:
    """Validate plain data against one concrete model and its wire schema."""
    try:
        if model.schema_name is not None:
            validate_instance(model.schema_name, value)
        model.model_validate(value)
    except (ValidationError, ValueError):
        return False
    return True


def _accepts_record(value: dict) -> bool:
    """True when one exported model accepts the wire dict without coercion."""
    return any(_accepts_model(model, value) for model in _RECORD_MODELS)


def _admit_wire(content: object) -> None:
    # ``content`` is plain data. Models are projected before this runs, and
    # strings are never parsed. The JSON check covers nested values; record
    # admission still uses the exported models and their schemas.
    if _json_wire(content):
        if type(content) is list:
            if all(type(item) is dict and _accepts_record(item) for item in content):
                return
        elif type(content) is dict and _accepts_record(content):
            return
    _refuse_content()


def _unset_default(model: models.NSIModel, name: str, value: object) -> bool:
    """True when ``value`` is an unset field default, absent from the wire.

    Schemas reject an explicit null for those fields. A stored value that is
    not the default stays in the projection, including a later ``__dict__`` write.
    """
    if name in model.__pydantic_fields_set__:
        return False
    field = type(model).model_fields[name]
    return not field.is_required() and value is field.default


def _project_model(model: models.NSIModel) -> dict[str, object]:
    """Read one model's stores into plain data.

    ``model_validate`` on an existing instance returns that instance and leaves
    mutated containers in place. ``model_dump`` omits undeclared ``__dict__``
    keys and Pydantic extras. Admission reads those stores and keeps every
    nested value for the strict wire check.
    """
    if type(model) not in _RECORD_MODEL_TYPES:
        _refuse_content()
    if model.__pydantic_extra__ or model.__pydantic_private__:
        _refuse_content()
    declared = type(model).model_fields
    storage = model.__dict__
    if any(key not in declared for key in storage):
        _refuse_content()
    if any(key not in declared for key in model.__pydantic_fields_set__):
        _refuse_content()
    projected: dict[str, object] = {}
    for key, value in storage.items():
        if _unset_default(model, key, value):
            continue
        projected[key] = _project_stored(value)
    # Preserve the concrete type's contract before aggregate wire admission
    # can interpret these fields as a different exported record or union arm.
    if not _json_wire(projected) or not _accepts_model(type(model), projected):
        _refuse_content()
    return projected


def _project_stored(value: object) -> object:
    """Project nested models and JSON containers without dropping stored values.

    Tuples and other non-JSON containers stay as they are. Converting them to
    lists would turn unchecked storage into wire.
    """
    if isinstance(value, models.NSIModel):
        return _project_model(value)
    if type(value) is list:
        return [_project_stored(item) for item in value]
    if type(value) is dict:
        return {key: _project_stored(item) for key, item in value.items()}
    return value


def _project_concrete_record(record: object, expected_type: type[models.NSIModel]) -> dict[str, object]:
    """Validate and project either an exact typed model or a wire dict for expected_type."""
    if type(record) is expected_type:
        return _project_model(record)
    if type(record) is dict:
        projected = _project_stored(record)
        if _json_wire(projected) and _accepts_model(expected_type, projected):
            return projected
    raise AuthorityRefusal(f"expected valid typed or wire {expected_type.__name__}")


def check_claim_authority(
    claim: object,
    actor: object,
    *,
    predicates: Mapping[str, object] | None = None,
    artifacts: Mapping[str, models.EvidenceArtifact] | None = None,
    repository_sources: Mapping[tuple[str, str], bytes] | None = None,
) -> None:
    """Validate claim authority against the calling actor and predicate rules.

    Strictly validates typed or wire IntakeClaim and ActorRef via shared helpers,
    refusing schema violations, actor spoofing, missing predicates, unpermitted
    admissions, and model proposals supplying admitted truth. Evidence resolves
    only through caller-supplied repository bytes, never claim-owned content.
    """
    projected_claim = _project_concrete_record(claim, models.IntakeClaim)
    projected_actor = _project_concrete_record(actor, models.ActorRef)

    if not isinstance(predicates, Mapping):
        raise AuthorityRefusal("missing or unresolved predicate definition")

    predicate_id = projected_claim["predicate_id"]
    if predicate_id not in predicates:
        raise AuthorityRefusal(f"unresolved predicate: {predicate_id}")

    raw_predicate = predicates[predicate_id]
    projected_predicate = _project_concrete_record(raw_predicate, models.PredicateDefinition)
    if projected_predicate["id"] != predicate_id:
        raise AuthorityRefusal(f"predicate ID mismatch: expected {predicate_id}, got {projected_predicate['id']}")

    allowed_authorities = projected_predicate["authority"]
    claim_state = projected_claim["state"]
    claim_truth = projected_claim["truth"]
    asserted_by = projected_claim["asserted_by"]
    admitted_by = projected_claim.get("admitted_by")

    if claim_state == "proposed":
        if projected_actor["id"] != asserted_by["id"] or projected_actor["kind"] != asserted_by["kind"]:
            raise AuthorityRefusal("actor does not match asserted_by for proposed claim")
        if claim_truth != "unknown":
            raise AuthorityRefusal("proposals cannot supply admitted truth")
        if admitted_by is not None:
            raise AuthorityRefusal("proposals cannot supply admitted truth or admitted_by")
    elif claim_state == "admitted":
        if admitted_by is None:
            raise AuthorityRefusal("admitted claim must specify admitted_by")
        if projected_actor["id"] != admitted_by["id"] or projected_actor["kind"] != admitted_by["kind"]:
            raise AuthorityRefusal("caller must match admitted_by for admitted claim")
        if projected_actor["kind"] not in ("owner", "rule_engine"):
            raise AuthorityRefusal("admitted claims must be admitted by owner or rule_engine, never model or tool")
        if asserted_by["kind"] not in allowed_authorities:
            raise AuthorityRefusal(
                f"claim asserted_by kind '{asserted_by['kind']}' is not permitted by predicate authority {allowed_authorities}"
            )
    else:
        raise AuthorityRefusal(f"unsupported claim state for authority check: {claim_state}")

    observation = projected_claim["origin"] in ("repository_observation", "tool_receipt")
    if observation and asserted_by["kind"] != "tool":
        raise AuthorityRefusal("observation claims must be asserted by a tool")
    evidence = projected_claim["evidence"]
    if not evidence and (observation or (claim_state == "admitted" and projected_actor["kind"] == "rule_engine")):
        raise AuthorityRefusal("observations and rule-engine admissions require repository evidence")
    for span in evidence:
        if not isinstance(artifacts, Mapping) or not isinstance(repository_sources, Mapping):
            raise AuthorityRefusal("evidence requires artifacts and trusted repository sources")
        artifact_id = span["artifact_id"]
        if artifact_id not in artifacts:
            raise AuthorityRefusal(f"unresolved evidence artifact: {artifact_id}")
        artifact = _project_concrete_record(artifacts[artifact_id], models.EvidenceArtifact)
        if artifact["id"] != artifact_id or artifact["actor"]["kind"] != "tool":
            raise AuthorityRefusal("evidence artifact must match its ID and identify a tool")
        source = repository_sources.get((artifact["uri"], artifact["revision"]))
        if not isinstance(source, bytes):
            raise AuthorityRefusal("evidence repository source unavailable for URI and revision")
        if hashlib.sha256(source).hexdigest() != artifact["content_hash"]:
            raise AuthorityRefusal("evidence artifact hash does not match repository bytes")
        start, end = span["start_byte"], span["end_byte"]
        if not 0 <= start < end <= len(source):
            raise AuthorityRefusal("evidence span is outside repository byte bounds")
        if "quote" in span:
            try:
                quote = span["quote"].encode("utf-8")
            except UnicodeEncodeError as exc:
                raise AuthorityRefusal("evidence quote is not UTF-8") from exc
            if quote != source[start:end]:
                raise AuthorityRefusal("evidence quote does not match repository bytes")


def refuse_markdown_canonical_state(content: T, *, context: object = None) -> T:
    """Return ``content`` unchanged when it is trusted NSI canonical state.

    ``context`` is trusted only when it is a nonempty mapping. The mapping is
    not an actor authorization. Omitted, ``None``, and empty mappings refuse
    even valid records. Accepted values are exported concrete NSI records,
    snapshots, contracts, ontologies, id-less receipts, and lists of those
    records. Each may be the model, the matching closed wire dict, or a mix of
    the two. An empty list is an empty record collection.

    A model is admitted only when its stored fields project to plain data that
    passes its concrete strict model and wire schema recursively. Undeclared
    ``__dict__`` fields, private stores, and Pydantic extras are refused. Raw
    Markdown, prose, bytes, JSON text, non-JSON values, unknown wrappers, and prose inside a record
    collection are refused, including when nested inside an otherwise valid
    record. Declared text and value fields stay data.
    """
    if not isinstance(context, Mapping) or len(context) == 0:
        raise AuthorityRefusal("canonical state requires a trusted context")
    _admit_wire(_project_stored(content))
    return content


__all__ = [
    "AuthorityRefusal",
    "check_claim_authority",
    "refuse_markdown_canonical_state",
]

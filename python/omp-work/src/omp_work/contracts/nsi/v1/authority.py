"""Fail-closed gate that admits only trusted NSI wire as canonical state."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import TypeVar

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


def _accepts_record(value: dict) -> bool:
    """True when one exported model accepts the wire dict without coercion."""
    for model in _RECORD_MODELS:
        schema_name = model.schema_name
        if schema_name is not None:
            try:
                validate_instance(schema_name, value)
            except ValueError:
                continue
        try:
            model.model_validate(value)
        except (ValidationError, ValueError):
            continue
        return True
    return False


def _admit_wire(content: object) -> None:
    # Plain dicts and lists are the wire form. Typed models stay refused until
    # a later slice, and strings are never parsed. The JSON check covers nested
    # values; record admission still uses the exported models and their schemas.
    if _json_wire(content):
        if type(content) is list:
            if all(type(item) is dict and _accepts_record(item) for item in content):
                return
        elif type(content) is dict and _accepts_record(content):
            return
    raise AuthorityRefusal("markdown and other non-records cannot be canonical state")


def refuse_markdown_canonical_state(content: T, *, context: object = None) -> T:
    """Return ``content`` unchanged when it is trusted NSI canonical wire.

    ``context`` is trusted only when it is a nonempty mapping. The mapping is
    not an actor authorization. Omitted, ``None``, and empty mappings refuse
    even valid records. Accepted values are exported NSI wire objects, including
    snapshots, contracts, ontologies, id-less receipts, and lists of those
    records. An empty list is an empty record collection. Raw Markdown, prose,
    bytes, JSON text, typed models, non-JSON values, unknown wrappers, and prose
    inside a record collection are refused, including when nested inside an
    otherwise valid record. Declared text and value fields stay data.
    """
    if not isinstance(context, Mapping) or len(context) == 0:
        raise AuthorityRefusal("canonical state requires a trusted context")
    _admit_wire(content)
    return content


__all__ = ["AuthorityRefusal", "refuse_markdown_canonical_state"]

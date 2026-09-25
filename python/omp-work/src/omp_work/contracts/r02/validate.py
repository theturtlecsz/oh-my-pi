"""Minimal R02 validators — fail precisely on malformed contracts."""
from __future__ import annotations

import functools
import json
import math
from pathlib import Path
from typing import Any

SCHEMA_DIR = Path(__file__).resolve().parent


@functools.lru_cache(maxsize=None)
def load_schema(name: str) -> dict[str, Any]:
    if not name.endswith(".json"):
        candidate = SCHEMA_DIR / f"{name}.schema.json"
        if candidate.is_file():
            path = candidate
        else:
            path = SCHEMA_DIR / f"{name}.json"
    else:
        path = SCHEMA_DIR / name
    return json.loads(path.read_text(encoding="utf-8"))


def _validate(val: Any, schema: dict[str, Any], field: str) -> None:
    expected_type = schema.get("type")
    if expected_type == "object":
        if not isinstance(val, dict):
            raise ValueError(f"field '{field}': expected object, got {type(val).__name__}")
    elif expected_type == "array":
        if not isinstance(val, list):
            raise ValueError(f"field '{field}': expected array, got {type(val).__name__}")
    elif expected_type == "string":
        if not isinstance(val, str):
            raise ValueError(f"field '{field}': expected string, got {type(val).__name__}")
    elif expected_type == "integer":
        if isinstance(val, bool) or not isinstance(val, int):
            raise ValueError(f"field '{field}': expected integer, got {type(val).__name__}")
    elif expected_type == "number":
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            raise ValueError(f"field '{field}': expected number, got {type(val).__name__}")
        if not math.isfinite(val):
            raise ValueError(f"field '{field}': metric or number must be finite, got {val}")
    elif expected_type == "boolean":
        if not isinstance(val, bool):
            raise ValueError(f"field '{field}': expected boolean, got {type(val).__name__}")

    if "enum" in schema:
        allowed = schema["enum"]
        if val not in allowed:
            raise ValueError(f"field '{field}': invalid value {val!r}, expected one of {allowed}")

    if "minimum" in schema:
        min_val = schema["minimum"]
        if val < min_val:
            raise ValueError(f"field '{field}': value {val} is less than minimum {min_val}")

    if "minLength" in schema:
        min_len = schema["minLength"]
        if len(val) < min_len:
            raise ValueError(f"field '{field}': length {len(val)} is less than minLength {min_len}")

    if "required" in schema and isinstance(val, dict):
        for req_field in schema["required"]:
            if req_field not in val:
                raise ValueError(f"field '{req_field}': missing required field")

    if "properties" in schema and isinstance(val, dict):
        for prop_name, prop_schema in schema["properties"].items():
            if prop_name in val:
                _validate(val[prop_name], prop_schema, field=prop_name)

    if "additionalProperties" in schema and isinstance(val, dict):
        additional = schema["additionalProperties"]
        props = schema.get("properties", {})
        if additional is False:
            for k in val:
                if k not in props:
                    raise ValueError(f"field '{k}': forbidden extra field")
        elif isinstance(additional, dict):
            for k, sub_val in val.items():
                if k not in props:
                    _validate(sub_val, additional, field=k)

    if "items" in schema and isinstance(val, list):
        items_schema = schema["items"]
        for item in val:
            _validate(item, items_schema, field=field)


def validate_instance(schema_name: str, instance: dict[str, Any]) -> None:
    schema = load_schema(schema_name)
    _validate(instance, schema, field="root")


def assert_execution_status_not_scientific_success(trial_status: str, evidence_trust: str) -> None:
    """Execution status cannot become scientific success (R02 acceptance)."""
    if trial_status == "succeeded" and evidence_trust == "untrusted":
        raise ValueError("succeeded trial cannot imply scientific success with untrusted evidence")

"""Isolated strategy protocol for code and config candidates (R13).

The candidate receives a sanitized snapshot and returns a proposal. It never
sees controller state. Process isolation stays with the R05 jail.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from omp_work.v1.canonical import canonical_json, sha256

__all__ = [
    "MAX_PROPOSAL_BYTES",
    "MAX_SOURCE_BYTES",
    "config_candidate",
    "load_strategy",
    "run_candidate",
    "run_with_fallback",
    "sanitized_snapshot",
    "validate_proposal",
]

MAX_SOURCE_BYTES = 8192
MAX_PROPOSAL_BYTES = 4096

_FORBIDDEN_NAMES = frozenset(
    {
        "open",
        "exec",
        "eval",
        "compile",
        "__import__",
        "input",
        "globals",
        "locals",
        "vars",
        "breakpoint",
        "exit",
        "quit",
        "getattr",
        "setattr",
        "delattr",
    }
)
_TAMPER_FRAGMENTS = ("receipt", "evaluator", "verify", "qualifies")
_REASON_ORDER = (
    "bad_schema",
    "proposal_too_large",
    "unknown_node",
    "parent_not_open",
    "action_not_allowed",
    "resource_exceeded",
)
_SAFE_BUILTINS: dict[str, Any] = {
    "sorted": sorted,
    "min": min,
    "max": max,
    "len": len,
    "sum": sum,
    "abs": abs,
    "round": round,
    "range": range,
    "enumerate": enumerate,
    "zip": zip,
    "float": float,
    "int": int,
    "str": str,
    "bool": bool,
    "list": list,
    "dict": dict,
    "set": set,
    "tuple": tuple,
    "frozenset": frozenset,
    "any": any,
    "all": all,
    "reversed": reversed,
}
_MISSING = object()


def load_strategy(source: str) -> Callable[..., tuple[Any, Any]]:
    """Compile one restricted ``propose(steps, opened, *, seed, width)`` function."""
    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise ValueError("too_large")
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as exc:
        raise ValueError("broken_code") from exc
    _reject_forbidden(tree)
    _require_interface(tree)
    namespace: dict[str, Any] = {"__builtins__": dict(_SAFE_BUILTINS)}
    compiled = compile(tree, "<strategy>", "exec")
    # AST-gated source runs under the allowlisted builtins above.
    # pylint: disable-next=exec-used
    exec(compiled, namespace)  # nosec B102
    propose = namespace.get("propose")
    if not callable(propose):
        raise ValueError("wrong_interface")
    return propose


def sanitized_snapshot(
    steps: Mapping[str, Any], opened: Iterable[Any]
) -> tuple[dict[str, Any], frozenset[Any]]:
    """Return a fresh steps dict and opened frozenset via a canonical JSON round trip."""
    cloned = json.loads(canonical_json({"opened": list(opened), "steps": steps}))
    return cloned["steps"], frozenset(cloned["opened"])


def validate_proposal(
    proposal: object,
    steps: Mapping[str, Any],
    opened: Iterable[Any],
    *,
    allowed_actions: Iterable[str],
    resource_caps: Mapping[str, Any],
) -> list[str]:
    """Return ordered, deduped rejection reasons. An empty list is sound."""
    found: set[str] = set()
    if not _schema_ok(proposal):
        found.add("bad_schema")
    size = _canonical_size(proposal)
    if size is not None and size > MAX_PROPOSAL_BYTES:
        found.add("proposal_too_large")
    if isinstance(proposal, dict):
        step_id = proposal.get("step_id", _MISSING)
        action = proposal.get("action", _MISSING)
        resources = proposal.get("resources", _MISSING)
        if isinstance(step_id, str):
            if step_id not in steps:
                found.add("unknown_node")
            else:
                node = steps[step_id]
                parent = node.get("parent_id") if isinstance(node, Mapping) else None
                if parent is not None and parent not in opened:
                    found.add("parent_not_open")
        if isinstance(action, str) and action not in allowed_actions:
            found.add("action_not_allowed")
        if isinstance(resources, dict) and _resources_exceeded(resources, resource_caps):
            found.add("resource_exceeded")
    return [reason for reason in _REASON_ORDER if reason in found]


def run_candidate(
    source_or_fn: str | Callable[..., tuple[Any, Any]],
    steps: Mapping[str, Any],
    opened: Iterable[Any],
    *,
    seed: int = 0,
    width: int = 1,
    allowed_actions: Iterable[str],
    resource_caps: Mapping[str, Any],
) -> dict[str, Any]:
    """Load and call a candidate on a snapshot. The caller's tree is never mutated."""
    if isinstance(source_or_fn, str):
        try:
            fn = load_strategy(source_or_fn)
        except ValueError as exc:
            return _failed(str(exc), seed)
    else:
        fn = source_or_fn
    snap_steps, snap_opened = sanitized_snapshot(steps, opened)
    try:
        returned = fn(snap_steps, snap_opened, seed=seed, width=width)
        if not isinstance(returned, tuple) or len(returned) != 2:
            raise TypeError("propose must return (step_id, seed)")
        step_id, new_seed = returned
    except Exception:
        return _failed("crashed", seed)
    proposal = {
        "step_id": step_id,
        "action": "open",
        "resources": _module_resources(fn),
    }
    reasons = validate_proposal(
        proposal,
        steps,
        opened,
        allowed_actions=allowed_actions,
        resource_caps=resource_caps,
    )
    return {
        "status": "ok" if not reasons else "rejected",
        "proposal": proposal,
        "reasons": reasons,
        "seed": new_seed,
    }


def run_with_fallback(
    source: str | Callable[..., tuple[Any, Any]],
    steps: Mapping[str, Any],
    opened: Iterable[Any],
    *,
    fallback_fn: Callable[..., tuple[Any, Any]],
    seed: int = 0,
    width: int = 1,
    **kwargs: Any,
) -> dict[str, Any]:
    """Return the candidate result when it is ok; otherwise the qualified policy's pick."""
    result = run_candidate(source, steps, opened, seed=seed, width=width, **kwargs)
    if result["status"] == "ok":
        return result
    return {
        "status": "fallback",
        "proposal": fallback_fn(steps, opened, seed=seed, width=width),
    }


def config_candidate(parameters: object, parent: object) -> dict[str, Any]:
    """Record a config candidate and the sha256 of its canonical parameters."""
    cloned = json.loads(canonical_json(parameters))
    return {
        "kind": "config",
        "parameters": cloned,
        "parent": parent,
        "sha256": sha256(cloned),
    }


def _failed(reason: str, seed: int) -> dict[str, Any]:
    return {
        "status": "failed",
        "reason": reason,
        "proposal": None,
        "reasons": [],
        "seed": seed,
    }


def _module_resources(fn: Callable[..., Any]) -> Any:
    global_ns = getattr(fn, "__globals__", None)
    if not isinstance(global_ns, dict) or "RESOURCES" not in global_ns:
        return {}
    raw = global_ns["RESOURCES"]
    if isinstance(raw, dict):
        return dict(raw)
    return raw


def _reject_forbidden(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise ValueError("forbidden_import")
    for ident, is_attr in _identifiers(tree):
        if ident in _FORBIDDEN_NAMES or (is_attr and _is_dunder(ident)):
            raise ValueError("forbidden_name")
    for ident, _is_attr in _identifiers(tree):
        lowered = ident.lower()
        if any(fragment in lowered for fragment in _TAMPER_FRAGMENTS):
            raise ValueError("evaluator_tampering")


def _identifiers(tree: ast.AST) -> list[tuple[str, bool]]:
    found: list[tuple[str, bool]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.append((node.id, False))
        elif isinstance(node, ast.Attribute):
            found.append((node.attr, True))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.append((node.name, False))
        elif isinstance(node, ast.arg):
            found.append((node.arg, False))
        elif isinstance(node, ast.keyword) and node.arg:
            found.append((node.arg, False))
        elif isinstance(node, ast.alias):
            found.append((node.name, False))
            if node.asname:
                found.append((node.asname, False))
        elif isinstance(node, ast.ExceptHandler) and node.name:
            found.append((node.name, False))
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            found.extend((name, False) for name in node.names)
        elif isinstance(node, ast.MatchAs) and node.name:
            found.append((node.name, False))
    return found


def _is_dunder(name: str) -> bool:
    return len(name) >= 4 and name.startswith("__") and name.endswith("__")


def _require_interface(tree: ast.Module) -> None:
    functions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if len(functions) != 1 or not isinstance(functions[0], ast.FunctionDef) or functions[0].name != "propose":
        raise ValueError("wrong_interface")
    args = functions[0].args
    positional = [*args.posonlyargs, *args.args]
    if [arg.arg for arg in positional] != ["steps", "opened"]:
        raise ValueError("wrong_interface")
    if args.vararg is not None or args.kwarg is not None:
        raise ValueError("wrong_interface")
    if [arg.arg for arg in args.kwonlyargs] != ["seed", "width"]:
        raise ValueError("wrong_interface")
    if len(args.kw_defaults) != 2 or any(default is None for default in args.kw_defaults):
        raise ValueError("wrong_interface")


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _schema_ok(proposal: object) -> bool:
    if not isinstance(proposal, dict):
        return False
    if set(proposal) != {"step_id", "action", "resources"}:
        return False
    step_id = proposal["step_id"]
    if step_id is not None and not isinstance(step_id, str):
        return False
    if not isinstance(proposal["action"], str):
        return False
    resources = proposal["resources"]
    if not isinstance(resources, dict):
        return False
    return all(isinstance(name, str) and _is_number(value) for name, value in resources.items())


def _canonical_size(proposal: object) -> int | None:
    try:
        return len(canonical_json(proposal).encode("utf-8"))
    except (TypeError, ValueError):
        return None


def _resources_exceeded(resources: Mapping[Any, Any], resource_caps: Mapping[str, Any]) -> bool:
    for name, value in resources.items():
        if not isinstance(name, str) or not _is_number(value):
            continue
        if name not in resource_caps or float(value) > float(resource_caps[name]):
            return True
    return False

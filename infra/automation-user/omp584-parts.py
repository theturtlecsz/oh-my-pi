"""Read-only structural classification of OMP-584 flood.py rollout parts."""
from __future__ import annotations

import argparse
import ast
from pathlib import Path
import sys


FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
PART_MARKERS = {
    "dependencies": (
        "dependency_tokens", "parse_dependency_context", "dependency_edge_violation",
        "validate_slices calls dependency_edge_violation",
    ),
    "start-metrics": ("CODE_SHA256", "Flood.start_event", "Flood.spawn calls self.start_event"),
    "planner-prompt": (
        "PLANNER_RULES_BYTES", "PLANNER_INDEPENDENCE", "PLANNER_RULES_SHA256",
        "Flood.plan_prompt uses PLANNER_INDEPENDENCE",
        "Flood.start_plan_review uses PLANNER_INDEPENDENCE",
        'Flood.run logs "loaded " using CODE_SHA256 and PLANNER_RULES_SHA256',
    ),
}


def markers(tree: ast.Module) -> dict[str, dict[str, bool]]:
    """Inspect syntax only; absent or malformed code never executes."""
    found = {part: dict.fromkeys(names, False) for part, names in PART_MARKERS.items()}
    functions = {node.name: node for node in tree.body if isinstance(node, FUNCTIONS)}
    methods = {
        node.name: node
        for cls in tree.body if isinstance(cls, ast.ClassDef) and cls.name == "Flood"
        for node in cls.body if isinstance(node, FUNCTIONS)
    }
    assignments = {
        node.id
        for statement in tree.body if isinstance(statement, (ast.Assign, ast.AnnAssign))
        and statement.value is not None
        for target in (statement.targets if isinstance(statement, ast.Assign) else [statement.target])
        for node in ast.walk(target) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
    }

    def nodes(node: ast.AST | None):
        return ast.walk(node) if node is not None else ()

    def uses(node: ast.AST, name: str) -> bool:
        return any(isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)
                   and child.id == name for child in ast.walk(node))

    for name in PART_MARKERS["dependencies"][:3]:
        found["dependencies"][name] = name in functions
    found["dependencies"][PART_MARKERS["dependencies"][3]] = any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "dependency_edge_violation"
        for node in nodes(functions.get("validate_slices"))
    )
    found["start-metrics"]["CODE_SHA256"] = "CODE_SHA256" in assignments
    found["start-metrics"]["Flood.start_event"] = "start_event" in methods
    found["start-metrics"]["Flood.spawn calls self.start_event"] = any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "start_event" and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
        for node in nodes(methods.get("spawn"))
    )
    for name in PART_MARKERS["planner-prompt"][:3]:
        found["planner-prompt"][name] = name in assignments
    for method in ("plan_prompt", "start_plan_review"):
        node = methods.get(method)
        found["planner-prompt"][f"Flood.{method} uses PLANNER_INDEPENDENCE"] = (
            node is not None and uses(node, "PLANNER_INDEPENDENCE")
        )
    found["planner-prompt"][PART_MARKERS["planner-prompt"][-1]] = any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "log"
        and any(isinstance(child, ast.Constant) and isinstance(child.value, str)
                and child.value.startswith("loaded ") for arg in node.args for child in ast.walk(arg))
        and uses(node, "CODE_SHA256") and uses(node, "PLANNER_RULES_SHA256")
        for node in nodes(methods.get("run"))
    )
    return found


def inspect(source: bytes) -> tuple[dict[str, str], dict[str, dict[str, bool]]]:
    try:
        present = markers(ast.parse(source))
    except (SyntaxError, ValueError):
        return (dict.fromkeys(PART_MARKERS, "conflict"),
                {part: dict.fromkeys(names, False) for part, names in PART_MARKERS.items()})
    states = {
        part: "installed" if all(values.values()) else "conflict" if any(values.values()) else "missing"
        for part, values in present.items()
    }
    return states, present


def classify(source: bytes) -> dict[str, str]:
    """A part is installed with all markers, missing with none, conflict otherwise."""
    return inspect(source)[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("flood_py", type=Path)
    source = parser.parse_args().flood_py.read_bytes()
    states, present = inspect(source)
    for part, state in states.items():
        print(f"{part} {state}")
        if state == "conflict":
            absent = ", ".join(name for name, exists in present[part].items() if not exists)
            print(f"{part}: absent markers: {absent}", file=sys.stderr)
    return 3 if "conflict" in states.values() else 0


if __name__ == "__main__":
    sys.exit(main())

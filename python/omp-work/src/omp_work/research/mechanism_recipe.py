"""Strict canonical recipe validation for the finite R13 generator grammar (OMP-606)."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from omp_work.v1 import canonical

__all__ = [
    "ADAPTIVE_RESTART",
    "FAMILIES",
    "ROOT_FIRST",
    "THRESHOLD_GRID",
    "canonical_recipe",
    "recipe_sha256",
]

ADAPTIVE_RESTART = "adaptive-restart.v1"
ROOT_FIRST = "root-first.v1"
FAMILIES = (ADAPTIVE_RESTART, ROOT_FIRST)
THRESHOLD_GRID = (0.25, 0.5, 0.75)
_FIELDS = frozenset({"family", "min_metric"})


def canonical_recipe(recipe: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``{"family", "min_metric"}`` or raise ValueError.

    A recipe without ``family`` is a legacy min_metric recipe and means adaptive-restart.v1.
    """
    if not isinstance(recipe, Mapping):
        raise ValueError("recipe must be a mapping")
    unknown = set(recipe) - _FIELDS
    if unknown:
        raise ValueError(f"unknown recipe fields: {sorted(map(str, unknown))}")
    family = recipe.get("family", ADAPTIVE_RESTART)
    if not isinstance(family, str) or family not in FAMILIES:
        raise ValueError("unknown recipe family")
    if "min_metric" not in recipe:
        raise ValueError("min_metric is required")
    metric = recipe["min_metric"]
    if isinstance(metric, bool) or not isinstance(metric, (int, float)):
        raise ValueError("min_metric must be a finite number")
    try:
        value = float(metric)
    except OverflowError:
        value = math.inf
    if not math.isfinite(value):
        raise ValueError("min_metric must be a finite number")
    return {"family": family, "min_metric": 0.0 if value == 0 else value}


def recipe_sha256(recipe: Mapping[str, Any]) -> str:
    """Digest of the canonical recipe."""
    return canonical.sha256(canonical_recipe(recipe))

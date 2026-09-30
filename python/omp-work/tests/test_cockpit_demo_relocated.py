"""The W4 cockpit demo modules no longer live in the product package."""

from __future__ import annotations

import importlib.util

import pytest

_MOVED = (
    "omp_work.cockpit_verbs",
    "omp_work.cockpit_persist",
    "omp_work.chaos_suite",
    "omp_work.full_path",
)


@pytest.mark.parametrize("module", _MOVED)
def test_cockpit_demo_modules_are_not_importable_from_omp_work(module: str) -> None:
    assert importlib.util.find_spec(module) is None


def test_moved_modules_load_from_the_fixtures_package() -> None:
    spec = importlib.util.find_spec("fixtures.cockpit_demo.cockpit_verbs")
    assert spec is not None

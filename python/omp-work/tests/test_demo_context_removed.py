"""OMP-419: the omp-work context stub and demo Cognee adapter are gone from the product."""

import importlib.util
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from omp_work.__main__ import main

_REMOVED_MODULES = (
    "omp_work.context_compile",
    "omp_work.cognee_adapter",
    "omp_work.context_compile_bar",
    "omp_work.cognee_store",
    "omp_work.engine_pipeline",
    "omp_work.knowledge_b1",
    "omp_work.cockpit_pipeline",
)


@pytest.mark.parametrize("module", _REMOVED_MODULES)
def test_removed_modules_are_not_importable(module: str) -> None:
    assert importlib.util.find_spec(module) is None


def test_demo_and_compile_bar_commands_are_gone() -> None:
    for argv in (
        ["demo", "pipeline", "--job-id", "j1", "--query", "q", "--objective", "o"],
        ["demo", "compile-bar", "--job-id", "j1", "--objective", "o"],
        ["compile-bar", "--job-id", "j1", "--objective", "o"],
    ):
        with pytest.raises(SystemExit) as excinfo:
            main(argv)
        assert excinfo.value.code == 2

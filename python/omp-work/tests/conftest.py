"""Shared test fixtures for the omp-work suite.

The intake support judge installs no hook in production. Tests default to an
echo hook that copies the caller relation, so intake claim writes through the
store stay label-blind and deterministic without a model. A test that needs a
scripted verdict (or no hook at all) installs its own ``use_judge_hook``.

``test_intake_support_judge`` is the exception: it asserts the hook global is
unset and that ``judge_link`` never dispatches through a hook, so it runs with
no hook installed and keeps owning the hook contract in isolation.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from omp_work.intake_support_judge import JudgeHook, use_judge_hook

_HOOK_UNIT_TEST_MODULE = "test_intake_support_judge"


@pytest.fixture(autouse=True)
def judge_echo_hook(request: pytest.FixtureRequest) -> Iterator[JudgeHook | None]:
    module = getattr(request.node, "module", None)
    if getattr(module, "__name__", None) == _HOOK_UNIT_TEST_MODULE:
        yield None
        return
    with use_judge_hook(JudgeHook(echo_caller=True)):
        yield JudgeHook(echo_caller=True)

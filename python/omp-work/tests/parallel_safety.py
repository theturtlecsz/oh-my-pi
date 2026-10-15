"""Pytest plugin keeping the omp-work suite safe under pytest-xdist.

Two hazards show up once the suite runs with ``-n N``:

* the default ``tmp_path`` embeds the test name and, under xdist, a
  ``popen-gwN/`` segment, which overflows the 107-byte AF_UNIX ``sun_path``
  limit for egress ``record.sock`` binds;
* parametrize ids built from ``uuid4()`` values differ per worker, so xdist
  aborts with "Different tests were collected".
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


@pytest.fixture
def tmp_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("t", numbered=True)


def pytest_make_parametrize_id(config: pytest.Config, val: object, argname: str) -> str | None:
    if not isinstance(val, str):
        return None
    return _UUID_RE.sub("uuid", val)

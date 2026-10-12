from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from omp_knowledge.config import KnowledgeConfig
from omp_knowledge.engine.cognee_adapter import COGNEE_AVAILABLE


@pytest.fixture
def knowledge_config(tmp_path: Path) -> KnowledgeConfig:
    state_dir = tmp_path / "knowledge_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return KnowledgeConfig(state_dir=state_dir)


_REAL_CONNECT = socket.socket.connect


def _refused_connect(self: socket.socket, address: object) -> None:
    """Behave like a closed port: refuse every outbound TCP dial."""
    raise ConnectionRefusedError(111, "Connection refused")


@pytest.fixture(autouse=True)
def _no_live_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse every outbound dial so the gate host behaves like CI.

    The default route prober and HttpEmbedder then see the same
    "unreachable: ... Connection refused" outcome a closed port gives, so a
    test that depends on a live host service fails here as it does on CI.
    Tests that start their own in-process loopback server opt out by
    depending on the ``live_loopback`` fixture.
    """
    monkeypatch.setattr(socket.socket, "connect", _refused_connect)


@pytest.fixture
def live_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opt out of the dial fence for a test that runs its own loopback server."""
    monkeypatch.setattr(socket.socket, "connect", _REAL_CONNECT)

from __future__ import annotations

import os
from pathlib import Path

import pytest

from omp_knowledge.config import KnowledgeConfig
from omp_knowledge.engine.cognee_adapter import COGNEE_AVAILABLE


@pytest.fixture
def knowledge_config(tmp_path: Path) -> KnowledgeConfig:
    state_dir = tmp_path / "knowledge_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return KnowledgeConfig(state_dir=state_dir)

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class KnowledgeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str = "127.0.0.1"
    port: int = Field(default=18090, ge=1024, le=65535)

    # Embedded Cognee/Ladybug/LanceDB state dir
    state_dir: Path = Field(
        default_factory=lambda: Path(
            os.environ.get(
                "OMP_KNOWLEDGE_STATE_DIR",
                str(Path.home() / ".local/state/omp-fleet-knowledge/knowledge-data"),
            )
        )
    )

    # Capability and configuration directory
    config_dir: Path = Field(
        default_factory=lambda: Path(
            os.environ.get(
                "OMP_KNOWLEDGE_CONFIG_DIR",
                str(Path.home() / ".config/omp-knowledge"),
            )
        )
    )

    # Provider routing configuration
    graph_engine: Literal["ladybug"] = "ladybug"
    graph_only: bool = True

    @property
    def cognee_dir(self) -> Path:
        return self.state_dir / "cognee"

    @property
    def artifacts_dir(self) -> Path:
        return self.state_dir / "artifacts"


def load_config() -> KnowledgeConfig:
    return KnowledgeConfig(
        host=os.environ.get("OMP_KNOWLEDGE_HOST", "127.0.0.1"),
        port=int(os.environ.get("OMP_KNOWLEDGE_PORT", "18090")),
        state_dir=Path(
            os.environ.get(
                "OMP_KNOWLEDGE_STATE_DIR",
                str(Path.home() / ".local/state/omp-fleet-knowledge/knowledge-data"),
            )
        ),
        config_dir=Path(
            os.environ.get(
                "OMP_KNOWLEDGE_CONFIG_DIR",
                str(Path.home() / ".config/omp-knowledge"),
            )
        ),
        graph_engine="ladybug",
        graph_only=os.environ.get("OMP_KNOWLEDGE_GRAPH_ONLY", "true").lower()
        in ("true", "1", "yes"),
    )

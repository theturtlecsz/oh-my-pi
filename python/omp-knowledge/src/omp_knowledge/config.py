from __future__ import annotations

import os
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class KnowledgeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str = "127.0.0.1"
    port: int = Field(default=18090, ge=1024, le=65535)

    # Storage settings (PostgreSQL)
    pg_host: str = "127.0.0.1"
    pg_port: int = Field(default=54321, ge=1024, le=65535)
    pg_database: str = "omp_knowledge"
    pg_user: str = "omp_knowledge_app"
    pg_password: str = ""
    pg_sslmode: str = "prefer"

    # Native storage settings (read-only Work database)
    native_pg_host: str = "127.0.0.1"
    native_pg_port: int = Field(default=54321, ge=1024, le=65535)
    native_pg_database: str = "omp_work"
    native_pg_user: str = "omp_work_readonly"
    native_pg_password: str = ""
    native_pg_sslmode: str = "prefer"

    # Embedded Cognee/Ladybug/LanceDB state dir
    state_dir: Path = Field(
        default_factory=lambda: Path(
            os.environ.get(
                "OMP_KNOWLEDGE_STATE_DIR",
                os.path.expanduser("~/.local/state/omp-fleet-knowledge/knowledge-data"),
            )
        )
    )

    # Capability-file authentication directory
    config_dir: Path = Field(
        default_factory=lambda: Path(
            os.environ.get(
                "OMP_KNOWLEDGE_CONFIG_DIR",
                os.path.expanduser("~/.config/omp-knowledge"),
            )
        )
    )

    # Provider routing configuration
    graph_engine: Literal["ladybug", "neo4j"] = "ladybug"
    vector_store: Literal["lancedb", "pgvector"] = "lancedb"
    embedding_provider: Literal["none", "openai_compatible"] = "none"
    # ``rerank_v1``: optional post-hydration reranking of query candidates through
    # an OpenAI-style ``POST /v1/rerank`` endpoint (semantic route only). Disabled
    # by default; only ``none`` ever had behaviour before this field was typed.
    reranking_provider: Literal["none", "rerank_v1"] = "none"
    generation_provider: str = "none"
    graph_only: bool = True
    embedding_model: str | None = None
    embedding_endpoint: str | None = None
    embedding_api_key_file: Path | None = None
    embedding_required: bool = False
    embedding_timeout_seconds: float = Field(default=30.0, gt=0.0)
    reranking_model: str | None = None
    reranking_endpoint: str | None = None
    reranking_api_key_file: Path | None = None
    reranking_required: bool = False
    reranking_timeout_seconds: float = Field(default=15.0, gt=0.0)

    # External Neo4j graph backend (used only when graph_engine == "neo4j").
    # The password is never held in config; only a secret-file reference is.
    neo4j_uri: str = "bolt://127.0.0.1:17687"
    neo4j_user: str = "neo4j"
    neo4j_password_file: Path | None = None
    neo4j_database: str | None = None

    # Cognee-owned PostgreSQL (relational metadata + pgvector) used only when
    # vector_store == "pgvector". Distinct from the omp_knowledge ledger database.
    # Defaults match the qualified external topology (dedicated omp_cognee
    # database on its own port, app role, 1024-dim vector columns); they are
    # inert while the embedded ladybug/lancedb route is selected.
    cognee_pg_host: str = "127.0.0.1"
    cognee_pg_port: int = Field(default=15432, ge=1024, le=65535)
    cognee_pg_database: str = "omp_cognee"
    cognee_pg_user: str = "omp_cognee_app"
    cognee_pg_password_file: Path | None = None
    vector_dimension: int = Field(default=1024, ge=1)

    @model_validator(mode="after")
    def _validate_semantic_mode(self) -> "KnowledgeConfig":
        # Semantic mode is the only route that produces embeddings; every input it
        # needs must be explicit so the service never guesses a store or endpoint.
        if not self.graph_only:
            if self.vector_store != "pgvector":
                raise ValueError("graph_only=False requires vector_store='pgvector'")
            if self.embedding_provider != "openai_compatible":
                raise ValueError("graph_only=False requires embedding_provider='openai_compatible'")
            if not self.embedding_model:
                raise ValueError("graph_only=False requires a non-empty embedding_model")
            if not self.embedding_endpoint:
                raise ValueError("graph_only=False requires a non-empty embedding_endpoint")
            if self.cognee_pg_password_file is None:
                raise ValueError("graph_only=False requires cognee_pg_password_file")
            # Config boundary: refuse an endpoint carrying userinfo or a non-http(s)
            # scheme before any route is derived from it. The backend boundary
            # (``resolve_backend_route``) applies the same helper, so a config built
            # without this validator cannot slip through either. The raised
            # BackendConfigError is a ValueError; pydantic reports it with the
            # sanitized endpoint only.
            from .engine.backends import validate_embedding_endpoint

            validate_embedding_endpoint(self.embedding_endpoint)
        if self.reranking_provider != "none":
            # Reranking permutes semantic candidates only; the graph-only route
            # (embedded or external) is left byte-identical and refuses the switch.
            if self.graph_only:
                raise ValueError("reranking_provider requires graph_only=False (semantic route)")
            if not self.reranking_model:
                raise ValueError(f"reranking_provider={self.reranking_provider!r} requires a non-empty reranking_model")
            if not self.reranking_endpoint:
                raise ValueError(f"reranking_provider={self.reranking_provider!r} requires a non-empty reranking_endpoint")
            # Same fail-closed endpoint rule as the embedding endpoint: fact text
            # is posted there, so only a credential-free http(s) URL is accepted.
            from .engine.backends import validate_embedding_endpoint

            validate_embedding_endpoint(self.reranking_endpoint, setting="reranking_endpoint")
        return self

    @property
    def capabilities_dir(self) -> Path:
        return self.config_dir / "capabilities"

    @property
    def cognee_dir(self) -> Path:
        return self.state_dir / "cognee"

    @property
    def artifacts_dir(self) -> Path:
        return self.state_dir / "artifacts"

    def pg_connection_string(self) -> str:
        parts = [
            f"host={self.pg_host}",
            f"port={self.pg_port}",
            f"dbname={self.pg_database}",
            f"user={self.pg_user}",
            f"sslmode={self.pg_sslmode}",
        ]
        if self.pg_password:
            parts.append(f"password={self.pg_password}")
        return " ".join(parts)

    def native_pg_connection_string(self) -> str:
        parts = [
            f"host={self.native_pg_host}",
            f"port={self.native_pg_port}",
            f"dbname={self.native_pg_database}",
            f"user={self.native_pg_user}",
            f"sslmode={self.native_pg_sslmode}",
        ]
        if self.native_pg_password:
            parts.append(f"password={self.native_pg_password}")
        return " ".join(parts)


def _optional_path(value: str | None) -> Path | None:
    if value is None or value == "":
        return None
    return Path(value)


def load_config() -> KnowledgeConfig:
    return KnowledgeConfig(
        host=os.environ.get("OMP_KNOWLEDGE_HOST", "127.0.0.1"),
        port=int(os.environ.get("OMP_KNOWLEDGE_PORT", "18090")),
        pg_host=os.environ.get("OMP_KNOWLEDGE_PG_HOST", "127.0.0.1"),
        pg_port=int(os.environ.get("OMP_KNOWLEDGE_PG_PORT", "54321")),
        pg_database=os.environ.get("OMP_KNOWLEDGE_PG_DATABASE", "omp_knowledge"),
        pg_user=os.environ.get("OMP_KNOWLEDGE_PG_USER", "omp_knowledge_app"),
        pg_password=os.environ.get("OMP_KNOWLEDGE_PG_PASSWORD", ""),
        pg_sslmode=os.environ.get("OMP_KNOWLEDGE_PG_SSLMODE", "prefer"),
        native_pg_host=os.environ.get("OMP_NATIVE_PG_HOST", os.environ.get("OMP_WORK_PG_HOST", "127.0.0.1")),
        native_pg_port=int(os.environ.get("OMP_NATIVE_PG_PORT", os.environ.get("OMP_WORK_PG_PORT", "54321"))),
        native_pg_database=os.environ.get("OMP_NATIVE_PG_DATABASE", os.environ.get("OMP_WORK_PG_DATABASE", "omp_work")),
        native_pg_user=os.environ.get("OMP_NATIVE_PG_USER", os.environ.get("OMP_WORK_PG_USER", "omp_work_readonly")),
        native_pg_password=os.environ.get("OMP_NATIVE_PG_PASSWORD", os.environ.get("OMP_WORK_PG_PASSWORD", "")),
        native_pg_sslmode=os.environ.get("OMP_NATIVE_PG_SSLMODE", os.environ.get("OMP_WORK_PG_SSLMODE", "prefer")),
        state_dir=Path(
            os.environ.get(
                "OMP_KNOWLEDGE_STATE_DIR",
                os.path.expanduser("~/.local/state/omp-fleet-knowledge/knowledge-data"),
            )
        ),
        config_dir=Path(
            os.environ.get(
                "OMP_KNOWLEDGE_CONFIG_DIR",
                os.path.expanduser("~/.config/omp-knowledge"),
            )
        ),
        graph_engine=os.environ.get("OMP_KNOWLEDGE_GRAPH_ENGINE", "ladybug"),  # type: ignore[arg-type]
        vector_store=os.environ.get("OMP_KNOWLEDGE_VECTOR_STORE", "lancedb"),  # type: ignore[arg-type]
        embedding_provider=os.environ.get("OMP_KNOWLEDGE_EMBEDDING_PROVIDER", "none"),  # type: ignore[arg-type]
        graph_only=os.environ.get("OMP_KNOWLEDGE_GRAPH_ONLY", "true").lower() in ("true", "1", "yes"),
        embedding_model=os.environ.get("OMP_KNOWLEDGE_EMBEDDING_MODEL") or None,
        embedding_endpoint=os.environ.get("OMP_KNOWLEDGE_EMBEDDING_ENDPOINT") or None,
        embedding_api_key_file=_optional_path(os.environ.get("OMP_KNOWLEDGE_EMBEDDING_API_KEY_FILE")),
        embedding_required=os.environ.get("OMP_KNOWLEDGE_EMBEDDING_REQUIRED", "false").lower() in ("true", "1", "yes"),
        embedding_timeout_seconds=float(os.environ.get("OMP_KNOWLEDGE_EMBEDDING_TIMEOUT_SECONDS", "30.0")),
        reranking_provider=os.environ.get("OMP_KNOWLEDGE_RERANKING_PROVIDER", "none"),  # type: ignore[arg-type]
        reranking_model=os.environ.get("OMP_KNOWLEDGE_RERANKING_MODEL") or None,
        reranking_endpoint=os.environ.get("OMP_KNOWLEDGE_RERANKING_ENDPOINT") or None,
        reranking_api_key_file=_optional_path(os.environ.get("OMP_KNOWLEDGE_RERANKING_API_KEY_FILE")),
        reranking_required=os.environ.get("OMP_KNOWLEDGE_RERANKING_REQUIRED", "false").lower() in ("true", "1", "yes"),
        reranking_timeout_seconds=float(os.environ.get("OMP_KNOWLEDGE_RERANKING_TIMEOUT_SECONDS", "15.0")),
        neo4j_uri=os.environ.get("OMP_KNOWLEDGE_NEO4J_URI", "bolt://127.0.0.1:17687"),
        neo4j_user=os.environ.get("OMP_KNOWLEDGE_NEO4J_USER", "neo4j"),
        neo4j_password_file=_optional_path(os.environ.get("OMP_KNOWLEDGE_NEO4J_PASSWORD_FILE")),
        neo4j_database=os.environ.get("OMP_KNOWLEDGE_NEO4J_DATABASE") or None,
        cognee_pg_host=os.environ.get("OMP_KNOWLEDGE_COGNEE_PG_HOST", "127.0.0.1"),
        cognee_pg_port=int(os.environ.get("OMP_KNOWLEDGE_COGNEE_PG_PORT", "15432")),
        cognee_pg_database=os.environ.get("OMP_KNOWLEDGE_COGNEE_PG_DATABASE", "omp_cognee"),
        cognee_pg_user=os.environ.get("OMP_KNOWLEDGE_COGNEE_PG_USER", "omp_cognee_app"),
        cognee_pg_password_file=_optional_path(os.environ.get("OMP_KNOWLEDGE_COGNEE_PG_PASSWORD_FILE")),
        vector_dimension=int(os.environ.get("OMP_KNOWLEDGE_VECTOR_DIMENSION", "1024")),
    )

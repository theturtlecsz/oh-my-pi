from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from omp_knowledge.config import KnowledgeConfig, load_config
from omp_knowledge.engine import cognee_adapter
from omp_knowledge.engine.backends import (
    BackendConfigError,
    SecretRefError,
    resolve_backend_route,
)
from omp_knowledge.engine.cognee_adapter import RealCogneeAdapter
from omp_knowledge.engine.protocol import EngineUnavailableError


def _cfg(tmp_path: Path, **overrides: object) -> KnowledgeConfig:
    return KnowledgeConfig(state_dir=tmp_path / "state", config_dir=tmp_path / "config", **overrides)


# ==============================================================================
# Config defaults and env parsing
# ==============================================================================


def test_config_defaults_preserve_embedded_route(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    assert cfg.graph_engine == "ladybug"
    assert cfg.vector_store == "lancedb"
    assert cfg.graph_only is True
    assert cfg.neo4j_password_file is None
    assert cfg.cognee_pg_password_file is None

    route = resolve_backend_route(cfg)
    assert route.embedded is True
    assert route.provider_name == "ladybug-embedded"
    assert route.graph_endpoint == "embedded"
    assert route.vector_endpoint == "embedded"
    assert route.metadata_store == "sqlite"
    assert route.required_modules == ()
    assert route.semantic is False
    assert route.embedding_provider == "none"
    assert route.embedding_model is None
    assert route.embedding_endpoint is None
    assert route.binding_tag is None


def test_config_rejects_unknown_backends(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _cfg(tmp_path, graph_engine="janus")
    with pytest.raises(ValueError):
        _cfg(tmp_path, vector_store="qdrant")


def test_load_config_parses_backend_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OMP_KNOWLEDGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OMP_KNOWLEDGE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OMP_KNOWLEDGE_GRAPH_ENGINE", "neo4j")
    monkeypatch.setenv("OMP_KNOWLEDGE_VECTOR_STORE", "pgvector")
    monkeypatch.setenv("OMP_KNOWLEDGE_NEO4J_URI", "neo4j+s://graph.internal:7687")
    monkeypatch.setenv("OMP_KNOWLEDGE_NEO4J_USER", "omp")
    monkeypatch.setenv("OMP_KNOWLEDGE_NEO4J_PASSWORD_FILE", str(tmp_path / "neo4j.secret"))
    monkeypatch.setenv("OMP_KNOWLEDGE_NEO4J_DATABASE", "knowledge")
    monkeypatch.setenv("OMP_KNOWLEDGE_COGNEE_PG_HOST", "pg.internal")
    monkeypatch.setenv("OMP_KNOWLEDGE_COGNEE_PG_PORT", "6543")
    monkeypatch.setenv("OMP_KNOWLEDGE_COGNEE_PG_DATABASE", "cognee_db")
    monkeypatch.setenv("OMP_KNOWLEDGE_COGNEE_PG_USER", "cognee_user")
    monkeypatch.setenv("OMP_KNOWLEDGE_COGNEE_PG_PASSWORD_FILE", str(tmp_path / "pg.secret"))
    monkeypatch.setenv("OMP_KNOWLEDGE_VECTOR_DIMENSION", "768")

    cfg = load_config()
    assert cfg.graph_engine == "neo4j"
    assert cfg.vector_store == "pgvector"
    assert cfg.neo4j_uri == "neo4j+s://graph.internal:7687"
    assert cfg.neo4j_user == "omp"
    assert cfg.neo4j_password_file == tmp_path / "neo4j.secret"
    assert cfg.neo4j_database == "knowledge"
    assert cfg.cognee_pg_host == "pg.internal"
    assert cfg.cognee_pg_port == 6543
    assert cfg.cognee_pg_database == "cognee_db"
    assert cfg.cognee_pg_user == "cognee_user"
    assert cfg.cognee_pg_password_file == tmp_path / "pg.secret"
    assert cfg.vector_dimension == 768

    route = resolve_backend_route(cfg)
    assert route.embedded is False
    assert route.graph_endpoint == "neo4j+s://graph.internal:7687"
    assert route.vector_endpoint == "postgresql://pg.internal:6543/cognee_db"
    assert route.metadata_store == "postgres"


def test_load_config_env_defaults_unchanged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OMP_KNOWLEDGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OMP_KNOWLEDGE_CONFIG_DIR", str(tmp_path / "config"))
    for var in (
        "OMP_KNOWLEDGE_GRAPH_ENGINE",
        "OMP_KNOWLEDGE_VECTOR_STORE",
        "OMP_KNOWLEDGE_NEO4J_PASSWORD_FILE",
        "OMP_KNOWLEDGE_COGNEE_PG_PASSWORD_FILE",
    ):
        monkeypatch.delenv(var, raising=False)
    cfg = load_config()
    assert cfg.graph_engine == "ladybug"
    assert cfg.vector_store == "lancedb"
    assert resolve_backend_route(cfg).embedded is True


# ==============================================================================
# Adapter route identity and fail-closed behaviour
# ==============================================================================


def test_embedded_adapter_route_identity_unchanged(tmp_path: Path) -> None:
    adapter = RealCogneeAdapter(_cfg(tmp_path), available=False)
    status = asyncio.run(adapter.status())

    route = status.active_route
    assert route.provider == "ladybug-embedded"
    assert route.endpoint == "embedded"
    assert route.graph_only is True
    assert route.model_inferred is False
    assert route.active is False

    assert status.graph_engine == "ladybug==0.19.0"
    assert status.details["graph_engine"] == "ladybug"
    assert status.details["embedded"] is True
    assert status.details["vector_indexing"] is False
    assert status.details["identity_scheme"] == "omp-fact/v1"


def test_neo4j_route_rejects_bad_uri_scheme(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, graph_engine="neo4j", neo4j_uri="http://graph.internal:7474")
    with pytest.raises(BackendConfigError):
        RealCogneeAdapter(cfg, available=False)


def test_external_route_fails_closed_on_missing_modules(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cognee_adapter, "missing_backend_modules", lambda route: ("neo4j",))
    cfg = _cfg(tmp_path, graph_engine="neo4j")
    with pytest.raises(EngineUnavailableError, match="neo4j"):
        RealCogneeAdapter(cfg, available=False)


def test_external_route_fails_closed_on_missing_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cognee_adapter, "missing_backend_modules", lambda route: ())
    cfg = _cfg(tmp_path, graph_engine="neo4j", neo4j_password_file=tmp_path / "absent.secret")
    with pytest.raises(SecretRefError):
        RealCogneeAdapter(cfg, available=False)


def test_neo4j_route_rejects_uri_userinfo_without_leaking_credentials(tmp_path: Path) -> None:
    embedded_user = "embedded-user-7f3a"
    embedded_password = "embedded-pass-9c1e"
    cfg = _cfg(
        tmp_path,
        graph_engine="neo4j",
        neo4j_uri=f"bolt://{embedded_user}:{embedded_password}@graph.internal:7687",
    )

    with pytest.raises(BackendConfigError) as excinfo:
        resolve_backend_route(cfg)

    message = str(excinfo.value)
    assert "neo4j_uri" in message
    assert "bolt://graph.internal:7687" in message
    assert embedded_user not in message
    assert embedded_password not in message
    assert f"{embedded_user}:{embedded_password}@" not in message
    assert "@graph.internal" not in message


def test_neo4j_route_rejects_username_only_userinfo(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, graph_engine="neo4j", neo4j_uri="neo4j+s://only-user-4d2b@graph.internal:7687")

    with pytest.raises(BackendConfigError) as excinfo:
        resolve_backend_route(cfg)

    message = str(excinfo.value)
    assert "neo4j+s://graph.internal:7687" in message
    assert "only-user-4d2b" not in message


def test_external_route_status_is_credential_free(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cognee_adapter, "missing_backend_modules", lambda route: ())
    secret = "s3cr3t-graph-password"
    secret_file = tmp_path / "neo4j.secret"
    secret_file.write_text(secret + "\n", encoding="utf-8")
    secret_file.chmod(0o600)

    cfg = _cfg(
        tmp_path,
        graph_engine="neo4j",
        neo4j_uri="bolt://graph.internal:7687",
        neo4j_user="omp",
        neo4j_password_file=secret_file,
        vector_store="pgvector",
        cognee_pg_password_file=None,
    )
    adapter = RealCogneeAdapter(cfg, available=False)
    status = asyncio.run(adapter.status())

    assert status.active_route.provider == "neo4j"
    assert status.active_route.endpoint == "bolt://graph.internal:7687"
    assert status.graph_engine == "neo4j"
    assert status.details["embedded"] is False
    assert status.details["metadata_store"] == "postgres"

    serialized = json.dumps(status.model_dump(mode="json"))
    assert secret not in serialized
    assert str(secret_file) not in serialized


def test_semantic_route_fields(tmp_path: Path) -> None:
    pw = tmp_path / "pg.secret"
    pw.write_text("secret\n", encoding="utf-8")
    cfg = _cfg(
        tmp_path,
        graph_only=False,
        vector_store="pgvector",
        embedding_provider="openai_compatible",
        embedding_model="qwen3-embedding-0.6b-q8",
        embedding_endpoint="http://127.0.0.1:18081",
        cognee_pg_password_file=pw,
    )
    route = resolve_backend_route(cfg)
    assert route.semantic is True
    assert route.embedding_provider == "openai_compatible"
    assert route.embedding_model == "qwen3-embedding-0.6b-q8"
    assert route.embedding_endpoint == "http://127.0.0.1:18081"
    assert route.binding_tag == "omp-emb:qwen3-embedding-0.6b-q8@1024"

    d = route.as_dict()
    assert d["semantic"] is True
    assert d["embedding_provider"] == "openai_compatible"
    assert d["embedding_model"] == "qwen3-embedding-0.6b-q8"
    assert d["embedding_endpoint"] == "http://127.0.0.1:18081"
    assert d["binding_tag"] == "omp-emb:qwen3-embedding-0.6b-q8@1024"


def test_semantic_route_validator_rejections(tmp_path: Path) -> None:
    pw = tmp_path / "pg.secret"
    pw.write_text("secret\n", encoding="utf-8")

    # Requires pgvector
    with pytest.raises(ValueError, match="vector_store='pgvector'"):
        _cfg(tmp_path, graph_only=False, vector_store="lancedb", cognee_pg_password_file=pw)

    # Requires openai_compatible
    with pytest.raises(ValueError, match="embedding_provider='openai_compatible'"):
        _cfg(tmp_path, graph_only=False, vector_store="pgvector", embedding_provider="none", cognee_pg_password_file=pw)

    # Requires non-empty embedding_model
    with pytest.raises(ValueError, match="non-empty embedding_model"):
        _cfg(tmp_path, graph_only=False, vector_store="pgvector", embedding_provider="openai_compatible", embedding_model="", cognee_pg_password_file=pw)

    # Requires non-empty embedding_endpoint
    with pytest.raises(ValueError, match="non-empty embedding_endpoint"):
        _cfg(tmp_path, graph_only=False, vector_store="pgvector", embedding_provider="openai_compatible", embedding_model="m", embedding_endpoint="", cognee_pg_password_file=pw)

    # Requires cognee_pg_password_file
    with pytest.raises(ValueError, match="cognee_pg_password_file"):
        _cfg(tmp_path, graph_only=False, vector_store="pgvector", embedding_provider="openai_compatible", embedding_model="m", embedding_endpoint="http://127.0.0.1:18081", cognee_pg_password_file=None)


def test_load_config_parses_semantic_env_variables(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pw = tmp_path / "pg.secret"
    pw.write_text("secret\n", encoding="utf-8")
    monkeypatch.setenv("OMP_KNOWLEDGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OMP_KNOWLEDGE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OMP_KNOWLEDGE_GRAPH_ONLY", "0")
    monkeypatch.setenv("OMP_KNOWLEDGE_VECTOR_STORE", "pgvector")
    monkeypatch.setenv("OMP_KNOWLEDGE_COGNEE_PG_PASSWORD_FILE", str(pw))
    monkeypatch.setenv("OMP_KNOWLEDGE_EMBEDDING_PROVIDER", "openai_compatible")
    monkeypatch.setenv("OMP_KNOWLEDGE_EMBEDDING_MODEL", "qwen3-embedding-0.6b-q8")
    monkeypatch.setenv("OMP_KNOWLEDGE_EMBEDDING_ENDPOINT", "http://127.0.0.1:18081")
    monkeypatch.setenv("OMP_KNOWLEDGE_EMBEDDING_REQUIRED", "1")
    monkeypatch.setenv("OMP_KNOWLEDGE_EMBEDDING_TIMEOUT_SECONDS", "45.0")

    cfg = load_config()
    assert cfg.graph_only is False
    assert cfg.vector_store == "pgvector"
    assert cfg.embedding_provider == "openai_compatible"
    assert cfg.embedding_model == "qwen3-embedding-0.6b-q8"
    assert cfg.embedding_endpoint == "http://127.0.0.1:18081"
    assert cfg.embedding_required is True
    assert cfg.embedding_timeout_seconds == 45.0

    route = resolve_backend_route(cfg)
    assert route.semantic is True
    assert route.binding_tag == "omp-emb:qwen3-embedding-0.6b-q8@1024"

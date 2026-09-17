"""Contract tests for external-route defaults and the Cognee config binding.

These assert concrete values and call contracts only:

* ``KnowledgeConfig`` / ``load_config`` external-route defaults match the qualified
  Cognee PostgreSQL topology while the embedded route defaults stay unchanged.
* Required-module checks name exactly what Cognee 1.5.4's Neo4j driver and
  pgvector adapter import, and nothing for the embedded route.
* ``RealCogneeAdapter`` wires a non-embedded route through Cognee's
  ``config.set_graph_db_config`` / ``set_relational_db_config`` /
  ``set_vector_db_config`` setters with credential-bearing payloads, reads every
  applied key back through Cognee's config getters, fails closed when a setter is
  absent, rejects the payload or does not read back, and redacts secrets from errors.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from omp_knowledge.config import KnowledgeConfig, load_config
from omp_knowledge.engine import cognee_adapter
from omp_knowledge.engine.backends import (
    REQUIRED_MODULES,
    build_cognee_backend_settings,
    resolve_backend_route,
)
from omp_knowledge.engine.cognee_adapter import RealCogneeAdapter
from omp_knowledge.engine.protocol import EngineUnavailableError
from support.fake_cognee import FakeCogneeConfig, install_fake_cognee, secret_file

QUALIFIED_COGNEE_PG_PORT = 15432
QUALIFIED_COGNEE_PG_DATABASE = "omp_cognee"
QUALIFIED_COGNEE_PG_USER = "omp_cognee_app"
QUALIFIED_VECTOR_DIMENSION = 1024

COGNEE_SETTER_NAMES = (
    "set_graph_db_config",
    "set_relational_db_config",
    "set_vector_db_config",
)

SEMANTIC_SETTER_NAMES = (
    "set_graph_db_config",
    "set_relational_db_config",
    "set_vector_db_config",
    "set_embedding_config",
)

EXTERNAL_ENV_VARS = (
    "OMP_KNOWLEDGE_GRAPH_ENGINE",
    "OMP_KNOWLEDGE_VECTOR_STORE",
    "OMP_KNOWLEDGE_NEO4J_URI",
    "OMP_KNOWLEDGE_NEO4J_USER",
    "OMP_KNOWLEDGE_NEO4J_PASSWORD_FILE",
    "OMP_KNOWLEDGE_NEO4J_DATABASE",
    "OMP_KNOWLEDGE_COGNEE_PG_HOST",
    "OMP_KNOWLEDGE_COGNEE_PG_PORT",
    "OMP_KNOWLEDGE_COGNEE_PG_DATABASE",
    "OMP_KNOWLEDGE_COGNEE_PG_USER",
    "OMP_KNOWLEDGE_COGNEE_PG_PASSWORD_FILE",
    "OMP_KNOWLEDGE_VECTOR_DIMENSION",
)


def _cfg(tmp_path: Path, **overrides: object) -> KnowledgeConfig:
    return KnowledgeConfig(state_dir=tmp_path / "state", config_dir=tmp_path / "config", **overrides)


_secret_file = secret_file
_FakeCogneeConfig = FakeCogneeConfig
_install_fake_cognee = install_fake_cognee


# ==============================================================================
# External-route defaults
# ==============================================================================


def test_model_defaults_keep_embedded_route_and_qualified_external_topology(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)

    # Embedded defaults are unchanged.
    assert cfg.graph_engine == "ladybug"
    assert cfg.vector_store == "lancedb"
    assert resolve_backend_route(cfg).embedded is True

    # External-route defaults match the qualified Cognee PostgreSQL topology.
    assert cfg.cognee_pg_host == "127.0.0.1"
    assert cfg.cognee_pg_port == QUALIFIED_COGNEE_PG_PORT
    assert cfg.cognee_pg_database == QUALIFIED_COGNEE_PG_DATABASE
    assert cfg.cognee_pg_user == QUALIFIED_COGNEE_PG_USER
    assert cfg.vector_dimension == QUALIFIED_VECTOR_DIMENSION


def test_load_config_env_selecting_pgvector_uses_qualified_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMP_KNOWLEDGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OMP_KNOWLEDGE_CONFIG_DIR", str(tmp_path / "config"))
    for var in EXTERNAL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OMP_KNOWLEDGE_VECTOR_STORE", "pgvector")

    cfg = load_config()
    assert cfg.graph_engine == "ladybug"
    assert cfg.vector_store == "pgvector"
    assert cfg.cognee_pg_port == QUALIFIED_COGNEE_PG_PORT
    assert cfg.cognee_pg_database == QUALIFIED_COGNEE_PG_DATABASE
    assert cfg.cognee_pg_user == QUALIFIED_COGNEE_PG_USER
    assert cfg.vector_dimension == QUALIFIED_VECTOR_DIMENSION

    route = resolve_backend_route(cfg)
    assert route.embedded is False
    assert route.metadata_store == "postgres"
    assert route.vector_database == QUALIFIED_COGNEE_PG_DATABASE
    assert route.vector_dimension == QUALIFIED_VECTOR_DIMENSION
    assert route.vector_endpoint == (
        f"postgresql://127.0.0.1:{QUALIFIED_COGNEE_PG_PORT}/{QUALIFIED_COGNEE_PG_DATABASE}"
    )

    settings = build_cognee_backend_settings(cfg, route)
    assert settings.graph is None
    assert settings.relational == {
        "db_provider": "postgres",
        "db_host": "127.0.0.1",
        "db_port": str(QUALIFIED_COGNEE_PG_PORT),
        "db_name": QUALIFIED_COGNEE_PG_DATABASE,
        "db_username": QUALIFIED_COGNEE_PG_USER,
        "db_password": "",
    }
    assert settings.vector == {"vector_db_provider": "pgvector"}


def test_load_config_without_external_env_keeps_embedded_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMP_KNOWLEDGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OMP_KNOWLEDGE_CONFIG_DIR", str(tmp_path / "config"))
    for var in EXTERNAL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    cfg = load_config()
    route = resolve_backend_route(cfg)
    assert route.embedded is True
    assert route.graph_endpoint == "embedded"
    assert route.vector_endpoint == "embedded"
    assert route.metadata_store == "sqlite"


# ==============================================================================
# Required modules per route
# ==============================================================================


def test_required_modules_match_cognee_adapter_imports(tmp_path: Path) -> None:
    assert REQUIRED_MODULES == {
        "neo4j": ("neo4j",),
        "pgvector": ("pgvector.sqlalchemy", "sqlalchemy.ext.asyncio", "asyncpg"),
    }

    embedded = resolve_backend_route(_cfg(tmp_path))
    assert embedded.required_modules == ()

    neo4j_only = resolve_backend_route(_cfg(tmp_path, graph_engine="neo4j"))
    assert neo4j_only.required_modules == ("neo4j",)

    pgvector_only = resolve_backend_route(_cfg(tmp_path, vector_store="pgvector"))
    assert pgvector_only.required_modules == ("pgvector.sqlalchemy", "sqlalchemy.ext.asyncio", "asyncpg")

    both = resolve_backend_route(_cfg(tmp_path, graph_engine="neo4j", vector_store="pgvector"))
    assert both.required_modules == ("neo4j", "pgvector.sqlalchemy", "sqlalchemy.ext.asyncio", "asyncpg")


# ==============================================================================
# Cognee config setter binding
# ==============================================================================


def test_adapter_binds_external_route_through_cognee_config_setters(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _FakeCogneeConfig()
    _install_fake_cognee(monkeypatch, fake)

    neo4j_secret = "graph-pw-0123"
    pg_secret = "pg-pw-4567"
    cfg = _cfg(
        tmp_path,
        graph_engine="neo4j",
        vector_store="pgvector",
        neo4j_uri="bolt://graph.internal:7687",
        neo4j_user="omp",
        neo4j_password_file=_secret_file(tmp_path, "neo4j.secret", neo4j_secret),
        neo4j_database="knowledge",
        cognee_pg_password_file=_secret_file(tmp_path, "pg.secret", pg_secret),
    )

    RealCogneeAdapter(cfg, available=True)

    assert [name for name, _ in fake.calls] == list(COGNEE_SETTER_NAMES)
    payloads = dict(fake.calls)
    assert payloads["set_graph_db_config"] == {
        "graph_database_provider": "neo4j",
        "graph_database_url": "bolt://graph.internal:7687",
        "graph_database_username": "omp",
        "graph_database_password": neo4j_secret,
        "graph_database_name": "knowledge",
    }
    assert payloads["set_relational_db_config"] == {
        "db_provider": "postgres",
        "db_host": "127.0.0.1",
        "db_port": str(QUALIFIED_COGNEE_PG_PORT),
        "db_name": QUALIFIED_COGNEE_PG_DATABASE,
        "db_username": QUALIFIED_COGNEE_PG_USER,
        "db_password": pg_secret,
    }
    assert payloads["set_vector_db_config"] == {"vector_db_provider": "pgvector"}
    # Readback through the getters sees exactly what the setters applied.
    assert vars(fake.applied["set_graph_db_config"]) == payloads["set_graph_db_config"]
    assert vars(fake.applied["set_vector_db_config"]) == {"vector_db_provider": "pgvector"}


def test_adapter_binds_semantic_route_through_cognee_config_setters(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _FakeCogneeConfig()
    _install_fake_cognee(monkeypatch, fake)

    neo4j_secret = "graph-pw-0123"
    pg_secret = "pg-pw-4567"
    cfg = _cfg(
        tmp_path,
        graph_engine="neo4j",
        vector_store="pgvector",
        graph_only=False,
        embedding_provider="openai_compatible",
        embedding_model="qwen3-embedding-0.6b-q8",
        embedding_endpoint="http://127.0.0.1:18081",
        neo4j_uri="bolt://graph.internal:7687",
        neo4j_user="omp",
        neo4j_password_file=_secret_file(tmp_path, "neo4j.secret", neo4j_secret),
        neo4j_database="knowledge",
        cognee_pg_password_file=_secret_file(tmp_path, "pg.secret", pg_secret),
    )

    RealCogneeAdapter(cfg, available=True)

    assert [name for name, _ in fake.calls] == list(SEMANTIC_SETTER_NAMES)
    payloads = dict(fake.calls)
    assert payloads["set_graph_db_config"] == {
        "graph_database_provider": "neo4j",
        "graph_database_url": "bolt://graph.internal:7687",
        "graph_database_username": "omp",
        "graph_database_password": neo4j_secret,
        "graph_database_name": "knowledge",
    }
    assert payloads["set_relational_db_config"] == {
        "db_provider": "postgres",
        "db_host": "127.0.0.1",
        "db_port": str(QUALIFIED_COGNEE_PG_PORT),
        "db_name": QUALIFIED_COGNEE_PG_DATABASE,
        "db_username": QUALIFIED_COGNEE_PG_USER,
        "db_password": pg_secret,
    }
    assert payloads["set_vector_db_config"] == {
        "vector_db_provider": "pgvector",
        "vector_db_host": "127.0.0.1",
        "vector_db_port": QUALIFIED_COGNEE_PG_PORT,
        "vector_db_name": QUALIFIED_COGNEE_PG_DATABASE,
        "vector_db_username": QUALIFIED_COGNEE_PG_USER,
        "vector_db_password": pg_secret,
    }
    assert payloads["set_embedding_config"] == {
        "embedding_provider": "openai_compatible",
        "embedding_model": "qwen3-embedding-0.6b-q8",
        "embedding_dimensions": QUALIFIED_VECTOR_DIMENSION,
        "embedding_endpoint": "http://127.0.0.1:18081",
        "embedding_api_key": "no-key-required",
    }
    # Readback through the getters sees exactly what the setters applied.
    assert vars(fake.applied["set_embedding_config"]) == payloads["set_embedding_config"]


def test_adapter_fails_closed_when_embedding_readback_getter_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _FakeCogneeConfig()
    _install_fake_cognee(monkeypatch, fake)
    getters = fake.getters()
    del getters["set_embedding_config"]
    monkeypatch.setattr(cognee_adapter, "CONFIG_READBACK_GETTERS", getters)

    cfg = _cfg(
        tmp_path,
        graph_engine="neo4j",
        vector_store="pgvector",
        graph_only=False,
        embedding_provider="openai_compatible",
        embedding_model="qwen3-embedding-0.6b-q8",
        embedding_endpoint="http://127.0.0.1:18081",
        neo4j_password_file=_secret_file(tmp_path, "neo4j.secret", "graph-pw"),
        cognee_pg_password_file=_secret_file(tmp_path, "pg.secret", "pg-pw"),
    )
    with pytest.raises(EngineUnavailableError, match="no readback getter for config.set_embedding_config"):
        RealCogneeAdapter(cfg, available=True)


def test_adapter_fails_closed_when_cognee_config_readback_mismatches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A setter that silently drops a key leaves Cognee on a different endpoint than
    the route claims; the adapter must refuse to come up and must name only keys.
    """
    secret = "readback-secret-value"
    fake = _FakeCogneeConfig(ignore_keys={"set_graph_db_config": ("graph_database_url", "graph_database_password")})
    _install_fake_cognee(monkeypatch, fake)

    cfg = _cfg(
        tmp_path,
        graph_engine="neo4j",
        neo4j_password_file=_secret_file(tmp_path, "neo4j.secret", secret),
    )
    with pytest.raises(EngineUnavailableError) as excinfo:
        RealCogneeAdapter(cfg, available=True)

    message = str(excinfo.value)
    assert "set_graph_db_config" in message
    assert "readback mismatch for keys: graph_database_password, graph_database_url" in message
    assert secret not in message
    assert cfg.neo4j_uri not in message


def test_adapter_fails_closed_without_cognee_readback_getter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _FakeCogneeConfig()
    _install_fake_cognee(monkeypatch, fake)
    monkeypatch.setattr(cognee_adapter, "CONFIG_READBACK_GETTERS", {})

    cfg = _cfg(
        tmp_path,
        graph_engine="neo4j",
        neo4j_password_file=_secret_file(tmp_path, "neo4j.secret", "graph-pw"),
    )
    with pytest.raises(EngineUnavailableError, match="no readback getter for config.set_graph_db_config"):
        RealCogneeAdapter(cfg, available=True)


def test_adapter_binds_only_the_setters_the_route_needs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _FakeCogneeConfig()
    _install_fake_cognee(monkeypatch, fake)

    cfg = _cfg(
        tmp_path,
        graph_engine="neo4j",
        neo4j_password_file=_secret_file(tmp_path, "neo4j.secret", "graph-pw"),
    )
    RealCogneeAdapter(cfg, available=True)

    assert [name for name, _ in fake.calls] == ["set_graph_db_config"]


def test_adapter_fails_closed_when_cognee_setter_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_fake_cognee(monkeypatch, SimpleNamespace())  # no set_*_db_config at all

    cfg = _cfg(
        tmp_path,
        graph_engine="neo4j",
        neo4j_password_file=_secret_file(tmp_path, "neo4j.secret", "graph-pw"),
    )
    with pytest.raises(EngineUnavailableError, match="set_graph_db_config"):
        RealCogneeAdapter(cfg, available=True)


def test_adapter_setter_rejection_fails_closed_and_redacts_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret = "very-secret-graph-password"
    fake = _FakeCogneeConfig(
        fail_on="set_graph_db_config",
        fail_message=f"'graph_database_password' rejected value {secret}",
    )
    _install_fake_cognee(monkeypatch, fake)

    cfg = _cfg(
        tmp_path,
        graph_engine="neo4j",
        neo4j_password_file=_secret_file(tmp_path, "neo4j.secret", secret),
    )
    with pytest.raises(EngineUnavailableError) as excinfo:
        RealCogneeAdapter(cfg, available=True)

    message = str(excinfo.value)
    assert "set_graph_db_config" in message
    assert "***" in message
    assert secret not in message

"""FK-3 inference routes: file validation, health probing, resolution, and CLI.

A route file declares one primary (and optional fallback) per role. An ``order``
reranker is healthy without a network call; a ``llama.cpp`` profile is healthy
iff ``GET {endpoint}/health`` returns 200. Resolution returns the selected
profile with the primary's failure detail as the reason, or raises
``RouteUnavailable`` (503). The CLI prints one row per role and exits 0 only when
every declared role resolves.
"""

from __future__ import annotations

import io
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest

from omp_knowledge.errors import KnowledgeError
from omp_knowledge.inference import (
    EmbeddingProfile,
    RouteConfigError,
    RouteHealth,
    RouteProfile,
    RouteUnavailable,
    load_routes,
    probe,
    resolve,
)
from omp_knowledge.inference.cli import EXIT_OK, EXIT_UNRESOLVED, main as health_main

_EMBED_PROFILE: dict[str, Any] = {
    "model": "Qwen3-Embedding-0.6B",
    "model_revision": "Q8_0",
    "dimensions": 1024,
    "pooling": "last",
    "query_prefix": "Instruct: Find code for this task\nQuery: ",
    "document_prefix": "",
    "normalize": True,
}


def _write(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "routes.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _embedding_route(profile: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "role": "embedding",
        "primary": {
            "name": "embed-gpu",
            "provider": "llama.cpp",
            "model": "Qwen3-Embedding-0.6B",
            "endpoint": "http://127.0.0.1:18081",
            "accelerator": "gpu",
            "embedding_profile": dict(_EMBED_PROFILE if profile is None else profile),
        },
        "fallback": {
            "name": "embed-cpu",
            "provider": "llama.cpp",
            "model": "Qwen3-Embedding-0.6B",
            "endpoint": "http://127.0.0.1:18084",
            "accelerator": "cpu",
            "embedding_profile": dict(_EMBED_PROFILE if profile is None else profile),
        },
    }


def _valid_routes() -> dict[str, Any]:
    return {
        "routes": [
            _embedding_route(),
            {
                "role": "reranker",
                "primary": {
                    "name": "rerank-gpu",
                    "provider": "llama.cpp",
                    "model": "Qwen3-Reranker-0.6B",
                    "endpoint": "http://127.0.0.1:18082",
                    "accelerator": "gpu",
                },
                "fallback": {
                    "name": "rerank-order",
                    "provider": "order",
                    "accelerator": "cpu",
                },
            },
            {
                "role": "generator",
                "primary": {
                    "name": "gen-gpu",
                    "provider": "llama.cpp",
                    "model": "Qwen3.8-27B",
                    "endpoint": "http://127.0.0.1:18083",
                    "accelerator": "gpu",
                },
                "fallback": None,
            },
        ]
    }


def test_valid_file_loads_and_pins_the_declared_identity(tmp_path: Path) -> None:
    route_set = load_routes(_write(tmp_path, _valid_routes()))

    assert [entry.role for entry in route_set.routes] == [
        "embedding",
        "reranker",
        "generator",
    ]
    embedding = route_set.entry("embedding")
    assert embedding is not None
    assert embedding.primary.endpoint == "http://127.0.0.1:18081"
    assert embedding.fallback is not None
    assert embedding.fallback.accelerator == "cpu"
    assert embedding.primary.embedding_profile is not None
    assert embedding.primary.embedding_profile.generation_id == (
        embedding.fallback.embedding_profile.generation_id
    )

    reranker = route_set.entry("reranker")
    assert reranker is not None
    assert reranker.fallback is not None
    assert reranker.fallback.provider == "order"
    assert reranker.fallback.model is None and reranker.fallback.endpoint is None
    assert reranker.primary.embedding_profile is None

    generator = route_set.entry("generator")
    assert generator is not None
    assert generator.fallback is None


def test_duplicate_role_is_refused(tmp_path: Path) -> None:
    payload = _valid_routes()
    payload["routes"].append(payload["routes"][2])
    with pytest.raises(RouteConfigError) as excinfo:
        load_routes(_write(tmp_path, payload))
    assert excinfo.value.code == "route_config_invalid"
    assert excinfo.value.status_code == 400
    assert "duplicate" in str(excinfo.value)


def test_order_provider_off_reranker_is_refused(tmp_path: Path) -> None:
    payload = _valid_routes()
    payload["routes"][2]["primary"] = {
        "name": "gen-order",
        "provider": "order",
        "accelerator": "cpu",
    }
    with pytest.raises(RouteConfigError) as excinfo:
        load_routes(_write(tmp_path, payload))
    assert excinfo.value.code == "route_config_invalid"
    assert "order" in str(excinfo.value)


def test_missing_embedding_profile_is_refused(tmp_path: Path) -> None:
    payload = _valid_routes()
    del payload["routes"][0]["primary"]["embedding_profile"]
    with pytest.raises(RouteConfigError) as excinfo:
        load_routes(_write(tmp_path, payload))
    assert excinfo.value.code == "route_config_invalid"
    assert "embedding profile" in str(excinfo.value)


def test_misplaced_embedding_profile_is_refused(tmp_path: Path) -> None:
    payload = _valid_routes()
    payload["routes"][2]["primary"]["embedding_profile"] = dict(_EMBED_PROFILE)
    with pytest.raises(RouteConfigError) as excinfo:
        load_routes(_write(tmp_path, payload))
    assert excinfo.value.code == "route_config_invalid"
    assert "must not declare" in str(excinfo.value)


def test_embedding_profile_model_mismatch_is_refused(tmp_path: Path) -> None:
    payload = _valid_routes()
    other = dict(_EMBED_PROFILE, model="Qwen3-Embedding-4B")
    payload["routes"][0]["fallback"]["embedding_profile"] = other
    with pytest.raises(RouteConfigError) as excinfo:
        load_routes(_write(tmp_path, payload))
    assert excinfo.value.code == "route_config_invalid"
    assert "does not match" in str(excinfo.value)


def test_fallback_generation_mismatch_is_route_incompatible(tmp_path: Path) -> None:
    payload = _valid_routes()
    payload["routes"][0]["fallback"]["embedding_profile"] = dict(
        _EMBED_PROFILE, dimensions=768
    )
    with pytest.raises(RouteConfigError) as excinfo:
        load_routes(_write(tmp_path, payload))
    assert excinfo.value.code == "route_incompatible"
    assert excinfo.value.status_code == 400
    assert isinstance(excinfo.value, KnowledgeError)


def test_non_loopback_endpoint_is_refused(tmp_path: Path) -> None:
    payload = _valid_routes()
    payload["routes"][0]["primary"]["endpoint"] = "http://10.0.0.5:18081"
    with pytest.raises(RouteConfigError) as excinfo:
        load_routes(_write(tmp_path, payload))
    assert excinfo.value.code == "route_config_invalid"
    assert "loopback" in str(excinfo.value)


def test_malformed_json_is_route_config_invalid(tmp_path: Path) -> None:
    path = tmp_path / "routes.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(RouteConfigError):
        load_routes(path)


class HealthHandler(BaseHTTPRequestHandler):
    """Answers ``GET /health`` with the class-pinned status and records paths."""

    status: ClassVar[int] = 200
    paths: ClassVar[list[str]] = []

    def log_message(self, format: str, *args: Any) -> None:
        return None

    def do_GET(self) -> None:
        HealthHandler.paths.append(self.path)
        self.send_response(self.status)
        self.end_headers()
        self.wfile.write(b"{}")


@pytest.fixture
def health_server() -> Any:
    server = HTTPServer(("127.0.0.1", 0), HealthHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    HealthHandler.status = 200
    HealthHandler.paths = []
    try:
        yield f"http://127.0.0.1:{port}", HealthHandler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_probe_ok_iff_200_and_order_is_always_ok(
    health_server: tuple[str, type[HealthHandler]],
) -> None:
    endpoint, handler = health_server
    profile = RouteProfile(
        name="embed-gpu",
        provider="llama.cpp",
        model="m",
        endpoint=endpoint,
        accelerator="gpu",
    )
    health = probe(profile)
    assert health.name == "embed-gpu"
    assert health.ok is True
    assert health.detail == "ok"
    assert handler.paths == ["/health"]
    assert health.latency_ms >= 0.0

    handler.status = 503
    assert probe(profile).ok is False
    assert handler.paths == ["/health", "/health"]

    order = RouteProfile(name="rerank-order", provider="order", accelerator="cpu")
    order_health = probe(order)
    assert order_health.ok is True and order_health.detail == "order"
    assert handler.paths == ["/health", "/health"]


def test_probe_unreachable_is_not_ok() -> None:
    profile = RouteProfile(
        name="embed-cpu",
        provider="llama.cpp",
        model="m",
        endpoint="http://127.0.0.1:9",
        accelerator="cpu",
    )
    health = probe(profile)
    assert health.ok is False
    assert "unreachable" in health.detail or "HTTP" in health.detail


def _health(name: str, ok: bool, detail: str = "ok") -> RouteHealth:
    return RouteHealth(name=name, ok=ok, detail=detail, latency_ms=0.0)


def test_resolve_prefers_healthy_primary(tmp_path: Path) -> None:
    route_set = load_routes(_write(tmp_path, _valid_routes()))
    calls: list[str] = []

    def prober(profile: RouteProfile) -> RouteHealth:
        calls.append(profile.name)
        return _health(profile.name, True)

    resolved = resolve(route_set, "generator", prober)
    assert resolved.used == "primary"
    assert resolved.profile.name == "gen-gpu"
    assert resolved.reason == ""
    assert calls == ["gen-gpu"]


def test_resolve_falls_back_and_reports_primary_detail(tmp_path: Path) -> None:
    route_set = load_routes(_write(tmp_path, _valid_routes()))
    calls: list[str] = []

    def prober(profile: RouteProfile) -> RouteHealth:
        calls.append(profile.name)
        if profile.name == "embed-gpu":
            return _health(profile.name, False, "HTTP 503")
        return _health(profile.name, True)

    resolved = resolve(route_set, "embedding", prober)
    assert resolved.used == "fallback"
    assert resolved.profile.name == "embed-cpu"
    assert resolved.reason == "HTTP 503"
    assert calls == ["embed-gpu", "embed-cpu"]


def test_resolve_refuses_when_no_profile_is_healthy(tmp_path: Path) -> None:
    route_set = load_routes(_write(tmp_path, _valid_routes()))

    def prober(profile: RouteProfile) -> RouteHealth:
        return _health(
            profile.name, profile.provider == "order", detail="unreachable: refused"
        )

    with pytest.raises(RouteUnavailable) as excinfo:
        resolve(route_set, "embedding", prober)
    assert excinfo.value.code == "route_unavailable"
    assert excinfo.value.status_code == 503
    assert isinstance(excinfo.value, KnowledgeError)


def test_resolve_refuses_undeclared_role(tmp_path: Path) -> None:
    route_set = load_routes(_write(tmp_path, _valid_routes()))
    with pytest.raises(RouteUnavailable) as excinfo:
        resolve(route_set, "extractor", lambda profile: _health(profile.name, True))
    assert excinfo.value.code == "route_unavailable"
    assert excinfo.value.status_code == 503


def test_health_cli_json_resolves_every_role_and_exits_0(tmp_path: Path) -> None:
    path = _write(tmp_path, _valid_routes())
    stdout = io.StringIO()
    code = health_main(
        ["health", "--routes", str(path), "--json"],
        prober=lambda profile: _health(profile.name, True),
        stdout=stdout,
    )
    assert code == EXIT_OK
    payload = json.loads(stdout.getvalue())
    assert [row["role"] for row in payload["roles"]] == [
        "embedding",
        "reranker",
        "generator",
    ]
    assert payload["roles"][0] == {
        "role": "embedding",
        "primary": "embed-gpu",
        "fallback": "embed-cpu",
        "used": "primary",
        "reason": "",
    }
    assert payload["roles"][2]["fallback"] is None


def test_health_cli_exits_2_when_a_role_is_unresolved(tmp_path: Path) -> None:
    path = _write(tmp_path, _valid_routes())
    stdout = io.StringIO()
    code = health_main(
        ["health", "--routes", str(path), "--json"],
        prober=lambda profile: _health(
            profile.name, profile.name == "rerank-order", detail="unreachable"
        ),
        stdout=stdout,
    )
    assert code == EXIT_UNRESOLVED
    payload = json.loads(stdout.getvalue())
    assert payload["roles"][0]["used"] == "none"
    assert payload["roles"][1]["used"] == "fallback"
    assert payload["roles"][2]["used"] == "none"


def test_health_cli_human_output_lists_every_role(tmp_path: Path) -> None:
    path = _write(tmp_path, _valid_routes())
    stdout = io.StringIO()
    code = health_main(
        ["health", "--routes", str(path)],
        prober=lambda profile: _health(
            profile.name,
            profile.name != "rerank-gpu",
            detail="HTTP 503",
        ),
        stdout=stdout,
    )
    assert code == EXIT_OK
    lines = stdout.getvalue().strip().splitlines()
    assert len(lines) == 3
    assert "embedding primary=embed-gpu fallback=embed-cpu used=primary" in lines[0]
    assert "reranker primary=rerank-gpu fallback=rerank-order used=fallback" in lines[1]
    assert "reason=HTTP 503" in lines[1]
    assert "generator primary=gen-gpu fallback=-" in lines[2]


def test_health_cli_refuses_an_invalid_file(tmp_path: Path, capsys: Any) -> None:
    payload = _valid_routes()
    payload["routes"][0]["primary"]["embedding_profile"] = dict(
        _EMBED_PROFILE, model="mismatch"
    )
    path = _write(tmp_path, payload)
    stdout = io.StringIO()
    code = health_main(
        ["health", "--routes", str(path), "--json"],
        prober=lambda profile: _health(profile.name, True),
        stdout=stdout,
    )
    captured = capsys.readouterr()
    assert code == EXIT_UNRESOLVED
    assert stdout.getvalue() == ""
    assert "route_config_invalid" in captured.err


def test_readme_block_loads_and_resolves_against_stubs(tmp_path: Path) -> None:
    """The README's ```json fk-routes``` block is a real, loadable route file."""
    readme = Path(__file__).resolve().parents[1] / "README.md"
    text = readme.read_text(encoding="utf-8")
    block = text.split("```json fk-routes", 1)[1].split("```", 1)[0]
    payload = json.loads(block)
    routes = payload["routes"]
    assert [row["role"] for row in routes] == ["embedding", "reranker", "generator"]

    # Repoint every llama.cpp endpoint at one stub port, as the runbook directs.
    handler = HealthHandler
    handler.status = 200
    handler.paths = []
    server = HTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for row in routes:
            for position in ("primary", "fallback"):
                profile = row[position]
                if profile is not None and profile["provider"] == "llama.cpp":
                    profile["endpoint"] = f"http://127.0.0.1:{port}"
        path = tmp_path / "readme-routes.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        route_set = load_routes(path)

        stdout = io.StringIO()
        code = health_main(
            ["health", "--routes", str(path), "--json"],
            prober=probe,
            stdout=stdout,
        )
        assert code == EXIT_OK
        rows = json.loads(stdout.getvalue())["roles"]
        assert [row["used"] for row in rows] == ["primary", "primary", "primary"]
        assert route_set.entry("embedding").fallback.accelerator == "cpu"
        assert all(
            profile.endpoint is None or profile.endpoint.startswith("http://127.0.0.1:")
            for entry in route_set.routes
            for profile in (entry.primary, entry.fallback)
            if profile is not None
        )

        # With the GPU reranker down, the order fallback resolves for its role.
        routes[1]["primary"]["endpoint"] = "http://127.0.0.1:9"
        down_path = tmp_path / "readme-routes-down.json"
        down_path.write_text(json.dumps(payload), encoding="utf-8")
        down_stdout = io.StringIO()
        assert health_main(
            ["health", "--routes", str(down_path), "--json"],
            prober=probe,
            stdout=down_stdout,
        ) == EXIT_OK
        down_rows = json.loads(down_stdout.getvalue())["roles"]
        assert down_rows[1]["used"] == "fallback"
        assert down_rows[1]["fallback"] == "rerank-order"
        assert down_rows[1]["reason"].startswith("unreachable")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_embedding_profile_round_trips_the_readme_identity(tmp_path: Path) -> None:
    route_set = load_routes(_write(tmp_path, _valid_routes()))
    profile = route_set.entry("embedding").primary.embedding_profile
    assert profile is not None
    assert profile == EmbeddingProfile.model_validate(_EMBED_PROFILE)
    assert profile.pooling == "last"
    assert profile.query_prefix == "Instruct: Find code for this task\nQuery: "
    assert profile.document_prefix == ""
    assert profile.normalize is True
    assert profile.dimensions == 1024

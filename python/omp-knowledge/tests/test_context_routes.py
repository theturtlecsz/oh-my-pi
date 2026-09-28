"""Contract tests for context compile routes and vector projection retrieval (OMP-278-s04).

Defines loopback stubs for inference probing, embedding, and reranking, and
verifies:
- Reranker primary down: bundle compiles; output and stored routes show used=fallback + reason.
- All reranker routes down: exit 2, no bundle row.
- Vector items render for a permitted published snapshot; unpermitted denied.
- Unpublished or unbuilt snapshots emit missing semantic items.
- Legacy --reranker-url and default runs store their route row.
- CLI argument validation for --routes and --vector-state-dir.
- Embed/rerank failures after resolution exit 2 with nothing stored.
- Maintenance rebuild without context-routes.sqlite succeeds.
- Maintenance rebuild with dropped context_bundle_routes table is refused.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Generator
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from omp_knowledge.context.cli import EXIT_ERROR, EXIT_OK, main
from omp_knowledge.context.routes import ContextRouteStore
from omp_knowledge.context.store import ContextBundleStore
from omp_knowledge.inference import EmbeddingProfile
from omp_knowledge.inference.embedding import HttpEmbedder
from omp_knowledge.maintenance import MaintenanceError, create_backup, rebuild_from_records
from omp_knowledge.publication import PublicationManager
from omp_knowledge.vectors.store import VectorProjectionStore
from omp_work.knowledge_publication import StructuralPublicationStore
from omp_work.knowledge_source import normalize_remote_url
from omp_work.v1.api_models import WorkflowView, WorkItemView
from omp_work.v1.models import (
    Candidate,
    EvidenceKind,
    EvidenceReceipt,
    WorkAlias,
    WorkRevision,
)
from support.fixtures import load_staged_fixture

_ORIGIN = "git@github.com:acme/widgets.git"
_TOKEN_BUDGET = 100_000
_SNAP_A = "5dc87df1316f9afab59d47c42eed60f6a3d78286d9dc852d5b9ff827b66d4aee"
_SNAP_UNPUBLISHED = "b" * 64
_SNAP_UNPERMITTED = "c" * 64

_EMBED_PROFILE: dict[str, Any] = {
    "model": "Qwen3-Embedding-0.6B",
    "model_revision": "Q8_0",
    "dimensions": 4,
    "pooling": "last",
    "query_prefix": "query: ",
    "document_prefix": "",
    "normalize": True,
}


def _make_stub_server(
    *,
    health_status: int = 200,
    embed_status: int = 200,
    rerank_status: int = 200,
    embedding_dim: int = 4,
) -> tuple[HTTPServer, str]:
    class StubHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            if self.path == "/health":
                self.send_response(health_status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status": "ok"}')
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            if self.path == "/v1/embeddings":
                if embed_status != 200:
                    self.send_response(embed_status)
                    self.end_headers()
                    return
                inputs = body.get("input", [])
                data = [
                    {"index": i, "embedding": [1.0] + [0.0] * (embedding_dim - 1)}
                    for i in range(len(inputs))
                ]
                resp = json.dumps({"data": data}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(resp)
            elif self.path == "/v1/rerank":
                if rerank_status != 200:
                    self.send_response(rerank_status)
                    self.end_headers()
                    return
                docs = body.get("documents", [])
                results = [
                    {"index": i, "relevance_score": float(len(docs) - i)}
                    for i in range(len(docs))
                ]
                resp = json.dumps({"results": results}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(resp)
            else:
                self.send_response(404)
                self.end_headers()

    server = HTTPServer(("127.0.0.1", 0), StubHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}"


@pytest.fixture
def stub_pool() -> Generator[list[HTTPServer], None, None]:
    servers: list[HTTPServer] = []
    try:
        yield servers
    finally:
        for s in servers:
            s.shutdown()
            s.server_close()


def _counter_script(path: Path) -> None:
    path.write_text(
        """\
import json
import sys

args = sys.argv[1:]
if len(args) < 3 or args[0] != "count" or args[1] != "--encoding":
    sys.stderr.write("expected: count --encoding <name>\\n")
    raise SystemExit(1)
encoding = args[2]
sys.stdout.write(
    json.dumps({"api": "countTokens", "encoding": encoding}, separators=(",", ":")) + "\\n"
)
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    item = json.loads(line)
    count = len(str(item["text"]).split())
    sys.stdout.write(
        json.dumps({"id": item["id"], "tokens": count}, separators=(",", ":")) + "\\n"
    )
""",
        encoding="utf-8",
    )


def _git_repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "origin", _ORIGIN], cwd=path, check=True)


def _make_view(
    work_id: UUID,
    revision_id: UUID,
    candidate_id: UUID,
    *,
    with_receipt: bool = False,
) -> WorkflowView:
    receipts: tuple[EvidenceReceipt, ...] = ()
    if with_receipt:
        receipts = (
            EvidenceReceipt(
                receipt_id=uuid4(),
                work_id=work_id,
                revision_id=revision_id,
                candidate_id=candidate_id,
                kind=EvidenceKind.VERIFICATION,
                payload={"exit_code": 0},
                payload_sha256="e" * 64,
                issuer="tester",
                issued_at=datetime(2026, 9, 28, tzinfo=timezone.utc),
                verdict="PASS",
            ),
        )
    item = WorkItemView(
        work_id=work_id,
        workspace_id=uuid4(),
        alias=WorkAlias(work_id=work_id, key="OMP-278", primary=True, origin="local"),
        state="in_progress",
        revision=WorkRevision(
            revision_id=revision_id,
            work_id=work_id,
            revision_number=1,
            title="Search vectors and route context",
            description="context compile with inference routes",
            scope="python/omp-knowledge",
            acceptance_criteria=("routes are recorded",),
            content_sha256="a" * 64,
            created_by="tester",
            created_at=datetime(2026, 9, 28, tzinfo=timezone.utc),
        ),
        candidate=Candidate(
            candidate_id=candidate_id,
            work_id=work_id,
            revision_id=revision_id,
            candidate_sha256="b" * 64,
            allocated_at=datetime(2026, 9, 28, tzinfo=timezone.utc),
        ),
        project_id=uuid4(),
    )
    return WorkflowView(item=item, receipts=receipts)


def _stdin(cwd: Path, view: WorkflowView) -> io.StringIO:
    payload = {
        "stage": "implement",
        "attempt_id": "attempt-1",
        "cwd": str(cwd),
        "workflow": json.loads(view.model_dump_json()),
    }
    return io.StringIO(json.dumps(payload))


def _publish_fixture(state_dir: Path, workspace_id: UUID, repository_id: UUID) -> StructuralPublicationStore:
    facts_bytes, receipt_bytes, _insights, _raw = load_staged_fixture("A")
    store = StructuralPublicationStore(state_dir)
    store.stage_enola_snapshot(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=_SNAP_A,
        facts_bytes=facts_bytes,
        receipt_bytes=receipt_bytes,
        manifest_files=[],
    )
    store.publish(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=_SNAP_A,
    )
    return store


def test_reranker_primary_down_uses_fallback(tmp_path: Path, stub_pool: list[HTTPServer]) -> None:
    server_down, down_endpoint = _make_stub_server(health_status=503)
    stub_pool.append(server_down)
    server_up, up_endpoint = _make_stub_server(health_status=200, rerank_status=200)
    stub_pool.append(server_up)

    routes_payload = {
        "routes": [
            {
                "role": "reranker",
                "primary": {
                    "name": "rerank-gpu",
                    "provider": "llama.cpp",
                    "model": "Qwen3-Reranker-0.6B",
                    "endpoint": down_endpoint,
                    "accelerator": "gpu",
                },
                "fallback": {
                    "name": "rerank-cpu",
                    "provider": "llama.cpp",
                    "model": "Qwen3-Reranker-0.6B",
                    "endpoint": up_endpoint,
                    "accelerator": "cpu",
                },
            }
        ]
    }
    routes_file = tmp_path / "routes.json"
    routes_file.write_text(json.dumps(routes_payload), encoding="utf-8")

    repo = tmp_path / "repo"
    _git_repo(repo)
    counter = tmp_path / "counter.py"
    _counter_script(counter)

    view = _make_view(uuid4(), uuid4(), uuid4())
    state_dir = tmp_path / "state"

    stdout = io.StringIO()
    code = main(
        [
            "compile",
            "--state-dir",
            str(state_dir),
            "--token-cmd",
            json.dumps([sys.executable, str(counter)]),
            "--encoding",
            "test-words",
            "--token-budget",
            str(_TOKEN_BUDGET),
            "--routes",
            str(routes_file),
            "--json",
        ],
        stdin=_stdin(repo, view),
        stdout=stdout,
    )
    assert code == EXIT_OK, stdout.getvalue()

    output = json.loads(stdout.getvalue())
    assert "routes" in output
    assert len(output["routes"]) == 1
    route_entry = output["routes"][0]
    assert route_entry["role"] == "reranker"
    assert route_entry["name"] == "rerank-cpu"
    assert route_entry["provider"] == "llama.cpp"
    assert route_entry["used"] == "fallback"
    assert "HTTP 503" in route_entry["reason"]

    # Verify stored in ContextRouteStore
    route_store = ContextRouteStore(state_dir)
    stored_routes = route_store.load(output["bundle_id"])
    assert len(stored_routes) == 1
    assert stored_routes[0]["role"] == "reranker"
    assert stored_routes[0]["name"] == "rerank-cpu"
    assert stored_routes[0]["used"] == "fallback"
    assert "HTTP 503" in stored_routes[0]["reason"]


def test_all_reranker_routes_down_exits_2_no_bundle_row(tmp_path: Path, stub_pool: list[HTTPServer]) -> None:
    server1, down_endpoint1 = _make_stub_server(health_status=503)
    stub_pool.append(server1)
    server2, down_endpoint2 = _make_stub_server(health_status=503)
    stub_pool.append(server2)

    routes_payload = {
        "routes": [
            {
                "role": "reranker",
                "primary": {
                    "name": "rerank-gpu",
                    "provider": "llama.cpp",
                    "model": "Qwen3-Reranker-0.6B",
                    "endpoint": down_endpoint1,
                    "accelerator": "gpu",
                },
                "fallback": {
                    "name": "rerank-cpu",
                    "provider": "llama.cpp",
                    "model": "Qwen3-Reranker-0.6B",
                    "endpoint": down_endpoint2,
                    "accelerator": "cpu",
                },
            }
        ]
    }
    routes_file = tmp_path / "routes.json"
    routes_file.write_text(json.dumps(routes_payload), encoding="utf-8")

    repo = tmp_path / "repo"
    _git_repo(repo)
    counter = tmp_path / "counter.py"
    _counter_script(counter)

    view = _make_view(uuid4(), uuid4(), uuid4())
    state_dir = tmp_path / "state"

    stdout = io.StringIO()
    code = main(
        [
            "compile",
            "--state-dir",
            str(state_dir),
            "--token-cmd",
            json.dumps([sys.executable, str(counter)]),
            "--encoding",
            "test-words",
            "--token-budget",
            str(_TOKEN_BUDGET),
            "--routes",
            str(routes_file),
            "--json",
        ],
        stdin=_stdin(repo, view),
        stdout=stdout,
    )
    assert code == EXIT_ERROR
    assert stdout.getvalue() == ""

    # Verify no bundle row stored
    db_path = state_dir / "context-bundles.sqlite"
    if db_path.exists():
        conn = sqlite3.connect(db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM context_bundles").fetchone()[0]
            assert count == 0
        finally:
            conn.close()


def test_vector_items_render_for_permitted_published_and_denied_for_unpermitted(
    tmp_path: Path, stub_pool: list[HTTPServer]
) -> None:
    embed_server, embed_endpoint = _make_stub_server(health_status=200, embed_status=200, embedding_dim=4)
    stub_pool.append(embed_server)
    rerank_server, rerank_endpoint = _make_stub_server(health_status=200, rerank_status=200)
    stub_pool.append(rerank_server)

    routes_payload = {
        "routes": [
            {
                "role": "embedding",
                "primary": {
                    "name": "embed-gpu",
                    "provider": "llama.cpp",
                    "model": "Qwen3-Embedding-0.6B",
                    "endpoint": embed_endpoint,
                    "accelerator": "gpu",
                    "embedding_profile": dict(_EMBED_PROFILE),
                },
                "fallback": None,
            },
            {
                "role": "reranker",
                "primary": {
                    "name": "rerank-gpu",
                    "provider": "llama.cpp",
                    "model": "Qwen3-Reranker-0.6B",
                    "endpoint": rerank_endpoint,
                    "accelerator": "gpu",
                },
                "fallback": None,
            },
        ]
    }
    routes_file = tmp_path / "routes.json"
    routes_file.write_text(json.dumps(routes_payload), encoding="utf-8")

    ws_id = uuid4()
    repo_permitted = uuid4()
    repo_unpermitted = uuid4()

    structural_dir = tmp_path / "structural"
    pub_store = _publish_fixture(structural_dir, ws_id, repo_permitted)

    vector_dir = tmp_path / "vectors"
    vec_store = VectorProjectionStore(vector_dir)
    embedder = HttpEmbedder(embed_endpoint, EmbeddingProfile.model_validate(_EMBED_PROFILE))
    vec_store.build(pub_store, ws_id, repo_permitted, _SNAP_A, embedder)

    repo = tmp_path / "repo"
    _git_repo(repo)
    counter = tmp_path / "counter.py"
    _counter_script(counter)

    view = _make_view(uuid4(), uuid4(), uuid4())
    state_dir = tmp_path / "state"

    stdout = io.StringIO()
    code = main(
        [
            "compile",
            "--state-dir",
            str(state_dir),
            "--token-cmd",
            json.dumps([sys.executable, str(counter)]),
            "--encoding",
            "test-words",
            "--token-budget",
            str(_TOKEN_BUDGET),
            "--structural-state-dir",
            str(structural_dir),
            "--vector-state-dir",
            str(vector_dir),
            "--snapshot",
            f"{ws_id}:{repo_permitted}:{_SNAP_A}",
            "--snapshot",
            f"{ws_id}:{repo_unpermitted}:{_SNAP_UNPERMITTED}",
            "--permit-repository",
            str(repo_permitted),
            "--semantic-limit",
            "5",
            "--routes",
            str(routes_file),
            "--json",
        ],
        stdin=_stdin(repo, view),
        stdout=stdout,
    )
    assert code == EXIT_OK, stdout.getvalue()

    output = json.loads(stdout.getvalue())
    text = output["text"]

    # Vector items render for permitted published snapshot
    assert "## semantic" in text
    assert "vector#" in text

    # Unpermitted snapshot is denied
    exclusions = output["exclusions"]
    denied = [e for e in exclusions if e["section"] == "semantic" and e["reason"] == "denied"]
    assert len(denied) == 1
    assert denied[0]["source"] == "vector"
    assert denied[0]["ref"] == str(repo_unpermitted)

    # Output JSON gains routes
    routes = output["routes"]
    assert len(routes) == 2
    roles = {r["role"]: r for r in routes}
    assert "embedding" in roles
    assert "reranker" in roles
    assert roles["embedding"]["name"] == "embed-gpu"
    assert roles["embedding"]["used"] == "primary"
    assert roles["reranker"]["name"] == "rerank-gpu"
    assert roles["reranker"]["used"] == "primary"

    # ContextRouteStore persists both routes
    stored = ContextRouteStore(state_dir).load(output["bundle_id"])
    assert len(stored) == 2
    stored_roles = {r["role"]: r for r in stored}
    assert "embedding" in stored_roles
    assert "reranker" in stored_roles


def test_unpublished_and_unbuilt_snapshots_produce_missing_items(
    tmp_path: Path, stub_pool: list[HTTPServer]
) -> None:
    embed_server, embed_endpoint = _make_stub_server(health_status=200, embed_status=200, embedding_dim=4)
    stub_pool.append(embed_server)

    routes_payload = {
        "routes": [
            {
                "role": "embedding",
                "primary": {
                    "name": "embed-gpu",
                    "provider": "llama.cpp",
                    "model": "Qwen3-Embedding-0.6B",
                    "endpoint": embed_endpoint,
                    "accelerator": "gpu",
                    "embedding_profile": dict(_EMBED_PROFILE),
                },
                "fallback": None,
            },
            {
                "role": "reranker",
                "primary": {
                    "name": "rerank-order",
                    "provider": "order",
                    "accelerator": "cpu",
                },
                "fallback": None,
            },
        ]
    }
    routes_file = tmp_path / "routes.json"
    routes_file.write_text(json.dumps(routes_payload), encoding="utf-8")

    ws_id = uuid4()
    repo_id = uuid4()

    structural_dir = tmp_path / "structural"
    _publish_fixture(structural_dir, ws_id, repo_id)

    # Vector store exists but _SNAP_A is NOT built in vector store
    vector_dir = tmp_path / "vectors"
    VectorProjectionStore(vector_dir)

    repo = tmp_path / "repo"
    _git_repo(repo)
    counter = tmp_path / "counter.py"
    _counter_script(counter)

    view = _make_view(uuid4(), uuid4(), uuid4())
    state_dir = tmp_path / "state"

    stdout = io.StringIO()
    code = main(
        [
            "compile",
            "--state-dir",
            str(state_dir),
            "--token-cmd",
            json.dumps([sys.executable, str(counter)]),
            "--encoding",
            "test-words",
            "--token-budget",
            str(_TOKEN_BUDGET),
            "--structural-state-dir",
            str(structural_dir),
            "--vector-state-dir",
            str(vector_dir),
            "--snapshot",
            f"{ws_id}:{repo_id}:{_SNAP_A}",  # published but unbuilt in vectors
            "--snapshot",
            f"{ws_id}:{repo_id}:{_SNAP_UNPUBLISHED}",  # unpublished
            "--permit-repository",
            str(repo_id),
            "--routes",
            str(routes_file),
            "--json",
        ],
        stdin=_stdin(repo, view),
        stdout=stdout,
    )
    assert code == EXIT_OK, stdout.getvalue()

    output = json.loads(stdout.getvalue())
    exclusions = output["exclusions"]

    vector_missing = [e for e in exclusions if e["section"] == "semantic" and e["source"] == "vector" and e["reason"] == "missing"]
    assert len(vector_missing) == 2
    refs = {e["ref"] for e in vector_missing}
    assert _SNAP_A in refs
    assert _SNAP_UNPUBLISHED in refs


def test_reranker_url_and_default_runs_store_route_row(tmp_path: Path, stub_pool: list[HTTPServer]) -> None:
    rerank_server, rerank_endpoint = _make_stub_server(health_status=200, rerank_status=200)
    stub_pool.append(rerank_server)

    repo = tmp_path / "repo"
    _git_repo(repo)
    counter = tmp_path / "counter.py"
    _counter_script(counter)
    view = _make_view(uuid4(), uuid4(), uuid4())

    # 1. Default run: order reranker
    state_dir_default = tmp_path / "state_default"
    stdout_default = io.StringIO()
    code_default = main(
        [
            "compile",
            "--state-dir",
            str(state_dir_default),
            "--token-cmd",
            json.dumps([sys.executable, str(counter)]),
            "--encoding",
            "test-words",
            "--token-budget",
            str(_TOKEN_BUDGET),
            "--json",
        ],
        stdin=_stdin(repo, view),
        stdout=stdout_default,
    )
    assert code_default == EXIT_OK
    out_default = json.loads(stdout_default.getvalue())
    assert out_default["routes"] == [
        {
            "role": "reranker",
            "name": "order",
            "provider": "order",
            "model": "",
            "accelerator": "cpu",
            "used": "primary",
            "reason": "",
        }
    ]
    stored_default = ContextRouteStore(state_dir_default).load(out_default["bundle_id"])
    assert len(stored_default) == 1
    assert stored_default[0]["name"] == "order"
    assert stored_default[0]["provider"] == "order"
    assert stored_default[0]["used"] == "primary"

    # 2. Legacy --reranker-url run
    state_dir_legacy = tmp_path / "state_legacy"
    stdout_legacy = io.StringIO()
    code_legacy = main(
        [
            "compile",
            "--state-dir",
            str(state_dir_legacy),
            "--token-cmd",
            json.dumps([sys.executable, str(counter)]),
            "--encoding",
            "test-words",
            "--token-budget",
            str(_TOKEN_BUDGET),
            "--reranker-url",
            rerank_endpoint,
            "--reranker-model",
            "custom-reranker-model",
            "--json",
        ],
        stdin=_stdin(repo, view),
        stdout=stdout_legacy,
    )
    assert code_legacy == EXIT_OK
    out_legacy = json.loads(stdout_legacy.getvalue())
    assert out_legacy["routes"] == [
        {
            "role": "reranker",
            "name": "cli",
            "provider": "llama.cpp",
            "model": "custom-reranker-model",
            "accelerator": "unknown",
            "used": "primary",
            "reason": "",
        }
    ]
    stored_legacy = ContextRouteStore(state_dir_legacy).load(out_legacy["bundle_id"])
    assert len(stored_legacy) == 1
    assert stored_legacy[0]["name"] == "cli"
    assert stored_legacy[0]["provider"] == "llama.cpp"
    assert stored_legacy[0]["model"] == "custom-reranker-model"


def test_cli_argument_validation(tmp_path: Path) -> None:
    routes_file = tmp_path / "routes.json"
    routes_file.write_text("{}", encoding="utf-8")

    # --routes with --reranker-url exits 2
    code1 = main(
        [
            "compile",
            "--state-dir",
            str(tmp_path / "s1"),
            "--token-cmd",
            "[]",
            "--encoding",
            "enc",
            "--token-budget",
            "1000",
            "--routes",
            str(routes_file),
            "--reranker-url",
            "http://127.0.0.1:1234",
            "--reranker-model",
            "m",
        ]
    )
    assert code1 == EXIT_ERROR

    # --vector-state-dir without --routes exits 2
    code2 = main(
        [
            "compile",
            "--state-dir",
            str(tmp_path / "s2"),
            "--token-cmd",
            "[]",
            "--encoding",
            "enc",
            "--token-budget",
            "1000",
            "--vector-state-dir",
            str(tmp_path / "v2"),
            "--structural-state-dir",
            str(tmp_path / "struct2"),
        ]
    )
    assert code2 == EXIT_ERROR

    # --vector-state-dir without --structural-state-dir exits 2
    code3 = main(
        [
            "compile",
            "--state-dir",
            str(tmp_path / "s3"),
            "--token-cmd",
            "[]",
            "--encoding",
            "enc",
            "--token-budget",
            "1000",
            "--routes",
            str(routes_file),
            "--vector-state-dir",
            str(tmp_path / "v3"),
        ]
    )
    assert code3 == EXIT_ERROR


def test_embed_or_rerank_failure_after_resolution_exits_2_nothing_stored(
    tmp_path: Path, stub_pool: list[HTTPServer]
) -> None:
    # Health check succeeds, but /v1/rerank fails with 500
    rerank_server, rerank_endpoint = _make_stub_server(health_status=200, rerank_status=500)
    stub_pool.append(rerank_server)

    routes_payload = {
        "routes": [
            {
                "role": "reranker",
                "primary": {
                    "name": "rerank-gpu",
                    "provider": "llama.cpp",
                    "model": "Qwen3-Reranker-0.6B",
                    "endpoint": rerank_endpoint,
                    "accelerator": "gpu",
                },
                "fallback": None,
            }
        ]
    }
    routes_file = tmp_path / "routes.json"
    routes_file.write_text(json.dumps(routes_payload), encoding="utf-8")

    repo = tmp_path / "repo"
    _git_repo(repo)
    counter = tmp_path / "counter.py"
    _counter_script(counter)
    # view with receipt so that optional items exist for reranking
    view = _make_view(uuid4(), uuid4(), uuid4(), with_receipt=True)
    state_dir = tmp_path / "state"

    stdout = io.StringIO()
    code = main(
        [
            "compile",
            "--state-dir",
            str(state_dir),
            "--token-cmd",
            json.dumps([sys.executable, str(counter)]),
            "--encoding",
            "test-words",
            "--token-budget",
            str(_TOKEN_BUDGET),
            "--routes",
            str(routes_file),
            "--json",
        ],
        stdin=_stdin(repo, view),
        stdout=stdout,
    )
    assert code == EXIT_ERROR
    assert stdout.getvalue() == ""

    # Nothing stored
    db_path = state_dir / "context-bundles.sqlite"
    if db_path.exists():
        conn = sqlite3.connect(db_path)
        try:
            assert conn.execute("SELECT COUNT(*) FROM context_bundles").fetchone()[0] == 0
        finally:
            conn.close()

    # Also test embedding failure after resolution: health 200, embed 500
    embed_server, embed_endpoint = _make_stub_server(health_status=200, embed_status=500, embedding_dim=4)
    stub_pool.append(embed_server)
    rerank_server_ok, rerank_endpoint_ok = _make_stub_server(health_status=200, rerank_status=200)
    stub_pool.append(rerank_server_ok)

    routes_payload2 = {
        "routes": [
            {
                "role": "embedding",
                "primary": {
                    "name": "embed-gpu",
                    "provider": "llama.cpp",
                    "model": "Qwen3-Embedding-0.6B",
                    "endpoint": embed_endpoint,
                    "accelerator": "gpu",
                    "embedding_profile": dict(_EMBED_PROFILE),
                },
                "fallback": None,
            },
            {
                "role": "reranker",
                "primary": {
                    "name": "rerank-gpu",
                    "provider": "llama.cpp",
                    "model": "Qwen3-Reranker-0.6B",
                    "endpoint": rerank_endpoint_ok,
                    "accelerator": "gpu",
                },
                "fallback": None,
            },
        ]
    }
    routes_file2 = tmp_path / "routes2.json"
    routes_file2.write_text(json.dumps(routes_payload2), encoding="utf-8")

    ws_id = uuid4()
    repo_id = uuid4()
    struct_dir = tmp_path / "struct_fail"
    _publish_fixture(struct_dir, ws_id, repo_id)
    vec_dir = tmp_path / "vec_fail"
    VectorProjectionStore(vec_dir)

    state_dir2 = tmp_path / "state2"
    stdout2 = io.StringIO()
    code2 = main(
        [
            "compile",
            "--state-dir",
            str(state_dir2),
            "--token-cmd",
            json.dumps([sys.executable, str(counter)]),
            "--encoding",
            "test-words",
            "--token-budget",
            str(_TOKEN_BUDGET),
            "--structural-state-dir",
            str(struct_dir),
            "--vector-state-dir",
            str(vec_dir),
            "--snapshot",
            f"{ws_id}:{repo_id}:{_SNAP_A}",
            "--permit-repository",
            str(repo_id),
            "--routes",
            str(routes_file2),
            "--json",
        ],
        stdin=_stdin(repo, view),
        stdout=stdout2,
    )
    assert code2 == EXIT_ERROR
    assert stdout2.getvalue() == ""
    db_path2 = state_dir2 / "context-bundles.sqlite"
    if db_path2.exists():
        conn = sqlite3.connect(db_path2)
        try:
            assert conn.execute("SELECT COUNT(*) FROM context_bundles").fetchone()[0] == 0
        finally:
            conn.close()


def test_backup_without_context_routes_sqlite_rebuilds(tmp_path: Path) -> None:
    state_root = tmp_path / "state_no_routes"
    state_root.mkdir(parents=True, exist_ok=True)
    ContextBundleStore(state_root)
    PublicationManager(state_root)

    backup_dir = tmp_path / "backup"
    manifest_info = create_backup(state_root, backup_dir)
    assert "context_routes" not in manifest_info["stores"]
    assert not (backup_dir / "context-routes.sqlite").exists()

    target_root = tmp_path / "target_rebuilt"
    report = rebuild_from_records(backup_dir, target_root)
    assert "context_bundles" in report["stores"]
    assert "context_routes" not in report["stores"]


def test_backup_with_dropped_context_routes_table_is_refused(tmp_path: Path) -> None:
    state_root = tmp_path / "state_with_routes"
    state_root.mkdir(parents=True, exist_ok=True)
    ContextBundleStore(state_root)
    route_store = ContextRouteStore(state_root)
    route_store.persist(
        "bundle-1",
        [
            {
                "role": "reranker",
                "name": "order",
                "provider": "order",
                "model": "",
                "accelerator": "cpu",
                "used": "primary",
                "reason": "",
            }
        ],
    )

    backup_dir = tmp_path / "backup"
    manifest_info = create_backup(state_root, backup_dir)
    assert "context_routes" in manifest_info["stores"]
    assert (backup_dir / "context-routes.sqlite").exists()

    # Drop the route table in records, sqlite, and manifest, and restamp checksums
    db_path = backup_dir / "context-routes.sqlite"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("DROP TABLE context_bundle_routes")
        conn.commit()
    finally:
        conn.close()

    records_path = backup_dir / "exact_records.json"
    records = json.loads(records_path.read_text(encoding="utf-8"))
    records["stores"]["context_routes"]["tables"] = {}
    records_path.write_text(json.dumps(records), encoding="utf-8")

    manifest_path = backup_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["stores"]["context_routes"]["tables"] = {}
    manifest["files"]["exact_records.json"] = {
        "sha256": hashlib.sha256(records_path.read_bytes()).hexdigest(),
        "size": records_path.stat().st_size,
    }
    manifest["files"]["context-routes.sqlite"] = {
        "sha256": hashlib.sha256(db_path.read_bytes()).hexdigest(),
        "size": db_path.stat().st_size,
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    # Rebuild must refuse because ContextRouteStore creates the table, but manifest recorded none
    target_root = tmp_path / "target_refused"
    with pytest.raises(MaintenanceError):
        rebuild_from_records(backup_dir, target_root)

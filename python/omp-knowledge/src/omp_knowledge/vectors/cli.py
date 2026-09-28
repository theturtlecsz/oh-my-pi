"""``python -m omp_knowledge.vectors build``.

Builds vector projection for a published snapshot using the declared embedding
route, and records float32 BLOB rows in vectors.sqlite.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, TextIO
from uuid import UUID

from omp_work.knowledge_namespace import validate_snapshot_id
from omp_work.knowledge_publication import StructuralPublicationStore

from omp_knowledge.inference.embedding import Embedder, HttpEmbedder
from omp_knowledge.inference.routes import load_routes, resolve
from omp_knowledge.vectors.store import VectorProjectionStore

EXIT_OK = 0
EXIT_ERROR = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m omp_knowledge.vectors")
    subcommands = parser.add_subparsers(dest="command", required=True)
    build_cmd = subcommands.add_parser(
        "build", help="build vector projection for a snapshot"
    )
    build_cmd.add_argument(
        "--state-dir", required=True, help="vector store state directory"
    )
    build_cmd.add_argument(
        "--structural-state-dir",
        required=True,
        help="structural publications state directory",
    )
    build_cmd.add_argument(
        "--snapshot",
        required=True,
        help="snapshot identity in WS:REPO:SNAP format",
    )
    build_cmd.add_argument(
        "--routes", required=True, help="path to routes configuration file"
    )
    build_cmd.add_argument(
        "--json", action="store_true", help="output structured JSON"
    )
    return parser


def _stderr(message: str, stderr: TextIO | None = None) -> None:
    stream = sys.stderr if stderr is None else stderr
    stream.write(message.strip() + "\n")
    flush = getattr(stream, "flush", None)
    if callable(flush):
        flush()


def main(
    argv: list[str] | None = None,
    *,
    embedder: Embedder | Any | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run vectors CLI command. Exits 0 on success, 2 on error."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) and code == 0 else EXIT_ERROR

    if args.command != "build":
        _stderr(f"unknown command: {args.command}", stderr)
        return EXIT_ERROR

    parts = args.snapshot.split(":")
    if len(parts) != 3:
        _stderr(
            f"snapshot must be in WS:REPO:SNAP format, got {args.snapshot!r}",
            stderr,
        )
        return EXIT_ERROR

    try:
        workspace_id = UUID(parts[0])
        repository_id = UUID(parts[1])
        snapshot_id = validate_snapshot_id(parts[2])
    except Exception as exc:
        _stderr(f"invalid snapshot identity: {exc}", stderr)
        return EXIT_ERROR

    try:
        route_set = load_routes(args.routes)
    except Exception as exc:
        _stderr(f"failed to load routes: {exc}", stderr)
        return EXIT_ERROR

    try:
        resolved = resolve(route_set, "embedding")
    except Exception as exc:
        _stderr(f"failed to resolve embedding route: {exc}", stderr)
        return EXIT_ERROR

    active_embedder = embedder
    if active_embedder is None:
        profile = resolved.profile
        if profile.endpoint is None or profile.embedding_profile is None:
            _stderr(
                "resolved embedding profile missing endpoint or embedding_profile",
                stderr,
            )
            return EXIT_ERROR
        try:
            active_embedder = HttpEmbedder(profile.endpoint, profile.embedding_profile)
        except Exception as exc:
            _stderr(f"failed to initialize embedder: {exc}", stderr)
            return EXIT_ERROR

    try:
        structural_store = StructuralPublicationStore(args.structural_state_dir)
        vector_store = VectorProjectionStore(args.state_dir)
        projection = vector_store.build(
            structural_store,
            workspace_id,
            repository_id,
            snapshot_id,
            active_embedder,
        )
    except Exception as exc:
        _stderr(str(exc), stderr)
        return EXIT_ERROR

    out = {
        "namespace": projection.namespace,
        "generation_id": projection.generation_id,
        "vector_count": projection.vector_count,
        "projection_sha256": projection.projection_sha256,
        "vectors_sha256": projection.vectors_sha256,
        "route": resolved.profile.name,
    }

    stream = sys.stdout if stdout is None else stdout
    if args.json:
        stream.write(json.dumps(out, sort_keys=True, ensure_ascii=False) + "\n")
    else:
        stream.write(
            f"{out['namespace']} generation_id={out['generation_id']} "
            f"vector_count={out['vector_count']} projection_sha256={out['projection_sha256']} "
            f"vectors_sha256={out['vectors_sha256']} route={out['route']}\n"
        )
    flush = getattr(stream, "flush", None)
    if callable(flush):
        flush()
    return EXIT_OK


__all__ = ["EXIT_ERROR", "EXIT_OK", "build_parser", "main"]

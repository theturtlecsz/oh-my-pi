"""Compile and reproduce a fleet context bundle from the command line (OMP-311).

``compile`` reads one workflow view from stdin, retrieves exact, structural,
procedural and semantic items, reranks the optional ones, compiles against
``omp tokens count``, and persists the bundle before it prints anything.
``reproduce`` replays a persisted bundle offline. A compile failure exits 2
with a reason on stderr and an empty stdout; a reproduction failure exits 3.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess  # nosec B404 - invokes git to resolve the workspace remote
import sys
from typing import Any, TextIO
from uuid import UUID

from omp_work.knowledge_publication import StructuralPublicationStore
from omp_work.knowledge_source import normalize_remote_url
from omp_work.v1.api_models import WorkflowView
from omp_work.v1.models import StrictModel
from pydantic import Field

from omp_knowledge.context.compiler import compile_bundle
from omp_knowledge.context.models import CompileRequest, ContextItem, Stage
from omp_knowledge.context.rerank import HttpReranker, OrderReranker, Reranker
from omp_knowledge.context.routes import ContextRouteStore
from omp_knowledge.context.semantic import semantic_items
from omp_knowledge.context.sources import (
    exact_items,
    identity_from_view,
    procedural_items,
    structural_items,
)
from omp_knowledge.context.store import ContextBundleStore
from omp_knowledge.context.tokens import OmpTokenCounter, ReproductionError
from omp_knowledge.engine.protocol import KnowledgeEngine
from omp_knowledge.errors import KnowledgeError, SnapshotNotPublishedError
from omp_knowledge.inference.embedding import Embedder, HttpEmbedder
from omp_knowledge.inference.routes import load_routes, resolve
from omp_knowledge.vectors.store import VectorProjectionStore

EXIT_OK = 0
EXIT_ERROR = 2
EXIT_REPRODUCTION = 3


class _CompileStdin(StrictModel):
    """The one JSON object ``compile`` reads from stdin."""

    stage: Stage
    attempt_id: str = Field(min_length=1)
    cwd: str = Field(min_length=1)
    workflow: WorkflowView


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m omp_knowledge.context")
    subcommands = parser.add_subparsers(dest="command", required=True)

    compile_parser = subcommands.add_parser("compile")
    compile_parser.add_argument("--state-dir", required=True)
    compile_parser.add_argument("--token-cmd", required=True)
    compile_parser.add_argument("--encoding", required=True)
    compile_parser.add_argument("--token-budget", required=True, type=int)
    compile_parser.add_argument("--structural-state-dir")
    compile_parser.add_argument("--snapshot", action="append", default=None)
    compile_parser.add_argument("--permit-repository", action="append", default=None)
    compile_parser.add_argument("--learning-db")
    compile_parser.add_argument("--engine", choices=("none", "cognee"), default="none")
    compile_parser.add_argument("--reranker-url")
    compile_parser.add_argument("--reranker-model")
    compile_parser.add_argument("--routes")
    compile_parser.add_argument("--vector-state-dir")
    compile_parser.add_argument("--structural-limit", type=int, default=40)
    compile_parser.add_argument("--semantic-limit", type=int, default=10)
    compile_parser.add_argument("--json", action="store_true")

    reproduce_parser = subcommands.add_parser("reproduce")
    reproduce_parser.add_argument("--state-dir", required=True)
    reproduce_parser.add_argument("--bundle-id", required=True)
    reproduce_parser.add_argument("--json", action="store_true")
    return parser


def _stderr(message: str) -> None:
    text = message.strip() or "error"
    print(text, file=sys.stderr)


def _emit(stdout: TextIO | None, payload: dict[str, Any]) -> None:
    line = json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n"
    stream = sys.stdout if stdout is None else stdout
    stream.write(line)
    flush = getattr(stream, "flush", None)
    if callable(flush):
        flush()


def _parse_token_cmd(raw: str) -> list[str]:
    parsed = json.loads(raw)
    if (
        not isinstance(parsed, list)
        or not parsed
        or not all(isinstance(part, str) and part for part in parsed)
    ):
        raise ValueError("--token-cmd must be a JSON array of non-empty strings")
    return parsed


def _parse_snapshot(value: str) -> tuple[UUID, UUID, str]:
    workspace_raw, separator, rest = value.partition(":")
    repository_raw, separator2, snapshot_id = rest.partition(":")
    if (
        not separator
        or not separator2
        or not workspace_raw
        or not repository_raw
        or not snapshot_id
    ):
        raise ValueError(f"--snapshot must be WS:REPO:SNAP, got {value!r}")
    return UUID(workspace_raw), UUID(repository_raw), snapshot_id


def _resolve_repository(cwd: str) -> str | None:
    """``git -C cwd remote get-url origin``, normalized, or None when git has no origin."""
    git = shutil.which("git")
    if git is None:
        return None
    try:
        result = subprocess.run(  # nosec B603 - absolute git path, no shell, fixed argv
            [git, "-C", cwd, "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return normalize_remote_url(result.stdout.strip())


def _read_compile_stdin(stdin: TextIO | None) -> _CompileStdin:
    stream = sys.stdin if stdin is None else stdin
    raw = stream.read()
    if not raw.strip():
        raise ValueError("compile expects a JSON object on stdin")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("compile stdin must be a JSON object")
    return _CompileStdin.model_validate(payload)


def _reranker(url: str | None, model: str | None) -> Reranker:
    if url:
        if not model:
            raise ValueError("--reranker-model is required with --reranker-url")
        return HttpReranker(url, model)
    return OrderReranker()


def _rerank_optional(
    reranker: Reranker, query: str, items: list[ContextItem]
) -> list[ContextItem]:
    """Score only non-mandatory items. Mandatory items stay untouched."""
    optional_at = [index for index, item in enumerate(items) if not item.mandatory]
    if not optional_at:
        return items
    reranked = list(reranker.rerank(query, [items[index] for index in optional_at]))
    if len(reranked) != len(optional_at):
        raise RuntimeError(
            f"reranker returned {len(reranked)} items for {len(optional_at)} optional items"
        )
    updated = list(items)
    for index, item in zip(optional_at, reranked, strict=True):
        updated[index] = item
    return updated


def _engine_for(args: argparse.Namespace, engine: KnowledgeEngine | None) -> KnowledgeEngine | None:
    if engine is not None:
        return engine
    if args.engine == "cognee":
        from omp_knowledge.config import load_config
        from omp_knowledge.engine.cognee_adapter import RealCogneeAdapter

        return RealCogneeAdapter(load_config())
    return None


def _compile(
    args: argparse.Namespace,
    *,
    stdin: TextIO | None,
    engine: KnowledgeEngine | None,
    embedder: Embedder | None = None,
    reranker: Reranker | None = None,
) -> dict[str, Any]:
    if args.routes is not None and args.reranker_url is not None:
        raise ValueError("--routes cannot be combined with --reranker-url")
    if args.vector_state_dir is not None:
        if args.routes is None:
            raise ValueError("--vector-state-dir requires --routes")
        if args.structural_state_dir is None:
            raise ValueError("--vector-state-dir requires --structural-state-dir")

    route_set = load_routes(args.routes) if args.routes is not None else None

    reranker_route: dict[str, Any]
    active_reranker: Reranker
    if reranker is not None:
        active_reranker = reranker
        if route_set is not None:
            resolved_reranker = resolve(route_set, "reranker")
            reranker_route = {
                "role": "reranker",
                "name": resolved_reranker.profile.name,
                "provider": resolved_reranker.profile.provider,
                "model": resolved_reranker.profile.model or "",
                "accelerator": resolved_reranker.profile.accelerator,
                "used": resolved_reranker.used,
                "reason": resolved_reranker.reason,
            }
        elif args.reranker_url is not None:
            reranker_route = {
                "role": "reranker",
                "name": "cli",
                "provider": "llama.cpp",
                "model": args.reranker_model or "",
                "accelerator": "unknown",
                "used": "primary",
                "reason": "",
            }
        else:
            reranker_route = {
                "role": "reranker",
                "name": "order",
                "provider": "order",
                "model": "",
                "accelerator": "cpu",
                "used": "primary",
                "reason": "",
            }
    elif route_set is not None:
        resolved_reranker = resolve(route_set, "reranker")
        if resolved_reranker.profile.provider == "llama.cpp":
            if not resolved_reranker.profile.endpoint or not resolved_reranker.profile.model:
                raise ValueError("llama.cpp reranker requires endpoint and model")
            active_reranker = HttpReranker(
                resolved_reranker.profile.endpoint,
                resolved_reranker.profile.model,
            )
        elif resolved_reranker.profile.provider == "order":
            active_reranker = OrderReranker()
        else:
            raise ValueError(
                f"unsupported reranker provider: {resolved_reranker.profile.provider}"
            )
        reranker_route = {
            "role": "reranker",
            "name": resolved_reranker.profile.name,
            "provider": resolved_reranker.profile.provider,
            "model": resolved_reranker.profile.model or "",
            "accelerator": resolved_reranker.profile.accelerator,
            "used": resolved_reranker.used,
            "reason": resolved_reranker.reason,
        }
    elif args.reranker_url is not None:
        active_reranker = _reranker(args.reranker_url, args.reranker_model)
        reranker_route = {
            "role": "reranker",
            "name": "cli",
            "provider": "llama.cpp",
            "model": args.reranker_model or "",
            "accelerator": "unknown",
            "used": "primary",
            "reason": "",
        }
    else:
        active_reranker = OrderReranker()
        reranker_route = {
            "role": "reranker",
            "name": "order",
            "provider": "order",
            "model": "",
            "accelerator": "cpu",
            "used": "primary",
            "reason": "",
        }

    body = _read_compile_stdin(stdin)
    view = body.workflow
    identity = identity_from_view(view, body.stage, body.attempt_id)
    selection = [_parse_snapshot(value) for value in (args.snapshot or [])]
    permitted: list[str] = list(args.permit_repository or [])

    items: list[ContextItem] = list(exact_items(view))
    if args.structural_state_dir is not None:
        structural = StructuralPublicationStore(args.structural_state_dir)
        items.extend(
            structural_items(
                structural,
                selection,
                permitted,
                args.structural_limit,
            )
        )
    elif selection:
        raise ValueError("--structural-state-dir is required when --snapshot is set")

    context: dict[str, str] = {}
    if view.item.project_id is not None:
        context["project_id"] = str(view.item.project_id)
    repository = _resolve_repository(body.cwd)
    if repository is not None:
        context["repository"] = repository
    if args.learning_db is not None:
        items.extend(procedural_items(args.learning_db, context))

    active_engine = _engine_for(args, engine)
    if active_engine is not None and selection:
        semantic = asyncio.run(
            semantic_items(
                active_engine,
                selection,
                permitted,
                view.item.revision.title,
                limit=args.semantic_limit,
            )
        )
        items.extend(semantic)

    embedding_route: dict[str, Any] | None = None
    if args.vector_state_dir is not None:
        if route_set is None:
            raise ValueError("--vector-state-dir requires --routes")
        resolved_embedding = resolve(route_set, "embedding")
        embedding_route = {
            "role": "embedding",
            "name": resolved_embedding.profile.name,
            "provider": resolved_embedding.profile.provider,
            "model": resolved_embedding.profile.model or "",
            "accelerator": resolved_embedding.profile.accelerator,
            "used": resolved_embedding.used,
            "reason": resolved_embedding.reason,
        }
        profile = resolved_embedding.profile
        if profile.endpoint is None or profile.embedding_profile is None:
            raise ValueError(
                "resolved embedding profile missing endpoint or embedding_profile"
            )

        active_embedder = embedder
        if active_embedder is None:
            active_embedder = HttpEmbedder(
                profile.endpoint, profile.embedding_profile
            )

        query_vector = active_embedder.embed_query(view.item.revision.title)
        gen_id = profile.embedding_profile.generation_id

        vector_store = VectorProjectionStore(args.vector_state_dir)
        structural_store = StructuralPublicationStore(args.structural_state_dir)

        permitted_repos = {str(repo_id) for repo_id in permitted}

        for ws_id, repo_id, snap_id in selection:
            repo_key = str(repo_id)
            if repo_key not in permitted_repos:
                items.append(
                    ContextItem(
                        section="semantic",
                        source="vector",
                        ref=repo_key,
                        text=f"repository {repo_key} not permitted",
                        status="denied",
                        detail="repo not permitted",
                    )
                )
                continue

            if not structural_store.is_published(
                workspace_id=ws_id,
                repository_id=repo_id,
                snapshot_id=snap_id,
            ):
                items.append(
                    ContextItem(
                        section="semantic",
                        source="vector",
                        ref=snap_id,
                        text=f"snapshot {snap_id} not published",
                        status="missing",
                        detail="snapshot_not_published",
                    )
                )
                continue

            try:
                hits = vector_store.search(
                    structural_store,
                    ws_id,
                    repo_id,
                    snap_id,
                    gen_id,
                    query_vector,
                    k=args.semantic_limit,
                )
            except SnapshotNotPublishedError:
                items.append(
                    ContextItem(
                        section="semantic",
                        source="vector",
                        ref=snap_id,
                        text=f"snapshot {snap_id} not published",
                        status="missing",
                        detail="snapshot_not_published",
                    )
                )
                continue
            except KnowledgeError as exc:
                if (
                    getattr(exc, "code", None) == "vector_generation_mismatch"
                    or "vector_generation_mismatch" in str(exc)
                ):
                    items.append(
                        ContextItem(
                            section="semantic",
                            source="vector",
                            ref=snap_id,
                            text=f"generation {gen_id} not built for snapshot {snap_id}",
                            status="missing",
                            detail="vector_generation_mismatch",
                        )
                    )
                    continue
                raise

            for hit in hits:
                items.append(
                    ContextItem(
                        section="semantic",
                        source="vector",
                        ref=hit.fact_id,
                        text=hit.text,
                        status="current",
                        detail=snap_id,
                        mandatory=False,
                        score=hit.score,
                    )
                )

    ranked = _rerank_optional(
        active_reranker,
        view.item.revision.title,
        items,
    )
    request = CompileRequest(
        identity=identity,
        token_budget=args.token_budget,
        encoding=args.encoding,
        items=tuple(ranked),
    )
    compiled = compile_bundle(
        request,
        OmpTokenCounter(_parse_token_cmd(args.token_cmd), args.encoding),
    )
    routes: list[dict[str, Any]] = []
    if embedding_route is not None:
        routes.append(embedding_route)
    routes.append(reranker_route)

    bundle_id = ContextBundleStore(args.state_dir).persist(request, compiled)
    ContextRouteStore(args.state_dir).persist(bundle_id, routes)
    return {
        "bundle_id": bundle_id,
        "bundle_sha256": compiled.sha256,
        "stage": identity.stage,
        "tokens": compiled.tokens,
        "token_budget": request.token_budget,
        "exclusions": [exclusion.model_dump(mode="json") for exclusion in compiled.exclusions],
        "text": compiled.text,
        "routes": routes,
    }


def _reproduce(args: argparse.Namespace) -> dict[str, Any]:
    store = ContextBundleStore(args.state_dir)
    text = store.reproduce(args.bundle_id)
    record = store.load(args.bundle_id)
    if record is None:
        raise ReproductionError(f"bundle not found: {args.bundle_id}")
    return {
        "bundle_id": record.bundle_id,
        "bundle_sha256": record.bundle_sha256,
        "text": text,
    }


def main(
    argv: list[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    engine: KnowledgeEngine | None = None,
    embedder: Embedder | None = None,
    reranker: Reranker | None = None,
) -> int:
    """Run one context command. Returns a process exit code and writes nothing on failure."""
    try:
        args = _build_parser().parse_args(argv)
        if args.command == "reproduce":
            try:
                payload = _reproduce(args)
            except ReproductionError as exc:
                _stderr(str(exc))
                return EXIT_REPRODUCTION
        elif args.command == "compile":
            payload = _compile(
                args,
                stdin=stdin,
                engine=engine,
                embedder=embedder,
                reranker=reranker,
            )
        else:
            raise RuntimeError(f"unknown command {args.command!r}")
        _emit(stdout, payload)
        return EXIT_OK
    except Exception as exc:
        _stderr(str(exc) or type(exc).__name__)
        return EXIT_ERROR


__all__ = ["EXIT_ERROR", "EXIT_OK", "EXIT_REPRODUCTION", "main"]

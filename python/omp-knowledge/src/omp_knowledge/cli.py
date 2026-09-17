from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from uuid import UUID, uuid4

from omp_work.knowledge_contracts import JobState, SnapshotRef
from omp_work.v1.canonical import canonical_json, sha256

from .config import KnowledgeConfig, load_config
from .engine.cognee_adapter import RealCogneeAdapter
from .native.consumer import NativeEventConsumer, NativeSourceUnavailableError
from .ownership import WriterOwnership
from .server import create_app
from .staging.manifest import validate_enola_artifacts
from .storage.db import (
    apply_migrations,
    execute_idempotent_job_async,
    get_db_connection,
    get_retained_artifact,
    is_snapshot_published,
    publish_staged_snapshot,
)


def main() -> None:
    parser = argparse.ArgumentParser(prog="omp-knowledge", description="Fleet Knowledge Engine CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # status
    subparsers.add_parser("status", help="Inspect knowledge engine and database status")

    # serve
    serve_p = subparsers.add_parser("serve", help="Run loopback API server")
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=18090)

    # migrate
    subparsers.add_parser("migrate", help="Apply database migrations")

    # consume-events
    consume_p = subparsers.add_parser("consume-events", help="Consume native domain events into knowledge observations")
    consume_p.add_argument("--workspace-id", required=True, type=UUID)
    consume_p.add_argument("--repository-id", required=True, type=UUID)
    consume_p.add_argument("--consumer-name", "--consumer", default="native_event_consumer")
    consume_p.add_argument("--batch-size", type=int, default=100)
    consume_p.add_argument("--actor-id", type=UUID, default=None)

    # checkpoint
    checkpoint_p = subparsers.add_parser("checkpoint", help="Inspect native event consumer checkpoint")
    checkpoint_p.add_argument("--workspace-id", required=True, type=UUID)
    checkpoint_p.add_argument("--repository-id", type=UUID, default=None)
    checkpoint_p.add_argument("--consumer-name", "--consumer", default="native_event_consumer")

    # query
    query_p = subparsers.add_parser("query", help="Query knowledge graph")
    query_p.add_argument("--workspace-id", required=True, type=UUID)
    query_p.add_argument("--repository-id", required=True, type=UUID)
    query_p.add_argument("--snapshot-id", required=True)
    query_p.add_argument("--query", required=True)
    query_p.add_argument("--limit", type=int, default=20)

    # lookup
    lookup_p = subparsers.add_parser("lookup", help="Exact lookup for fact")
    lookup_p.add_argument("--workspace-id", required=True, type=UUID)
    lookup_p.add_argument("--repository-id", required=True, type=UUID)
    lookup_p.add_argument("--snapshot-id", required=True)
    lookup_p.add_argument("--fact-id", required=True)

    # failures
    subparsers.add_parser("failures", help="List failed ingestion jobs")

    # retire
    retire_p = subparsers.add_parser("retire", help="Retire snapshot from graph")
    retire_p.add_argument("--workspace-id", required=True, type=UUID)
    retire_p.add_argument("--repository-id", required=True, type=UUID)
    retire_p.add_argument("--snapshot-id", required=True)

    # rebuild
    rebuild_p = subparsers.add_parser("rebuild", help="Rebuild snapshot from retained artifacts")
    rebuild_p.add_argument("--workspace-id", required=True, type=UUID)
    rebuild_p.add_argument("--repository-id", required=True, type=UUID)
    rebuild_p.add_argument("--snapshot-id", required=True)

    args = parser.parse_args()
    config = load_config()

    if args.command == "serve":
        import uvicorn
        app = create_app(config)
        uvicorn.run(app, host=args.host, port=args.port)
    elif args.command == "migrate":
        conn = get_db_connection(config)
        apply_migrations(conn)
        print("Migrations applied successfully.")
    elif args.command == "consume-events":
        conn = get_db_connection(config)
        writer = WriterOwnership(config)
        if not writer.acquire():
            print("Error: Could not acquire writer ownership", file=sys.stderr)
            sys.exit(1)
        writer.recover_interrupted_jobs()

        if not args.actor_id:
            print("Error: --actor-id is required for consume-events", file=sys.stderr)
            sys.exit(1)
        actor_id = args.actor_id
        consumer = NativeEventConsumer(
            config,
            workspace_id=args.workspace_id,
            actor_id=actor_id,
            repository_id=args.repository_id,
            consumer_name=args.consumer_name,
            batch_size=args.batch_size,
        )
        op_id = uuid4()
        req_hash = sha256({"op": "consume-events", "workspace_id": str(args.workspace_id), "op_id": str(op_id)})

        async def do_consume() -> tuple[JobState, dict[str, Any], list[str]]:
            res = consumer.consume_batch(conn)
            return (JobState.COMPLETED, res, ["native_events_consumed"])

        try:
            job_res = asyncio.run(execute_idempotent_job_async(
                conn,
                writer=writer,
                operation_id=op_id,
                workspace_id=args.workspace_id,
                repository_id=args.repository_id,
                snapshot_id=None,
                request_hash=req_hash,
                action=do_consume,
                skip_snapshot_owner_cas=True,
                skip_ingest_snapshot_owner_cas=True,
            ))
            print(json.dumps(job_res, indent=2, default=str))
        except NativeSourceUnavailableError as exc:
            print(f"Error: Native source unavailable: {exc}", file=sys.stderr)
            sys.exit(1)
        finally:
            conn.close()
    elif args.command == "checkpoint":
        conn = get_db_connection(config)
        try:
            suffix = f":{args.workspace_id}"
            checkpoint_key = (
                args.consumer_name
                if args.consumer_name.endswith(suffix)
                else f"{args.consumer_name}:{args.workspace_id}"
            )
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT consumer, workspace_id, after_sequence, pending_gaps, applied_event_ids, pending_gap_metadata, updated_at
                    FROM omp_knowledge.native_event_checkpoints
                    WHERE consumer = %s
                    """,
                    (checkpoint_key,),
                )
                row = cur.fetchone()
                if not row:
                    out = {
                        "consumer": checkpoint_key,
                        "workspace_id": str(args.workspace_id),
                        "after_sequence": 0,
                        "pending_gaps": [],
                        "applied_event_ids": [],
                        "pending_gap_metadata": {},
                        "updated_at": None,
                    }
                else:
                    raw_gaps = row["pending_gaps"]
                    gaps = json.loads(raw_gaps) if isinstance(raw_gaps, str) else (raw_gaps or [])
                    raw_applied = row["applied_event_ids"]
                    applied = json.loads(raw_applied) if isinstance(raw_applied, str) else (raw_applied or [])
                    raw_meta = row["pending_gap_metadata"]
                    meta = json.loads(raw_meta) if isinstance(raw_meta, str) else (raw_meta or {})
                    updated_at = row["updated_at"]
                    out = {
                        "consumer": row["consumer"],
                        "workspace_id": str(row["workspace_id"] or args.workspace_id),
                        "after_sequence": int(row["after_sequence"]),
                        "pending_gaps": gaps,
                        "applied_event_ids": applied,
                        "pending_gap_metadata": meta,
                        "updated_at": updated_at.isoformat() if hasattr(updated_at, "isoformat") else str(updated_at),
                    }
                print(json.dumps(out, indent=2, default=str))
        finally:
            conn.close()
    elif args.command == "status":
        adapter = RealCogneeAdapter(config)
        status = asyncio.run(adapter.status())
        print(json.dumps(status.model_dump(mode="json"), indent=2))
    elif args.command == "query":
        conn = get_db_connection(config)
        if not is_snapshot_published(conn, workspace_id=args.workspace_id, repository_id=args.repository_id, snapshot_id=args.snapshot_id):
            print(f"Error: Snapshot {args.snapshot_id} is not published.", file=sys.stderr)
            sys.exit(1)
        adapter = RealCogneeAdapter(config)
        res = asyncio.run(adapter.query(
            workspace_id=args.workspace_id,
            repository_id=args.repository_id,
            snapshot_id=args.snapshot_id,
            query_text=args.query,
            limit=args.limit,
        ))
        print(json.dumps(res.model_dump(mode="json"), indent=2))
    elif args.command == "lookup":
        conn = get_db_connection(config)
        if not is_snapshot_published(conn, workspace_id=args.workspace_id, repository_id=args.repository_id, snapshot_id=args.snapshot_id):
            print(f"Error: Snapshot {args.snapshot_id} is not published.", file=sys.stderr)
            sys.exit(1)
        adapter = RealCogneeAdapter(config)
        res = asyncio.run(adapter.lookup(
            workspace_id=args.workspace_id,
            repository_id=args.repository_id,
            snapshot_id=args.snapshot_id,
            fact_id=args.fact_id,
        ))
        print(json.dumps(res.model_dump(mode="json"), indent=2))
    elif args.command == "failures":
        conn = get_db_connection(config)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT operation_id, workspace_id, repository_id, snapshot_id,
                       state, request_sha256, error, diagnostics, created_at
                FROM omp_knowledge.ingestion_jobs
                WHERE state = 'failed'
                ORDER BY created_at DESC LIMIT 50
                """
            )
            rows = cur.fetchall()
            print(json.dumps([dict(r) for r in rows], indent=2, default=str))
    elif args.command == "retire":
        print(
            "Error: Direct CLI snapshot retirement is unsupported. "
            "Mutations must route through the HTTP API (/v1/retire) with durable operation tracking and publication gating.",
            file=sys.stderr,
        )
        sys.exit(1)
    elif args.command == "rebuild":
        print(
            "Error: Direct CLI snapshot rebuild is unsupported. "
            "Mutations must route through the HTTP API (/v1/rebuild) with durable operation tracking, preflight verification, and publication hash gating.",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()

"""File-backed parallel streams admit loop (Chris lock in_flight_max=10).

Implements heartbeat, SKIP LOCKED-style claim, path leases, budget reserve,
provider partitions. Store: ACTIVE/parallel-runtime/parallel.db
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ACTIVE = Path(
    os.environ.get("OMP_ECONOMY_ACTIVE_DIR") or (Path.home() / ".codex/workflows/economy/ACTIVE")
)
RUNTIME = ACTIVE / "parallel-runtime"
DB = RUNTIME / "parallel.db"
POLICY = ACTIVE / "PARALLEL-STREAMS.json"


def _connect() -> sqlite3.Connection:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    with _connect() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS heartbeat (
              id INTEGER PRIMARY KEY CHECK (id = 1),
              last_tick_at REAL NOT NULL,
              in_flight INTEGER NOT NULL,
              admittable_backlog INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
              job_id TEXT PRIMARY KEY,
              status TEXT NOT NULL,
              mission_id TEXT,
              provider_partition TEXT NOT NULL,
              path_lease TEXT NOT NULL,
              expected_max INTEGER NOT NULL,
              budget_reservation_id TEXT,
              depends_on TEXT,
              packet_path TEXT,
              blocker TEXT,
              created_at REAL NOT NULL,
              updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS leases (
              path_glob TEXT PRIMARY KEY,
              job_id TEXT NOT NULL,
              held_until REAL
            );
            CREATE TABLE IF NOT EXISTS reservations (
              reservation_id TEXT PRIMARY KEY,
              job_id TEXT NOT NULL,
              tokens INTEGER NOT NULL,
              provider_partition TEXT NOT NULL,
              expires_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
            """
        )


def load_policy() -> dict[str, Any]:
    if POLICY.is_file():
        return json.loads(POLICY.read_text())
    return {
        "in_flight_max": 10,
        "provider_partitions": {
            "gemini_flash": 6,
            "ollama_cloud": 4,
            "anthropic_fable": 2,
            "kimi": 2,
            "chatgpt_astra": 0,
        },
        "default_expected_max_tokens": 40000,
    }


def write_heartbeat(*, in_flight: int, admittable_backlog: int) -> None:
    init_db()
    now = time.time()
    with _connect() as c:
        c.execute(
            "INSERT INTO heartbeat(id,last_tick_at,in_flight,admittable_backlog) VALUES (1,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET last_tick_at=excluded.last_tick_at, "
            "in_flight=excluded.in_flight, admittable_backlog=excluded.admittable_backlog",
            (now, in_flight, admittable_backlog),
        )


def enqueue(
    *,
    job_id: str,
    provider_partition: str,
    path_lease: str,
    expected_max: int,
    mission_id: str | None = None,
    packet_path: str | None = None,
    depends_on: list[str] | None = None,
) -> dict[str, Any]:
    init_db()
    pol = load_policy()
    parts = pol.get("provider_partitions") or {}
    if int(parts.get(provider_partition, -1)) == 0 and provider_partition == "chatgpt_astra":
        return {"ok": False, "error": "partition_closed", "provider_partition": provider_partition}
    now = time.time()
    with _connect() as c:
        existing = c.execute("SELECT status FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if existing:
            return {"ok": True, "job_id": job_id, "deduped": True, "status": existing["status"]}
        c.execute(
            "INSERT INTO jobs(job_id,status,mission_id,provider_partition,path_lease,expected_max,"
            "depends_on,packet_path,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                job_id,
                "backlog",
                mission_id,
                provider_partition,
                path_lease,
                int(expected_max),
                json.dumps(depends_on or []),
                packet_path,
                now,
                now,
            ),
        )
    return {"ok": True, "job_id": job_id, "deduped": False, "status": "backlog"}


def _slot_count(c: sqlite3.Connection) -> int:
    row = c.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE status IN ('admitted','in_flight','returned','checking')"
    ).fetchone()
    return int(row["n"])


def _partition_count(c: sqlite3.Connection, partition: str) -> int:
    row = c.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE provider_partition=? AND status IN "
        "('admitted','in_flight','returned','checking')",
        (partition,),
    ).fetchone()
    return int(row["n"])


def _lease_held(c: sqlite3.Connection, path_glob: str) -> str | None:
    row = c.execute("SELECT job_id FROM leases WHERE path_glob=?", (path_glob,)).fetchone()
    return row["job_id"] if row else None


def claim_one() -> dict[str, Any] | None:
    """Atomically claim one backlog job if slot/lease/partition/reserve allow."""
    init_db()
    pol = load_policy()
    max_n = int(pol.get("in_flight_max") or 10)
    parts = pol.get("provider_partitions") or {}
    now = time.time()
    with _connect() as c:
        c.execute("BEGIN IMMEDIATE")
        if _slot_count(c) >= max_n:
            c.execute("COMMIT")
            return None
        rows = c.execute(
            "SELECT * FROM jobs WHERE status='backlog' ORDER BY created_at ASC"
        ).fetchall()
        for row in rows:
            job_id = row["job_id"]
            partition = row["provider_partition"]
            path_glob = row["path_lease"]
            expected = int(row["expected_max"])
            lim = int(parts.get(partition, 0))
            if lim <= 0:
                c.execute(
                    "UPDATE jobs SET blocker=?, updated_at=? WHERE job_id=?",
                    ("partition", now, job_id),
                )
                continue
            if _partition_count(c, partition) >= lim:
                c.execute(
                    "UPDATE jobs SET blocker=?, updated_at=? WHERE job_id=?",
                    ("partition_full", now, job_id),
                )
                continue
            holder = _lease_held(c, path_glob)
            if holder and holder != job_id:
                c.execute(
                    "UPDATE jobs SET blocker=?, updated_at=? WHERE job_id=?",
                    ("lease", now, job_id),
                )
                continue
            # depends_on: all must be sealed
            deps = json.loads(row["depends_on"] or "[]")
            blocked = False
            for d in deps:
                st = c.execute("SELECT status FROM jobs WHERE job_id=?", (d,)).fetchone()
                if not st or st["status"] != "sealed":
                    c.execute(
                        "UPDATE jobs SET blocker=?, updated_at=? WHERE job_id=?",
                        (f"dep:{d}", now, job_id),
                    )
                    blocked = True
                    break
            if blocked:
                continue
            rid = f"rsv-{uuid.uuid4().hex[:10]}"
            c.execute(
                "INSERT INTO leases(path_glob, job_id, held_until) VALUES (?,?,?)",
                (path_glob, job_id, now + 3600),
            )
            c.execute(
                "INSERT INTO reservations(reservation_id, job_id, tokens, provider_partition, expires_at) "
                "VALUES (?,?,?,?,?)",
                (rid, job_id, expected, partition, now + 3600),
            )
            c.execute(
                "UPDATE jobs SET status='admitted', budget_reservation_id=?, blocker=NULL, updated_at=? "
                "WHERE job_id=?",
                (rid, now, job_id),
            )
            c.execute("COMMIT")
            return {
                "ok": True,
                "job_id": job_id,
                "status": "admitted",
                "budget_reservation_id": rid,
                "provider_partition": partition,
                "path_lease": path_glob,
                "packet_path": row["packet_path"],
                "expected_max": expected,
            }
        c.execute("COMMIT")
    return None


def mark_in_flight(job_id: str) -> None:
    """Admit path: backlog or admitted → in_flight (was admitted-only; backlog no-op caused relaunch thrash)."""
    with _connect() as c:
        c.execute(
            "UPDATE jobs SET status='in_flight', updated_at=? "
            "WHERE job_id=? AND status IN ('backlog', 'admitted')",
            (time.time(), job_id),
        )


def release_at_checking_complete(job_id: str, *, final_status: str = "sealed", closeout: dict[str, Any] | None = None) -> bool:
    """Slot release at checking_complete: free lease + reservation; set sealed/failed."""
    terminal = {"sealed", "failed", "cancelled", "empty_soft", "checkpoint_pending"}
    if final_status not in terminal:
        raise ValueError("final_status must be terminal")
    now = time.time()
    with _connect() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT status,blocker FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None or (row["status"] in terminal and row["status"] != final_status):
            return False
        c.execute("DELETE FROM leases WHERE job_id=?", (job_id,))
        c.execute("DELETE FROM reservations WHERE job_id=?", (job_id,))
        # Commit the downstream completion obligation with the terminal state.
        # A repeated release must not erase an unfinished obligation.
        blocker = row["blocker"] if str(row["blocker"] or "").startswith("closeout_pending:") else None
        if closeout is not None and blocker is None and row["status"] not in terminal:
            blocker = "closeout_pending:" + json.dumps({"classification": closeout, "ledger_done": False}, sort_keys=True)
        c.execute("UPDATE jobs SET status=?, blocker=?, updated_at=? WHERE job_id=?",
                  (final_status, blocker, now, job_id))
    return True


def status() -> dict[str, Any]:
    init_db()
    pol = load_policy()
    with _connect() as c:
        hb = c.execute("SELECT * FROM heartbeat WHERE id=1").fetchone()
        by_status: dict[str, int] = {}
        for row in c.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"):
            by_status[row["status"]] = row["n"]
        slots = _slot_count(c)
        jobs = [
            dict(r)
            for r in c.execute(
                "SELECT job_id,status,provider_partition,path_lease,blocker,mission_id FROM jobs "
                "ORDER BY created_at"
            )
        ]
    return {
        "ok": True,
        "in_flight_max": pol.get("in_flight_max"),
        "slot_count": slots,
        "by_status": by_status,
        "heartbeat": dict(hb) if hb else None,
        "provider_partitions": pol.get("provider_partitions"),
        "jobs": jobs,
        "live_slots_open": bool(pol.get("live_slots_open", True)),
    }


def tick() -> dict[str, Any]:
    """One admit-loop tick: claim until full or backlog empty; refresh heartbeat."""
    init_db()
    pol = load_policy()
    if pol.get("live_slots_open") is False:
        write_heartbeat(in_flight=0, admittable_backlog=0)
        return {"ok": False, "error": "live_slots_not_open", "admitted": []}
    admitted: list[dict[str, Any]] = []
    while True:
        one = claim_one()
        if not one:
            break
        admitted.append(one)
    st = status()
    write_heartbeat(
        in_flight=int(st.get("slot_count") or 0),
        admittable_backlog=int((st.get("by_status") or {}).get("backlog") or 0),
    )
    return {"ok": True, "admitted": admitted, "status": status()}

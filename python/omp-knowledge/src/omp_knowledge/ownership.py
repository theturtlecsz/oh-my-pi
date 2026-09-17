from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, AsyncIterator

import psycopg

from .storage.db import recover_interrupted_jobs as db_recover_interrupted_jobs

if TYPE_CHECKING:
    from omp_knowledge.config import KnowledgeConfig

OMP_KNOWLEDGE_ADVISORY_KEY: int = 0x4F4D505F4B4E4F57


class WriterOwnershipLost(RuntimeError):
    """Raised when writer ownership has been lost or was not established."""


class WriterOwnership:
    """Manages single-writer lifetime ownership for knowledge engine operations."""

    def __init__(self, config: KnowledgeConfig, token: str | None = None) -> None:
        self._config = config
        self.token: str = token or str(uuid.uuid4())
        self._conn: psycopg.Connection | None = None
        self._lock_fd: int | None = None
        self._lock_path: Path = config.state_dir / ".writer.lock"
        self._engine_lock: asyncio.Lock = asyncio.Lock()
        self._stack: contextlib.ExitStack | None = None
        self._attempted = self._owned = self._dead = self._released = False
        self._backend_pid: int | None = None

    @property
    def backend_pid(self) -> int | None:
        return self._backend_pid

    @property
    def attempted(self) -> bool:
        return self._attempted

    @property
    def owned(self) -> bool:
        return self._owned

    @property
    def dead(self) -> bool:
        return self._dead

    @property
    def released(self) -> bool:
        return self._released

    def acquire(self) -> bool:
        if self._attempted:
            if self._owned and not self._dead and not self._released:
                self.ensure_alive()
                return True
            raise RuntimeError("Duplicate acquire on same WriterOwnership instance is forbidden; explicit single lifecycle")
        self._attempted = True

        with contextlib.ExitStack() as stack:
            conn = psycopg.connect(
                self._config.pg_connection_string(), autocommit=True,
                keepalives=1, keepalives_idle=5, keepalives_interval=2, keepalives_count=3,
            )
            stack.callback(conn.close)
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", (OMP_KNOWLEDGE_ADVISORY_KEY,))
                row = cur.fetchone()
                if not row or not row[0]:
                    return False

            self._backend_pid = conn.info.backend_pid
            self._config.state_dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self._lock_path), os.O_RDWR | os.O_CREAT, 0o600)
            stack.callback(os.close, fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError) as exc:
                if isinstance(exc, BlockingIOError) or exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
                    return False
                raise

            self._conn, self._lock_fd = conn, fd
            self._stack = stack.pop_all()
            self._owned, self._dead = True, False
            return True

    def ensure_alive(self) -> None:
        if self._dead or not self._owned or self._released or self._conn is None or self._conn.closed:
            self._dead = True
            raise WriterOwnershipLost("Writer ownership is not held or has been lost")
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT 1")
        except Exception as exc:
            self._dead = True
            raise WriterOwnershipLost(f"Writer ownership connection failure: {exc}") from exc

    def alive(self) -> bool:
        try:
            self.ensure_alive()
            return True
        except WriterOwnershipLost:
            return False

    def is_acquired(self) -> bool:
        return self._owned and not self._dead and not self._released

    @contextlib.asynccontextmanager
    async def mutation(self) -> AsyncIterator[None]:
        async with self._engine_lock:
            self.ensure_alive()
            yield

    async def release(self) -> None:
        async with self._engine_lock:
            if self._released:
                return
            self._released = True
            if self._stack is not None:
                self._stack.close()
                self._stack = None
            self._conn, self._lock_fd = None, None

    def recover_interrupted_jobs(self) -> int:
        self.ensure_alive()
        assert self._conn is not None
        return db_recover_interrupted_jobs(self._conn, current_token=self.token)

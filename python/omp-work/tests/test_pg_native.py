"""Native fixture startup must not depend on Unix socket pathname limits."""

import os
import socket
from pathlib import Path

import psycopg
import pytest

from pg_native import native_postgres


@pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
def test_long_space_containing_root_serves_tcp_and_stops(tmp_path: Path) -> None:
    root = tmp_path / ("long-postgres-fixture-" * 6) / "path with spaces"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    connection = {
        "host": "127.0.0.1",
        "port": port,
        "user": "postgres",
        "dbname": "postgres",
        "connect_timeout": 1,
    }
    with native_postgres(root, port):
        with psycopg.connect(**connection) as client:
            assert client.execute(
                "SELECT host(inet_server_addr()), inet_server_port()"
            ).fetchone() == ("127.0.0.1", port)
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(**connection)

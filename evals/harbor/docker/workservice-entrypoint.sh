#!/usr/bin/env bash
# Start the real WorkService for a Harbor fixture.
#
# This is NOT a stand-in. It starts the bundled PostgreSQL 18, provisions the
# ledger credentials explicitly (generated files under XDG_CONFIG_HOME, mode
# 0600 in a 0700 directory), runs the versioned bootstrap and migrations, and
# then serves the contract app produced by
# omp_work.v1.server.create_app — the same code path `python -m omp_work serve`
# runs. No HTTP route is defined here, no health or readiness is patched, and
# no token is fabricated: if PostgreSQL or the contract bundle cannot start, the
# container exits non-zero and the trial fails.
set -euo pipefail

CONFIG_DIR="${XDG_CONFIG_HOME:-/root/.config}/omp/work-ledger"
CREDENTIALS_DIR="$CONFIG_DIR/credentials"
CAPABILITIES_DIR="$CONFIG_DIR/capabilities"
DATA_DIR="${OMP_PG_DATA_DIR:-/var/lib/postgresql/data}"
HOST="${OMP_SERVICE_BIND_HOST:-127.0.0.1}"
PORT="${OMP_SERVICE_BIND_PORT:-8080}"
PGPORT="${OMP_WORK_POSTGRES_PORT:-54321}"
PGUSER="${OMP_PG_SUPERUSER:-postgres}"
PGLOG="${OMP_PG_LOG:-/tmp/postgres.log}"

mkdir -p "$CONFIG_DIR" "$CREDENTIALS_DIR" "$CAPABILITIES_DIR" "$DATA_DIR" "$(dirname "$PGLOG")"
chmod 0700 "$CONFIG_DIR" "$CREDENTIALS_DIR" "$CAPABILITIES_DIR"
chown -R "$PGUSER:$PGUSER" "$DATA_DIR"
chown "$PGUSER:$PGUSER" "$PGLOG" 2>/dev/null || true

as_pg() {
	su "$PGUSER" -s /bin/bash -c "$1"
}

if [ ! -f "$DATA_DIR/PG_VERSION" ]; then
	echo "workservice: initializing PostgreSQL 18 data directory at $DATA_DIR" >&2
	rm -rf "${DATA_DIR:?}"/*
	as_pg "initdb -D '$DATA_DIR' -A trust"
fi

as_pg "pg_ctl -D '$DATA_DIR' -l '$PGLOG' -o '-p $PGPORT -k /tmp -c listen_addresses=127.0.0.1' -w start"

stop_postgres() {
	as_pg "pg_ctl -D '$DATA_DIR' -m fast stop" >/dev/null 2>&1 || true
}
trap stop_postgres EXIT

ids="$(python -m omp_work ops credentials init)"
WORKSPACE_ID="$(python -c 'import json, sys; print(json.load(sys.stdin)["workspace_id"])' <<<"$ids")"
OWNER_ID="$(python -c 'import json, sys; print(json.load(sys.stdin)["owner_id"])' <<<"$ids")"

python -m omp_work ops bootstrap
python -m omp_work ops capabilities init \
	--workspace-id "$WORKSPACE_ID" \
	--owner-id "$OWNER_ID" \
	--base-url "http://$HOST:$PORT" >/dev/null

echo "workservice: serving omp_work.v1.server.create_app on $HOST:$PORT" >&2
exec python -m omp_work serve --host "$HOST" --port "$PORT" --capabilities-dir "$CAPABILITIES_DIR"

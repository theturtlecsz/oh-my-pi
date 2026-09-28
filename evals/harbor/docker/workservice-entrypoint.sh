#!/usr/bin/env bash
# Start the real WorkService for a Harbor fixture.
#
# Installs the credential files mounted at /run/omp/supplied (written on the
# host by scripts/build-images.sh before the trial) into the service config
# directory, starts PostgreSQL 18 with password authentication, bootstraps,
# applies the fixture ledger seed named by OMP_HARBOR_LEDGER_SEED when that
# variable is set, and execs `python -m omp_work serve`. This script defines
# no HTTP route and creates no token, capability, or owner file. If the
# supplied files, the ledger seed, PostgreSQL, or the service cannot start,
# the container exits non-zero.
set -euo pipefail

SUPPLIED="${OMP_SUPPLIED_DIR:-/run/omp/supplied}"
CONFIG_DIR="${XDG_CONFIG_HOME:-/root/.config}/omp/work-ledger"
CREDENTIALS_DIR="$CONFIG_DIR/credentials"
CAPABILITIES_DIR="$CONFIG_DIR/capabilities"
DATA_DIR="${OMP_PG_DATA_DIR:-/var/lib/postgresql/data}"
HOST="${OMP_SERVICE_BIND_HOST:-127.0.0.1}"
PORT="${OMP_SERVICE_BIND_PORT:-8080}"
PGPORT="${OMP_WORK_POSTGRES_PORT:-54321}"
PGUSER="${OMP_PG_SUPERUSER:-postgres}"
PGLOG="${OMP_PG_LOG:-/tmp/postgres.log}"
PWFILE=""

credential_names=(
	postgres
	omp_work_migrator
	omp_work_app
	omp_work_importer
	omp_work_readonly
	omp_work_backup
	gpg-passphrase
	operator-actor-id
	workspace-id
)

as_pg() {
	su "$PGUSER" -s /bin/bash -c "$1"
}

cleanup() {
	if [ -n "$PWFILE" ]; then
		rm -f "$PWFILE"
	fi
	if [ -f "$DATA_DIR/PG_VERSION" ]; then
		as_pg "pg_ctl -D '$DATA_DIR' -m fast stop" >/dev/null 2>&1 || true
	fi
}
trap cleanup EXIT

install_secret() {
	local src="$1" dest="$2"
	if [ ! -s "$src" ]; then
		echo "workservice: missing supplied credential $src" >&2
		echo "workservice: run: bash evals/harbor/scripts/build-images.sh" >&2
		exit 1
	fi
	install -d -m 0700 "$(dirname "$dest")"
	install -m 0600 "$src" "$dest"
	chmod 0700 "$(dirname "$dest")"
}

mkdir -p "$DATA_DIR" "$(dirname "$PGLOG")"
chmod 0700 "$DATA_DIR"
chown -R "$PGUSER:$PGUSER" "$DATA_DIR"
touch "$PGLOG"
chown "$PGUSER:$PGUSER" "$PGLOG"
chmod 0600 "$PGLOG"

for name in "${credential_names[@]}"; do
	install_secret "$SUPPLIED/credentials/$name" "$CREDENTIALS_DIR/$name"
done
install_secret "$SUPPLIED/capabilities/owner.json" "$CAPABILITIES_DIR/owner.json"
chmod 0700 "$CONFIG_DIR" "$CREDENTIALS_DIR" "$CAPABILITIES_DIR"

python - "$CAPABILITIES_DIR/owner.json" "$CREDENTIALS_DIR/workspace-id" <<'PY'
import json
import sys
from pathlib import Path

owner = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
workspace = Path(sys.argv[2]).read_text(encoding="utf-8").strip()
token = owner.get("token")
if not isinstance(token, str) or not token or any(ch.isspace() for ch in token):
    raise SystemExit("supplied owner.json token must be a single secret value")
workspaces = owner.get("workspaces")
if not isinstance(workspaces, list) or workspace not in workspaces:
    raise SystemExit("supplied owner.json does not include the workspace-id credential")
PY

write_hba() {
	local path="$DATA_DIR/pg_hba.conf"
	cat >"$path" <<'EOF'
local all all scram-sha-256
host all all 127.0.0.1/32 scram-sha-256
host all all ::1/128 scram-sha-256
host all all 0.0.0.0/0 reject
host all all ::/0 reject
EOF
	chown "$PGUSER:$PGUSER" "$path"
	chmod 0600 "$path"
}

if [ ! -f "$DATA_DIR/PG_VERSION" ]; then
	echo "workservice: initializing PostgreSQL 18 at $DATA_DIR" >&2
	rm -rf "${DATA_DIR:?}"/*
	PWFILE="$(mktemp)"
	install -m 0600 -o "$PGUSER" -g "$PGUSER" "$CREDENTIALS_DIR/postgres" "$PWFILE"
	as_pg "initdb -D '$DATA_DIR' -U '$PGUSER' --auth-local=scram-sha-256 --auth-host=scram-sha-256 -E UTF8 --data-checksums --pwfile='$PWFILE'"
	rm -f "$PWFILE"
	PWFILE=""
	chown -R "$PGUSER:$PGUSER" "$DATA_DIR"
	chmod 0700 "$DATA_DIR"
fi

write_hba

as_pg "pg_ctl -D '$DATA_DIR' -l '$PGLOG' -o '-p $PGPORT -k /tmp -c listen_addresses=127.0.0.1 -c password_encryption=scram-sha-256' -w start"

echo "workservice: serving omp_work.v1.server.create_app on $HOST:$PORT" >&2
python -m omp_work ops bootstrap
if [ -n "${OMP_HARBOR_LEDGER_SEED:-}" ]; then
	if [ ! -s "$OMP_HARBOR_LEDGER_SEED" ]; then
		echo "workservice: ledger seed is missing or empty: $OMP_HARBOR_LEDGER_SEED" >&2
		exit 1
	fi
	python /opt/harbor/ledger_seed.py "$OMP_HARBOR_LEDGER_SEED"
fi
trap - EXIT
exec python -m omp_work serve --host "$HOST" --port "$PORT" --capabilities-dir "$CAPABILITIES_DIR"

#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
xdg_config=${XDG_CONFIG_HOME:-$HOME/.config}
xdg_state=${XDG_STATE_HOME:-$HOME/.local/state}
xdg_data=${XDG_DATA_HOME:-$HOME/.local/share}
config=$xdg_config/omp/work-ledger
state=$xdg_state/omp/work-ledger
share=$xdg_data/omp/work-ledger
pgdata=$state/postgres
pgport=${OMP_WORK_POSTGRES_PORT:-54321}
httpport=${OMP_WORK_HTTP_PORT:-54322}
while [ "$#" -gt 0 ]; do
  case "$1" in
    --postgres-data) pgdata=${2:?--postgres-data needs a path}; shift 2 ;;
    --postgres-port) pgport=${2:?--postgres-port needs a port}; shift 2 ;;
    --http-port) httpport=${2:?--http-port needs a port}; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
case "$pgdata" in /*) ;; *) echo '--postgres-data must be absolute' >&2; exit 2 ;; esac
case "$pgport:$httpport" in *[!0-9:]*) echo 'ports must be numeric' >&2; exit 2 ;; esac
for path in "$config" "$pgdata" "$state" "$share/wal"; do install -d -m 700 "$path"; done
for bin in initdb postgres pg_ctl; do command -v "$bin" >/dev/null || { echo "missing native PostgreSQL binary: $bin" >&2; exit 1; }
done
[ "$(postgres --version | grep -oE '[0-9]+' | head -1)" = "18" ] || { echo 'native PostgreSQL 18 required' >&2; exit 1; }
python3 - "$root" "$config" "$state" "$share" "$pgdata" "$pgport" "$httpport" "$xdg_config" "$xdg_state" "$xdg_data" <<'PY'
from pathlib import Path
import sys
root, config, state, share, pgdata, pgport, httpport, xdg_config, xdg_state, xdg_data = sys.argv[1:]
environment = f'''Environment="XDG_CONFIG_HOME={xdg_config}"
Environment="XDG_STATE_HOME={xdg_state}"
Environment="XDG_DATA_HOME={xdg_data}"
Environment="OMP_WORK_POSTGRES_PORT={pgport}"
'''
units = {
    "omp-work-postgres.service": f'''[Unit]
Description=OMP Work Ledger PostgreSQL

[Service]
Type=simple
ExecStartPre=/bin/sh -c 'test -s {pgdata}/PG_VERSION || /usr/bin/initdb -D {pgdata} -U postgres --auth-local=trust --auth-host=scram-sha-256 -E UTF8 --data-checksums --pwfile={config}/credentials/postgres'
ExecStart=/usr/bin/postgres -D {pgdata} -p {pgport} -c listen_addresses=127.0.0.1 -k {state} -c password_encryption=scram-sha-256 -c wal_level=replica -c archive_mode=on -c archive_command='test ! -f {share}/wal/%%f && cp %%p {share}/wal/%%f || cmp %%p {share}/wal/%%f'
ExecStop=/usr/bin/pg_ctl -D {pgdata} -m fast -w stop
Restart=on-failure

[Install]
WantedBy=default.target
''',
    "omp-work-service.service": f'''[Unit]
Description=OMP Work Ledger WorkService (loopback)
Requires=omp-work-postgres.service
After=omp-work-postgres.service

[Service]
Type=simple
{environment}
ExecStartPre={root}/python/omp-work/.venv/bin/python -m omp_work ops migrate
ExecStart={root}/python/omp-work/.venv/bin/python -m omp_work serve --port {httpport} --capabilities-dir {config}/capabilities
Restart=on-failure

[Install]
WantedBy=default.target
''',
    "omp-work-backup.service": f'''[Service]
Type=oneshot
{environment}
ExecStart={root}/python/omp-work/.venv/bin/omp-work ops backup create
''',
    "omp-work-wal.service": f'''[Service]
Type=oneshot
{environment}
ExecStart={root}/python/omp-work/.venv/bin/omp-work ops backup wal
''',
    "omp-work-restore-drill.service": f'''[Service]
Type=oneshot
{environment}
ExecStart={root}/python/omp-work/.venv/bin/omp-work ops restore drill --source latest --reason monthly
''',
    "omp-work-backup.timer": '''[Timer]
OnCalendar=daily
Persistent=true
[Install]
WantedBy=timers.target
''',
    "omp-work-wal.timer": '''[Timer]
OnBootSec=5m
OnUnitActiveSec=5m
Persistent=true
[Install]
WantedBy=timers.target
''',
    "omp-work-restore-drill.timer": '''[Timer]
OnCalendar=monthly
Persistent=true
[Install]
WantedBy=timers.target
''',
}
out = Path.home() / ".config/systemd/user"
out.mkdir(parents=True, exist_ok=True)
for name, content in units.items():
    (out / name).write_text(content)
PY

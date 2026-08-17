#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
config=${XDG_CONFIG_HOME:-$HOME/.config}/omp/work-ledger
state=${XDG_STATE_HOME:-$HOME/.local/state}/omp/work-ledger
share=${XDG_DATA_HOME:-$HOME/.local/share}/omp/work-ledger
for path in "$config" "$state/postgres" "$share/wal"; do install -d -m 700 "$path"; done
for bin in initdb postgres pg_ctl; do command -v "$bin" >/dev/null || { echo "missing native PostgreSQL binary: $bin" >&2; exit 1; }
done
[ "$(postgres --version | grep -oE '[0-9]+' | head -1)" = "18" ] || { echo 'native PostgreSQL 18 required' >&2; exit 1; }
python3 - "$root" "$config" "$state" "$share" <<'PY'
from pathlib import Path
import sys
root, config, state, share = sys.argv[1:]
unit = f'''[Unit]
Description=OMP Work Ledger PostgreSQL

[Service]
Type=simple
ExecStartPre=/bin/sh -c 'test -s {state}/postgres/PG_VERSION || /usr/bin/initdb -D {state}/postgres -U postgres --auth-local=trust --auth-host=scram-sha-256 -E UTF8 --data-checksums --pwfile={config}/credentials/postgres'
ExecStart=/usr/bin/postgres -D {state}/postgres -p 54321 -c listen_addresses=127.0.0.1 -k {state} -c password_encryption=scram-sha-256 -c wal_level=replica -c archive_mode=on -c archive_command='test ! -f {share}/wal/%%f && cp %%p {share}/wal/%%f || cmp %%p {share}/wal/%%f'
ExecStop=/usr/bin/pg_ctl -D {state}/postgres -m fast -w stop
Restart=on-failure

[Install]
WantedBy=default.target
'''
units = {
    "omp-work-postgres.service": unit,
    "omp-work-service.service": f"""[Unit]
Description=OMP Work Ledger WorkService (loopback)
After=omp-work-postgres.service

[Service]
Type=simple
ExecStart={root}/python/omp-work/.venv/bin/python -m omp_work serve --capabilities-dir {config}/capabilities
Restart=on-failure

[Install]
WantedBy=default.target
""",
    "omp-work-backup.service": f"""[Service]
Type=oneshot
ExecStart={root}/python/omp-work/.venv/bin/omp-work ops backup create
""",
    "omp-work-wal.service": f"""[Service]
Type=oneshot
ExecStart={root}/python/omp-work/.venv/bin/omp-work ops backup wal
""",
    "omp-work-restore-drill.service": f"""[Service]
Type=oneshot
ExecStart={root}/python/omp-work/.venv/bin/omp-work ops restore drill --source latest --reason monthly
""",
    "omp-work-backup.timer": """[Timer]
OnCalendar=daily
Persistent=true
[Install]
WantedBy=timers.target
""",
    "omp-work-wal.timer": """[Timer]
OnBootSec=5m
OnUnitActiveSec=5m
Persistent=true
[Install]
WantedBy=timers.target
""",
    "omp-work-restore-drill.timer": """[Timer]
OnCalendar=monthly
Persistent=true
[Install]
WantedBy=timers.target
""",
}
out = Path.home() / ".config/systemd/user"
out.mkdir(parents=True, exist_ok=True)
for name, content in units.items():
    (out / name).write_text(content)
PY

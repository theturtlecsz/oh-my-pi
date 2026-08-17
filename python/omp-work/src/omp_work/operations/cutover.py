"""HOME-148: cutover coordinator — preflight, rehearsal, execute, finalize, rollback.

Single-writer invariant at every instant:

- execute: freeze Linear (marker) → final delta export/import + parity → activate the
  exact sealed manifest → switch the selector to work. Linear is never unfrozen while
  Work is inactive, and Work is never activated before Linear is frozen.
- rollback (pre-first-mutation only): epoch active → rolled_back while Linear stays
  frozen → selector back to linear → freeze marker removed LAST.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import stat
import subprocess
import time
from uuid import UUID, uuid4

import httpx
import psycopg

from ..integration.exporter import LinearExporter
from ..integration.importer import LinearImporter
from ..integration.linear import load_credential
from ..v1.client import WorkClient
from ..v1.models import (
    ActivateCutoverCommand,
    ActivateCutoverPayload,
    CommandEnvelope,
    CommandSmokeResult,
    CutoverManifest,
    ReconciliationCounts,
    ReconciliationHashes,
)
from . import backup
from .capabilities import OWNER_SCOPES, provision_cutover, provision_owner, write_capability, write_client_config
from .config import OperationsConfig
from .. import CONTRACT_VERSION, contract_sha256
from ..integration.importer import TRANSFORMATION_VERSION
from .database import bootstrap, collect_health, migrate, migration_set_sha256
from .fingerprints import code_fingerprint, config_fingerprint, transform_sha256

LINEAR_GraphQL = "https://api.linear.app/graphql"
STATE_FILE = "cutover-state.json"
DEFAULT_MAPPING_FILE = "infra/work-ledger/linear-import-map.json"


class CutoverBlocked(RuntimeError):
    """A cutover step was refused; carries the human-readable blockers."""

    def __init__(self, blockers: list[str]) -> None:
        self.blockers = blockers
        super().__init__("; ".join(blockers))


# --- client-state tree (~/.config/omp-work): shared with the TS fence and client.json ---

def client_state_dir() -> Path:
    # Same root the TS side uses (workflow/config.ts + the linear.ts freeze fence).
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "omp-work"


def freeze_marker_path() -> Path:
    return client_state_dir() / "linear-frozen.json"


def _write_freeze_marker(payload: dict[str, object]) -> Path:
    """Atomic marker write: tmp file + rename, 0600. The TS fence reads this exact path."""
    directory = client_state_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = freeze_marker_path()
    tmp = directory / f".linear-frozen.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    os.rename(tmp, target)
    return target


def _remove_freeze_marker() -> None:
    try:
        freeze_marker_path().unlink()
    except FileNotFoundError:
        pass


# --- coordinator state ---

def _state_path(config: OperationsConfig) -> Path:
    return config.config_dir / STATE_FILE


def _load_state(config: OperationsConfig) -> dict[str, object]:
    try:
        return json.loads(_state_path(config).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"rehearsals": {}, "schema": 1}


def _save_state(config: OperationsConfig, state: dict[str, object]) -> None:
    path = _state_path(config)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    os.rename(tmp, path)


def _fingerprints() -> dict[str, str]:
    return {"code": code_fingerprint(), "contract": contract_sha256(), "migration_set": migration_set_sha256()}


# --- small seams (subprocess / network), injectable in tests ---

def _run_selector(backend: str) -> None:
    repo = Path(__file__).resolve().parents[5]
    result = subprocess.run(["bash", str(repo / "session-system" / "install.sh"), "--backend", backend], capture_output=True, text=True)
    if result.returncode != 0:
        raise CutoverBlocked([f"install.sh --backend {backend} failed: {result.stderr.strip()}"])


def _linear_graphql(token: str, query: str, *, oauth: bool, variables: dict[str, object] | None = None) -> dict[str, object]:
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}" if oauth else token}
    response = httpx.post(LINEAR_GraphQL, headers=headers, json={"query": query, "variables": variables or {}}, timeout=15)
    if response.status_code in (401, 403):
        raise CutoverBlocked(["linear_auth_rejected"])
    return response.json()


def _linear_project_ids(credential_token: str) -> set[str]:
    """All project IDs visible to the read-only credential (pagination-bounded)."""
    ids: set[str] = set()
    after: str | None = None
    query = "query($after: String) { projects(first: 100, after: $after, includeArchived: true) { nodes { id } pageInfo { hasNextPage endCursor } } }"
    for _ in range(50):
        payload = _linear_graphql(credential_token, query, oauth=True, variables={"after": after})
        projects = payload.get("data", {}).get("projects", {})  # type: ignore[union-attr]
        for node in projects.get("nodes", []):
            ids.add(node["id"])
        page = projects.get("pageInfo", {})
        if not page.get("hasNextPage"):
            return ids
        after = page.get("endCursor")
    raise CutoverBlocked(["linear project pagination exceeded 50 pages"])


def _personal_key_status() -> str:
    """'revoked' only on explicit 401/403; 'live' when the key still authenticates.
    Any other outcome (timeout, 429, 5xx) blocks finalization rather than passing it."""
    env_path = Path.home() / ".config" / "linear.env"
    try:
        key = next(line.split("=", 1)[1].strip() for line in env_path.read_text(encoding="utf-8").splitlines() if line.startswith("LINEAR_API_KEY="))
    except (FileNotFoundError, StopIteration):
        return "revoked"  # key file already gone
    try:
        response = httpx.post(
            LINEAR_GraphQL,
            headers={"Authorization": key, "Content-Type": "application/json"},
            json={"query": "query { viewer { id } }"},
            timeout=15,
        )
    except httpx.HTTPError as error:
        raise CutoverBlocked([f"personal key revocation probe failed: {error}"]) from error
    if response.status_code in (401, 403):
        return "revoked"
    if response.status_code == 200:
        body = response.json()
        if body.get("data", {}).get("viewer", {}).get("id"):
            return "live"
        errors = body.get("errors") or []
        if any("auth" in str(e.get("message", "")).lower() for e in errors):
            return "revoked"
        raise CutoverBlocked([f"personal key probe returned unexpected GraphQL errors: {errors}"])
    raise CutoverBlocked([f"personal key probe returned HTTP {response.status_code}; cannot confirm revocation"])


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _psql(config: OperationsConfig, sql: str, *, role: str = "postgres") -> None:
    env = {**os.environ, "PGPASSWORD": config.read_secret(role)}
    result = subprocess.run(
        ["psql", "-h", config.host, "-p", str(config.port), "-U", role, "-d", config.database, "-v", "ON_ERROR_STOP=1", "-c", sql],
        capture_output=True, text=True, env=env,
    )
    if result.returncode != 0:
        raise CutoverBlocked([f"psql failed: {result.stderr.strip()}"])


def _epoch_row(config: OperationsConfig, workspace_id: UUID) -> dict[str, object] | None:
    """Latest epoch joined with its authority row. Query failures RAISE — silently
    treating an error as "no epoch" would let rollback remove the freeze marker
    while Work is still authoritative."""
    with psycopg.connect(**config.connection_kwargs("postgres")) as connection, connection.cursor() as cur:
        cur.execute(
            "SELECT e.epoch_id, e.state, a.first_work_mutation_at, e.candidate_manifest_sha256,"
            " e.revoked_at, e.final_report_sha256"
            " FROM omp_control.cutover_epochs e"
            " LEFT JOIN omp_control.workspace_authority a ON a.epoch_id = e.epoch_id"
            " WHERE e.workspace_id = %s ORDER BY e.activated_at DESC LIMIT 1",
            (str(workspace_id),),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {"epoch_id": row[0], "state": row[1], "first_work_mutation_at": row[2], "candidate_manifest_sha256": row[3], "revoked_at": row[4], "final_report_sha256": row[5]}


# --- preflight ---

def preflight(config: OperationsConfig, *, mapping_file: Path) -> dict[str, object]:
    """Every gate that must hold before the first rehearsal. Returns the check report;
    raises CutoverBlocked with every failing check when any blocks."""
    checks: dict[str, str] = {}
    try:
        migrate(config)
        checks["migrations_current"] = "ok"
    except Exception as error:
        checks["migrations_current"] = f"blocked: {error}"
    try:
        report = collect_health(config)
        checks["database_ready"] = "ok" if report.ready else "blocked: health not ready"
    except Exception as error:
        checks["database_ready"] = f"blocked: {error}"
    workspace_id: UUID | None = None
    try:
        workspace_id = config.workspace_id()
        config.actor_id()
        checks["operator_identity"] = "ok"
    except ValueError as error:
        checks["operator_identity"] = f"blocked: {error}"
    try:
        credential = load_credential(config.secret_path("linear-export.json"))
        if credential.kind != "oauth" or set(credential.scopes) != {"read"}:
            checks["linear_oauth_read_only"] = "blocked: credential is not a read-only OAuth token"
        else:
            checks["linear_oauth_read_only"] = "ok"
            try:
                project_ids = _linear_project_ids(credential.access_token.get_secret_value())
                mapping = json.loads(mapping_file.read_text(encoding="utf-8"))
                mapped = set(mapping.get("project_repositories", {}))
                missing = sorted(project_ids - mapped)
                checks["import_map_coverage"] = "ok" if not missing else f"blocked: unmapped Linear projects: {', '.join(missing)}"
            except CutoverBlocked as error:
                checks["import_map_coverage"] = f"blocked: {'; '.join(error.blockers)}"
    except ValueError as error:
        checks["linear_oauth_read_only"] = f"blocked: {error}"
        checks["import_map_coverage"] = "blocked: no credential to enumerate projects"
    try:
        backup.verify_target(config)
        checks["backup_target"] = "ok"
    except Exception as error:
        checks["backup_target"] = f"blocked: {error}"
    blocked = [f"{name}: {verdict}" for name, verdict in checks.items() if verdict.startswith("blocked")]
    report_out = {"checks": checks, "fingerprints": _fingerprints(), "workspace_id": str(workspace_id) if workspace_id else None}
    if blocked:
        raise CutoverBlocked(blocked)
    return report_out


# --- manifest ---

def _build_manifest(config: OperationsConfig, *, workspace_id: UUID, batch_id: UUID, smoke_results: list[CommandSmokeResult], freeze_at: datetime) -> CutoverManifest:
    """Assemble the exact CutoverManifest for a promoted batch from database state.
    Field set mirrors v1.models.CutoverManifest and the store's activation checks."""
    with psycopg.connect(**config.connection_kwargs("postgres"), autocommit=True) as connection:
        connection.execute("SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)", (str(workspace_id), str(config.actor_id())))
        row = connection.execute(
            "SELECT e.source_boundary, e.raw_export_sha256, b.parity_hashes "
            "FROM omp_integration.import_batches b JOIN omp_integration.raw_exports e ON e.export_id = b.export_id "
            "WHERE b.workspace_id = %s AND b.batch_id = %s AND b.state = 'promoted'",
            (workspace_id, batch_id),
        ).fetchone()
        if row is None:
            raise CutoverBlocked([f"batch {batch_id} is not promoted"])
        boundary, raw_sha, parity = row
        blocking = connection.execute("SELECT count(*) FROM omp_integration.migration_anomalies WHERE workspace_id = %s AND batch_id = %s AND disposition = 'blocking'", (workspace_id, batch_id)).fetchone()[0]
        if blocking:
            raise CutoverBlocked([f"batch {batch_id} still carries {blocking} blocking anomalies"])
        anomalies = [
            {"code": code, "disposition": disposition}
            for code, disposition in connection.execute(
                "SELECT code, disposition FROM omp_integration.migration_anomalies WHERE workspace_id = %s AND batch_id = %s AND disposition <> 'blocking' ORDER BY created_at",
                (workspace_id, batch_id),
            ).fetchall()
        ]
        # operations_evidence is global (no workspace binding): latest passed receipt per kind
        receipts = dict(connection.execute(
            "SELECT DISTINCT ON (kind) kind, receipt_sha256 FROM omp_control.operations_evidence "
            "WHERE kind IN ('backup','restore_drill') AND outcome LIKE 'passed%' ORDER BY kind, created_at DESC",
        ).fetchall())
    if "backup" not in receipts or "restore_drill" not in receipts:
        raise CutoverBlocked(["missing passed backup/restore evidence receipts"])
    bundle = parity if isinstance(parity, dict) else json.loads(parity)
    return CutoverManifest.model_validate({
        "epoch_id": str(uuid4()),
        "contract_version": CONTRACT_VERSION,
        "contract_sha256": contract_sha256(),
        "schema_sha256": migration_set_sha256(),
        "transform_version": TRANSFORMATION_VERSION,
        "transform_sha256": transform_sha256(),
        "source_boundary": boundary.isoformat(),
        "source_watermark": boundary.isoformat(),
        "raw_export_sha256": raw_sha,
        "import_batch_id": str(batch_id),
        "dimension_counts": bundle["dimension_counts"],
        "dimension_hashes": bundle["dimension_hashes"],
        "parity_groups": bundle["parity_groups"],
        "anomalies": anomalies,
        "parity_differences": [],
        "backup_receipt_sha256": receipts["backup"],
        "restore_receipt_sha256": receipts["restore_drill"],
        "command_smoke_results": [r.model_dump(mode="json") for r in smoke_results],
        "code_fingerprint": code_fingerprint(),
        "config_fingerprint": config_fingerprint(config),
        "freeze_at": freeze_at.isoformat(),
        "actor": "owner",
    })


# --- scratch postgres lifecycle ---

def _run(cmd: list[str], *, env: dict[str, str] | None = None, cwd: Path | None = None) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=cwd)
    if result.returncode != 0:
        raise CutoverBlocked([f"{' '.join(cmd[:2])} failed: {result.stderr.strip()[:500]}"])
    return result.stdout


def _pg_init(config: OperationsConfig, pgdata: Path, port: int, socket_dir: Path) -> None:
    pwfile = pgdata.parent / "pgpw"
    pwfile.write_text(config.read_secret("postgres") + "\n", encoding="utf-8")
    pwfile.chmod(0o600)
    _run(["initdb", "-D", str(pgdata), "-U", "postgres", "--pwfile", str(pwfile), "--auth-host=scram-sha-256", "--auth-local=trust"])
    _run(["pg_ctl", "-D", str(pgdata), "-w", "-l", str(pgdata.parent / "pg.log"), "-o", f"-p {port} -k {socket_dir} -c listen_addresses=127.0.0.1", "start"])


def _pg_stop(pgdata: Path) -> None:
    _run(["pg_ctl", "-D", str(pgdata), "-m", "fast", "-w", "stop"])


def _pg_clone(src: Path, dst: Path) -> None:
    """Byte copy of a STOPPED cluster's data directory."""
    shutil.copytree(src, dst, symlinks=True)


def _serve(config: OperationsConfig, http_port: int) -> subprocess.Popen[bytes]:
    project = Path(__file__).resolve().parents[3]
    env = {
        **os.environ,
        "XDG_CONFIG_HOME": str(config.config_dir.parent.parent),
        "XDG_STATE_HOME": str(config.state_dir.parent.parent),
        "XDG_DATA_HOME": str(config.data_dir.parent.parent),
        "OMP_WORK_POSTGRES_PORT": str(config.port),
    }
    proc = subprocess.Popen(
        ["uv", "run", "python", "-m", "omp_work", "serve", "--port", str(http_port), "--capabilities-dir", str(config.config_dir / "capabilities")],
        cwd=project, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{http_port}"
    for _ in range(60):
        try:
            if httpx.get(f"{base}/v1/health/live", timeout=1).status_code == 200:
                return proc
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    proc.kill()
    raise CutoverBlocked(["candidate service never became live"])


def _seed_authority_sql(config: OperationsConfig, workspace_id: UUID, actor_id: UUID) -> None:
    """Test/rehearsal-only: authority without the cutover path, on disposable clones ONLY."""
    _psql(config, f"INSERT INTO omp_control.workspaces(workspace_id) VALUES ('{workspace_id}') ON CONFLICT DO NOTHING")
    _psql(config, f"INSERT INTO omp_control.cutover_epochs(workspace_id, epoch_id, state, candidate_manifest, candidate_manifest_sha256) VALUES ('{workspace_id}', '{uuid4()}', 'sealed', '{{}}'::jsonb, '{'0' * 64}') ON CONFLICT DO NOTHING")
    _psql(config, f"INSERT INTO omp_control.workspace_authority(workspace_id, epoch_id) SELECT '{workspace_id}', epoch_id FROM omp_control.cutover_epochs WHERE workspace_id = '{workspace_id}' ON CONFLICT DO NOTHING")


def _run_smoke(config: OperationsConfig, *, base_url: str, workspace_id: UUID, owner_id: UUID, work_dir: Path) -> list[CommandSmokeResult]:
    """Drive the extended candidate smoke harness in reuse mode against a live service."""
    repo = Path(__file__).resolve().parents[5]
    # The probe repo scopes itself to a real project; use one the import produced.
    tree = WorkClient(base_url, workspace_id, config.config_dir / "capabilities" / "owner.json").tree()
    if not tree.projects:
        raise CutoverBlocked(["imported ledger has no projects for the smoke probe"])
    project_name = tree.projects[0].name
    env = {
        **os.environ,
        "OMP_WORK_POSTGRES_INTEGRATION": "1",
        "OMP_WORK_SMOKE_REUSE": "1",
        "OMP_WORK_SMOKE_BASE_URL": base_url,
        "OMP_WORK_SMOKE_WORKSPACE": str(workspace_id),
        "OMP_WORK_SMOKE_OWNER": str(owner_id),
        "OMP_WORK_SMOKE_XDG": str(work_dir / "xdg"),
        "OMP_WORK_SMOKE_CAPABILITIES": str(config.config_dir / "capabilities"),
        "OMP_WORK_SMOKE_PROJECT": project_name,
    }
    output = _run(["bun", "run", str(repo / "session-system" / "tests" / "work-service-candidate-smoke.ts")], env=env, cwd=repo)
    last = [line for line in output.splitlines() if line.startswith("work-service-candidate-smoke:")]
    if not last or "PASS" not in last[-1]:
        raise CutoverBlocked([f"candidate smoke failed: {output.strip()[-500:]}"])
    return [CommandSmokeResult(command_type=name, passed=True) for name in ("capture", "plan_stamp", "summary_freeze", "verification", "audit", "closeout", "done_push", "first_screen")]


# --- rehearse ---

def rehearse(config: OperationsConfig, *, ordinal: int, retain_candidate: bool, mapping_file: Path) -> dict[str, object]:
    state = _load_state(config)
    rehearsals: dict[str, object] = state.setdefault("rehearsals", {})  # type: ignore[assignment]
    fingerprints = _fingerprints()
    if ordinal == 2:
        first = rehearsals.get("1")
        if not isinstance(first, dict) or first.get("verdict") != "pass":
            raise CutoverBlocked(["rehearsal 2 requires a passing rehearsal 1"])
        if first.get("fingerprints") != fingerprints:
            raise CutoverBlocked(["code/contract/migration fingerprints drifted since rehearsal 1"])
    if retain_candidate and ordinal != 2:
        raise CutoverBlocked(["only rehearsal 2 retains the final candidate"])
    scratch = config.state_dir / f"cutover-rehearsal-{ordinal}"
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True)
    xdg = scratch / "xdg"
    port, http_port = _free_port(), _free_port()
    scratch_config = replace(
        config,
        config_dir=xdg / "omp" / "work-ledger",
        state_dir=(scratch / "state") / "omp" / "work-ledger",
        data_dir=(scratch / "data") / "omp" / "work-ledger",
        port=port,
    )
    scratch_config.credentials_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    # The retained candidate must authenticate with the PRODUCTION role credentials
    # (the final service runs with the real config against this cluster), and the
    # candidate must carry the PROD workspace identity. Copy every credential.
    for source in config.credentials_dir.iterdir():
        if source.is_file():
            target = scratch_config.secret_path(source.name)
            shutil.copyfile(source, target)
            target.chmod(0o600)
    workspace_id, owner_id = scratch_config.workspace_id(), scratch_config.actor_id()

    pgdata = scratch / "pgdata"
    _pg_init(scratch_config, pgdata, port, scratch)
    try:
        bootstrap(scratch_config)
        export_manifest = LinearExporter(scratch_config).full(workspace_id)
        if any(anomaly.disposition == "blocking" for anomaly in export_manifest.anomalies):
            raise CutoverBlocked(["full export reported blocking anomalies"])
        importer = LinearImporter(scratch_config)
        staged = importer.stage(workspace_id, export_manifest.export_id, mapping_file)
        importer.reconcile(staged.batch_id)
        promoted = importer.promote(staged.batch_id)
        backup.create(scratch_config)
        backup.restore_drill(scratch_config, reason="clean-instance")

        # Snapshot the pristine pre-activation candidate BEFORE anything activates.
        _pg_stop(pgdata)
        candidate_pgdata = scratch / "candidate-pgdata"
        _pg_clone(pgdata, candidate_pgdata)

        # Clone A: authority-seeded disposable clone → real command smoke results.
        clone_a = scratch / "clone-a-pgdata"
        _pg_clone(pgdata, clone_a)
        port_a, http_a = _free_port(), _free_port()
        config_a = replace(scratch_config, port=port_a)
        _run(["pg_ctl", "-D", str(clone_a), "-w", "-l", str(scratch / "pg-a.log"), "-o", f"-p {port_a} -k {scratch} -c listen_addresses=127.0.0.1", "start"])
        try:
            _seed_authority_sql(config_a, workspace_id, owner_id)
            # Clones get the capability FILE only — write_client_config would clobber the
            # real installed client.json with a soon-dead clone URL. The isolated smoke
            # harness writes its own client config inside its temp HOME.
            write_capability(config_a, "owner", actor_id=owner_id, actor_kind="owner", workspaces=(workspace_id,), scopes=OWNER_SCOPES)
            service_a = _serve(config_a, http_a)
            try:
                smoke = _run_smoke(config_a, base_url=f"http://127.0.0.1:{http_a}", workspace_id=workspace_id, owner_id=owner_id, work_dir=scratch / "smoke-a")
            finally:
                service_a.kill()
        finally:
            _pg_stop(clone_a)

        # Clone B: fresh pristine copy → REAL activate_cutover with the real smoke
        # results (no predeclared PASS), then post-activation sanity.
        clone_b = scratch / "clone-b-pgdata"
        _pg_clone(pgdata, clone_b)
        port_b, http_b = _free_port(), _free_port()
        config_b = replace(scratch_config, port=port_b)
        _run(["pg_ctl", "-D", str(clone_b), "-w", "-l", str(scratch / "pg-b.log"), "-o", f"-p {port_b} -k {scratch} -c listen_addresses=127.0.0.1", "start"])
        try:
            provision_cutover(config_b, workspace_id=workspace_id, actor_id=owner_id)
            service_b = _serve(config_b, http_b)
            try:
                manifest = _build_manifest(config_b, workspace_id=workspace_id, batch_id=promoted.batch_id, smoke_results=smoke, freeze_at=datetime.now(timezone.utc))
                envelope = CommandEnvelope(
                    api_version="work.omp.dev/v1",
                    workspace_id=workspace_id,
                    operation_id=uuid4(),
                    request_id=uuid4(),
                    correlation_id=uuid4(),
                    command=ActivateCutoverCommand(type="activate_cutover", payload=ActivateCutoverPayload(manifest=manifest)),
                )
                client = WorkClient(f"http://127.0.0.1:{http_b}", workspace_id, config_b.config_dir / "capabilities" / "cutover.json")
                response = client.execute(envelope)
                if response.receipt.state != "applied":
                    raise CutoverBlocked([f"activation rejected: {response.receipt.diagnostics}"])
            finally:
                service_b.kill()
        finally:
            _pg_stop(clone_b)
    finally:
        if (pgdata / "postmaster.pid").exists():
            _pg_stop(pgdata)

    verdict: dict[str, object] = {
        "verdict": "pass",
        "ordinal": ordinal,
        "fingerprints": fingerprints,
        "batch_id": str(promoted.batch_id),
        "workspace_id": str(workspace_id),
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }
    rehearsals[str(ordinal)] = verdict
    if retain_candidate:
        state["candidate"] = {"pgdata": str(candidate_pgdata), "fingerprints": fingerprints, "batch_id": str(promoted.batch_id), "workspace_id": str(workspace_id), "owner_id": str(owner_id)}
    else:
        shutil.rmtree(scratch, ignore_errors=True)
    _save_state(config, state)
    return verdict


# --- execute ---

def execute(config: OperationsConfig, *, mapping_file: Path) -> dict[str, object]:
    """The freeze window. Order is load-bearing (single writer at every instant):
    freeze Linear → final delta import into the pristine candidate → smoke on a
    disposable clone → activate the exact sealed manifest → switch the selector."""
    state = _load_state(config)
    rehearsals: dict[str, object] = state.get("rehearsals", {})  # type: ignore[assignment]
    candidate = state.get("candidate")
    fingerprints = _fingerprints()
    blockers: list[str] = []
    for ordinal in ("1", "2"):
        entry = rehearsals.get(ordinal)
        if not isinstance(entry, dict) or entry.get("verdict") != "pass":
            blockers.append(f"rehearsal {ordinal} has not passed")
    if not isinstance(candidate, dict):
        blockers.append("no retained candidate from rehearsal 2")
    elif candidate.get("fingerprints") != fingerprints:
        blockers.append("candidate fingerprint drifted since rehearsal 2 — discard and rerun rehearsal 2")
    if freeze_marker_path().exists():
        blockers.append("linear freeze marker already present")
    if blockers:
        raise CutoverBlocked(blockers)
    assert isinstance(candidate, dict)
    candidate_pgdata = Path(str(candidate["pgdata"]))
    workspace_id = UUID(str(candidate["workspace_id"]))
    owner_id = UUID(str(candidate["owner_id"]))
    scratch = candidate_pgdata.parent
    scratch_config = replace(
        config,
        config_dir=(scratch / "xdg") / "omp" / "work-ledger",
        state_dir=(scratch / "state") / "omp" / "work-ledger",
        data_dir=(scratch / "data") / "omp" / "work-ledger",
        port=_free_port(),
    )
    candidate_config = scratch_config
    http_port = _free_port()
    _run(["pg_ctl", "-D", str(candidate_pgdata), "-w", "-l", str(candidate_pgdata.parent / "pg-final.log"), "-o", f"-p {candidate_config.port} -k {candidate_pgdata.parent} -c listen_addresses=127.0.0.1", "start"])
    try:
        if _epoch_row(candidate_config, workspace_id) is not None:
            raise CutoverBlocked(["candidate database already carries an epoch"])
        # 1. Freeze Linear. From here the TS fence refuses every Linear mutation.
        frozen_at = datetime.now(timezone.utc)
        _write_freeze_marker({"frozen_at": frozen_at.isoformat(), "workspace_id": str(workspace_id), "actor": "owner"})

        # 2. Final delta export + import (importer role bypasses the app fence;
        #    the candidate is still pre-activation).
        export_manifest = LinearExporter(candidate_config).delta(workspace_id)
        if any(anomaly.disposition == "blocking" for anomaly in export_manifest.anomalies):
            raise CutoverBlocked(["final delta export reported blocking anomalies"])
        importer = LinearImporter(candidate_config)
        staged = importer.stage(workspace_id, export_manifest.export_id, mapping_file)
        importer.reconcile(staged.batch_id)
        promoted = importer.promote(staged.batch_id)

        # 3. Smoke on a disposable authority-seeded clone; real results feed the manifest.
        _pg_stop(candidate_pgdata)
        clone = candidate_pgdata.parent / "execute-smoke-pgdata"
        _pg_clone(candidate_pgdata, clone)
        clone_config = replace(scratch_config, port=_free_port())
        clone_http = _free_port()
        _run(["pg_ctl", "-D", str(clone), "-w", "-l", str(candidate_pgdata.parent / "pg-smoke.log"), "-o", f"-p {clone_config.port} -k {candidate_pgdata.parent} -c listen_addresses=127.0.0.1", "start"])
        try:
            _seed_authority_sql(clone_config, workspace_id, owner_id)
            write_capability(clone_config, "owner", actor_id=owner_id, actor_kind="owner", workspaces=(workspace_id,), scopes=OWNER_SCOPES)
            smoke_service = _serve(clone_config, clone_http)
            try:
                smoke = _run_smoke(clone_config, base_url=f"http://127.0.0.1:{clone_http}", workspace_id=workspace_id, owner_id=owner_id, work_dir=candidate_pgdata.parent / "smoke-final")
            finally:
                smoke_service.kill()
        finally:
            _pg_stop(clone)
        shutil.rmtree(clone, ignore_errors=True)
        _run(["pg_ctl", "-D", str(candidate_pgdata), "-w", "-l", str(candidate_pgdata.parent / "pg-final.log"), "-o", f"-p {candidate_config.port} -k {candidate_pgdata.parent} -c listen_addresses=127.0.0.1", "start"])

        # 4. Activate the exact sealed manifest on the real candidate.
        # Rehearsal clone B already wrote cutover.json into this caps dir; rotate it.
        provision_cutover(candidate_config, workspace_id=workspace_id, actor_id=owner_id, rotate=True)
        service = _serve(candidate_config, http_port)
        try:
            manifest = _build_manifest(candidate_config, workspace_id=workspace_id, batch_id=promoted.batch_id, smoke_results=smoke, freeze_at=frozen_at)
            envelope = CommandEnvelope(
                api_version="work.omp.dev/v1",
                workspace_id=workspace_id,
                operation_id=uuid4(),
                request_id=uuid4(),
                correlation_id=uuid4(),
                command=ActivateCutoverCommand(type="activate_cutover", payload=ActivateCutoverPayload(manifest=manifest)),
            )
            client = WorkClient(f"http://127.0.0.1:{http_port}", workspace_id, candidate_config.config_dir / "capabilities" / "cutover.json")
            response = client.execute(envelope)
            if response.receipt.state != "applied":
                raise CutoverBlocked([f"activation rejected: {response.receipt.diagnostics}"])
            epoch_id = manifest.epoch_id
        finally:
            service.kill()
        # Persist the in-progress window IMMEDIATELY: if the selector switch or
        # readiness smoke fails from here, rollback/finalize can still locate the
        # candidate DB and the mutation timestamp decides legality.
        state["window"] = {"state": "activating", "epoch_id": str(epoch_id), "frozen_at": frozen_at.isoformat(), "candidate_pgdata": str(candidate_pgdata), "port": candidate_config.port, "http_port": http_port}
        _save_state(config, state)
    except Exception:
        _pg_stop(candidate_pgdata)
        raise

    # 5. Switch the selector to work and point the installed client at the final
    #    service. The readiness smoke below is the first WorkService mutation and
    #    ends the pre-write rollback window by design.
    _run_selector("work")
    # The candidate cluster carries production role credentials, so the final
    # service authenticates with the real persistent config it will keep.
    final_config = replace(config, port=candidate_config.port)
    provision_owner(final_config, workspace_id=workspace_id, owner_id=owner_id, base_url=f"http://127.0.0.1:{http_port}")
    final_service = _serve(final_config, http_port)
    readiness = _run_smoke(final_config, base_url=f"http://127.0.0.1:{http_port}", workspace_id=workspace_id, owner_id=owner_id, work_dir=candidate_pgdata.parent / "smoke-readiness")
    window = state["window"]
    assert isinstance(window, dict)
    window["state"] = "executed"
    _save_state(config, state)
    return {"epoch_id": str(epoch_id), "authority": "work", "readiness_smoke": [r.command_type for r in readiness], "service_pid": final_service.pid}


# --- finalize ---

def finalize(config: OperationsConfig) -> dict[str, object]:
    """After the owner revokes the personal Linear key in the browser."""
    state = _load_state(config)
    window = state.get("window")
    if not isinstance(window, dict) or window.get("state") != "executed":
        raise CutoverBlocked(["no executed cutover window to finalize"])
    candidate = state.get("candidate")
    if not isinstance(candidate, dict):
        raise CutoverBlocked(["no candidate recorded"])
    workspace_id = UUID(str(candidate["workspace_id"]))
    status = _personal_key_status()
    if status != "revoked":
        raise CutoverBlocked(["personal Linear API key is still live — revoke it in Linear API settings before finalize"])
    epoch_config = replace(config, port=int(window["port"]))
    epoch = _epoch_row(epoch_config, workspace_id)
    if epoch is None or epoch["state"] != "active":
        raise CutoverBlocked(["epoch is not active"])
    # Key file removed only after revocation is proven.
    env_path = Path.home() / ".config" / "linear.env"
    env_path.unlink(missing_ok=True)
    revoked_at = datetime.now(timezone.utc)
    _psql(epoch_config, f"UPDATE omp_control.cutover_epochs SET state='sealed', revoked_at='{revoked_at.isoformat()}', recovery_path='pre_write' WHERE epoch_id='{epoch['epoch_id']}' AND state='active'")
    window["state"] = "finalized"
    window["revoked_at"] = revoked_at.isoformat()
    _save_state(config, state)
    return {"epoch_id": str(epoch["epoch_id"]), "state": "sealed", "personal_key": "revoked"}


# --- rollback ---

def rollback(config: OperationsConfig) -> dict[str, object]:
    """Pre-first-mutation rollback to Linear. Order is load-bearing: Work authority is
    removed while Linear stays frozen, the selector switches back, and the freeze
    marker is removed LAST."""
    state = _load_state(config)
    candidate = state.get("candidate")
    window = state.get("window")
    epoch = None
    if isinstance(candidate, dict) and isinstance(window, dict) and "port" in window:
        epoch = _epoch_row(replace(config, port=int(window["port"])), UUID(str(candidate["workspace_id"])))
    if epoch is not None:
        if epoch["state"] != "active" or epoch["first_work_mutation_at"]:
            raise CutoverBlocked(["rollback refused: the first WorkService mutation already landed — repair/restore is the only path"])
        # One transaction: epoch rolls back and Work authority disappears together.
        _psql(
            replace(config, port=int(window["port"])),  # type: ignore[index]
            f"BEGIN; UPDATE omp_control.cutover_epochs SET state='rolled_back', recovery_path='pre_write' WHERE epoch_id='{epoch['epoch_id']}'; "
            f"DELETE FROM omp_control.workspace_authority WHERE workspace_id='{candidate['workspace_id']}'; COMMIT;",  # type: ignore[index]
        )
    if epoch is None and freeze_marker_path().exists():
        raise CutoverBlocked(["freeze marker present but no epoch row found — verify no Work authority exists before removing the marker manually"])
    _run_selector("linear")
    _remove_freeze_marker()
    state["window"] = {"state": "rolled_back", "at": datetime.now(timezone.utc).isoformat()}
    _save_state(config, state)
    return {"rolled_back": True, "epoch_id": str(epoch["epoch_id"]) if epoch else None}


# --- status ---

def status(config: OperationsConfig) -> dict[str, object]:
    """Read-only operator view: coordinator state plus the epoch/authority record."""
    state = _load_state(config)
    report: dict[str, object] = {
        "rehearsals": state.get("rehearsals", {}),
        "candidate_retained": isinstance(state.get("candidate"), dict),
        "window": state.get("window"),
        "freeze_marker": freeze_marker_path().exists(),
        "fingerprints": _fingerprints(),
    }
    epoch: dict[str, object] | None = None
    candidate = state.get("candidate")
    window = state.get("window")
    if isinstance(candidate, dict):
        port = int(window["port"]) if isinstance(window, dict) and "port" in window else config.port
        try:
            row = _epoch_row(replace(config, port=port), UUID(str(candidate["workspace_id"])))
        except Exception as error:  # cluster may be stopped; status must stay read-only-safe
            row = {"error": f"{type(error).__name__}: {error}"}
        if row is not None:
            epoch = {
                "epoch_id": str(row.get("epoch_id", "")),
                "state": row.get("state"),
                "first_work_mutation_at": str(row["first_work_mutation_at"]) if row.get("first_work_mutation_at") else None,
                "revoked_at": str(row["revoked_at"]) if row.get("revoked_at") else None,
                "candidate_manifest_sha256": row.get("candidate_manifest_sha256"),
                "final_report_sha256": row.get("final_report_sha256"),
                "error": row.get("error"),
            }
    report["epoch"] = epoch
    report["authority"] = "work" if isinstance(epoch, dict) and epoch.get("state") in ("active", "sealed") else "linear"
    return report

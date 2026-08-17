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
from datetime import datetime, timedelta, timezone
from hashlib import sha256 as bytes_sha256
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
from uuid import UUID, uuid4

import httpx
import psycopg

from ..integration.exporter import ExportManifest, LinearExporter, load_manifest
from ..integration.importer import LinearImporter
from ..integration.linear import refresh_credential
from ..v1.canonical import sha256
from ..v1.client import WorkClient
from ..v1.models import (
    ActivateCutoverCommand,
    ActivateCutoverPayload,
    AttestCutoverPlanCommand,
    AttestCutoverPlanPayload,
    CommandEnvelope,
    CommandSmokeResult,
    CutoverManifest,
    FocusSlot,
    ReconciliationCounts,
    ReconciliationHashes,
    SetFocusCommand,
    SetFocusPayload,
)
from . import backup
from .artifacts import read_json_artifact, write_json_artifact
from .capabilities import OWNER_SCOPES, provision_cutover, provision_owner, write_capability, write_client_config
from .config import OperationsConfig
from .. import CONTRACT_VERSION, contract_sha256
from ..integration.importer import TRANSFORMATION_VERSION
from .database import bootstrap, collect_health, migrate, migration_set_sha256
from .fingerprints import code_fingerprint, config_fingerprint, transform_sha256

LINEAR_GraphQL = "https://api.linear.app/graphql"
STATE_FILE = "cutover-state.json"
DEFAULT_MAPPING_FILE = "infra/work-ledger/linear-import-map.json"
# Plan §4.2/§6.1: the freeze marker precedes the delta export by the Linear adapter's
# worst-case in-flight window (6s timeout + 1s slack). Without the drain a mutation
# admitted at T-epsilon commits after source_boundary and is silently excluded.
FREEZE_DRAIN_SECONDS = 7.0
# Plan §6.1: the freeze window is T+45 minutes. Past the deadline before activation
# the coordinator auto-rolls-back (pre-write); after the pivot it proceeds (rollback
# is already impossible) and reports the overrun.
WINDOW_LIMIT_MINUTES = 45
WINDOW_HARD_LIMIT_MINUTES = 60
PLAN_ITEM_ALIAS = "HOME-148"
WORKSERVICE_PORT = 54322
LINEAR_ENV_PATH = Path.home() / ".config" / "linear.env"
# The wrapper (session-system/tests/work-service-candidate-smoke.ts) must report
# exactly these cases; a renamed/missing case fails closed.
EXPECTED_SMOKE_COMMANDS = frozenset({
    "first_screen", "capture", "now_select", "plan_stamp", "summary_freeze",
    "auditor_spawn", "verification", "audit", "closeout", "request_closeout",
    "done_push", "focus_cleared", "loopback_only", "workflow_view",
})


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


def _expect_backend(backend: str) -> None:
    """install.sh's own read-only verdict on the live extension set; the selector's
    exit code alone never proved which set is installed."""
    repo = Path(__file__).resolve().parents[5]
    result = subprocess.run(["bash", str(repo / "session-system" / "install.sh"), "--expect-backend", backend], capture_output=True, text=True)
    if result.returncode != 0:
        raise CutoverBlocked([f"backend '{backend}' not installed: {result.stderr.strip()}"])


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
    """'revoked' ONLY on an explicit auth rejection (401/403, or GraphQL auth errors)
    from a probe of the bound key; 'live' when the key still authenticates; 'unknown'
    for everything else — including a missing or unparseable key file. Local deletion
    is not proof of remote revocation: callers must fail closed on 'unknown'."""
    try:
        key = _read_linear_key_bytes().decode("utf-8")
    except CutoverBlocked:
        return "unknown"
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
            " e.revoked_at, e.final_report_sha256, (a.workspace_id IS NOT NULL)"
            " FROM omp_control.cutover_epochs e"
            " LEFT JOIN omp_control.workspace_authority a ON a.epoch_id = e.epoch_id"
            " WHERE e.workspace_id = %s ORDER BY e.activated_at DESC LIMIT 1",
            (str(workspace_id),),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {"epoch_id": row[0], "state": row[1], "first_work_mutation_at": row[2], "candidate_manifest_sha256": row[3], "revoked_at": row[4], "final_report_sha256": row[5], "authority_present": bool(row[6])}


# --- cutover evidence: encrypted immutable reports, plan artifact, key material ---

def _read_linear_key_bytes() -> bytes:
    """Exact personal-key bytes from linear.env. Hash these, never a re-encoded copy."""
    try:
        for line in LINEAR_ENV_PATH.read_text(encoding="utf-8").splitlines():
            if line.startswith("LINEAR_API_KEY="):
                return line.split("=", 1)[1].strip().encode("utf-8")
    except FileNotFoundError:
        pass
    raise CutoverBlocked([f"personal Linear key not readable at {LINEAR_ENV_PATH}"])


def _write_report(config: OperationsConfig, *, kind: str, run_id: str, payload: dict[str, object]) -> tuple[str, str, str]:
    """Persist a bound cutover report as an immutable encrypted artifact in the
    PERSISTENT data dir (never the rehearsal scratch tree). No-clobber install;
    returns (relative_path, plaintext_sha256, ciphertext_sha256)."""
    root = config.data_dir / "cutover" / kind / run_id
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging = config.state_dir / "cutover-staging"
    staging.mkdir(mode=0o700, parents=True, exist_ok=True)
    return write_json_artifact(root, staging, "report", payload, config.secret_path("gpg-passphrase"), data_dir=config.data_dir)


def _read_report(config: OperationsConfig, relative: str, *, expected_plaintext_sha256: str, expected_ciphertext_sha256: str) -> dict[str, object]:
    staging = config.state_dir / "cutover-staging"
    staging.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = read_json_artifact(
        Path(relative),
        staging / "report-readback.json",
        config.secret_path("gpg-passphrase"),
        expected_plaintext_sha256=expected_plaintext_sha256,
        expected_ciphertext_sha256=expected_ciphertext_sha256,
        data_dir=config.data_dir,
    )
    if not isinstance(payload, dict):
        raise CutoverBlocked(["cutover report artifact is not an object"])
    return payload


def _resolve_plan_work_id(config: OperationsConfig, workspace_id: UUID) -> UUID:
    """The imported HOME-148 ledger item. The attestation binds the plan to exactly
    this id; absence means the import map dropped the item — a hard blocker."""
    with psycopg.connect(**config.connection_kwargs("postgres")) as connection, connection.cursor() as cur:
        cur.execute("SELECT work_id FROM omp_work.work_aliases WHERE workspace_id=%s AND key=%s AND primary_alias", (str(workspace_id), PLAN_ITEM_ALIAS))
        row = cur.fetchone()
    if row is None:
        raise CutoverBlocked([f"imported ledger has no {PLAN_ITEM_ALIAS} alias — the plan stamp has no target"])
    return UUID(str(row[0]))


def _authority_row(config: OperationsConfig, workspace_id: UUID) -> dict[str, object] | None:
    with psycopg.connect(**config.connection_kwargs("postgres")) as connection, connection.cursor() as cur:
        cur.execute(
            "SELECT epoch_id, first_work_mutation_at, first_work_mutation_request_id, expected_first_request_id"
            " FROM omp_control.workspace_authority WHERE workspace_id=%s",
            (str(workspace_id),),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {"epoch_id": row[0], "first_work_mutation_at": row[1], "first_work_mutation_request_id": row[2], "expected_first_request_id": row[3]}


def _attest_plan(config: OperationsConfig, *, base_url: str, capability: Path, workspace_id: UUID, manifest: CutoverManifest, plan_artifact: str, operation_id: UUID, correlation_id: UUID, timeout: float = 10) -> dict[str, object]:
    """Submit the anointed first mutation and verify the store recorded exactly the
    nominated request carrying exactly the sealed plan hash. All envelope ids come
    from the persisted step record so a retry after a dropped response replays
    byte-identical bytes against the same operation_id."""
    envelope = CommandEnvelope(
        api_version="work.omp.dev/v1",
        workspace_id=workspace_id,
        operation_id=operation_id,
        request_id=manifest.first_mutation_request_id,
        correlation_id=correlation_id,
        command=AttestCutoverPlanCommand(
            type="attest_cutover_plan",
            payload=AttestCutoverPlanPayload(
                epoch_id=manifest.epoch_id,
                work_id=manifest.plan_work_id,
                plan_name=manifest.plan_name,
                plan_sha256=manifest.plan_sha256,
                plan_artifact=plan_artifact,
            ),
        ),
    )
    client = WorkClient(base_url, workspace_id, capability, timeout=timeout)
    response = client.execute(envelope)
    if response.receipt.state not in ("applied", "replayed"):
        raise CutoverBlocked([f"plan attestation rejected: {response.receipt.diagnostics}"])
    authority = _authority_row(config, workspace_id)
    if authority is None or authority["first_work_mutation_at"] is None:
        raise CutoverBlocked(["attestation applied but first_work_mutation_at was not stamped"])
    if str(authority["first_work_mutation_request_id"]) != str(manifest.first_mutation_request_id):
        raise CutoverBlocked(["first WorkService mutation was not the nominated attestation request"])
    return {"operation_id": str(operation_id), "request_id": str(manifest.first_mutation_request_id), "state": response.receipt.state}


def _focus_plan_item(
    config: OperationsConfig,
    *,
    base_url: str,
    workspace_id: UUID,
    owner_id: UUID,
    plan_work_id: UUID,
    frozen_at: datetime | None,
) -> None:
    """Read each focus input under a fresh deadline; recovery is read-only."""
    capability = config.config_dir / "capabilities" / "owner.json"
    timeout = 10.0 if frozen_at is None else min(10.0, _window_remaining_seconds(frozen_at))
    WorkClient(base_url, workspace_id, capability, timeout=timeout).work_item(PLAN_ITEM_ALIAS)
    timeout = 10.0 if frozen_at is None else min(10.0, _window_remaining_seconds(frozen_at))
    focus = WorkClient(base_url, workspace_id, capability, timeout=timeout).focus(owner_id)
    if focus.work_id == plan_work_id:
        return
    if frozen_at is None:
        raise CutoverBlocked([f"{PLAN_ITEM_ALIAS} is not focused during post-write recovery; no production mutation was attempted"])
    set_focus = CommandEnvelope(
        api_version="work.omp.dev/v1",
        workspace_id=workspace_id,
        operation_id=uuid4(),
        request_id=uuid4(),
        correlation_id=uuid4(),
        command=SetFocusCommand(
            type="set_focus",
            payload=SetFocusPayload(
                slot=FocusSlot(workspace_id=workspace_id, owner_id=owner_id, work_id=plan_work_id, version=focus.version + 1),
                expected_version=focus.version,
            ),
        ),
    )
    write_client = WorkClient(base_url, workspace_id, capability, timeout=min(10.0, _window_remaining_seconds(frozen_at)))
    focus_response = write_client.execute(set_focus)
    if focus_response.receipt.state not in ("applied", "replayed"):
        raise CutoverBlocked([f"focusing {PLAN_ITEM_ALIAS} failed: {focus_response.receipt.diagnostics}"])


def _installed_backend() -> str:
    extensions = Path.home() / ".omp" / "agent" / "extensions"
    if (extensions / "work-now.ts").exists() and not (extensions / "linear-now.ts").exists():
        return "work"
    if (extensions / "linear-now.ts").exists() and not (extensions / "work-now.ts").exists():
        return "linear"
    return "unknown"


# --- execute step ledger (AC-10): ids and inputs are persisted BEFORE first use ---
#
# A step entry is {"input_sha256", "state": "started"|"completed", ...ids, "output"?}.
# Resume rules: completed + same input → skip. started + same input → the step's own
# resolve path (envelope steps replay the persisted ids; others verify or rerun
# idempotently). A changed input refuses: the operator discards the window.

def _window_remaining_seconds(frozen_at: datetime) -> float:
    """Seconds left before the hard ceiling; nonpositive means the window is over.
    Recomputed at every call site so a slow step can never borrow time from one
    that ran before it."""
    remaining = WINDOW_HARD_LIMIT_MINUTES * 60 - (datetime.now(timezone.utc) - frozen_at).total_seconds()
    if remaining <= 0:
        raise CutoverBlocked([f"freeze window exceeded the T+{WINDOW_HARD_LIMIT_MINUTES}m hard deadline"])
    return remaining


def _enforce_window_deadline(frozen_at: datetime) -> None:
    """The one-hour hard ceiling. Call sites sit INSIDE execute's failure-handled
    region so the except path persists state, stops the cluster, and kills the
    service; pre-authority raises there also auto-unfreeze Linear."""
    _window_remaining_seconds(frozen_at)



def _enforce_cutover_deadline(frozen_at: datetime, *, post_write_recovery: bool) -> None:
    if not post_write_recovery:
        _enforce_window_deadline(frozen_at)

def _step_entry(window: dict[str, object], name: str) -> dict[str, object] | None:
    steps = window.setdefault("steps", {})
    assert isinstance(steps, dict)
    entry = steps.get(name)
    return entry if isinstance(entry, dict) else None


def _enforce_pre_activation_deadline(
    config: OperationsConfig,
    state: dict[str, object],
    window: dict[str, object],
    frozen_at: datetime,
    *,
    activation_committed: bool | None = None,
) -> None:
    """Auto-unfreeze at T+45 only while activation has never been submitted.

    A persisted submission may have committed before a crash; removing the marker
    then would create simultaneous Work and Linear authorities. Immediately before
    a first submission, a database check may prove that activation has not landed.
    """
    if datetime.now(timezone.utc) - frozen_at <= timedelta(minutes=WINDOW_LIMIT_MINUTES):
        return
    if _step_entry(window, "activation") is not None and activation_committed is not False:
        return
    _remove_freeze_marker()
    window["state"] = "failed"
    _save_state(config, state)
    raise CutoverBlocked([f"freeze window exceeded T+{WINDOW_LIMIT_MINUTES}m before activation — Linear auto-unfrozen; discard the candidate and rerun rehearsals"])


def _step_start(config: OperationsConfig, state: dict[str, object], window: dict[str, object], name: str, input_sha256: str, **fields: object) -> dict[str, object]:
    entry = _step_entry(window, name)
    if entry is not None:
        if entry.get("input_sha256") != input_sha256:
            raise CutoverBlocked([f"cutover step '{name}' input changed since the window opened — run rollback or discard the window"])
        return entry
    entry = {"input_sha256": input_sha256, "state": "started", **fields}
    steps = window.setdefault("steps", {})
    assert isinstance(steps, dict)
    steps[name] = entry
    _save_state(config, state)
    return entry


def _step_complete(config: OperationsConfig, state: dict[str, object], window: dict[str, object], name: str, output: dict[str, object] | None = None) -> None:
    entry = _step_entry(window, name)
    assert entry is not None
    entry["state"] = "completed"
    if output:
        entry["output"] = output
    _save_state(config, state)


def _authority_present(config: OperationsConfig, pgdata: Path, workspace_id: UUID) -> bool | None:
    """DB truth for the failure paths: is a Work authority row committed? None means
    unknown (cluster unreachable even after one restart) — callers must stay frozen."""
    for attempt in (False, True):
        try:
            return _authority_row(config, workspace_id) is not None
        except Exception:
            if attempt:
                return None
            try:
                _pg_start(pgdata, port=config.port, socket_dir=pgdata.parent, log=pgdata.parent / "pg-probe.log")
            except CutoverBlocked:
                return None
    return None


def _window_delta_export(config: OperationsConfig, workspace_id: UUID, frozen_at: datetime) -> ExportManifest:
    """The window's delta export, discovered from the ledger — never blindly re-run.
    A completed-but-never-imported delta is loaded; a running one is resumed; only a
    window with no delta row starts a new export. Re-running delta() after a crash
    would base on the crashed run and silently skip its changes."""
    with psycopg.connect(**config.connection_kwargs("postgres")) as connection, connection.cursor() as cur:
        cur.execute(
            "SELECT export_id, state FROM omp_integration.raw_exports"
            " WHERE workspace_id=%s AND mode='delta' AND source_started_at >= %s"
            " ORDER BY source_started_at DESC LIMIT 1",
            (str(workspace_id), frozen_at),
        )
        row = cur.fetchone()
    if row is None:
        return LinearExporter(config).delta(workspace_id)
    export_id, export_state = UUID(str(row[0])), str(row[1])
    if export_state == "complete":
        return load_manifest(config, export_id)
    if export_state == "running":
        return LinearExporter(config).resume(export_id)
    raise CutoverBlocked([f"window delta export {export_id} is in state '{export_state}' — resolve manually before resuming"])


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
        credential = refresh_credential(config.secret_path("linear-export.json"))
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

def _build_manifest(
    config: OperationsConfig,
    *,
    workspace_id: UUID,
    batch_id: UUID,
    smoke_results: list[CommandSmokeResult],
    freeze_at: datetime,
    backup_id: str,
    credential_sha256: str,
    plan: dict[str, str],
    first_mutation_request_id: UUID,
) -> CutoverManifest:
    """Assemble the exact CutoverManifest for a promoted batch from database state.
    Receipts bind the SPECIFIC post-delta backup (never 'latest of kind'), and the
    plan/credential/nominated-request fields make the first mutation provable."""
    with psycopg.connect(**config.connection_kwargs("postgres"), autocommit=True) as connection:
        connection.execute("SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)", (str(workspace_id), str(config.actor_id())))
        row = connection.execute(
            "SELECT e.source_boundary, e.source_watermark, e.raw_export_sha256, b.parity_hashes "
            "FROM omp_integration.import_batches b JOIN omp_integration.raw_exports e ON e.export_id = b.export_id "
            "WHERE b.workspace_id = %s AND b.batch_id = %s AND b.state = 'promoted'",
            (workspace_id, batch_id),
        ).fetchone()
        if row is None:
            raise CutoverBlocked([f"batch {batch_id} is not promoted"])
        boundary, source_watermark, raw_sha, parity = row
        blocking = connection.execute("SELECT count(*) FROM omp_integration.migration_anomalies WHERE workspace_id = %s AND batch_id = %s AND disposition = 'blocking'", (workspace_id, batch_id)).fetchone()[0]
        if blocking:
            raise CutoverBlocked([f"batch {batch_id} still carries {blocking} blocking anomalies"])
        if source_watermark is None:
            raise CutoverBlocked([f"batch {batch_id} export has no source watermark"])
        anomalies = [
            {"code": code, "disposition": disposition}
            for code, disposition in connection.execute(
                "SELECT code, disposition FROM omp_integration.migration_anomalies WHERE workspace_id = %s AND batch_id = %s AND disposition <> 'blocking' ORDER BY created_at",
                (workspace_id, batch_id),
            ).fetchall()
        ]
        receipts = dict(connection.execute(
            "SELECT kind, receipt_sha256 FROM omp_control.operations_evidence "
            "WHERE backup_id = %s AND kind IN ('backup','restore_drill') AND outcome LIKE 'passed%%'",
            (backup_id,),
        ).fetchall())
    missing = {"backup", "restore_drill"} - set(receipts)
    if missing:
        raise CutoverBlocked([f"backup {backup_id} is missing passed evidence receipts: {sorted(missing)}"])
    bundle = parity if isinstance(parity, dict) else json.loads(parity)
    return CutoverManifest.model_validate({
        "epoch_id": str(uuid4()),
        "contract_version": CONTRACT_VERSION,
        "contract_sha256": contract_sha256(),
        "schema_sha256": migration_set_sha256(),
        "transform_version": TRANSFORMATION_VERSION,
        "transform_sha256": transform_sha256(),
        "source_boundary": boundary.isoformat(),
        "source_watermark": source_watermark.isoformat(),
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
        "linear_credential_sha256": credential_sha256,
        "plan_name": plan["name"],
        "plan_sha256": plan["sha256"],
        "plan_work_id": plan["work_id"],
        "first_mutation_request_id": str(first_mutation_request_id),
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
    try:
        _run(["initdb", "-D", str(pgdata), "-U", "postgres", "--pwfile", str(pwfile), "--auth-host=scram-sha-256", "--auth-local=trust"])
    finally:
        # initdb is the only consumer; the superuser password must not linger in a
        # scratch tree the rehearsal otherwise retains.
        pwfile.unlink(missing_ok=True)
    _run(["pg_ctl", "-D", str(pgdata), "-w", "-l", str(pgdata.parent / "pg.log"), "-o", f"-p {port} -k {socket_dir} -c listen_addresses=127.0.0.1", "start"])


def _pg_status(pgdata: Path) -> int:
    return subprocess.run(["pg_ctl", "-D", str(pgdata), "status"], capture_output=True).returncode


def _pg_start(pgdata: Path, *, port: int, socket_dir: Path, log: Path) -> None:
    if _pg_status(pgdata) == 0:
        return
    _run(["pg_ctl", "-D", str(pgdata), "-w", "-l", str(log), "-o", f"-p {port} -k {socket_dir} -c listen_addresses=127.0.0.1", "start"])


def _pg_stop(pgdata: Path) -> None:
    if _pg_status(pgdata) != 0:
        return  # not running (stale postmaster.pid after a crash lands here too)
    _run(["pg_ctl", "-D", str(pgdata), "-m", "fast", "-w", "stop"])


def _pg_clone(src: Path, dst: Path) -> None:
    """Byte copy of a STOPPED cluster's data directory; clears a stale dst first."""
    if dst.exists():
        _pg_stop(dst)
        shutil.rmtree(dst)
    shutil.copytree(src, dst, symlinks=True)


def _scrub_rehearsal_scratch(scratch: Path, *, retain_candidate: bool) -> None:
    """Retain only the inactive candidate and encrypted source artifacts."""
    keep = {"candidate-pgdata", "data"} if retain_candidate else set()
    for child in scratch.iterdir():
        if child.name in keep:
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink(missing_ok=True)


def _invalidate_failed_candidate(
    config: OperationsConfig,
    state: dict[str, object],
    window: dict[str, object],
    scratch: Path,
    candidate_pgdata: Path,
) -> None:
    """Archive the poisoned export/import chain and require a fresh rehearsal 2."""
    _pg_stop(candidate_pgdata)
    archive = config.state_dir / "cutover-failed" / str(window["attempt_id"])
    archive.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if scratch.exists():
        os.replace(scratch, archive)
    window["failed_candidate_path"] = str(archive)
    window["candidate_pgdata"] = str(archive / candidate_pgdata.name)
    state.pop("candidate", None)
    rehearsals = state.get("rehearsals")
    if isinstance(rehearsals, dict):
        rehearsals.pop("2", None)


def _serve(config: OperationsConfig, http_port: int) -> subprocess.Popen[bytes]:
    project = Path(__file__).resolve().parents[3]
    env = {
        **os.environ,
        "XDG_CONFIG_HOME": str(config.config_dir.parent.parent),
        "XDG_STATE_HOME": str(config.state_dir.parent.parent),
        "XDG_DATA_HOME": str(config.data_dir.parent.parent),
        "OMP_WORK_POSTGRES_PORT": str(config.port),
        "OMP_WORK_S3_PREFIX": config.prefix,
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


def _start_managed_runtime(config: OperationsConfig, *, pgdata: Path, http_port: int, workspace_id: UUID) -> None:
    """Render, enable, and prove the durable PostgreSQL/WorkService units."""
    repo = Path(__file__).resolve().parents[5]
    _pg_stop(pgdata)
    _run(
        [
            "bash",
            str(repo / "infra" / "work-ledger" / "install.sh"),
            "--postgres-data",
            str(pgdata),
            "--postgres-port",
            str(config.port),
            "--http-port",
            str(http_port),
        ]
    )
    _run(["systemctl", "--user", "daemon-reload"])
    _run(["systemctl", "--user", "enable", "omp-work-postgres.service", "omp-work-service.service"])
    _run(["systemctl", "--user", "restart", "omp-work-postgres.service"])
    _run(["systemctl", "--user", "restart", "omp-work-service.service"])
    base = f"http://127.0.0.1:{http_port}"
    for _ in range(60):
        try:
            response = httpx.get(f"{base}/v1/health/ready", timeout=1)
            if response.status_code == 200 and response.json().get("ready") is True and _epoch_row(config, workspace_id) is not None:
                return
        except (httpx.HTTPError, psycopg.Error):
            pass
        time.sleep(0.5)
    raise CutoverBlocked(["managed WorkService did not become ready on the activated candidate"])


def _stop_managed_runtime() -> None:
    stop = subprocess.run(
        [
            "systemctl",
            "--user",
            "stop",
            "omp-work-service.service",
            "omp-work-postgres.service",
            "omp-work-backup.timer",
            "omp-work-wal.timer",
            "omp-work-restore-drill.timer",
        ],
        capture_output=True,
        text=True,
    )
    if stop.returncode != 0:
        raise CutoverBlocked([f"managed WorkService stop failed: {stop.stderr.strip()}"])
    disable = subprocess.run(
        [
            "systemctl",
            "--user",
            "disable",
            "omp-work-service.service",
            "omp-work-postgres.service",
            "omp-work-backup.timer",
            "omp-work-wal.timer",
            "omp-work-restore-drill.timer",
        ],
        capture_output=True,
        text=True,
    )
    if disable.returncode != 0:
        raise CutoverBlocked([f"managed WorkService disable failed: {disable.stderr.strip()}"])


def _seed_workspace_sql(config: OperationsConfig, workspace_id: UUID) -> None:
    """Provision the workspace root required by imported workspace-scoped rows."""
    _psql(config, f"INSERT INTO omp_control.workspaces(workspace_id) VALUES ('{workspace_id}') ON CONFLICT DO NOTHING")


def _seed_authority_sql(config: OperationsConfig, workspace_id: UUID, actor_id: UUID) -> None:
    """Test/rehearsal-only: authority without the cutover path, on disposable clones ONLY."""
    _seed_workspace_sql(config, workspace_id)
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
    results_path = work_dir / "results.json"
    env["OMP_WORK_SMOKE_RESULTS"] = str(results_path)
    output = _run(["bun", "run", str(repo / "session-system" / "tests" / "work-service-candidate-smoke.ts")], env=env, cwd=repo)
    last = [line for line in output.splitlines() if line.startswith("work-service-candidate-smoke:")]
    if not last or "PASS" not in last[-1]:
        raise CutoverBlocked([f"candidate smoke failed: {output.strip()[-500:]}"])
    # Machine-readable results, written by the wrapper as it asserts. A missing or
    # mismatched case set fails closed — the manifest must never bind assumed results.
    try:
        raw = json.loads(results_path.read_text(encoding="utf-8"))
        entries = raw["results"]
        results = [CommandSmokeResult(command_type=str(entry["command_type"]), passed=bool(entry["passed"])) for entry in entries]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise CutoverBlocked([f"smoke results file unreadable: {error}"]) from None
    names = {result.command_type for result in results}
    if names != EXPECTED_SMOKE_COMMANDS:
        raise CutoverBlocked([f"smoke results incomplete: got {sorted(names)}, need {sorted(EXPECTED_SMOKE_COMMANDS)}"])
    if not all(result.passed for result in results):
        raise CutoverBlocked(["smoke results report a failed command"])
    return results


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
    run_id = uuid4().hex
    rehearsal_prefix = f"{config.prefix}/cutover/rehearsals/{run_id}"
    rehearsals[str(ordinal)] = {"state": "running", "run_id": run_id, "prefix": rehearsal_prefix}
    _save_state(config, state)
    scratch = config.state_dir / f"cutover-rehearsal-{ordinal}"
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(mode=0o700, parents=True)
    xdg = scratch / "xdg"
    port, http_port = _free_port(), _free_port()
    scratch_config = replace(
        config,
        config_dir=xdg / "omp" / "work-ledger",
        state_dir=(scratch / "state") / "omp" / "work-ledger",
        data_dir=(scratch / "data") / "omp" / "work-ledger",
        port=port,
        prefix=rehearsal_prefix,
    )
    success = False
    delta_probe: ExportManifest | None = None
    stage = "scratch_setup"
    try:
        scratch_config.credentials_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        # The retained candidate uses production role secrets only while it is built;
        # successful handoff scrubs these copies and execute reads the live files.
        for source in config.credentials_dir.iterdir():
            if source.is_file():
                target = scratch_config.secret_path(source.name)
                shutil.copyfile(source, target)
                target.chmod(0o600)
        workspace_id, owner_id = scratch_config.workspace_id(), scratch_config.actor_id()
        pgdata = scratch / "pgdata"
        _pg_init(scratch_config, pgdata, port, scratch)
    except Exception:
        _scrub_rehearsal_scratch(scratch, retain_candidate=False)
        raise
    try:
        stage = "database_bootstrap"
        bootstrap(scratch_config)
        _seed_workspace_sql(scratch_config, workspace_id)
        stage = "full_export"
        export_manifest = LinearExporter(scratch_config).full(workspace_id)
        if any(anomaly.disposition == "blocking" for anomaly in export_manifest.anomalies):
            raise CutoverBlocked(["full export reported blocking anomalies"])
        if export_manifest.source_watermark is None:
            raise CutoverBlocked(["full export contains no source watermark"])
        importer = LinearImporter(scratch_config)
        stage = "import_stage"
        staged = importer.stage(workspace_id, export_manifest.export_id, mapping_file)
        stage = "import_reconcile"
        importer.reconcile(staged.batch_id)
        stage = "import_promote"
        promoted = importer.promote(staged.batch_id)
        stage = "backup_create"
        backup_id = backup.create(scratch_config)
        stage = "restore_drill"
        backup.restore_drill(scratch_config, reason="clean-instance", backup_id=backup_id)
        stage = "candidate_health"
        health = collect_health(scratch_config)
        if not health.ready:
            raise CutoverBlocked(["candidate health check is not ready — rehearsal cannot pass"])

        # Snapshot the pristine pre-activation candidate BEFORE anything activates.
        _pg_stop(pgdata)
        candidate_pgdata = scratch / "candidate-pgdata"
        _pg_clone(pgdata, candidate_pgdata)
        if ordinal == 2:
            # Probe the future-delta fast path on a disposable database clone. A delta
            # reports the merged snapshot, so equality with the full export — not zero
            # counts — proves no source changes. The retained import chain stays clean.
            probe_pgdata = scratch / "delta-probe-pgdata"
            _pg_clone(pgdata, probe_pgdata)
            probe_config = replace(scratch_config, port=_free_port())
            _pg_start(probe_pgdata, port=probe_config.port, socket_dir=scratch, log=scratch / "pg-delta-probe.log")
            try:
                delta_probe = LinearExporter(probe_config).delta(workspace_id)
                if (
                    delta_probe.dimension_counts != export_manifest.dimension_counts
                    or delta_probe.dimension_hashes != export_manifest.dimension_hashes
                    or any(anomaly.disposition == "blocking" for anomaly in delta_probe.anomalies)
                ):
                    raise CutoverBlocked(["post-promote delta changed the merged source snapshot"])
            finally:
                _pg_stop(probe_pgdata)
                shutil.rmtree(probe_pgdata, ignore_errors=True)

        # Clone A: authority-seeded disposable clone → real command smoke results.
        stage = "command_smoke"
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

        # Clone B: fresh pristine copy → REAL activate_cutover + the anointed
        stage = "activation_smoke"
        # attest_cutover_plan, verifying the recorded request/hash. The load-bearing
        # first-mutation path is fully exercised here, never first in production.
        plan_bytes = f"HOME-148 rehearsal plan (synthetic)\nrun={run_id}\nordinal={ordinal}\n".encode("utf-8")
        plan_sha = bytes_sha256(plan_bytes).hexdigest()
        artifact_root = config.data_dir / "cutover" / "rehearsals" / run_id
        artifact_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        artifact_staging = scratch / "artifact-staging"
        artifact_staging.mkdir(mode=0o700)
        plan_artifact, _, _ = write_json_artifact(
            artifact_root,
            artifact_staging,
            "plan",
            {"plan_name": "rehearsal-plan", "plan_sha256": plan_sha, "body": plan_bytes.decode("utf-8")},
            config.secret_path("gpg-passphrase"),
            data_dir=config.data_dir,
        )
        clone_b = scratch / "clone-b-pgdata"
        _pg_clone(pgdata, clone_b)
        port_b, http_b = _free_port(), _free_port()
        config_b = replace(scratch_config, port=port_b)
        _run(["pg_ctl", "-D", str(clone_b), "-w", "-l", str(scratch / "pg-b.log"), "-o", f"-p {port_b} -k {scratch} -c listen_addresses=127.0.0.1", "start"])
        try:
            provision_cutover(config_b, workspace_id=workspace_id, actor_id=owner_id)
            service_b = _serve(config_b, http_b)
            try:
                plan_work_id = _resolve_plan_work_id(config_b, workspace_id)
                manifest = _build_manifest(
                    config_b,
                    workspace_id=workspace_id,
                    batch_id=promoted.batch_id,
                    smoke_results=smoke,
                    freeze_at=datetime.now(timezone.utc),
                    backup_id=backup_id,
                    credential_sha256=bytes_sha256(_read_linear_key_bytes()).hexdigest(),
                    plan={"name": "rehearsal-plan", "sha256": plan_sha, "work_id": str(plan_work_id)},
                    first_mutation_request_id=uuid4(),
                )
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
                attestation = _attest_plan(
                    config_b,
                    base_url=f"http://127.0.0.1:{http_b}",
                    capability=config_b.config_dir / "capabilities" / "cutover.json",
                    workspace_id=workspace_id,
                    manifest=manifest,
                    plan_artifact=plan_artifact,
                    operation_id=uuid4(),
                    correlation_id=uuid4(),
                )
            finally:
                service_b.kill()
        finally:
            _pg_stop(clone_b)
        success = True
    except Exception as error:
        failure: dict[str, object] = {
            "verdict": "fail",
            "ordinal": ordinal,
            "run_id": run_id,
            "fingerprints": fingerprints,
            "prefix": rehearsal_prefix,
            "error": type(error).__name__,
            "stage": stage,
            "ran_at": datetime.now(timezone.utc).isoformat(),
        }
        if isinstance(error, CutoverBlocked):
            failure["blockers"] = error.blockers
        try:
            report_path, report_digest, report_ciphertext = _write_report(config, kind="rehearsals", run_id=run_id, payload=failure)
            failure["report"] = {"path": report_path, "sha256": report_digest, "ciphertext_sha256": report_ciphertext}
        except Exception as report_error:
            failure["report_error"] = type(report_error).__name__
        rehearsals[str(ordinal)] = failure
        _save_state(config, state)
        raise
    finally:
        if pgdata.exists() and (pgdata / "postmaster.pid").exists():
            _pg_stop(pgdata)
        _scrub_rehearsal_scratch(scratch, retain_candidate=success and retain_candidate)
    report_payload: dict[str, object] = {
        "run_id": run_id,
        "ordinal": ordinal,
        "fingerprints": fingerprints,
        "workspace_id": str(workspace_id),
        "export_id": str(export_manifest.export_id),
        "source_boundary": export_manifest.source_boundary.isoformat(),
        "source_watermark": export_manifest.source_watermark.isoformat(),
        "raw_export_sha256": export_manifest.raw_export_sha256,
        "batch_id": str(promoted.batch_id),
        "dimension_counts": export_manifest.dimension_counts.model_dump(),
        "dimension_hashes": export_manifest.dimension_hashes.model_dump(),
        "anomalies": [{"code": a.code, "disposition": a.disposition} for a in export_manifest.anomalies],
        "delta_probe": None if delta_probe is None else {
            "export_id": str(delta_probe.export_id),
            "base_export_id": str(delta_probe.base_export_id),
            "dimension_counts": delta_probe.dimension_counts.model_dump(),
            "dimension_hashes": delta_probe.dimension_hashes.model_dump(),
        },
        "backup_id": backup_id,
        "health_ready": health.ready,
        "smoke_results": [r.model_dump(mode="json") for r in smoke],
        "plan_sha256": plan_sha,
        "plan_artifact": plan_artifact,
        "attestation": attestation,
        "selector": "linear (untouched by rehearsal)",
        "recovery_decision": "n/a — rehearsal never activates the production candidate",
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }
    report_path, report_digest, report_ciphertext = _write_report(config, kind="rehearsals", run_id=run_id, payload=report_payload)
    verdict: dict[str, object] = {
        "verdict": "pass",
        "ordinal": ordinal,
        "run_id": run_id,
        "fingerprints": fingerprints,
        "batch_id": str(promoted.batch_id),
        "workspace_id": str(workspace_id),
        "report": {"path": report_path, "sha256": report_digest, "ciphertext_sha256": report_ciphertext},
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }
    rehearsals[str(ordinal)] = verdict
    if retain_candidate:
        state["candidate"] = {
            "pgdata": str(candidate_pgdata),
            "data_dir": str(scratch_config.data_dir),
            "prefix": scratch_config.prefix,
            "fingerprints": fingerprints,
            "batch_id": str(promoted.batch_id),
            "workspace_id": str(workspace_id),
            "owner_id": str(owner_id),
        }
    _save_state(config, state)
    return verdict


# --- execute ---

def execute(config: OperationsConfig, *, mapping_file: Path, plan_file: Path) -> dict[str, object]:
    """The freeze window. Order is load-bearing (single writer at every instant):
    freeze Linear (+drain) → final delta import → post-delta backup/restore/health →
    smoke on a disposable clone → activate the exact sealed manifest → selector to
    work → attest_cutover_plan as the anointed first mutation → readiness.
    Every step persists its ids/inputs before first use; a rerun resumes, it never
    duplicates. On failure the DB decides: authority absent → Linear auto-unfreezes;
    authority present or unknown → the marker stays and the window resumes."""
    state = _load_state(config)
    rehearsals: dict[str, object] = state.get("rehearsals", {})  # type: ignore[assignment]
    candidate = state.get("candidate")
    fingerprints = _fingerprints()
    existing = state.get("window")
    resuming = isinstance(existing, dict) and existing.get("state") in ("activating", "repair_required")
    if not resuming:
        blockers: list[str] = []
        first, second = rehearsals.get("1"), rehearsals.get("2")
        for ordinal in ("1", "2"):
            entry = rehearsals.get(ordinal)
            if not isinstance(entry, dict) or entry.get("verdict") != "pass":
                blockers.append(f"rehearsal {ordinal} has not passed")
        if isinstance(first, dict) and isinstance(second, dict):
            if first.get("batch_id") == second.get("batch_id") or first.get("run_id") == second.get("run_id"):
                blockers.append("rehearsals 1 and 2 are not distinct runs")
        if not isinstance(candidate, dict):
            blockers.append("no retained candidate from rehearsal 2")
        elif isinstance(second, dict) and candidate.get("batch_id") != second.get("batch_id"):
            blockers.append("retained candidate does not match rehearsal 2's batch")
        if isinstance(candidate, dict) and candidate.get("fingerprints") != fingerprints:
            blockers.append("candidate fingerprint drifted since rehearsal 2 — discard and rerun rehearsal 2")
        if freeze_marker_path().exists():
            blockers.append("linear freeze marker already present")
        if blockers:
            raise CutoverBlocked(blockers)
    assert isinstance(candidate, dict)
    candidate_pgdata = Path(str(candidate["pgdata"]))
    workspace_id = UUID(str(candidate["workspace_id"]))
    owner_id = UUID(str(candidate["owner_id"]))
    try:
        plan_bytes = plan_file.read_bytes()
    except OSError as error:
        raise CutoverBlocked([f"plan file unreadable: {error}"]) from None
    plan_sha = bytes_sha256(plan_bytes).hexdigest()
    credential_sha = bytes_sha256(_read_linear_key_bytes()).hexdigest()
    scratch = candidate_pgdata.parent
    scratch_config = replace(
        config,
        state_dir=scratch / "runtime-state",
        data_dir=Path(str(candidate.get("data_dir", config.data_dir))),
        port=_free_port() if not resuming else int(str(existing["port"])),  # type: ignore[index]
    )
    candidate_config = scratch_config
    http_port = int(str(existing["http_port"])) if resuming else WORKSERVICE_PORT  # type: ignore[index]
    window: dict[str, object] = dict(existing) if resuming else {  # type: ignore[arg-type]
        "state": "activating",
        "candidate_pgdata": str(candidate_pgdata),
        "port": candidate_config.port,
        "http_port": http_port,
        "plan_sha256": plan_sha,
        "credential_sha256": credential_sha,
        "attempt_id": str(uuid4()),
        "steps": {},
    }
    if window.get("plan_sha256") != plan_sha:
        raise CutoverBlocked(["plan file changed since the window opened — run rollback or discard the window"])
    if window.get("credential_sha256") != credential_sha:
        raise CutoverBlocked(["personal Linear key changed since the window opened — discard the window and rerun rehearsal 2"])
    state["window"] = window
    _save_state(config, state)
    try:
        runtime = _step_entry(window, "managed_runtime")
        if runtime is not None:
            managed_port = int(str(window.get("managed_port", config.port)))
            candidate_config = replace(config, port=managed_port)
            provision_owner(candidate_config, workspace_id=workspace_id, owner_id=owner_id, base_url=f"http://127.0.0.1:{http_port}")
            provision_cutover(candidate_config, workspace_id=workspace_id, actor_id=owner_id, rotate=True)
            _start_managed_runtime(candidate_config, pgdata=candidate_pgdata, http_port=http_port, workspace_id=workspace_id)
            window["port"] = managed_port
            _save_state(config, state)
            if runtime.get("state") != "completed":
                _step_complete(
                    config,
                    state,
                    window,
                    "managed_runtime",
                    {
                        "postgres_unit": "omp-work-postgres.service",
                        "service_unit": "omp-work-service.service",
                        "pgdata": str(candidate_pgdata),
                        "postgres_port": managed_port,
                        "http_port": http_port,
                    },
                )
        else:
            _pg_start(candidate_pgdata, port=candidate_config.port, socket_dir=scratch, log=scratch / "pg-final.log")
        authority = _authority_row(candidate_config, workspace_id)
        post_write_recovery = authority is not None and authority["first_work_mutation_at"] is not None
        if post_write_recovery:
            window["state"] = "repair_required"
            _save_state(config, state)
        if _epoch_row(candidate_config, workspace_id) is not None and not resuming:
            raise CutoverBlocked(["candidate database already carries an epoch"])

        # 1. Freeze Linear (+drain). From here the TS fence refuses Linear mutations.
        freeze_input = sha256({"workspace_id": str(workspace_id), "plan_sha256": plan_sha})
        entry = _step_start(config, state, window, "freeze", freeze_input)
        if entry["state"] != "completed":
            frozen_at = datetime.now(timezone.utc)
            _write_freeze_marker({"frozen_at": frozen_at.isoformat(), "workspace_id": str(workspace_id), "actor": "owner"})
            window["frozen_at"] = frozen_at.isoformat()
            _save_state(config, state)
            time.sleep(FREEZE_DRAIN_SECONDS)
            _step_complete(config, state, window, "freeze", {"frozen_at": frozen_at.isoformat(), "drain_seconds": FREEZE_DRAIN_SECONDS})
        frozen_at = datetime.fromisoformat(str(window["frozen_at"]))
        _enforce_cutover_deadline(frozen_at, post_write_recovery=post_write_recovery)  # resume entry

        # 2. Final delta export + import (importer role bypasses the app fence).
        entry = _step_start(config, state, window, "final_delta", sha256({"frozen_at": frozen_at.isoformat()}))
        if entry["state"] != "completed":
            export_manifest = _window_delta_export(candidate_config, workspace_id, frozen_at)
            if any(anomaly.disposition == "blocking" for anomaly in export_manifest.anomalies):
                raise CutoverBlocked(["final delta export reported blocking anomalies"])
            importer = LinearImporter(candidate_config)
            staged = importer.stage(workspace_id, export_manifest.export_id, mapping_file)
            importer.reconcile(staged.batch_id)
            promoted = importer.promote(staged.batch_id)
            _step_complete(config, state, window, "final_delta", {"batch_id": str(promoted.batch_id), "export_id": str(export_manifest.export_id)})
        _enforce_cutover_deadline(frozen_at, post_write_recovery=post_write_recovery)  # after the long import
        batch_id = UUID(str(_step_entry(window, "final_delta")["output"]["batch_id"]))  # type: ignore[index]

        # 3. Post-delta safety: backup + restore drill + health on the final content.
        entry = _step_start(config, state, window, "post_delta_safety", sha256({"batch_id": str(batch_id)}), operation_id=str(uuid4()))
        if entry["state"] != "completed":
            backup_id = backup.create(candidate_config)
            backup.restore_drill(candidate_config, reason="pre-activation-final", backup_id=backup_id)
            health = collect_health(candidate_config)
            if not health.ready:
                raise CutoverBlocked(["post-delta candidate health check is not ready"])
            _step_complete(config, state, window, "post_delta_safety", {"backup_id": backup_id})
        _enforce_cutover_deadline(frozen_at, post_write_recovery=post_write_recovery)  # after backup + restore drill
        backup_id = str(_step_entry(window, "post_delta_safety")["output"]["backup_id"])  # type: ignore[index]

        # 4. Smoke on a disposable authority-seeded clone; real results feed the manifest.
        entry = _step_start(config, state, window, "smoke", sha256({"batch_id": str(batch_id), "backup_id": backup_id}))
        if entry["state"] != "completed":
            _pg_stop(candidate_pgdata)
            clone = scratch / "execute-smoke-pgdata"
            _pg_clone(candidate_pgdata, clone)
            clone_config = replace(scratch_config, port=_free_port())
            clone_http = _free_port()
            _pg_start(clone, port=clone_config.port, socket_dir=scratch, log=scratch / "pg-smoke.log")
            try:
                _seed_authority_sql(clone_config, workspace_id, owner_id)
                write_capability(clone_config, "owner", actor_id=owner_id, actor_kind="owner", workspaces=(workspace_id,), scopes=OWNER_SCOPES)
                smoke_service = _serve(clone_config, clone_http)
                try:
                    smoke = _run_smoke(clone_config, base_url=f"http://127.0.0.1:{clone_http}", workspace_id=workspace_id, owner_id=owner_id, work_dir=scratch / "smoke-final")
                finally:
                    smoke_service.kill()
            finally:
                _pg_stop(clone)
            shutil.rmtree(clone, ignore_errors=True)
            _pg_start(candidate_pgdata, port=candidate_config.port, socket_dir=scratch, log=scratch / "pg-final.log")
            _step_complete(config, state, window, "smoke", {"results": [r.model_dump(mode="json") for r in smoke]})
        _enforce_cutover_deadline(frozen_at, post_write_recovery=post_write_recovery)  # after the smoke run
        smoke = [CommandSmokeResult.model_validate(r) for r in _step_entry(window, "smoke")["output"]["results"]]  # type: ignore[index]

        # 5. Window deadline: T+45 auto-rollback is legal only when activation
        # has never started. Started/completed activation stays fenced and resumes.
        _enforce_pre_activation_deadline(config, state, window, frozen_at)

        # 6. Activate the exact sealed manifest. The manifest is persisted BEFORE the
        #    first submission; a resume replays the persisted ids byte-identically.
        entry = _step_entry(window, "activation")
        if entry is None:
            plan_work_id = _resolve_plan_work_id(candidate_config, workspace_id)
            manifest = _build_manifest(
                candidate_config,
                workspace_id=workspace_id,
                batch_id=batch_id,
                smoke_results=smoke,
                freeze_at=frozen_at,
                backup_id=backup_id,
                credential_sha256=credential_sha,
                plan={"name": plan_file.name, "sha256": plan_sha, "work_id": str(plan_work_id)},
                first_mutation_request_id=uuid4(),
            )
            entry = _step_start(
                config, state, window, "activation",
                sha256(json.loads(manifest.model_dump_json())),
                operation_id=str(uuid4()), request_id=str(uuid4()), correlation_id=str(uuid4()),
                manifest=json.loads(manifest.model_dump_json()),
            )
        manifest = CutoverManifest.model_validate(entry["manifest"])
        if entry["state"] != "completed":
            provision_cutover(candidate_config, workspace_id=workspace_id, actor_id=owner_id, rotate=True)
            service = _serve(candidate_config, http_port)
            try:
                envelope = CommandEnvelope(
                    api_version="work.omp.dev/v1",
                    workspace_id=workspace_id,
                    operation_id=UUID(str(entry["operation_id"])),
                    request_id=UUID(str(entry["request_id"])),
                    correlation_id=UUID(str(entry["correlation_id"])),
                    command=ActivateCutoverCommand(type="activate_cutover", payload=ActivateCutoverPayload(manifest=manifest)),
                )
                client = WorkClient(f"http://127.0.0.1:{http_port}", workspace_id, candidate_config.config_dir / "capabilities" / "cutover.json")
                if "submission_started_at" not in entry:
                    _enforce_pre_activation_deadline(
                        config,
                        state,
                        window,
                        frozen_at,
                        activation_committed=_epoch_row(candidate_config, workspace_id) is not None,
                    )
                    entry["submission_started_at"] = datetime.now(timezone.utc).isoformat()
                    _save_state(config, state)
                response = client.execute(envelope)
                if response.receipt.state not in ("applied", "replayed"):
                    raise CutoverBlocked([f"activation rejected: {response.receipt.diagnostics}"])
            finally:
                service.kill()
            if _epoch_row(candidate_config, workspace_id) is None:
                raise CutoverBlocked(["activation reported success but no epoch row exists"])
            _step_complete(config, state, window, "activation", {"epoch_id": str(manifest.epoch_id)})
        # 7. Hand the authoritative candidate to durable user services before the
        # selector or first mutation can depend on it.
        final_config = config
        window["managed_port"] = final_config.port
        window["http_port"] = http_port
        _save_state(config, state)
        runtime_entry = _step_start(
            config,
            state,
            window,
            "managed_runtime",
            sha256({"epoch_id": str(manifest.epoch_id), "pgdata": str(candidate_pgdata), "postgres_port": final_config.port, "http_port": http_port}),
        )
        provision_owner(final_config, workspace_id=workspace_id, owner_id=owner_id, base_url=f"http://127.0.0.1:{http_port}")
        provision_cutover(final_config, workspace_id=workspace_id, actor_id=owner_id, rotate=True)
        candidate_config = final_config
        _start_managed_runtime(final_config, pgdata=candidate_pgdata, http_port=http_port, workspace_id=workspace_id)
        window["port"] = final_config.port
        _save_state(config, state)
        if runtime_entry["state"] != "completed":
            _step_complete(
                config,
                state,
                window,
                "managed_runtime",
                {
                    "postgres_unit": "omp-work-postgres.service",
                    "service_unit": "omp-work-service.service",
                    "pgdata": str(candidate_pgdata),
                    "postgres_port": final_config.port,
                    "http_port": http_port,
                },
            )


        # 8. Selector to work; the installed client points at the managed service.
        entry = _step_start(config, state, window, "selector_work", sha256({"epoch_id": str(manifest.epoch_id)}))
        if entry["state"] != "completed":
            _run_selector("work")
            _expect_backend("work")
            _step_complete(config, state, window, "selector_work")

        # 8. The anointed first mutation: attest the approved plan onto HOME-148.
        #    Irreversible (rollback closes after it) — the ceiling is re-checked
        #    immediately before it lands.
        _enforce_cutover_deadline(frozen_at, post_write_recovery=post_write_recovery)
        entry = _step_entry(window, "attestation")
        if entry is None:
            entry = _step_start(config, state, window, "attestation", sha256({"epoch_id": str(manifest.epoch_id), "plan_sha256": plan_sha}), operation_id=str(uuid4()), correlation_id=str(uuid4()))
        if entry["state"] != "completed":
            artifact_root = config.data_dir / "cutover" / "epochs" / str(manifest.epoch_id)
            artifact_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            artifact_staging = config.state_dir / "cutover-staging"
            artifact_staging.mkdir(mode=0o700, parents=True, exist_ok=True)
            plan_artifact, _, _ = write_json_artifact(
                artifact_root,
                artifact_staging,
                "plan",
                {"plan_name": plan_file.name, "plan_sha256": plan_sha, "body": plan_bytes.decode("utf-8")},
                config.secret_path("gpg-passphrase"),
                data_dir=config.data_dir,
            )
            if post_write_recovery:
                if authority is None or str(authority["first_work_mutation_request_id"]) != str(manifest.first_mutation_request_id):
                    raise CutoverBlocked(["database first-mutation stamp does not match the nominated plan attestation"])
                attestation = {
                    "operation_id": str(entry["operation_id"]),
                    "request_id": str(manifest.first_mutation_request_id),
                    "state": "recovered",
                }
            else:
                cutover_capability = final_config.config_dir / "capabilities" / "cutover.json"
                # The request itself is bounded by the window time still remaining —
                # recomputed immediately before the managed-service call.
                attestation = _attest_plan(
                    final_config,
                    base_url=f"http://127.0.0.1:{http_port}",
                    capability=cutover_capability,
                    workspace_id=workspace_id,
                    manifest=manifest,
                    plan_artifact=plan_artifact,
                    operation_id=UUID(str(entry["operation_id"])),
                    correlation_id=UUID(str(entry["correlation_id"])),
                    timeout=_window_remaining_seconds(frozen_at),
                )
            _step_complete(config, state, window, "attestation", {**attestation, "plan_artifact": plan_artifact})
        attestation_entry = _step_entry(window, "attestation")
        attestation_output = attestation_entry.get("output") if attestation_entry is not None else None
        if not isinstance(attestation_output, dict) or not isinstance(attestation_output.get("request_id"), str):
            raise CutoverBlocked(["persisted plan attestation receipt is missing its request id"])
        attestation = dict(attestation_output)
        _enforce_cutover_deadline(frozen_at, post_write_recovery=post_write_recovery)  # after the anointed first mutation

        # 9. Readiness: mutation smoke on a disposable post-attestation clone (the
        #    production ledger carries no probe writes), read-only checks on prod,
        #    and focus on HOME-148 — the only post-attestation prod mutation.
        entry = _step_start(config, state, window, "readiness", sha256({"epoch_id": str(manifest.epoch_id), "request_id": attestation["request_id"]}))
        if entry["state"] != "completed":
            clone = scratch / "execute-readiness-pgdata"
            if clone.exists():
                _pg_stop(clone)
                shutil.rmtree(clone)
            backup.clone_primary(final_config, clone)
            clone_config = replace(scratch_config, port=_free_port())
            clone_http = _free_port()
            _pg_start(clone, port=clone_config.port, socket_dir=scratch, log=scratch / "pg-readiness.log")
            try:
                write_capability(clone_config, "owner", actor_id=owner_id, actor_kind="owner", workspaces=(workspace_id,), scopes=OWNER_SCOPES)
                clone_service = _serve(clone_config, clone_http)
                try:
                    readiness_smoke = _run_smoke(clone_config, base_url=f"http://127.0.0.1:{clone_http}", workspace_id=workspace_id, owner_id=owner_id, work_dir=scratch / "smoke-readiness")
                finally:
                    clone_service.kill()
            finally:
                _pg_stop(clone)
            shutil.rmtree(clone, ignore_errors=True)
            # The clone smoke may have crossed the ceiling; the production focus
            # mutation below is the last irreversible write — gate it.
            _enforce_cutover_deadline(frozen_at, post_write_recovery=post_write_recovery)
            _focus_plan_item(final_config, base_url=f"http://127.0.0.1:{http_port}", workspace_id=workspace_id, owner_id=owner_id, plan_work_id=manifest.plan_work_id, frozen_at=None if post_write_recovery else frozen_at)
            _step_complete(config, state, window, "readiness", {"item": PLAN_ITEM_ALIAS, "focused": True, "clone_smoke": [r.command_type for r in readiness_smoke]})
        entry = _step_start(config, state, window, "backup_automation", sha256({"epoch_id": str(manifest.epoch_id)}))
        if entry["state"] != "completed":
            _run(
                [
                    "systemctl",
                    "--user",
                    "enable",
                    "--now",
                    "omp-work-backup.timer",
                    "omp-work-wal.timer",
                    "omp-work-restore-drill.timer",
                ]
            )
            _step_complete(config, state, window, "backup_automation", {"timers": ["omp-work-backup.timer", "omp-work-wal.timer", "omp-work-restore-drill.timer"]})
        # Terminal ceiling, inside the failure-handled region. Pre-write failure
        # archives the candidate and returns to Linear. Post-write recovery ignores
        # the elapsed wall clock but remains frozen until finalize or explicit repair.
        _enforce_cutover_deadline(frozen_at, post_write_recovery=post_write_recovery)
    except Exception:
        present = _authority_present(candidate_config, candidate_pgdata, workspace_id)
        if present is False:
            # Proven pre-activation: Linear is still the sole authority. Archive the
            # now-poisoned delta chain and force a fresh rehearsal 2 before retry.
            _remove_freeze_marker()
            window["state"] = "failed"
            _invalidate_failed_candidate(config, state, window, scratch, candidate_pgdata)
        else:
            # Authority present or unknown: Linear stays frozen; repair/resume only.
            window["state"] = "repair_required" if present is True else "activating"
        _save_state(config, state)
        raise

    overrun = datetime.now(timezone.utc) - frozen_at
    window["state"] = "executed"
    window["executed_at"] = datetime.now(timezone.utc).isoformat()
    _save_state(config, state)
    return {
        "epoch_id": str(manifest.epoch_id),
        "authority": "work",
        "plan_attestation": attestation,
        "window_minutes": round(overrun.total_seconds() / 60, 1),
        "managed_service": "omp-work-service.service",
    }


# --- finalize ---

def finalize(config: OperationsConfig) -> dict[str, object]:
    """After the owner revokes the personal Linear key in the browser. Every gate is
    strict: anything short of proven keeps the epoch active and the report unwritten.
    Order: probes → encrypted final report → seal (report hash + revocation + derived
    recovery path) → delete the key file LAST. A crash anywhere is resumable: an
    already-sealed epoch is proof the probes passed, so only cleanup is redone."""
    state = _load_state(config)
    window = state.get("window")
    if not isinstance(window, dict) or window.get("state") != "executed":
        raise CutoverBlocked(["no executed cutover window to finalize"])
    candidate = state.get("candidate")
    if not isinstance(candidate, dict):
        raise CutoverBlocked(["no candidate recorded"])
    workspace_id = UUID(str(candidate["workspace_id"]))
    env_path = Path.home() / ".config" / "linear.env"
    epoch_config = replace(config, port=int(window["port"]))
    epoch = _epoch_row(epoch_config, workspace_id)
    if epoch is None:
        raise CutoverBlocked(["no epoch row for the executed window"])
    already_sealed = epoch["state"] == "sealed" and epoch.get("revoked_at") and epoch.get("final_report_sha256")
    if not already_sealed:
        if epoch["state"] != "active":
            raise CutoverBlocked([f"epoch is '{epoch['state']}', not active — nothing to finalize"])
        blockers: list[str] = []
        key_status = _personal_key_status()
        if key_status != "revoked":
            blockers.append(f"personal Linear API key status is '{key_status}' — revoke it in Linear API settings before finalize")
        try:
            _expect_backend("work")
        except CutoverBlocked:
            blockers.append("installed backend is not 'work'")
        if not freeze_marker_path().exists():
            blockers.append("freeze marker missing — Linear is unfrozen outside the rollback path")
        # The OAuth read-only credential must still answer Linear positively.
        project_ids: set[str] = set()
        try:
            oauth = refresh_credential(config.secret_path("linear-export.json"))
            project_ids = _linear_project_ids(oauth.access_token.get_secret_value())
            if not project_ids:
                blockers.append("OAuth read-only credential sees no Linear projects")
        except Exception as error:
            blockers.append(f"OAuth read-only probe failed: {error}")
        # The sealed epoch must be the manifest this window activated, bound to the
        # personal key that is still on disk.
        steps = window.get("steps")
        activation = steps.get("activation") if isinstance(steps, dict) else None
        if isinstance(activation, dict) and isinstance(activation.get("manifest"), dict):
            if sha256(activation["manifest"]) != epoch["candidate_manifest_sha256"]:
                blockers.append("window manifest does not match the sealed epoch manifest")
        if window.get("credential_sha256"):
            try:
                if bytes_sha256(_read_linear_key_bytes()).hexdigest() != window["credential_sha256"]:
                    blockers.append("personal Linear key on disk changed since activation")
            except CutoverBlocked:
                blockers.append("personal Linear key file unreadable during finalize")
        if blockers:
            raise CutoverBlocked(blockers)
        attestation = steps.get("attestation") if isinstance(steps, dict) else None  # type: ignore[union-attr]
        revoked_at = datetime.now(timezone.utc)
        report_relative, report_digest, _ = _write_report(
            config,
            kind="finalize",
            run_id=str(epoch["epoch_id"]),
            payload={
                "epoch_id": str(epoch["epoch_id"]),
                "candidate_manifest_sha256": epoch["candidate_manifest_sha256"],
                "window": {k: window.get(k) for k in ("plan_sha256", "credential_sha256", "frozen_at", "executed_at")},
                "attestation": attestation.get("output") if isinstance(attestation, dict) else None,
                "smoke_commands": sorted(EXPECTED_SMOKE_COMMANDS),
                "selector": _installed_backend(),
                "personal_key": key_status,
                "oauth_project_count": len(project_ids),
                "revoked_at": revoked_at.isoformat(),
            },
        )
        recovery_path = "post_write" if epoch["first_work_mutation_at"] else "pre_write"
        # Seal in one parameterized statement and DEMAND the row: if a concurrent
        # rollback or an earlier seal won the race, rowcount is 0 and the key file
        # must NOT be deleted for an epoch that did not seal.
        with psycopg.connect(**epoch_config.connection_kwargs("postgres")) as connection, connection.cursor() as cur:
            cur.execute(
                "UPDATE omp_control.cutover_epochs e SET state='sealed', revoked_at=%s, recovery_path=%s, final_report_sha256=%s "
                "FROM omp_control.workspace_authority a "
                "WHERE e.epoch_id=%s AND e.state='active' AND a.epoch_id=e.epoch_id "
                "AND a.workspace_id=e.workspace_id AND a.first_work_mutation_at IS NOT NULL",
                (revoked_at.isoformat(), recovery_path, report_digest, str(epoch["epoch_id"])),
            )
            if cur.rowcount != 1:
                raise CutoverBlocked(["epoch is no longer active or lacks the committed first mutation — seal refused; the key file stays"])
        report: dict[str, object] = {"path": report_relative, "sha256": report_digest, "recovery_path": recovery_path}
    else:
        report = {"sealed": True, "revoked_at": str(epoch["revoked_at"])}
    # Key file removed only after revocation is proven AND the epoch is sealed.
    env_path.unlink(missing_ok=True)
    window["state"] = "finalized"
    window["finalized_at"] = datetime.now(timezone.utc).isoformat()
    _save_state(config, state)
    return {"epoch_id": str(epoch["epoch_id"]), "state": "sealed", "personal_key": "revoked", "report": report}


# --- rollback ---

def _rollback_epoch(config: OperationsConfig, epoch_id: object, workspace_id: UUID) -> None:
    """One parameterized transaction: epoch rolls back and Work authority disappears
    together. Refuses unless the epoch is still active — a concurrent seal or an
    earlier rollback turns this into a hard error, not a silent no-op. The authority
    row is locked FIRST and the first-mutation stamp re-checked inside the lock:
    an attestation racing this transaction either lands first (stamp visible →
    refuse) or blocks on the lock until the delete commits (no authority left to
    stamp). The Python-side check in rollback() is a fast path only; this guard is
    the correctness boundary."""
    with psycopg.connect(**config.connection_kwargs("postgres")) as connection, connection.cursor() as cur:
        cur.execute(
            "SELECT first_work_mutation_at FROM omp_control.workspace_authority WHERE workspace_id=%s FOR UPDATE",
            (str(workspace_id),),
        )
        authority = cur.fetchone()
        if authority is None:
            raise CutoverBlocked(["no Work authority row — nothing to roll back"])
        if authority[0] is not None:
            raise CutoverBlocked(["rollback refused: the first WorkService mutation already landed — repair/restore is the only path"])
        cur.execute(
            "UPDATE omp_control.cutover_epochs SET state='rolled_back', recovery_path='pre_write' WHERE epoch_id=%s AND state='active'",
            (str(epoch_id),),
        )
        if cur.rowcount != 1:
            raise CutoverBlocked(["epoch is no longer active — rollback refused"])
        cur.execute("DELETE FROM omp_control.workspace_authority WHERE workspace_id=%s", (str(workspace_id),))


def rollback(config: OperationsConfig) -> dict[str, object]:
    """Pre-first-mutation rollback to Linear. Order is load-bearing: Work authority is
    removed while Linear stays frozen, the selector switches back, and the freeze
    marker is removed LAST. The marker is never touched until the DB proves no
    active authority remains."""
    state = _load_state(config)
    candidate = state.get("candidate")
    window = state.get("window")
    runtime = _step_entry(window, "managed_runtime") if isinstance(window, dict) else None
    epoch = None
    epoch_config = config
    epoch_port: int | None = None
    managed_port: int | None = None
    if isinstance(candidate, dict) and isinstance(window, dict) and "port" in window:
        current_port = int(str(window["port"]))
        if runtime is not None:
            managed_port = int(str(window.get("managed_port", config.port)))
        ports = [current_port]
        if managed_port is not None and managed_port != current_port:
            ports.append(managed_port)
        pgdata = Path(str(window.get("candidate_pgdata", "")))
        reachable = False
        for port in ports:
            probe_config = replace(config, port=port)
            try:
                probed = _epoch_row(probe_config, UUID(str(candidate["workspace_id"])))
            except Exception:
                continue
            reachable = True
            if probed is not None:
                epoch = probed
                epoch_config = probe_config
                epoch_port = port
                break
        if epoch is None and not reachable:
            # One restart attempt on the last persisted live port. If the cluster
            # stays unreachable the freeze marker remains and execute can converge a
            # started managed-runtime handoff onto managed_port.
            if not pgdata.exists():
                raise CutoverBlocked(["candidate PostgreSQL data directory is missing — freeze marker left in place"])
            _pg_stop(pgdata)
            _pg_start(pgdata, port=current_port, socket_dir=pgdata.parent, log=pgdata.parent / "pg-rollback.log")
            epoch_config = replace(config, port=current_port)
            epoch = _epoch_row(epoch_config, UUID(str(candidate["workspace_id"])))
            epoch_port = current_port if epoch is not None else None
    if epoch is not None:
        if epoch["state"] == "active" and not epoch["first_work_mutation_at"]:
            _rollback_epoch(epoch_config, epoch["epoch_id"], UUID(str(candidate["workspace_id"])))  # type: ignore[possibly-undefined]
        elif epoch["state"] == "rolled_back" and not epoch["authority_present"]:
            # Resume: a previous rollback committed the DB phase (epoch rolled back,
            # authority deleted) but died before selector/unfreeze. Redo only those.
            pass
        else:
            # active+stamped, sealed, or rolled_back with an authority row still
            # present (inconsistent — never produced by _rollback_epoch): fail closed.
            raise CutoverBlocked(["rollback refused: the first WorkService mutation already landed or the epoch is in an unexpected state — repair/restore is the only path"])
    if epoch is None and freeze_marker_path().exists():
        raise CutoverBlocked(["freeze marker present but no epoch row found — verify no Work authority exists before removing the marker manually"])
    if runtime is not None and managed_port is not None and epoch_port == managed_port:
        _stop_managed_runtime()
    elif isinstance(window, dict):
        pgdata = Path(str(window.get("candidate_pgdata", "")))
        if pgdata.exists():
            _pg_stop(pgdata)
    _run_selector("linear")
    try:
        _expect_backend("linear")
    except CutoverBlocked:
        raise CutoverBlocked(["selector switch did not restore the linear backend — freeze marker left in place"]) from None
    _remove_freeze_marker()
    rolled = {**(window if isinstance(window, dict) else {}), "state": "rolled_back", "rolled_back_at": datetime.now(timezone.utc).isoformat()}
    state["window"] = rolled
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
    probe_failed = False
    candidate = state.get("candidate")
    window = state.get("window")
    if isinstance(candidate, dict):
        port = int(window["port"]) if isinstance(window, dict) and "port" in window else config.port
        try:
            row = _epoch_row(replace(config, port=port), UUID(str(candidate["workspace_id"])))
        except Exception as error:  # cluster may be stopped; unknown is safer than guessing Linear
            probe_failed = True
            row = {"error": f"{type(error).__name__}: {error}"}
        if row is not None:
            epoch = {
                "epoch_id": str(row.get("epoch_id", "")),
                "state": row.get("state"),
                "authority_present": row.get("authority_present"),
                "first_work_mutation_at": str(row["first_work_mutation_at"]) if row.get("first_work_mutation_at") else None,
                "first_work_mutation_request_id": str(row["first_work_mutation_request_id"]) if row.get("first_work_mutation_request_id") else None,
                "revoked_at": str(row["revoked_at"]) if row.get("revoked_at") else None,
                "candidate_manifest_sha256": row.get("candidate_manifest_sha256"),
                "final_report_sha256": row.get("final_report_sha256"),
                "error": row.get("error"),
            }
    report["epoch"] = epoch
    if probe_failed or (freeze_marker_path().exists() and epoch is None):
        report["authority"] = "unknown"
    elif isinstance(epoch, dict) and epoch.get("state") in ("active", "sealed") and epoch.get("authority_present"):
        report["authority"] = "work"
    else:
        report["authority"] = "linear"
    return report

"""Workers have no direct tier 2/3 egress path (OMP-403).

Standing ``network_access`` still grants reads. It does not grant git
receive-pack, and it does not grant a non-GET/HEAD or bodied request to a
project remote host. The sandbox case runs the same two actions as a worker
command and records the refusals.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from omp_work.action_tiers import TIER2, TIER3
from omp_work.egress_gateway import EgressGateway
from omp_work.egress_policy import (
    Identity,
    MemoryRecorder,
    ProjectEgress,
    Request,
    Verdict,
    build_policy,
    decide,
)
from omp_work.egress_sandbox import SandboxUnavailable, run_sandboxed
from omp_work.standing_policy import StandingPolicy

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
REMOTE = "https://git.example.com/org/repo"
GIT = "git.example.com"
PARTNER = "api.partner.test"

# action class, method, host, path, query, has_body, code, klass
_REFUSED: tuple[tuple[str, str, str, str, str, bool, str, str], ...] = (
    ("push_branch", "POST", GIT, "/org/repo.git/git-receive-pack", "", True, "git_receive_pack", "standing"),
    ("create_pull_request", "POST", "api.github.com", "/repos/org/repo/pulls", "", True, "destination_not_allowed", "none"),
    ("nonprod_update", "PUT", "deploy.example.com", "/apps/demo", "", True, "destination_not_allowed", "none"),
    ("spend_beyond_threshold", "POST", "billing.example.com", "/charges", "", True, "destination_not_allowed", "none"),
    ("network_access", "POST", "other.example.net", "/open", "", True, "destination_not_allowed", "none"),
    ("disposable_cloud", "POST", "ec2.amazonaws.com", "/", "", True, "destination_not_allowed", "none"),
    (
        "merge_protected_branch",
        "GET",
        GIT,
        "/org/repo.git/info/refs",
        "service=git-receive-pack",
        False,
        "git_receive_pack",
        "standing",
    ),
    ("production_deploy", "POST", "prod.example.com", "/deploy", "", True, "destination_not_allowed", "none"),
    ("destructive_infra", "DELETE", "compute.googleapis.com", "/v1/projects/p", "", False, "destination_not_allowed", "none"),
    ("credential_change", "PUT", "iam.example.com", "/keys", "", True, "destination_not_allowed", "none"),
    ("delete_persistent_data", "DELETE", "db.example.com", "/v1/tables/t", "", False, "destination_not_allowed", "none"),
    ("billing_change", "POST", "billing.example.com", "/accounts", "", True, "destination_not_allowed", "none"),
    ("publish_as_owner", "POST", "uploads.example.com", "/publish", "", True, "destination_not_allowed", "none"),
    ("broaden_scope", "POST", GIT, "/org/repo", "", True, "control_plane_action", "standing"),
    ("outside_secrets", "GET", "vault.example.com", "/v1/secret/data/x", "", False, "destination_not_allowed", "none"),
    ("disable_safeguards", "POST", PARTNER, "/git-receive-pack", "", True, "git_receive_pack", "standing"),
)


def _policy():
    standing = StandingPolicy(
        policy_id="pol-net",
        action_class="network_access",
        destinations=(GIT, PARTNER),
        decision_id="dec-net",
    )
    return build_policy(
        ProjectEgress(registries=(), remotes=(REMOTE,), decision_id="dec-egress"),
        (standing,),
        "proxy.example.com:8443",
    )


def _worker(stage: str = "repository") -> Identity:
    return Identity(
        workspace_id="ws-403",
        project_id="proj-403",
        mission_id="m-403",
        worker_id="w-403",
        stage=stage,  # type: ignore[arg-type]
    )


def _request(
    method: str,
    host: str,
    path: str = "/",
    query: str = "",
    *,
    has_body: bool = False,
    tunnel: bool = False,
) -> Request:
    return Request(
        scheme="https",
        host=host,
        port=None,
        method=method,
        path=path,
        query=query,
        headers={},
        has_body=has_body,
        tunnel=tunnel,
    )


def test_every_tier2_and_tier3_class_is_covered() -> None:
    assert {row[0] for row in _REFUSED} == set(TIER2 | TIER3)


@pytest.mark.parametrize(
    ("action_class", "method", "host", "path", "query", "has_body", "code", "klass"),
    _REFUSED,
    ids=[row[0] for row in _REFUSED],
)
def test_worker_tier_request_refused(
    action_class: str,
    method: str,
    host: str,
    path: str,
    query: str,
    has_body: bool,
    code: str,
    klass: str,
) -> None:
    verdict = decide(_policy(), _worker(), _request(method, host, path, query, has_body=has_body), NOW)
    assert verdict.allowed is False
    assert verdict == Verdict(False, klass, code, "pol-net" if klass == "standing" else None)


def test_get_partner_stays_allowed() -> None:
    verdict = decide(_policy(), _worker(), _request("GET", PARTNER, "/v1/status"), NOW)
    assert verdict == Verdict(True, "standing", None, "pol-net")


def test_post_partner_is_not_a_project_remote() -> None:
    verdict = decide(_policy(), _worker(), _request("POST", PARTNER, "/v1/hooks", has_body=True), NOW)
    assert verdict == Verdict(True, "standing", None, "pol-net")


def test_get_and_head_on_project_remote_stay_allowed() -> None:
    policy = _policy()
    worker = _worker()
    for method in ("GET", "HEAD"):
        verdict = decide(policy, worker, _request(method, GIT, "/org/repo/info/refs", "service=git-upload-pack"), NOW)
        assert verdict == Verdict(True, "standing", None, "pol-net")


def test_bodied_get_on_project_remote_is_control_plane() -> None:
    verdict = decide(_policy(), _worker(), _request("GET", GIT, "/org/repo", has_body=True), NOW)
    assert verdict == Verdict(False, "standing", "control_plane_action", "pol-net")


def test_research_identity_is_unchanged() -> None:
    verdict = decide(
        _policy(),
        _worker("research"),
        _request("GET", GIT, "/org/repo.git/info/refs", "service=git-receive-pack"),
        NOW,
    )
    assert verdict == Verdict(True, "research", None, None)


class _Resolver:
    def __call__(self, host: str, port: int) -> list[str]:
        return ["203.0.113.10"]


class _Connector:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def __call__(self, ip: str, port: int, timeout: float):
        self.calls.append((ip, port))
        raise OSError("no upstream")


def _gateway(tmp_path: Path) -> tuple[EgressGateway, MemoryRecorder, _Connector, Path]:
    recorder = MemoryRecorder()
    connector = _Connector()
    identity = _worker()
    gateway = EgressGateway(
        policy_source=lambda _identity, _now: _policy(),
        recorder=recorder,
        fetch=None,
        resolver=_Resolver(),
        connector=connector,
        model_proxy_upstream=None,
        clock=lambda: NOW,
    )
    sock = gateway.open_sandbox(identity, tmp_path / "sandbox")
    return gateway, recorder, connector, sock


async def _connect(sock: Path, authority: str) -> bytes:
    reader, writer = await asyncio.open_unix_connection(str(sock))
    try:
        writer.write(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode())
        await writer.drain()
        return await asyncio.wait_for(reader.read(65536), 5.0)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass


def test_standing_project_remote_tunnel_is_inspected(tmp_path: Path) -> None:
    gateway, recorder, connector, sock = _gateway(tmp_path)
    try:
        raw = asyncio.run(_connect(sock, f"{GIT}:443"))
        assert raw.startswith(b"HTTP/1.1 403")
        assert b"tls_inspection_required" in raw
        assert connector.calls == []
        record = recorder.records[-1]
        assert record.outcome == "refused"
        assert record.code == "tls_inspection_required"
        assert record.klass == "standing"
        assert record.host == GIT
    finally:
        gateway.close_sandbox(_worker())


def test_standing_partner_tunnel_stays_open(tmp_path: Path) -> None:
    gateway, recorder, connector, sock = _gateway(tmp_path)
    try:
        raw = asyncio.run(_connect(sock, f"{PARTNER}:443"))
        assert b"upstream_failed" in raw
        assert connector.calls
        record = recorder.records[-1]
        assert record.outcome == "refused"
        assert record.code == "upstream_failed"
        assert record.klass == "standing"
    finally:
        gateway.close_sandbox(_worker())


def test_sandbox_git_push_and_https_post_refused(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    result_path = tmp_path / "result.txt"
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(
        "[safe]\n\tdirectory = *\n[user]\n\tname = t\n\temail = t@example.com\n",
        encoding="utf-8",
    )
    recorder = MemoryRecorder()
    connector = _Connector()
    gateway = EgressGateway(
        policy_source=lambda _identity, _now: _policy(),
        recorder=recorder,
        fetch=None,
        resolver=_Resolver(),
        connector=connector,
        model_proxy_upstream=None,
        clock=lambda: NOW,
    )
    script = """
set +e
cd "$OMP_WORKDIR"
rm -rf repo
git init -q -b main repo || { echo init > "$OMP_RESULT"; exit 2; }
cd repo
echo hello > README
git add README
git commit -qm init || { echo commit > "$OMP_RESULT"; exit 2; }
GIT_TERMINAL_PROMPT=0 git -c http.lowSpeedLimit=1 -c http.lowSpeedTime=2 \\
  push https://git.example.com/org/repo HEAD:refs/heads/worker
git_rc=$?
python3 -c 'import urllib.request
try:
    urllib.request.urlopen("https://git.example.com/org/repo", data=b"{}", timeout=5)
except Exception:
    raise SystemExit(7)
'
py_rc=$?
printf '%s %s\\n' "$git_rc" "$py_rc" > "$OMP_RESULT"
exit 0
"""
    env = {
        **os.environ,
        "HOME": str(home),
        "OMP_WORKDIR": str(home),
        "OMP_RESULT": str(result_path),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_GLOBAL": str(gitconfig),
    }
    try:
        try:
            rc = run_sandboxed(
                ["sh", "-c", script],
                _worker(),
                recorder,
                home,
                env,
                45,
                sockets_root=tmp_path / "socks",
                gateway=gateway,
            )
        except SandboxUnavailable as exc:
            pytest.skip(str(exc))
    finally:
        gateway.close_sandbox(_worker())
    assert rc == 0
    git_rc, py_rc = result_path.read_text(encoding="utf-8").split()
    assert int(git_rc) != 0
    assert int(py_rc) != 0
    refused = [record for record in recorder.records if record.outcome == "refused" and record.host == GIT]
    assert refused
    assert all(record.outcome != "allowed" or record.host != GIT for record in recorder.records)

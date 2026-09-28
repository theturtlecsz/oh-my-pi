"""Netns-bound WorkService probe and model sidecar against the fake docker."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest

from omp_harbor_eval.adapter import ProbeError
from omp_harbor_eval.docker_ops import DockerError
from omp_harbor_eval.netns import (
    HARBOR_SRC,
    RPC_SRC,
    SCRIPT,
    ExecProbe,
    model_port,
    start_model_sidecar,
)

DOCKER = Path(__file__).resolve().parent / "fake_docker.py"
WS = "w1"
WORKSPACE = "00000000-0000-4000-8000-0000000000aa"
WORK_ID = "00000000-0000-4000-8000-0000000000bb"
BEARER = "harbor-test-token"
BASE_URL = "http://127.0.0.1:8080"
PORT = 9090
IMAGE = "omp-f1-agent:dev"
STEPS = [{"text": "first"}, {"text": "second"}]


@pytest.fixture
def fake_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "docker"
    directory.mkdir()
    monkeypatch.setenv("FAKE_DOCKER_DIR", str(directory))
    _write_config(directory)
    return directory


def test_read_returns_the_three_views_and_keeps_the_bearer_off_argv(fake_dir: Path) -> None:
    _write_config(
        fake_dir,
        responses={
            WS: {
                "/v1/health/ready": {"status": 200, "body": {"live": True, "ready": True}},
                f"/v1/workspaces/{WORKSPACE}/execution": {
                    "status": 200,
                    "body": {"grant": {"state": "active"}, "active_item": {"work_id": WORK_ID}},
                },
                f"/v1/work-items/{WORK_ID}": {"status": 200, "body": {"work_id": WORK_ID, "state": "completed"}},
            }
        },
    )
    probe = ExecProbe(BASE_URL, BEARER, WORKSPACE, WS, docker=str(DOCKER))

    readback = probe.read()

    assert readback == {
        "health": {"live": True, "ready": True},
        "execution": {"grant": {"state": "active"}, "active_item": {"work_id": WORK_ID}},
        "work_item": {"work_id": WORK_ID, "state": "completed"},
    }
    execs = [record["argv"] for record in _calls(fake_dir) if record["argv"][1] == "exec"]
    assert len(execs) == 3
    for argv in execs:
        assert argv[1:6] == ["exec", "-i", WS, "python", "-c"]
        assert argv[6] == SCRIPT
        assert not any(BEARER in token for token in argv)
    assert [argv[7] for argv in execs] == [
        "http://127.0.0.1:8080/v1/health/ready",
        f"http://127.0.0.1:8080/v1/workspaces/{WORKSPACE}/execution",
        f"http://127.0.0.1:8080/v1/work-items/{WORK_ID}",
    ]


def test_401_raises_probe_error(fake_dir: Path) -> None:
    _write_config(
        fake_dir,
        responses={
            WS: {
                "/v1/health/ready": {"status": 200, "body": {"ready": True}},
                f"/v1/workspaces/{WORKSPACE}/execution": {"status": 401, "body": {"error": {"code": "unauthenticated"}}},
            }
        },
    )
    probe = ExecProbe(BASE_URL, BEARER, WORKSPACE, WS, docker=str(DOCKER))

    with pytest.raises(ProbeError, match="401"):
        probe.read()


def test_an_unanswered_route_raises_probe_error(fake_dir: Path) -> None:
    # No canned response, so SCRIPT runs for real and cannot connect to port 1.
    probe = ExecProbe("http://127.0.0.1:1", BEARER, WORKSPACE, WS, docker=str(DOCKER))

    with pytest.raises(ProbeError):
        probe.read()


def test_sidecar_readiness_polls_the_worker_until_the_404(fake_dir: Path, tmp_path: Path) -> None:
    _write_config(fake_dir, responses={WS: {"/": {"status": 404, "body": {"error": "not_found"}}}})
    staging = tmp_path / "staging"

    cid = start_model_sidecar(WS, IMAGE, STEPS, PORT, staging, docker=str(DOCKER), ready_timeout_s=5)

    assert cid == _expected_id(staging)
    assert json.loads((staging / "model-script.json").read_text(encoding="utf-8")) == STEPS
    calls = _calls(fake_dir)
    assert calls[0]["argv"] == _run_argv(staging)
    assert [record["argv"][1:] for record in calls[1:]] == [
        ["exec", "-i", WS, "python", "-c", SCRIPT, f"http://127.0.0.1:{PORT}/"],
    ]


def test_sidecar_without_a_ready_404_is_removed_and_times_out(fake_dir: Path, tmp_path: Path) -> None:
    staging = tmp_path / "staging"

    started = time.monotonic()
    with pytest.raises(DockerError, match="did not answer"):
        start_model_sidecar(WS, IMAGE, STEPS, PORT, staging, docker=str(DOCKER), ready_timeout_s=1)
    assert time.monotonic() - started < 5

    calls = _calls(fake_dir)
    assert calls[0]["argv"] == _run_argv(staging)
    assert calls[-1]["argv"][1:] == ["rm", "-f", _expected_id(staging)]
    assert any(record["argv"][1] == "exec" for record in calls[1:-1])


def test_model_port_reads_loopback_urls_with_a_path() -> None:
    assert model_port("http://127.0.0.1:9090/v1") == 9090
    assert model_port("http://localhost:9090") == 9090
    with pytest.raises(ValueError):
        model_port("http://203.0.113.5:9090/v1")
    with pytest.raises(ValueError):
        model_port("http://127.0.0.1/v1")


def test_sidecar_sources_exist() -> None:
    assert (HARBOR_SRC / "omp_harbor_eval" / "scripted_model.py").is_file()
    assert (RPC_SRC / "omp_rpc").is_dir()


def _write_config(fake_dir: Path, **overrides: object) -> None:
    config: dict[str, object] = {"containers": {}, "fail": [], "omp": [], "responses": {}}
    config.update(overrides)
    (fake_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")


def _calls(fake_dir: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in (fake_dir / "calls.jsonl").read_text(encoding="utf-8").splitlines()]


def _run_argv(staging: Path) -> list[str]:
    return [
        str(DOCKER),
        "run",
        "-d",
        "--rm",
        "--network",
        f"container:{WS}",
        "-e",
        "PYTHONPATH=/opt/h:/opt/r",
        "-v",
        f"{HARBOR_SRC}:/opt/h:ro",
        "-v",
        f"{RPC_SRC}:/opt/r:ro",
        "-v",
        f"{staging}:/opt/s:ro",
        IMAGE,
        "python",
        "-m",
        "omp_harbor_eval.scripted_model",
        "--script",
        "/opt/s/model-script.json",
        "--log",
        "/tmp/model.jsonl",
        "--host",
        "127.0.0.1",
        "--port",
        str(PORT),
    ]


def _expected_id(staging: Path) -> str:
    return hashlib.sha256("\0".join(_run_argv(staging)).encode()).hexdigest()[:12]

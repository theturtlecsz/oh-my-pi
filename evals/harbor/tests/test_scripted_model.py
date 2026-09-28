"""Scripted OpenAI-compatible model server: frames, log, exhaustion, CLI."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml

from omp_harbor_eval.scripted_model import (
    ScriptedModelServer,
    models_yml,
)

CHAT_PATH = "/v1/chat/completions"
CHAT_SUFFIX = "/chat/completions"
SRC_DIR = Path(__file__).resolve().parents[1] / "src"
_HTTP_TIMEOUT_S = 5.0


def _write_script(path: Path, steps: list[dict]) -> Path:
    path.write_text(json.dumps(steps), encoding="utf-8")
    return path


def _text_frames(text: str) -> bytes:
    packet = {
        "id": "scripted-response",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "scripted",
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
    }
    finish = {
        **packet,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    return f"data: {json.dumps(packet)}\n\ndata: {json.dumps(finish)}\n\ndata: [DONE]\n\n".encode()


def _tool_frames(call_id: str, arguments: str) -> bytes:
    packet = {
        "id": "scripted-response",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "scripted",
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": call_id,
                            "type": "function",
                            "function": {"name": "bash", "arguments": arguments},
                        }
                    ]
                },
                "finish_reason": None,
            }
        ],
    }
    finish = {
        **packet,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
    }
    return f"data: {json.dumps(packet)}\n\ndata: {json.dumps(finish)}\n\ndata: [DONE]\n\n".encode()


def _read_log(log_path: Path) -> list[dict]:
    text = log_path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


def _post(base_url: str, path: str, body: dict | None = None, *, method: str = "POST"):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        base_url + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_S) as response:
            return response.status, response.headers.get("Content-Type"), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type"), exc.read()


def test_respond_replays_steps_in_order_then_exhausts(tmp_path: Path) -> None:
    script = _write_script(
        tmp_path / "script.json",
        [
            {"text": "first"},
            {"tool_calls": [{"name": "bash", "arguments": {"cmd": "ls"}, "id": "call-1"}]},
            {"text": "third"},
        ],
    )
    server = ScriptedModelServer(script, tmp_path / "requests.jsonl")

    first = server.respond({"model": "scripted"})
    assert first.status == 200
    assert first.content_type == "text/event-stream"
    assert first.step == 0
    assert first.payload == _text_frames("first")

    second = server.respond({"model": "scripted"})
    assert second.step == 1
    assert second.payload == _tool_frames("call-1", '{"cmd": "ls"}')

    third = server.respond({"model": "scripted"})
    assert third.step == 2
    assert third.payload == _text_frames("third")

    exhausted = server.respond({"model": "scripted"})
    assert exhausted.status == 500
    assert exhausted.content_type == "application/json"
    assert exhausted.step is None
    assert json.loads(exhausted.payload) == {"error": "script_exhausted"}


def test_respond_serializes_object_arguments_and_defaults_tool_id(tmp_path: Path) -> None:
    script = _write_script(
        tmp_path / "script.json",
        [{"tool_calls": [{"name": "read", "arguments": {"path": "a.txt"}}]}],
    )
    server = ScriptedModelServer(script, tmp_path / "requests.jsonl")
    payload = server.respond({"model": "scripted"}).payload.decode()
    packet = json.loads(payload.split("\n\n", 1)[0].removeprefix("data: "))
    call = packet["choices"][0]["delta"]["tool_calls"][0]
    assert call["id"] == "scripted-0-0"
    assert call["function"]["name"] == "read"
    assert call["function"]["arguments"] == '{"path": "a.txt"}'


def test_socket_serves_frames_logs_requests_and_exhausts(tmp_path: Path) -> None:
    script = _write_script(tmp_path / "script.json", [{"text": "only"}])
    log_path = tmp_path / "logs" / "requests.jsonl"
    with ScriptedModelServer(script, log_path) as server:
        assert server.base_url == f"http://127.0.0.1:{server.port}/v1"
        status, content_type, body = _post(
            server.base_url, CHAT_SUFFIX, {"model": "scripted", "messages": []}
        )
        assert status == 200
        assert content_type == "text/event-stream"
        assert body == _text_frames("only")

        status, content_type, body = _post(server.base_url, CHAT_SUFFIX, {"model": "scripted"})
        assert status == 500
        assert content_type == "application/json"
        assert json.loads(body) == {"error": "script_exhausted"}

        status, content_type, body = _post(server.base_url, "/models", method="GET")
        assert status == 404
        assert content_type == "application/json"
        assert json.loads(body) == {"error": "not_found"}

    records = _read_log(log_path)
    assert [record["ordinal"] for record in records] == [1, 2, 3]
    assert [record["path"] for record in records] == [CHAT_PATH, CHAT_PATH, "/v1/models"]
    assert [record["step"] for record in records] == [0, None, None]
    assert [record["status"] for record in records] == [200, 500, 404]
    assert records[0]["body"] == {"model": "scripted", "messages": []}
    assert records[1]["body"] == {"model": "scripted"}


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "example.com", ""])
def test_non_loopback_host_raises_before_bind(tmp_path: Path, host: str) -> None:
    script = _write_script(tmp_path / "script.json", [{"text": "hi"}])
    with pytest.raises(ValueError, match="loopback"):
        ScriptedModelServer(script, tmp_path / "requests.jsonl", host)
    with pytest.raises(ValueError, match="loopback"):
        ScriptedModelServer(script, tmp_path / "requests.jsonl", host, port=1)


def test_models_yml_parses_with_one_zero_cost_keyless_model() -> None:
    document = yaml.safe_load(models_yml("http://127.0.0.1:8123/v1"))
    provider = document["providers"]["scripted"]
    assert provider["baseUrl"] == "http://127.0.0.1:8123/v1"
    assert provider["api"] == "openai-completions"
    assert provider["auth"] == "none"
    assert len(provider["models"]) == 1
    model = provider["models"][0]
    assert model["id"] == "scripted"
    assert model["cost"] == {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _read_line(proc: subprocess.Popen[str], timeout: float) -> str:
    lines: list[str] = []

    def read() -> None:
        lines.append(proc.stdout.readline() if proc.stdout is not None else "")

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    reader.join(timeout)
    assert lines and lines[0], "CLI did not print a base_url"
    return lines[0].strip()


def test_cli_serves_script_on_a_free_port(tmp_path: Path) -> None:
    script = _write_script(tmp_path / "script.json", [{"text": "cli-step"}])
    log_path = tmp_path / "cli.jsonl"
    port = _free_port()
    env = dict(os.environ)
    pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(SRC_DIR) if not pythonpath else f"{SRC_DIR}{os.pathsep}{pythonpath}"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "omp_harbor_eval.scripted_model",
            "--script",
            str(script),
            "--port",
            str(port),
            "--log",
            str(log_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        base_url = _read_line(proc, timeout=15)
        assert base_url == f"http://127.0.0.1:{port}/v1"
        status, _content_type, body = _post(base_url, CHAT_SUFFIX, {"model": "scripted"})
        assert status == 200
        assert body == _text_frames("cli-step")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)

    assert proc.pid is not None
    records = _read_log(log_path)
    assert [(record["ordinal"], record["step"], record["status"]) for record in records] == [(1, 0, 200)]


def test_non_loopback_host_raises_before_bind_keeps_port_free(tmp_path: Path) -> None:
    script = _write_script(tmp_path / "script.json", [{"text": "hi"}])
    port = _free_port()
    with pytest.raises(ValueError, match="loopback"):
        ScriptedModelServer(script, tmp_path / "requests.jsonl", "0.0.0.0", port=port)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", port))

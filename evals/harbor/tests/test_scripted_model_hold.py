"""Tests for ScriptedModelServer hold_s: delayed responses, concurrent requests, disconnects, validation."""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from omp_harbor_eval.scripted_model import ScriptedModelServer, _load_script

CHAT_PATH = "/v1/chat/completions"
CHAT_SUFFIX = "/chat/completions"
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


def _read_log(log_path: Path) -> list[dict]:
    text = log_path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


def _post(base_url: str, path: str, body: dict | None = None, *, method: str = "POST", timeout: float = _HTTP_TIMEOUT_S):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        base_url + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.headers.get("Content-Type"), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type"), exc.read()


def test_held_step_response_arrives_no_earlier_than_hold_s(tmp_path: Path) -> None:
    hold_s = 0.2
    script = _write_script(
        tmp_path / "script.json",
        [
            {"text": "held step", "hold_s": hold_s},
            {"text": "instant step"},
        ],
    )
    log_path = tmp_path / "requests.jsonl"
    with ScriptedModelServer(script, log_path) as server:
        t0 = time.monotonic()
        status, content_type, body = _post(server.base_url, CHAT_SUFFIX, {"model": "scripted"})
        elapsed = time.monotonic() - t0
        assert status == 200
        assert content_type == "text/event-stream"
        assert body == _text_frames("held step")
        assert elapsed >= hold_s - 0.02, f"response arrived earlier than hold_s: {elapsed} < {hold_s}"

        t1 = time.monotonic()
        status, content_type, body = _post(server.base_url, CHAT_SUFFIX, {"model": "scripted"})
        elapsed_fast = time.monotonic() - t1
        assert status == 200
        assert body == _text_frames("instant step")
        assert elapsed_fast < hold_s


def test_held_step_sleeps_outside_lock_so_other_requests_served_meanwhile(tmp_path: Path) -> None:
    script = _write_script(
        tmp_path / "script.json",
        [
            {"text": "step 0 (held)", "hold_s": 0.3},
            {"text": "step 1 (quick)"},
        ],
    )
    log_path = tmp_path / "requests.jsonl"
    results: list[tuple[str, float]] = []

    with ScriptedModelServer(script, log_path) as server:
        def request_step_0() -> None:
            t0 = time.monotonic()
            status, _, body = _post(server.base_url, CHAT_SUFFIX, {"model": "scripted"})
            assert status == 200
            assert body == _text_frames("step 0 (held)")
            results.append(("step 0", time.monotonic() - t0))

        thread = threading.Thread(target=request_step_0)
        thread.start()

        # Give thread time to acquire step 0 and start holding
        time.sleep(0.05)

        # Request step 1 while step 0 is holding
        t1 = time.monotonic()
        status, _, body = _post(server.base_url, CHAT_SUFFIX, {"model": "scripted"})
        elapsed_step_1 = time.monotonic() - t1
        assert status == 200
        assert body == _text_frames("step 1 (quick)")
        results.append(("step 1", elapsed_step_1))

        thread.join(timeout=2.0)

    # step 1 completed before step 0 because step 0 was sleeping outside the lock
    assert results[0][0] == "step 1"
    assert results[1][0] == "step 0"


def test_client_giving_up_on_held_step_0_leaves_server_up_and_serves_step_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _write_script(
        tmp_path / "script.json",
        [
            {"text": "held step 0", "hold_s": 0.3},
            {"text": "step 1"},
        ],
    )
    log_path = tmp_path / "requests.jsonl"
    with ScriptedModelServer(script, log_path) as server:
        # Client 1 connects and gives up (short timeout)
        req = urllib.request.Request(
            server.base_url + CHAT_SUFFIX,
            data=b'{"model": "scripted"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=0.05) as response:
                response.read()
        except (TimeoutError, urllib.error.URLError, socket.timeout, OSError):
            pass

        # Server is still up; next request gets step 1
        status, content_type, body = _post(server.base_url, CHAT_SUFFIX, {"model": "scripted"})
        assert status == 200
        assert content_type == "text/event-stream"
        assert body == _text_frames("step 1")

        # Wait for step 0 hold to finish and verify both steps were logged
        time.sleep(0.35)
        records = _read_log(log_path)
        steps = [r["step"] for r in records]
        assert 0 in steps
        assert 1 in steps

    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "BrokenPipeError" not in captured.err


def test_client_socket_close_during_hold_drops_write_quietly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _write_script(
        tmp_path / "script.json",
        [
            {"text": "step 0 held", "hold_s": 0.25},
            {"text": "step 1 alive"},
        ],
    )
    log_path = tmp_path / "requests.jsonl"
    with ScriptedModelServer(script, log_path) as server:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect(("127.0.0.1", server.port))
        req = (
            b"POST /v1/chat/completions HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 21\r\n"
            b"Connection: close\r\n\r\n"
            b'{"model": "scripted"}'
        )
        sock.sendall(req)
        sock.close()

        # Server is still up and serves step 1
        status, _, body = _post(server.base_url, CHAT_SUFFIX, {"model": "scripted"})
        assert status == 200
        assert body == _text_frames("step 1 alive")

        time.sleep(0.3)
        records = _read_log(log_path)
        assert len(records) == 2
        assert {r["step"] for r in records} == {0, 1}

    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "BrokenPipeError" not in captured.err


@pytest.mark.parametrize(
    ("bad_value", "step_index"),
    [
        (True, 0),
        (False, 0),
        ("0.5", 0),
        (None, 0),
        ([1], 0),
        ({"s": 1}, 0),
        (-1, 0),
        (-0.01, 0),
        (True, 2),
        (-5.0, 3),
        (float("nan"), 0),
        (float("inf"), 0),
    ],
)
def test_invalid_hold_s_raises_naming_step(tmp_path: Path, bad_value: object, step_index: int) -> None:
    steps: list[dict] = [{"text": f"step-{i}"} for i in range(step_index)]
    steps.append({"text": "bad-step", "hold_s": bad_value})
    script = _write_script(tmp_path / "script.json", steps)

    with pytest.raises(ValueError, match=f"step {step_index}"):
        _load_script(script)

    with pytest.raises(ValueError, match=f"step {step_index}"):
        ScriptedModelServer(script, tmp_path / "requests.jsonl")


@pytest.mark.parametrize("valid_hold", [0, 0.0, 0.5, 1, 10.0])
def test_valid_hold_s_accepted(tmp_path: Path, valid_hold: float | int) -> None:
    script = _write_script(tmp_path / "script.json", [{"text": "ok", "hold_s": valid_hold}])
    loaded = _load_script(script)
    assert len(loaded) == 1
    assert loaded[0]["hold_s"] == valid_hold

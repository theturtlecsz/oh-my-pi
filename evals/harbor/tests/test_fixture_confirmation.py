"""Fixture F1's scripted confirmation lands against the real confirm gate.

The workflow host mints confirmation ids as random single-use values, so a
scripted model must confirm with the id from the preview it just received. The
f1 scripts carry the ``$confirmation_id`` placeholder with ``resolved``:
``confirmation_id``; the scripted provider fills it from the preceding tool
result. These tests prove the whole contract: no fixture pins a literal id, the
provider substitutes the preview's real id, and ``confirmWrite`` accepts the
resulting call.

Failure modes a consumer would observe if this regressed:

- the provider ships ``$confirmation_id`` verbatim and ``confirmWrite`` answers
  ``REFUSED — unknown or already-used confirmation_id`` (the f1 known_good run
  can never amend);
- a fixture pins a fixed id and the random single-use receipt is never found;
- the id is resolved from the wrong turn and the stale receipt is refused.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from omp_harbor_eval.scripted_model import CONFIRMATION_ID_PLACEHOLDER as PLACEHOLDER
from omp_harbor_eval.scripted_model import ScriptedModelServer

HARBOR_DIR = Path(__file__).resolve().parent.parent
FIXTURE_DIR = HARBOR_DIR / "fixtures" / "f1"
HANDSHAKE = Path(__file__).with_name("confirm_handshake.ts")


def _fixture_script(fixture_dir: Path) -> list:
    return json.loads((fixture_dir / "scenario.json").read_text(encoding="utf-8"))["model_script"]


def _confirm_call(steps: list) -> dict:
    for step in steps:
        for call in step.get("tool_calls", []):
            if call.get("arguments", {}).get("confirm") is True:
                return call
    raise AssertionError("no scripted confirm call")


def _frame_arguments(response) -> dict:
    first_frame = response.payload.decode("utf-8").split("\n\n", 1)[0].removeprefix("data: ")
    call = json.loads(first_frame)["choices"][0]["delta"]["tool_calls"][0]
    return json.loads(call["function"]["arguments"])


def _chat(messages: list[dict]) -> dict:
    return {"model": "scripted", "messages": messages}


class _Handshake:
    """The bun harness holding one pending receipt across preview and confirm."""

    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            ["bun", str(HANDSHAKE)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def __enter__(self) -> _Handshake:
        return self

    def __exit__(self, *_exc: object) -> bool:
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        self.proc.wait(timeout=30)
        assert self.proc.returncode == 0, self.proc.stderr.read()
        return False

    def _send(self, message: dict) -> dict:
        assert self.proc.stdin is not None
        assert self.proc.stdout is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    def preview(self, request: dict) -> dict:
        return self._send({"phase": "preview", **request})

    def confirm(self, confirmation_id: str, params: dict | None = None) -> dict:
        message = {"phase": "confirm", "confirmation_id": confirmation_id}
        if params is not None:
            message["params"] = params
        return self._send(message)


def _revision_request() -> dict:
    steps = _fixture_script(FIXTURE_DIR)
    preview_args = steps[0]["tool_calls"][0]["arguments"]
    return {
        "action": "revise_work",
        "question": "Model wants to revise this work in place",
        "detail": "OMP-1 (revision 00000000-0000-7000-8000-000000000010)",
        "params": {
            "action": "revise_work",
            "work": preview_args["work"],
            "scope": preview_args["scope"],
            "acceptance_criteria": preview_args["acceptance_criteria"],
            "expected_revision_id": preview_args["expected_revision_id"],
        },
        "options": {
            "expectedRevisionId": preview_args["expected_revision_id"],
            "currentRevisionId": preview_args["expected_revision_id"],
        },
    }


def test_no_harbor_fixture_pins_a_literal_confirmation_id() -> None:
    """Every fixture script confirms only through the resolved placeholder."""

    for fixture_dir in sorted((HARBOR_DIR / "fixtures").iterdir()):
        script_path = fixture_dir / "scenario.json"
        if not script_path.is_file():
            continue
        for step in json.loads(script_path.read_text(encoding="utf-8")).get("model_script", []):
            for call in step.get("tool_calls", []):
                value = call.get("arguments", {}).get("confirmation_id")
                if value is None:
                    continue
                assert value == PLACEHOLDER, f"{script_path}: fixed confirmation_id {value!r}"
                assert call.get("resolved") == "confirmation_id", f"{script_path}: placeholder without resolved"


def test_f1_sidecar_matches_the_scenario_and_resolves_the_placeholder() -> None:
    scenario = json.loads((FIXTURE_DIR / "scenario.json").read_text(encoding="utf-8"))
    sidecar = json.loads((FIXTURE_DIR / "model-script.json").read_text(encoding="utf-8"))
    assert sidecar == scenario["model_script"]

    call = _confirm_call(scenario["model_script"])
    assert call["resolved"] == "confirmation_id"
    assert call["arguments"]["confirmation_id"] == PLACEHOLDER


def test_scripted_provider_fills_the_confirmation_id_from_the_preceding_preview(tmp_path: Path) -> None:
    server = ScriptedModelServer(FIXTURE_DIR / "model-script.json", tmp_path / "requests.jsonl")

    preview = server.respond(_chat([]))  # the preview step, no receipt in context yet
    assert preview.status == 200
    assert "confirm" not in _frame_arguments(preview)

    preview_text = (
        "CONFIRM REQUIRED — nothing written.\n\nModel wants to revise this work in place\n\n"
        "confirmation_id: cf-deadbeef\n"
    )
    confirm = server.respond(
        _chat([{"role": "tool", "tool_call_id": "revise-amend-preview", "content": preview_text}])
    )
    assert confirm.status == 200
    assert _frame_arguments(confirm)["confirmation_id"] == "cf-deadbeef"

    # A confirm script with no preceding preview fails loudly rather than
    # shipping the placeholder.
    server = ScriptedModelServer(FIXTURE_DIR / "model-script.json", tmp_path / "requests2.jsonl")
    server.respond(_chat([]))
    missing = server.respond(_chat([{"role": "user", "content": "/execute OMP-1"}]))
    assert missing.status == 500
    assert PLACEHOLDER not in missing.payload.decode("utf-8")


def test_scripted_confirm_is_accepted_by_the_real_confirm_gate(tmp_path: Path) -> None:
    """Preview mints an id; the provider fills the scripted call; the gate accepts it."""

    server = ScriptedModelServer(FIXTURE_DIR / "model-script.json", tmp_path / "requests.jsonl")
    server.respond(_chat([]))  # consume the preview step

    with _Handshake() as handshake:
        preview = handshake.preview(_revision_request())
        assert preview["approved"] is False
        assert "CONFIRM REQUIRED" in preview["preview"]
        minted = preview["preview"].split("confirmation_id:", 1)[1].split()[0]
        assert minted.startswith("cf-")

        # The provider substitutes the id the preview just minted, from the
        # preview text the agent feeds back as a tool result.
        confirm = server.respond(
            _chat([{"role": "tool", "tool_call_id": "x", "content": preview["preview"]}])
        )
        resolved = _frame_arguments(confirm)
        assert resolved["confirmation_id"] == minted
        assert PLACEHOLDER not in json.dumps(resolved)

        # The resolved call the script actually emits is accepted by the gate
        # that minted the id — this is the f1 confirm call, not a stand-in.
        accepted = handshake.confirm(resolved["confirmation_id"], params=resolved)

    assert accepted["approved"] is True


def test_scripted_provider_fills_confirmation_ids_for_both_f1_confirm_steps(tmp_path: Path) -> None:
    """The provider fills confirmation_id for amend confirm and stale retry confirm in turn."""
    server = ScriptedModelServer(FIXTURE_DIR / "model-script.json", tmp_path / "requests.jsonl")

    # Step 0: amend preview
    p1 = server.respond(_chat([]))
    assert p1.status == 200
    assert "confirm" not in _frame_arguments(p1)

    # Step 1: amend confirm (resolves to cf-aaaa11)
    p1_tool_result = (
        "CONFIRM REQUIRED — nothing written.\n\nModel wants to revise this work in place\n\n"
        "confirmation_id: cf-aaaa11\n"
    )
    c1 = server.respond(
        _chat([{"role": "tool", "tool_call_id": "revise-amend-preview", "content": p1_tool_result}])
    )
    assert c1.status == 200
    assert _frame_arguments(c1)["confirmation_id"] == "cf-aaaa11"

    # Step 2: stale retry preview
    p2_tool_result = "OMP-1 revised to revision 2"
    p2 = server.respond(
        _chat([
            {"role": "tool", "tool_call_id": "revise-amend-preview", "content": p1_tool_result},
            {"role": "tool", "tool_call_id": "revise-amend-confirm", "content": p2_tool_result},
        ])
    )
    assert p2.status == 200
    assert "confirm" not in _frame_arguments(p2)

    # Step 3: stale retry confirm (resolves to cf-bbbb22)
    stale_tool_result = (
        "CONFIRM REQUIRED — nothing written.\n\nModel wants to revise this work in place\n\n"
        "confirmation_id: cf-bbbb22\n"
    )
    c2 = server.respond(
        _chat([
            {"role": "tool", "tool_call_id": "revise-amend-preview", "content": p1_tool_result},
            {"role": "tool", "tool_call_id": "revise-amend-confirm", "content": p2_tool_result},
            {"role": "tool", "tool_call_id": "revise-stale-retry", "content": stale_tool_result},
        ])
    )
    assert c2.status == 200
    assert _frame_arguments(c2)["confirmation_id"] == "cf-bbbb22"
    assert _frame_arguments(c2)["confirm"] is True

"""Real installed controller recovery; local provider supplies protocol inputs only."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import threading
import time
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import httpx
from installed_runtime_support import (
    InstalledRelease,
    RpcProcess,
    _free_port,
    _health,
    _process,
    _run,
)
from installed_runtime_support import (
    installed_release as installed_release,  # noqa: PLC0414 -- pytest fixture re-export
)
from omp_work.operations.config import OperationsConfig
from pg_native import native_postgres, seed_authority


class RecoveryProvider:
    """Pause a real HTTP request after production review returned its tool result."""

    def __init__(self, root: Path, checkpoint: Literal["review", "resume"]):
        self.root = root
        self.checkpoint = checkpoint
        self.key = ""
        self.calls: list[dict] = []
        self.barrier = threading.Event()
        self.release = threading.Event()
        self.recovered = threading.Event()
        self.restarting = False
        self.error: str | None = None
        self.recovery_calls = 0

    def respond(self, request: dict) -> dict:
        self.calls.append(request)
        (self.root / f"provider-{len(self.calls)}.json").write_text(json.dumps(request))
        tools = request.get("tools", [])
        if not any(tool.get("function", {}).get("name") == "work" for tool in tools):
            return {"content": "Recovery fixture"}
        if self.restarting:
            self.recovery_calls += 1
            self.recovered.set()
            return {"content": "Recovery continuation observed."}
        if self.checkpoint == "resume":
            self.barrier.set()
            self.release.wait(60)
            return {"content": "Turn ended."}
        messages = request.get("messages", [])
        results = [message for message in messages if message.get("role") == "tool"]
        if results:
            latest = str(results[-1].get("content", ""))
            if "END YOUR TURN NOW" in latest:
                self.barrier.set()
                self.release.wait(60)
                return {"content": "Turn ended."}
            if any(
                text in latest
                for text in [
                    "REFUSED",
                    "no active execution grant",
                    "failed",
                    "mismatch",
                ]
            ):
                self.error = latest
                self.barrier.set()
                return {"content": "Fixture setup failed."}
        actions = [
            (
                "work",
                {
                    "action": "seal_execution_criteria",
                    "work": self.key,
                    "criteria": ["result.txt contains after"],
                },
            ),
            (
                "work",
                {
                    "action": "stamp_execution_plan",
                    "work": self.key,
                    "plan_file": "recovery-plan.md",
                    "paths": ["result.txt"],
                },
            ),
            ("write", {"path": "result.txt", "content": "after\n"}),
            ("read", {"path": "result.txt"}),
            (
                "work",
                {
                    "action": "begin_execution_review",
                    "work": self.key,
                    "body": "The real read tool read result.txt and returned after. Disposable recovery fixture; no audit verdict asserted.",
                },
            ),
        ]
        index = len(results)
        if index >= len(actions):
            self.error = (
                f"Unexpected tool result without checkpoint barrier: {results[-1]}"
            )
            self.barrier.set()
            return {"content": "Fixture setup failed."}
        name, arguments = actions[index]
        return {
            "tool_calls": [
                {
                    "index": 0,
                    "id": f"recovery-{index}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ]
        }

    @contextlib.contextmanager
    def serve(self) -> Generator[str]:
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_POST(self) -> None:
                request = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                delta = provider.respond(request)
                packet = {
                    "id": "recovery-response",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "local-recovery",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                }
                finish = {
                    **packet,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": "tool_calls"
                            if "tool_calls" in delta
                            else "stop",
                        }
                    ],
                }
                data = (
                    f"data: {json.dumps(packet)}\n\ndata: {json.dumps(finish)}\n\ndata: [DONE]\n\n"
                ).encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/v1"
        finally:
            self.release.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def execution_message_count(entries: list[dict]) -> int:
    """Count real persisted delivery records, including receipt-backed transport."""
    return sum(
        entry.get("type") == "custom_message"
        and (
            entry.get("customType") == "work-execute"
            or any(
                delivery.get("customType") == "work-execute"
                for delivery in entry.get("details", {}).get("deliveries", [])
            )
        )
        for entry in entries
    )


def exercise_controller_recovery(
    release: InstalledRelease, tmp_path: Path, checkpoint: Literal["review", "resume"]
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    state = tmp_path / "runtime"
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    for args in (
        ["init", "-b", "main"],
        ["config", "user.name", "Recovery Fixture"],
        ["config", "user.email", "fixture@example.invalid"],
    ):
        _run(["git", *args], repository, env)
    (repository / "result.txt").write_text("before\n")
    shutil.copyfile(
        Path(__file__).parent / "fixtures/recovery-plan.md",
        repository / "recovery-plan.md",
    )
    _run(["git", "add", "."], repository, env)
    _run(["git", "commit", "-m", "recovery fixture baseline"], repository, env)
    remote = tmp_path / "remote.git"
    _run(["git", "init", "--bare", str(remote)], repository, env)
    _run(["git", "remote", "add", "origin", str(remote)], repository, env)
    _run(["git", "push", "-u", "origin", "main"], repository, env)
    pg_port, service_port = _free_port(), _free_port()
    service_args = ("--service", "--postgres-port", str(pg_port))

    def service_command(*args: str) -> str:
        return _run(
            release.command(state, repository, *service_args, *args), repository, env
        )

    identity = json.loads(service_command("ops", "credentials", "init"))
    config = OperationsConfig(
        config_dir=state / "config/omp/work-ledger",
        state_dir=state / "state/omp/work-ledger",
        data_dir=state / "data/omp/work-ledger",
        port=pg_port,
    )
    base_url = f"http://127.0.0.1:{service_port}"
    provider = RecoveryProvider(tmp_path, checkpoint)
    with native_postgres(tmp_path / "postgres", pg_port), provider.serve() as model_url:
        service_command("ops", "bootstrap")
        seed_authority(
            config.connection_kwargs("postgres"),
            UUID(identity["workspace_id"]),
            UUID(identity["owner_id"]),
        )
        service_command(
            "ops",
            "capabilities",
            "init",
            "--workspace-id",
            identity["workspace_id"],
            "--owner-id",
            identity["owner_id"],
            "--base-url",
            base_url,
        )
        agent_dir = state / "home/.omp/agent"
        (agent_dir / "models.yml").write_text(
            json.dumps(
                {
                    "providers": {
                        "qualification": {
                            "baseUrl": model_url,
                            "api": "openai-completions",
                            "apiKey": "local-fixture-only",
                            "models": [
                                {
                                    "id": "local-recovery",
                                    "name": "Local recovery",
                                    "reasoning": False,
                                    "input": ["text"],
                                    "cost": {
                                        "input": 0,
                                        "output": 0,
                                        "cacheRead": 0,
                                        "cacheWrite": 0,
                                    },
                                    "contextWindow": 200000,
                                    "maxTokens": 2048,
                                }
                            ],
                        }
                    }
                }
            )
        )
        (agent_dir / "config.yml").write_text(
            "modelRoles:\n  audit: qualification/local-recovery\n  default: qualification/local-recovery\n  smol: qualification/local-recovery\nadvisor:\n  enabled: false\ntools:\n  xdev: false\n"
        )
        service_log = tmp_path / "service.stderr"
        with _process(
            release.command(
                state,
                repository,
                *service_args,
                "serve",
                "--port",
                str(service_port),
                "--capabilities-dir",
                str(config.config_dir / "capabilities"),
            ),
            repository,
            env,
            service_log,
        ) as service:
            _health(base_url, service, service_log)
            source_root = Path(__file__).resolve().parents[3]
            helper = (
                source_root
                / "session-system/tests/fixtures/installed-recovery-setup.ts"
            )
            setup_args = [str(release.root / "bin/bun"), str(helper)]
            setup_env = json.loads(
                _run(
                    [
                        *setup_args,
                        "environment",
                        str(release.root),
                        str(state),
                        str(repository),
                    ],
                    source_root,
                    env,
                )
            )
            setup = json.loads(
                _run(
                    [
                        *setup_args,
                        "setup",
                        str(release.root),
                        str(state),
                        str(repository),
                    ],
                    source_root,
                    setup_env,
                )
            )
            (tmp_path / "setup.json").write_text(json.dumps(setup, indent=2))
            provider.key = setup["issue"]["key"]
            client_config = json.loads(
                (state / "config/omp-work/client.json").read_text()
            )
            capability = json.loads(Path(client_config["bearer_file"]).read_text())
            headers: dict[str, str] = {
                "Authorization": f"Bearer {capability['token']}",
                "X-OMP-Workspace-ID": identity["workspace_id"],
                "X-OMP-Contract-SHA256": setup["tcb"]["judgeManifest"][
                    "contract_sha256"
                ],
            }
            session_path = tmp_path / "recovery-session.jsonl"
            cli_args = (
                "--mode",
                "rpc",
                "--session",
                str(session_path),
                "--provider",
                "qualification",
                "--model",
                "local-recovery",
            )
            cli_log = tmp_path / "controller-before.stderr"
            with httpx.Client(
                base_url=base_url, headers=headers, trust_env=False
            ) as client:
                with _process(
                    release.command(state, repository, *cli_args),
                    repository,
                    env,
                    cli_log,
                ) as cli:
                    rpc = RpcProcess(cli, cli_log)
                    initial = rpc.request("get_state")
                    assert provider.barrier.wait(60), (
                        f"Production review barrier not reached: {cli_log.read_text()[-6000:]}"
                    )
                    assert provider.error is None, provider.error
                    execution_url = f"/v1/workspaces/{identity['workspace_id']}/execution/{provider.key}"
                    if checkpoint == "resume":
                        # Real service pause and RPC resume, with unchanged phase,
                        # candidate and HEAD. The first provider request stays held.
                        current_response = client.get(execution_url)
                        current_response.raise_for_status()
                        current = current_response.json()
                        pause_response = client.post(
                            "/v1/commands",
                            json={
                                "api_version": "work.omp.dev/v1",
                                "workspace_id": identity["workspace_id"],
                                "operation_id": str(uuid4()),
                                "request_id": str(uuid4()),
                                "correlation_id": str(uuid4()),
                                "command": {
                                    "type": "set_execution_state",
                                    "payload": {
                                        "grant_id": current["grant"]["grant_id"],
                                        "expected_grant_version": current["grant"][
                                            "grant_version"
                                        ],
                                        "target_state": "paused",
                                        "reason": "disposable_recovery_fixture_pause",
                                        "judge_sha256": current["grant"][
                                            "judge_sha256"
                                        ],
                                    },
                                },
                            },
                        )
                        pause_response.raise_for_status()
                        paused = pause_response.json()["result"]["grant"]
                        assert paused["state"] == "paused"
                        request_id = rpc.send(
                            "prompt", message=f"/execute resume {provider.key}"
                        )
                        # prompt_result waits for the existing streaming turn;
                        # observe the actual service transition instead.
                        deadline = time.monotonic() + 10
                        while True:
                            resumed_response = client.get(execution_url)
                            resumed_response.raise_for_status()
                            if (
                                resumed_response.json()["grant"]["state"] == "active"
                                or time.monotonic() >= deadline
                            ):
                                break
                            time.sleep(0.05)
                        assert resumed_response.json()["grant"]["state"] == "active"
                        assert (
                            resumed_response.json()["grant"]["grant_version"]
                            == paused["grant_version"] + 1
                        )
                        (tmp_path / "resume-transition.json").write_text(
                            json.dumps(
                                {
                                    "before": current,
                                    "pause": pause_response.json(),
                                    "resumeRpcRequest": request_id,
                                    "resumed": resumed_response.json(),
                                },
                                indent=2,
                            )
                        )
                    barrier_state = rpc.request("get_state")
                    assert barrier_state["isStreaming"] is True, (
                        "Provider barrier must hold the existing turn"
                    )
                    actual_session = Path(str(barrier_state["sessionFile"]))
                    deadline = time.monotonic() + 5
                    while True:
                        entries = [
                            json.loads(line)
                            for line in actual_session.read_text().splitlines()
                            if line
                        ]
                        if (
                            checkpoint == "review"
                            or any(
                                entry.get("customType") == "work-now-execute-outbox"
                                and entry.get("data", {}).get("postVersion")
                                == paused["grant_version"] + 1
                                for entry in entries
                            )
                            or time.monotonic() >= deadline
                        ):
                            break
                        time.sleep(0.05)
                    outbox = [
                        entry
                        for entry in entries
                        if entry.get("customType") == "work-now-execute-outbox"
                    ]
                    assert outbox, "Production continuation intent was not persisted"
                    intent = outbox[-1]["data"]
                    assert intent["grantId"] == setup["execution"]["grant"]["grant_id"]
                    assert any(
                        row["data"]["messageId"] == intent["messageId"]
                        and row["data"]["status"] == "pending"
                        for row in outbox
                    )
                    assert execution_message_count(entries) == 1, (
                        "Hidden continuation already injected; wrong crash window"
                    )
                    intent_index = next(
                        i
                        for i, entry in enumerate(entries)
                        if entry.get("data", {}).get("messageId") == intent["messageId"]
                    )
                    assert execution_message_count(entries[intent_index:]) == 0
                    workflow_url = f"/v1/work-items/{provider.key}/workflow"
                    deadline = time.monotonic() + 10
                    while True:
                        workflow_response = client.get(workflow_url)
                        workflow_response.raise_for_status()
                        workflow = workflow_response.json()
                        events = workflow.get("close_attempt_events", [])
                        delivered_ids = {
                            row["event_id"]
                            for row in workflow.get("checkpoint_deliveries", [])
                            if row["status"] == "delivered"
                        }
                        pending = [
                            row
                            for row in events
                            if row["requires_delivery"]
                            and row["event_id"] not in delivered_ids
                        ]
                        if not pending or time.monotonic() >= deadline:
                            break
                        time.sleep(0.05)
                    assert not pending, (
                        "Pending checkpoint replay would mask the hidden-continuation loss"
                    )
                    assert all(
                        attempt["launch_count"] == 0
                        for attempt in workflow["close_attempts"]
                    ), "Fixture must stop before launching an auditor"
                    before_response = client.get(execution_url)
                    before_response.raise_for_status()
                    before_execution = before_response.json()
                    assert before_execution["grant"]["state"] == "active"
                    assert (
                        before_execution["grant"]["grant_version"]
                        == intent["postVersion"]
                    )
                    effect_ref = (
                        setup["execution"]["grant"]["remote_ref"]
                        if checkpoint == "review"
                        else "refs/heads/main"
                    )
                    remote_tip = _run(
                        ["git", "rev-parse", effect_ref],
                        remote,
                        env,
                    ).strip()
                    before_head = _run(
                        ["git", "rev-parse", "HEAD"],
                        Path(setup["workspace"]["path"]),
                        env,
                    ).strip()
                    before_remote_refs = _run(["git", "show-ref"], remote, env)
                    if checkpoint == "review":
                        assert remote_tip == workflow["item"]["candidate"]["commit_sha"]
                    else:
                        assert workflow["item"]["candidate"] is None
                        assert not workflow["close_attempts"]
                        assert (
                            remote_tip
                            == before_execution["active_item"]["initial_git_baseline"]
                        )
                        assert before_head == remote_tip
                    execution_workspace = Path(setup["workspace"]["path"])
                    result_path = execution_workspace / "result.txt"
                    before_result = result_path.read_text()
                    assert before_result == (
                        "after\n" if checkpoint == "review" else "before\n"
                    )

                    def assert_local_candidate_unchanged() -> None:
                        assert (
                            _run(
                                ["git", "rev-parse", "HEAD"], execution_workspace, env
                            ).strip()
                            == before_head
                        ), "Recovery changed the local candidate HEAD"
                        assert result_path.read_text() == before_result, (
                            "Recovery changed local fixture content"
                        )

                    (tmp_path / "before-workflow.json").write_text(
                        json.dumps(workflow, indent=2)
                    )
                    (tmp_path / "before-execution.json").write_text(
                        json.dumps(before_execution, indent=2)
                    )
                    shutil.copyfile(actual_session, tmp_path / "before-session.jsonl")
                    os.killpg(cli.pid, signal.SIGKILL)
                    assert cli.wait(timeout=10) == -signal.SIGKILL
                provider.restarting = True
                restart_log = tmp_path / "controller-after.stderr"
                restart_args = (
                    "--mode",
                    "rpc",
                    "--session",
                    str(actual_session),
                    "--provider",
                    "qualification",
                    "--model",
                    "local-recovery",
                )
                with _process(
                    release.command(state, repository, *restart_args),
                    repository,
                    env,
                    restart_log,
                ) as restarted:
                    restarted_rpc = RpcProcess(restarted, restart_log)
                    resumed = restarted_rpc.request("get_state")
                    assert resumed["sessionId"] == initial["sessionId"]
                    recovered = provider.recovered.wait(10)
                    after_response = client.get(execution_url)
                    after_response.raise_for_status()
                    after_execution = after_response.json()
                    (tmp_path / "after-execution.json").write_text(
                        json.dumps(after_execution, indent=2)
                    )
                    shutil.copyfile(actual_session, tmp_path / "after-session.jsonl")
                    evidence = {
                        "checkpoint": checkpoint,
                        "release": str(release.root),
                        "manifest": release.digest,
                        "beforePid": cli.pid,
                        "afterPid": restarted.pid,
                        "signal": "SIGKILL",
                        "session": str(actual_session),
                        "sessionId": initial["sessionId"],
                        "outbox": outbox,
                        "recovered": recovered,
                        "beforeExecution": before_execution,
                        "afterExecution": after_execution,
                        "remoteTip": remote_tip,
                        "beforeHead": before_head,
                        "beforeResult": before_result,
                        "beforeRemoteRefs": before_remote_refs,
                        "heldProviderRequest": len(provider.calls),
                    }
                    (tmp_path / "recovery-evidence.json").write_text(
                        json.dumps(evidence, indent=2)
                    )
                    startup_events = [
                        json.loads(line)
                        for line in restarted_rpc.stdout_log.read_text().splitlines()
                        if line
                    ]
                    recovery_refusals = [
                        event
                        for event in startup_events
                        if event.get("type") == "extension_ui_request"
                        and event.get("method") == "notify"
                        and "Execution recovery skipped:"
                        in str(event.get("message", ""))
                    ]
                    (tmp_path / "recovery-refusals.json").write_text(
                        json.dumps(recovery_refusals, indent=2)
                    )
                    if checkpoint == "resume":
                        assert not recovery_refusals, (
                            f"Preflight refusal prevents isolated outbox attribution: {recovery_refusals}"
                        )
                    assert after_execution["grant"] == before_execution["grant"], (
                        "Recovery must not reserve another continuation or change authority"
                    )
                    assert (
                        after_execution["active_item"]
                        == before_execution["active_item"]
                    )
                    assert _run(["git", "show-ref"], remote, env) == before_remote_refs
                    assert recovered, (
                        f"Authorized execution did not recover after SIGKILL; startup refusals: {recovery_refusals}"
                    )
                    deadline = time.monotonic() + 5
                    while (
                        restarted_rpc.request("get_state")["isStreaming"]
                        and time.monotonic() < deadline
                    ):
                        time.sleep(0.05)
                    resumed_entries = [
                        json.loads(line)
                        for line in actual_session.read_text().splitlines()
                        if line
                    ]
                    assert execution_message_count(resumed_entries) == 2, (
                        "Recovery must persist the previously hidden continuation exactly once"
                    )
                    recovered_messages = [
                        entry
                        for entry in resumed_entries
                        if entry.get("type") == "custom_message"
                        and entry.get("customType") == "work-execute"
                        and entry.get("details", {})
                        .get("executionContinuation", {})
                        .get("messageId")
                        == intent["messageId"]
                    ]
                    assert len(recovered_messages) == 1, (
                        "Recovery must inject the saved intent, not a fresh unrelated continuation"
                    )
                    identity_fields = (
                        "messageId",
                        "grantId",
                        "sessionId",
                        "workId",
                        "revisionId",
                        "preReservationVersion",
                        "postVersion",
                    )
                    recovered_identity = recovered_messages[0]["details"][
                        "executionContinuation"
                    ]
                    assert {
                        field: recovered_identity.get(field)
                        for field in identity_fields
                    } == {field: intent[field] for field in identity_fields}, (
                        "Recovered continuation changed its saved authority binding"
                    )
                    assert_local_candidate_unchanged()
                    assert provider.recovery_calls == 1
                    evidence["recoveredIdentity"] = recovered_identity
                    evidence["localCandidateUnchangedAfterRecovery"] = True
                    (tmp_path / "recovery-evidence.json").write_text(
                        json.dumps(evidence, indent=2)
                    )
                with _process(
                    release.command(state, repository, *restart_args),
                    repository,
                    env,
                    tmp_path / "controller-again.stderr",
                ) as again:
                    again_rpc = RpcProcess(again, tmp_path / "controller-again.stderr")
                    assert (
                        again_rpc.request("get_state")["sessionId"]
                        == initial["sessionId"]
                    )
                    time.sleep(1)
                    assert provider.recovery_calls == 1, (
                        "A second restart duplicated consumed continuation"
                    )
                    again_response = client.get(execution_url)
                    again_response.raise_for_status()
                    assert again_response.json()["grant"] == before_execution["grant"]
                    assert _run(["git", "show-ref"], remote, env) == before_remote_refs
                    assert_local_candidate_unchanged()
                    assert (
                        _run(
                            [
                                "git",
                                "rev-parse",
                                effect_ref,
                            ],
                            remote,
                            env,
                        ).strip()
                        == remote_tip
                    )
                    evidence["localCandidateUnchangedAfterSecondRestart"] = True
                    (tmp_path / "recovery-evidence.json").write_text(
                        json.dumps(evidence, indent=2)
                    )


def test_killed_controller_recovers_frozen_candidate_continuation(
    installed_release: InstalledRelease, tmp_path: Path
) -> None:
    """Review freeze must not strand recovery behind the previous baseline HEAD."""
    exercise_controller_recovery(installed_release, tmp_path, "review")


def test_killed_controller_recovers_queued_resume_continuation(
    installed_release: InstalledRelease, tmp_path: Path
) -> None:
    """With unchanged baseline HEAD, queued resume work must survive controller death."""
    exercise_controller_recovery(installed_release, tmp_path, "resume")

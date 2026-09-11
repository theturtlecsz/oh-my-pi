"""Real installed controller recovery; local provider supplies protocol inputs only."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import httpx
import psycopg
from installed_runtime_support import (
    AuthorityResponseProxy,
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
from omp_work.v1.api_models import CommandResponse
from pg_native import native_postgres, seed_authority
from psycopg.rows import dict_row
from test_workflow_service import _build_completion_evidence_from_view, _receipt

TaskFault = Literal[
    "child-request",
    "repeated-child-request",
    "parent-result",
    "child-result-gap",
    "first-run-result-gap",
    "child-read-gap",
]


class FirstRunCutMiss(AssertionError):
    """Observed first-run completion did not satisfy the frozen cut; preserve and retry fresh."""


class RecoveryProvider:
    """Pause a real HTTP request after production review returned its tool result."""

    def __init__(
        self,
        root: Path,
        checkpoint: Literal["review", "resume", "persisted", "task-active"],
        progress: bool = False,
        task_fault: TaskFault = "child-request",
    ):
        self.root = root
        self.progress = progress
        self.progressed = threading.Event()
        self.operation_calls = 0
        self.checkpoint = checkpoint
        self.key = ""
        self.calls: list[dict] = []
        self.barrier = threading.Event()
        self.release = threading.Event()
        self.recovered = threading.Event()
        self.restarting = False
        self.error: str | None = None
        self.recovery_calls = 0
        self.request_lock = threading.Lock()
        self.request_observations: list[dict] = []
        self.child_held = threading.Event()
        self.child_read_held = threading.Event()
        self.child_read_cleanup_release: tuple[int, float] | None = None
        self.read_resume_context: tuple[Path, dict, dict] | None = None
        self.read_resume_boundary: dict | None = None
        self.child_recovered = threading.Event()
        self.task_calls = 0
        self.task_fault = task_fault
        self.restart_generation = 0
        self.restarted_child_held = threading.Event()
        self.parent_result_held = threading.Event()
        self.completion_observer_ready = threading.Event()
        self.child_read_content: object | None = None
        self.task_assignment = (
            (Path(__file__).parent / "fixtures/task-active-assignment.md").read_text()
            if checkpoint == "task-active"
            else ""
        )

    def record_request(self, request: dict) -> int:
        with self.request_lock:
            self.calls.append(request)
            ordinal = len(self.calls)
            self.request_observations.append(
                {
                    "ordinal": ordinal,
                    "model": request.get("model"),
                    "arrivedAt": time.time(),
                    "afterRestart": self.restarting,
                    "restartGeneration": self.restart_generation,
                    "responseStarted": False,
                    "responseBytesWritten": 0,
                }
            )
            (self.root / f"provider-{ordinal}.json").write_text(json.dumps(request))
            self.save_request_observations()
            return ordinal

    def save_request_observations(self) -> None:
        (self.root / "provider-observations.json").write_text(
            json.dumps(self.request_observations, indent=2)
        )

    def observe_read_resume_claim(self, ordinal: int, request: dict) -> bool:
        """Observe durable cold claim at first resumed MAIN HTTP, before response."""
        if (
            self.task_fault != "child-read-gap"
            or not self.restarting
            or not self.is_child_main_request(request)
        ):
            return True
        with self.request_lock:
            if self.read_resume_boundary is not None:
                return self.error is None
            observation = dict(self.request_observations[ordinal - 1])
            boundary: dict = {
                "request": observation,
                "observedAt": time.time(),
                "scope": "First resumed child MAIN HTTP arrival; not proof of claim before session_start hooks",
            }
            self.read_resume_boundary = boundary
            try:
                assert (
                    observation["afterRestart"]
                    and observation["restartGeneration"] == 1
                )
                assert (
                    not observation["responseStarted"]
                    and observation["responseBytesWritten"] == 0
                )
                assert self.read_resume_context is not None, (
                    "Original read witness unavailable"
                )
                child_file, binding, ready = self.read_resume_context
                raw = child_file.read_bytes()
                snapshot = self.root / f"read-resume-request-{ordinal}-child.jsonl"
                snapshot.write_bytes(raw)
                boundary["childSnapshot"] = str(snapshot)
                boundary["childSha256"] = hashlib.sha256(raw).hexdigest()
                assert raw.endswith(b"\n"), (
                    "Incomplete child journal at resumed HTTP arrival"
                )
                entries = [
                    json.loads(line)
                    for line in raw.decode("utf-8").splitlines()
                    if line
                ]
                branch = durable_session_branch(entries)
                ready_records = [
                    entry
                    for entry in entries
                    if entry.get("customType") == "task-read-continuation-ready"
                ]
                claims = [
                    entry
                    for entry in entries
                    if entry.get("customType") == "task-read-continuation-started"
                ]
                assert ready_records == [ready] and ready in branch
                assert len(claims) == 1 and claims[0] in branch, (
                    "Cold claim not durable before resumed MAIN request"
                )
                assert claims[0]["data"] == {
                    "version": 1,
                    "protocol": "bound-native-read-v1",
                    "call": binding["call"],
                    "contractSha256": binding["contractSha256"],
                    "readyEntryId": ready["id"],
                    "readySha256": task_recovery_hash(ready["data"]),
                    "reason": "cold-resume",
                }
                assert branch.index(ready) < branch.index(claims[0])
                assert child_file.read_bytes() == raw, (
                    "Child changed while observing pre-response claim"
                )
                boundary["claim"] = claims[0]
                boundary["claimSha256"] = task_recovery_hash(claims[0]["data"])
                boundary["verifiedBeforeResponse"] = True
            except (AssertionError, KeyError, TypeError, ValueError, OSError) as error:
                self.error = f"Read-resume claim boundary failed: {error}"
                boundary["error"] = self.error
            (self.root / "read-resume-request-boundary.json").write_text(
                json.dumps(boundary, indent=2)
            )
            return self.error is None

    @staticmethod
    def is_child_main_request(request: dict) -> bool:
        names = {
            tool.get("function", {}).get("name") for tool in request.get("tools", [])
        }
        return request.get("model") == "local-task" and {"read", "yield"} <= names

    def respond(self, request: dict) -> dict:
        if request.get("model") == "local-predecessor-audit":
            artifact = Path(request["qualificationArtifact"]).resolve()
            assert artifact.is_relative_to(self.root.resolve()) and artifact.name == "result.txt"
            observed = artifact.read_text()
            verdict = "PASS" if observed == "after\n" else "NEEDS_FIX"
            report = (f"VERDICT: {verdict}\nFINDINGS\nObserved result.txt bytes: {observed!r}\n"
                "ACCEPTANCE COVERAGE\nresult.txt contains after\nOUT OF SCOPE\nProduction acceptance\n"
                "CHECKS RUN\nScripted local provider read actual installed-controller output file\n"
                "REMAINING QUESTIONS\nnone")
            return {"content": json.dumps({"verdict": verdict, "report": report})}
        if request.get("model") == "local-unrelated":
            return {"content": "Unrelated session completed its own request."}
        # A task may itself have work tools. Route by its explicitly configured
        # model before parent/auxiliary handling, including after restart.
        if self.checkpoint == "task-active" and request.get("model") == "local-task":
            if self.task_fault == "child-read-gap" and not self.is_child_main_request(
                request
            ):
                return {"content": "Recovery fixture"}
            if self.restarting or self.task_fault in (
                "first-run-result-gap",
                "child-read-gap",
            ):
                if (
                    self.task_fault in ("first-run-result-gap", "child-read-gap")
                    and not self.restarting
                    and not any(
                        message.get("role") == "tool"
                        for message in request.get("messages", [])
                    )
                ):
                    self.child_held.set()
                    self.completion_observer_ready.wait(300)
                if (
                    self.task_fault == "child-result-gap"
                    and self.restart_generation == 1
                ):
                    self.completion_observer_ready.wait(300)
                if (
                    self.task_fault == "repeated-child-request"
                    and self.restart_generation == 1
                ):
                    self.restarted_child_held.set()
                    self.release.wait(300)
                    return {"content": "Disconnected held recovery request."}
                if self.restarting:
                    self.child_recovered.set()
                results = [
                    message
                    for message in request.get("messages", [])
                    if message.get("role") == "tool"
                ]
                if not results:
                    name, arguments, call_id = (
                        "read",
                        {"path": "result.txt"},
                        "task-child-read",
                    )
                else:
                    self.child_read_content = results[-1].get("content")
                    if self.task_fault == "child-read-gap" and not self.restarting:
                        self.child_read_held.set()
                        self.release.wait(300)
                        return {
                            "content": "Disconnected post-read request released for cleanup."
                        }
                    name, arguments, call_id = (
                        "yield",
                        {
                            "result": {
                                "data": {
                                    "path": "result.txt",
                                    "observed": results[-1].get("content"),
                                }
                            }
                        },
                        "task-child-yield",
                    )
                return {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ]
                }
            self.child_held.set()
            self.release.wait(300)
            return {
                "content": "Disconnected pre-kill child response released for fixture cleanup."
            }
        tools = request.get("tools", [])
        if not any(tool.get("function", {}).get("name") == "work" for tool in tools):
            return {"content": "Recovery fixture"}
        if self.restarting:
            if self.checkpoint == "task-active":
                results = [
                    message
                    for message in request.get("messages", [])
                    if message.get("role") == "tool"
                    and message.get("tool_call_id") == "task-active-call"
                ]
                if len(results) != 1:
                    self.error = (
                        f"Parent resumed without original task result: {results}"
                    )
                if self.task_fault == "parent-result" and self.restart_generation == 1:
                    self.parent_result_held.set()
                    self.release.wait(300)
                    return {"content": "Disconnected parent result request."}
            self.recovery_calls += 1
            self.recovered.set()
            if self.progress:
                results = [
                    message
                    for message in request.get("messages", [])
                    if message.get("role") == "tool"
                ]
                if not results:
                    self.operation_calls += 1
                    return {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "recovered-criteria",
                                "type": "function",
                                "function": {
                                    "name": "work",
                                    "arguments": json.dumps(
                                        {
                                            "action": "seal_execution_criteria",
                                            "work": self.key,
                                            "criteria": ["result.txt contains after"],
                                        }
                                    ),
                                },
                            }
                        ]
                    }
                if "criteria sealed successfully" not in str(
                    results[-1].get("content", "")
                ):
                    self.error = str(results[-1])
                self.progressed.set()
            return {"content": "Recovery continuation observed."}
        if self.checkpoint == "task-active":
            results = [
                message
                for message in request.get("messages", [])
                if message.get("role") == "tool"
            ]
            if self.task_fault == "first-run-result-gap" and results:
                return {"content": "First-run parent received the actual task result."}
            if results or self.task_calls:
                self.error = (
                    f"Task did not enter held child boundary: {json.dumps(results)}"
                )
                self.barrier.set()
                return {"content": "Task fixture setup failed."}
            self.task_calls += 1
            return {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "task-active-call",
                        "type": "function",
                        "function": {
                            "name": "task",
                            "arguments": json.dumps(
                                {
                                    "name": "RecoveryTask",
                                    "agent": "task",
                                    "task": self.task_assignment,
                                }
                            ),
                        },
                    }
                ]
            }
        if self.checkpoint in ("resume", "persisted"):
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
                ordinal = provider.record_request(request)
                if not provider.observe_read_resume_claim(ordinal, request):
                    return
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
                    with provider.request_lock:
                        cleanup_release = provider.child_read_cleanup_release
                        if (
                            cleanup_release is not None
                            and cleanup_release[0] == ordinal
                        ):
                            provider.request_observations[ordinal - 1][
                                "cleanupAfterDeath"
                            ] = True
                            provider.request_observations[ordinal - 1][
                                "cleanupReleasedAt"
                            ] = cleanup_release[1]
                        provider.request_observations[ordinal - 1][
                            "responseStarted"
                        ] = True
                        provider.request_observations[ordinal - 1][
                            "responseStartedAt"
                        ] = time.time()
                        provider.save_request_observations()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    with provider.request_lock:
                        provider.request_observations[ordinal - 1][
                            "responseBytesWritten"
                        ] = len(data)
                        provider.save_request_observations()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/v1"
        finally:
            self.release.set()
            self.completion_observer_ready.set()
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


def read_complete_session(path: Path) -> list[dict]:
    """Read only complete system-written JSONL records while a process appends."""
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return []
    complete, separator, _tail = raw.rpartition("\n")
    if not separator:
        return []
    return [json.loads(line) for line in complete.splitlines() if line]


def process_group_snapshot(
    group_id: int, *, include_threads: bool = False
) -> list[dict]:
    """Observe Linux processes; task registry IDs are never treated as PIDs."""
    processes: list[dict] = []
    for directory in Path("/proc").iterdir():
        if not directory.name.isdigit():
            continue
        try:
            fields = (directory / "stat").read_text().rsplit(")", 1)[1].split()
            if int(fields[2]) != group_id:
                continue
            record = {
                "pid": int(directory.name),
                "state": fields[0],
                "parentPid": int(fields[1]),
                "processGroupId": int(fields[2]),
                "processSessionId": int(fields[3]),
                "startTicks": fields[19],
                "argv": (directory / "cmdline")
                .read_bytes()
                .decode(errors="replace")
                .rstrip("\x00")
                .split("\x00"),
            }
            for link in ("exe", "cwd"):
                try:
                    record[link] = os.readlink(directory / link)
                except OSError:
                    record[link] = None
            if include_threads:
                threads = []
                for task in (directory / "task").iterdir():
                    try:
                        task_fields = (
                            (task / "stat").read_text().rsplit(")", 1)[1].split()
                        )
                        threads.append(
                            {
                                "tid": int(task.name),
                                "state": task_fields[0],
                                "startTicks": task_fields[19],
                            }
                        )
                    except (FileNotFoundError, ProcessLookupError):
                        continue
                record["threads"] = sorted(threads, key=lambda task: task["tid"])
            processes.append(record)
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    return sorted(processes, key=lambda process: process["pid"])


def installed_cli_host(
    release: InstalledRelease, topology: list[dict], wrapper_pid: int
) -> dict:
    hosts = [
        row
        for row in topology
        if str(release.root / "source/packages/coding-agent/src/cli.ts") in row["argv"]
    ]
    assert len(hosts) == 1, topology
    host = hosts[0]
    by_pid = {row["pid"]: row for row in topology}
    launcher = by_pid.get(host["parentPid"])
    assert (
        launcher is not None
        and str(release.root / "source/session-system/runtime/run.ts")
        in launcher["argv"]
    ), topology
    seen = {host["pid"]}
    ancestor = host
    while ancestor["pid"] != wrapper_pid:
        assert ancestor["parentPid"] not in seen and ancestor["parentPid"] in by_pid, (
            topology
        )
        ancestor = by_pid[ancestor["parentPid"]]
        seen.add(ancestor["pid"])
    return host


def durable_session_branch(entries: list[dict]) -> list[dict]:
    """Walk the leaf SessionManager reconstructs from complete on-disk entries."""
    physical = entries[1:] if entries and entries[0].get("type") == "title" else entries
    if not physical:
        return []
    assert physical[0].get("type") == "session", "Journal has no session header"
    indexed = physical[1:]
    by_id = {}
    for entry in indexed:
        assert entry.get("type") not in ("session", "title"), (
            "Unexpected journal header"
        )
        entry_id = entry.get("id")
        assert isinstance(entry_id, str) and entry_id not in by_id, (
            "Missing or duplicate journal entry ID"
        )
        by_id[entry_id] = entry
    branch = []
    seen = set()
    leaf = indexed[-1]["id"] if indexed else None
    while leaf is not None:
        assert leaf not in seen, "Durable journal branch contains a cycle"
        assert leaf in by_id, "Durable journal branch has a missing parent"
        seen.add(leaf)
        entry = by_id[leaf]
        branch.append(entry)
        leaf = entry["parentId"]
    return list(reversed(branch))


def task_preparation_records(
    entries: list[dict], binding: dict, *, require_complete_suffix: bool = False
) -> list[dict]:
    """Validate production-owned membership without classifying context by text."""
    entries = durable_session_branch(entries)
    records = [
        entry["data"]
        for entry in entries
        if entry.get("type") == "custom"
        and entry.get("customType") == "prompt-preparation"
        and entry.get("data", {}).get("taskBindingId") == binding["call"]["bindingId"]
    ]
    assert records, "Child has no durable core preparation association"
    by_id = {entry["id"]: entry for entry in entries if "id" in entry}
    anchors = {record["anchorEntryId"] for record in records}
    assert len(anchors) == 1, "Recovery injected a replacement child assignment"
    anchor_id = next(iter(anchors))
    anchor = by_id[anchor_id]
    assert anchor["type"] == "message" and anchor["message"]["role"] == "user"
    assert anchor["message"]["attribution"] == "agent"
    batches = set()
    members = set()
    positions = {
        entry["id"]: index for index, entry in enumerate(entries) if "id" in entry
    }
    for record in records:
        assert record["version"] == 1
        assert record["sessionId"] == binding["child"]["sessionId"]
        assert record["batchId"] not in batches
        batches.add(record["batchId"])
        receipt = next(
            entry
            for entry in entries
            if entry.get("customType") == "prompt-preparation"
            and entry.get("data") == record
        )
        ancestors = set()
        parent_id = receipt.get("parentId")
        while parent_id is not None:
            assert parent_id not in ancestors, "Preparation branch contains a cycle"
            ancestors.add(parent_id)
            parent_id = by_id[parent_id].get("parentId")
        assert anchor_id in ancestors
        for member_id in record["preparationEntryIds"]:
            assert member_id != anchor_id and member_id not in members
            members.add(member_id)
            assert member_id in ancestors, (
                "Preparation receipt references another branch"
            )
            member = by_id[member_id]
            assert positions[member_id] > positions[anchor_id]
            assert (
                member.get("attribution", member.get("message", {}).get("attribution"))
                == "agent"
            )
    if require_complete_suffix:
        conversation_suffix = {
            entry["id"]
            for entry in entries[positions[anchor_id] + 1 :]
            if entry.get("type") in ("message", "custom_message")
        }
        assert conversation_suffix == members, (
            "Held child has conversation outside core preparation membership"
        )
    return records


def task_recovery_hash(value: object) -> str:
    """Hash canonical fixture JSON with taskRecoveryHash's sorted-key domain."""
    canonical = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(b"omp-task-recovery-v1\0" + canonical.encode()).hexdigest()


def require_task_binding(
    parent: list[dict], child: list[dict], child_snapshot: dict
) -> dict:
    bindings = [
        entry
        for entry in parent
        if entry.get("type") == "custom"
        and entry.get("customType") == "task-run-binding"
    ]
    assert len(bindings) == 1, "Original call must have one production-written binding"
    parent_branch = durable_session_branch(parent)
    child_branch = durable_session_branch(child)
    assert bindings[0] in parent_branch, "Task binding is outside durable parent branch"
    binding = bindings[0]["data"]
    assert binding["version"] == 1 and binding["mode"] == "sync-flat"
    assert task_recovery_hash(binding["contract"]) == binding["contractSha256"]
    call = binding["call"]
    parent_header = next(entry for entry in parent if entry.get("type") == "session")
    assert call["sessionId"] == parent_header["id"]
    assistant = next(
        entry for entry in parent_branch if entry.get("id") == call["assistantEntryId"]
    )
    calls = [
        part
        for part in assistant["message"]["content"]
        if part.get("type") == "toolCall"
    ]
    assert (
        len(calls) == 1
        and calls[0]["id"] == call["toolCallId"] == child_snapshot["parentToolCallId"]
    )
    assert calls[0]["name"] == "task"
    raw_args = calls[0]["arguments"]
    assert set(raw_args) == {"name", "agent", "task"}
    effective_args = {key: raw_args[key].strip() for key in ("name", "agent", "task")}
    assert binding["contract"]["args"] == effective_args
    assert call["argumentsSha256"] == task_recovery_hash(effective_args)
    prompt = next(
        entry for entry in parent_branch if entry.get("id") == call["promptEntryId"]
    )
    assert (
        parent_branch.index(prompt)
        < parent_branch.index(assistant)
        < parent_branch.index(bindings[0])
    ), "Prompt, assistant and binding are not ordered on durable ancestry"
    assert prompt["type"] == "custom_message" and prompt["customType"] == "work-execute"
    assert any(
        entry.get("customType") == "prompt-preparation"
        and entry.get("data", {}).get("anchorEntryId") == prompt["id"]
        and entry["data"]["sessionId"] == call["sessionId"]
        for entry in parent_branch
    ), "Parent binding lacks core prompt provenance"
    child_header = next(entry for entry in child if entry.get("type") == "session")
    init = next(entry for entry in child_branch if entry.get("type") == "session_init")
    assert binding["child"]["registryId"] == child_snapshot["id"] == "RecoveryTask"
    assert binding["child"]["sessionId"] == child_header["id"]
    assert Path(binding["child"]["sessionFile"]) == Path(child_snapshot["sessionFile"])
    assert binding["child"]["initEntryId"] == init["id"]
    assert init["taskCall"] == call
    initialization_fields = (
        "systemPrompt",
        "task",
        "tools",
        "agent",
        "modelRole",
        "resolvedModel",
        "readOnly",
        "outputSchema",
        "outputSchemaMode",
        "restrictToolNames",
        "spawns",
        "readSummarize",
        "advisor",
    )
    assert binding["contract"]["initialization"] == {
        key: init[key] for key in initialization_fields if key in init
    }
    assert binding["contract"]["runtime"]["model"] == {
        "provider": "qualification",
        "api": "openai-completions",
        "id": "local-task",
    }
    task_preparation_records(child, binding)
    return binding


def capture_task_active_recovery(
    *,
    release: InstalledRelease,
    tmp_path: Path,
    repository: Path,
    remote: Path,
    state: Path,
    env: dict[str, str],
    client: httpx.Client,
    workspace_id: str,
    setup: dict,
    provider: RecoveryProvider,
    cli: subprocess.Popen,
    rpc: RpcProcess,
    initial: dict[str, object],
    service_pid: int,
    authority_proxy: AuthorityResponseProxy | None = None,
) -> None:
    """Fault observer for real synchronous task execution; no runtime repair writes."""
    evidence: dict = {
        "checkpoint": "task-active",
        "taskFault": provider.task_fault,
        "release": str(release.root),
        "manifest": release.digest,
        "launcherPid": cli.pid,
        "barrierReached": False,
    }
    if provider.task_fault == "child-result-gap":
        evidence["completionBarrierReached"] = False
    evidence_file = tmp_path / "task-recovery-evidence.json"

    def save() -> None:
        evidence_file.write_text(json.dumps(evidence, indent=2))

    def get_view(url: str) -> dict:
        response = client.get(url)
        response.raise_for_status()
        return response.json()

    execution_url = f"/v1/workspaces/{workspace_id}/execution/{provider.key}"
    workflow_url = f"/v1/work-items/{provider.key}/workflow"
    execution_workspace = Path(setup["workspace"]["path"])

    def effects() -> dict:
        return {
            "head": _run(
                ["git", "rev-parse", "HEAD"], execution_workspace, env
            ).strip(),
            "dirtyPaths": _run(
                ["git", "status", "--porcelain"], execution_workspace, env
            ),
            "result": (execution_workspace / "result.txt").read_text(),
            "remoteRefs": _run(["git", "show-ref"], remote, env),
        }

    deadline = time.monotonic() + 40
    while not provider.child_held.wait(0.05) and time.monotonic() < deadline:
        if provider.error:
            break
    evidence["setupError"] = provider.error
    evidence["initialRpcState"] = initial
    evidence["executionBeforeBarrier"] = get_view(execution_url)
    evidence["subagentsAtBarrier"] = rpc.request("get_subagents")
    save()
    assert provider.error is None, provider.error
    assert provider.child_held.is_set(), (
        "Actual child provider request never reached the task-active barrier"
    )

    current = rpc.request("get_state")
    parent_path = Path(str(current["sessionFile"]))
    evidence["parentRpcState"] = current
    evidence["parentSessionFile"] = str(parent_path)
    assert current["sessionId"] == initial["sessionId"]
    assert current["isStreaming"] is True
    task_tools = [
        tool for tool in current.get("dumpTools", []) if tool["name"] == "task"
    ]
    assert len(task_tools) == 1, (
        "Installed parent did not advertise the actual task tool"
    )
    task_properties = task_tools[0]["parameters"]["properties"]
    assert "task" in task_properties and "tasks" not in task_properties, task_properties
    evidence["taskTool"] = task_tools[0]

    snapshots = evidence["subagentsAtBarrier"]["subagents"]
    assert len(snapshots) == 1, snapshots
    child = snapshots[0]
    assert child["status"] == "running", child
    assert child["agent"] == "task" and child["agentSource"] == "bundled", child
    assert child["id"] == "RecoveryTask", (
        "Unexpected fresh task allocation or role override"
    )
    child_path = Path(child["sessionFile"])
    call_id = child["parentToolCallId"]
    assert call_id, "Live lifecycle did not bind child to original parent tool call"
    evidence["childSnapshot"] = child

    deadline = time.monotonic() + 10
    while True:
        parent_entries = read_complete_session(parent_path)
        child_entries = read_complete_session(child_path)
        parent_calls = [
            part
            for entry in parent_entries
            if entry.get("type") == "message"
            and entry.get("message", {}).get("role") == "assistant"
            for part in entry["message"].get("content", [])
            if part.get("type") == "toolCall" and part.get("id") == call_id
        ]
        starts = [
            entry
            for entry in parent_entries
            if entry.get("type") == "custom"
            and entry.get("customType") == "tool_execution_start"
            and entry.get("data", {}).get("toolCallId") == call_id
        ]
        inits = [
            entry for entry in child_entries if entry.get("type") == "session_init"
        ]
        task_messages = [
            entry
            for entry in child_entries
            if entry.get("type") == "message"
            and entry.get("message", {}).get("role") in ("user", "developer")
            and provider.task_assignment.strip()
            in json.dumps(entry["message"], ensure_ascii=False).replace("\\n", "\n")
        ]
        has_binding = any(
            entry.get("customType") == "task-run-binding" for entry in parent_entries
        )
        has_preparation = any(
            entry.get("customType") == "prompt-preparation" for entry in child_entries
        )
        if (
            parent_calls
            and starts
            and inits
            and task_messages
            and has_binding
            and has_preparation
        ) or time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    shutil.copyfile(parent_path, tmp_path / "task-parent-before.jsonl")
    if child_path.exists():
        shutil.copyfile(child_path, tmp_path / "task-child-before.jsonl")
    evidence["parentToolCalls"] = parent_calls
    evidence["parentToolStarts"] = starts
    evidence["childSessionInit"] = inits
    save()
    assert len(parent_calls) == 1 and parent_calls[0]["name"] == "task", parent_calls
    assert parent_calls[0]["arguments"] == {
        "name": "RecoveryTask",
        "agent": "task",
        "task": provider.task_assignment,
    }
    assert len(starts) == 1, "Parent task execution-start was not durable"
    assert not any(
        entry.get("type") == "message"
        and entry.get("message", {}).get("role") == "toolResult"
        and entry["message"].get("toolCallId") == call_id
        for entry in parent_entries
    )
    assert not any(
        entry.get("type") == "message"
        and entry.get("message", {}).get("role") == "assistant"
        and entry["message"].get("stopReason") == "stop"
        for entry in parent_entries
    )
    assert len(inits) == 1 and task_messages, (
        "Child initialization and assignment were not persisted"
    )
    child_headers = [entry for entry in child_entries if entry.get("type") == "session"]
    assert len(child_headers) == 1
    child_header = child_headers[0]
    assert child_header["id"] != current["sessionId"]
    assert Path(child_header["cwd"]).resolve() == execution_workspace.resolve()
    assert inits[0]["agent"] == "task"
    assert inits[0]["modelRole"] == "task"
    assert inits[0]["resolvedModel"] == "qualification/local-task"
    assert "yield" in inits[0]["tools"]
    assert "spawns" in inits[0]
    binding = require_task_binding(parent_entries, child_entries, child)
    original_preparation = task_preparation_records(
        child_entries, binding, require_complete_suffix=True
    )
    evidence["taskBinding"] = binding
    evidence["originalPreparation"] = original_preparation
    assert not any(
        entry.get("type") == "message"
        and entry.get("message", {}).get("role") in ("assistant", "toolResult")
        or entry.get("type") == "custom"
        and entry.get("customType") == "tool_execution_start"
        for entry in child_entries
    )
    child_requests = [
        row
        for row in provider.request_observations
        if row["model"] == "local-task"
        and (
            provider.task_fault != "child-read-gap"
            or provider.is_child_main_request(provider.calls[row["ordinal"] - 1])
        )
    ]
    assert len(child_requests) == 1 and child_requests[0]["responseStarted"] is False
    child_request = provider.calls[child_requests[0]["ordinal"] - 1]
    assert provider.task_assignment.strip() in json.dumps(
        child_request, ensure_ascii=False
    ).replace("\\n", "\n")
    assert any(
        tool.get("function", {}).get("name") == "yield"
        for tool in child_request.get("tools", [])
    )

    before_execution = get_view(execution_url)
    before_workflow = get_view(workflow_url)
    bound_messages = [
        entry
        for entry in parent_entries
        if entry.get("type") == "custom_message"
        and entry.get("customType") == "work-execute"
    ]
    assert len(bound_messages) == 1
    intent = bound_messages[0]["details"]["executionContinuation"]
    active = before_execution["active_item"]
    assert before_execution["grant"]["state"] == "active"
    assert intent["grantId"] == before_execution["grant"]["grant_id"]
    assert intent["postVersion"] == before_execution["grant"]["grant_version"]
    assert intent["sessionId"] == current["sessionId"]
    assert intent["workId"] == active["work_id"]
    assert intent["revisionId"] == (
        active["criteria_revision_id"] or active["claimed_revision_id"]
    )
    assert before_execution == evidence["executionBeforeBarrier"]
    assert before_workflow["item"]["candidate"] is None
    assert before_workflow["close_attempts"] == []
    delivered = {
        row["event_id"]
        for row in before_workflow.get("checkpoint_deliveries", [])
        if row["status"] == "delivered"
    }
    assert not any(
        row["requires_delivery"] and row["event_id"] not in delivered
        for row in before_workflow.get("close_attempt_events", [])
    )
    pending_records = []
    for claim_path in sorted(
        (state / "config/omp-work/pending-operations").glob("*.json")
    ):
        claim = json.loads(claim_path.read_text())
        pending_records.append({"file": str(claim_path), "record": claim})
    evidence["pendingOperationRecordsBefore"] = pending_records
    save()
    assert all(
        record["record"].get("result") is not None for record in pending_records
    ), "An unresolved setup mutation would confound task recovery"
    before_effects = effects()
    assert before_effects["result"] == "before\n" and before_effects["dirtyPaths"] == ""
    evidence.update(
        {
            "intentEntry": bound_messages[0],
            "beforeExecution": before_execution,
            "beforeWorkflow": before_workflow,
            "beforeEffects": before_effects,
            "providerObservationsBefore": [
                dict(row) for row in provider.request_observations
            ],
            "kills": [],
            "restarts": [],
        }
    )

    def kill_host(
        process: subprocess.Popen, label: str, *, qualified: bool = True
    ) -> None:
        group_id = os.getpgid(process.pid)
        topology = process_group_snapshot(group_id)
        host = installed_cli_host(release, topology, process.pid)
        assert group_id == process.pid
        postgres_pid = int(
            (tmp_path / "postgres/pgdata/postmaster.pid").read_text().splitlines()[0]
        )
        outsiders = [service_pid, os.getpid(), postgres_pid]
        assert all(os.getpgid(pid) != group_id for pid in outsiders)
        record = {
            "label": label,
            "launcherPid": process.pid,
            "launcherRole": "bubblewrap external GitHub fixture"
            if release.github_adapter
            else "runtime launcher",
            "runtimeLauncherPid": host["parentPid"],
            "processGroupId": group_id,
            "cliPidHostingParentAndChildSessions": host["pid"],
            "topologyBefore": topology,
            "outsideGroup": outsiders,
            "signal": "SIGKILL",
            "killedAt": time.time(),
        }
        evidence["kills"].append(record)
        if qualified:
            evidence["barrierReached"] = True
        save()
        os.killpg(group_id, signal.SIGKILL)
        record["exitCode"] = process.wait(timeout=10)
        assert record["exitCode"] == -signal.SIGKILL
        deadline = time.monotonic() + 5
        while True:
            after_topology = process_group_snapshot(group_id)
            alive = [
                row
                for row in after_topology
                if row["pid"] == host["pid"]
                and row["startTicks"] == host["startTicks"]
                and row["state"] not in ("Z", "X")
            ]
            if not alive or time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        record["topologyAfterKill"] = after_topology
        save()
        assert not alive, "Actual CLI hosting the task survived group kill"

    def original_results(entries: list[dict]) -> list[dict]:
        return [
            entry
            for entry in entries
            if entry.get("type") == "message"
            and entry.get("message", {}).get("role") == "toolResult"
            and entry["message"].get("toolCallId") == call_id
        ]

    def result_records(entries: list[dict], custom_type: str) -> list[dict]:
        # One task exists in this fixture. Scan retained history, not just the
        # active branch, so rewind cannot hide competing completion ownership.
        return [
            entry
            for entry in entries
            if entry.get("type") == "custom" and entry.get("customType") == custom_type
        ]

    def native_ready(
        entries: list[dict], *, expected_producer: str = "recovered-sync-task-v1"
    ) -> dict | None:
        records = result_records(entries, "task-native-result-ready")
        if not records:
            return None
        assert len(records) == 1, "Competing retained native completion records"
        entry = records[0]
        branch = durable_session_branch(entries)
        assert entry in branch, "Native ready checkpoint is outside bound branch"
        data = entry["data"]
        assert data["version"] == 1 and data["producer"] == expected_producer
        assert data["processingProtocol"] == "claim-before-parent-processing-v1"
        assert (
            data["call"] == binding["call"]
            and data["contractSha256"] == binding["contractSha256"]
        )
        assert data["child"]["sessionId"] == binding["child"]["sessionId"]
        assert data["child"]["initEntryId"] == binding["child"]["initEntryId"]
        assert data["payloadSha256"] == task_recovery_hash(data["payloadJson"])
        raw = json.loads(data["payloadJson"])
        assert not raw.get("isError")
        native = raw["details"]["results"]
        assert len(native) == 1 and native[0]["id"] == child["id"]
        assert native[0]["agent"] == "task" and native[0]["agentSource"] == "bundled"
        assert (
            native[0]["exitCode"] == 0
            and not native[0].get("aborted")
            and not native[0].get("error")
        )
        assert native[0]["outputPath"] == data["output"]["path"]
        assert any(
            item.get("status") == "success"
            for item in native[0]["extractedToolData"]["yield"]
        )
        return entry

    def require_unstarted_native_ready(
        parent_entries: list[dict],
        child_entries: list[dict],
        child_bytes: bytes,
        output_path: Path,
        output_bytes: bytes,
        *,
        expected_producer: str = "recovered-sync-task-v1",
    ) -> dict:
        entry = native_ready(parent_entries, expected_producer=expected_producer)
        assert entry is not None, "Completed cut has no runtime-certified ready result"
        assert not result_records(parent_entries, "task-result-processing-started"), (
            "Parent processing already entered before frozen cut"
        )
        data = entry["data"]
        branch = durable_session_branch(child_entries)
        indexed_entries = [
            row for row in child_entries if row.get("type") not in ("title", "session")
        ]
        yields = [
            row
            for row in branch
            if row.get("type") == "message"
            and row.get("message", {}).get("role") == "toolResult"
            and row["message"].get("toolCallId") == "task-child-yield"
        ]
        assert len(yields) == 1 and not yields[0]["message"].get("isError")
        assert data["child"] == {
            "sessionId": binding["child"]["sessionId"],
            "initEntryId": binding["child"]["initEntryId"],
            "promptEntryId": original_preparation[0]["anchorEntryId"],
            "leafId": branch[-1]["id"],
            "branchSha256": task_recovery_hash(branch),
            "entriesSha256": task_recovery_hash(indexed_entries),
            "fileSha256": hashlib.sha256(child_bytes).hexdigest(),
            "yieldResultEntryId": yields[0]["id"],
        }
        assert data["output"] == {
            "path": str(output_path),
            "bytes": len(output_bytes),
            "sha256": hashlib.sha256(output_bytes).hexdigest(),
        }
        assert json.loads(
            json.loads(data["payloadJson"])["details"]["results"][0]["output"]
        ) == {
            "path": "result.txt",
            "observed": provider.child_read_content,
        }
        return entry

    def successful_result(entries: list[dict]) -> dict:
        results = original_results(entries)
        assert len(results) == 1 and not results[0]["message"].get("isError"), results
        entry = results[0]
        branch = durable_session_branch(entries)
        assert entry in branch, "Original task result is outside durable parent branch"
        binding_entry = next(
            row for row in branch if row.get("customType") == "task-run-binding"
        )
        assert branch.index(binding_entry) < branch.index(entry), (
            "Task result precedes original binding"
        )
        expected_ref = {
            "bindingId": binding["call"]["bindingId"],
            "contractSha256": binding["contractSha256"],
            "toolCallId": call_id,
            "childSessionId": binding["child"]["sessionId"],
        }
        ready_entry = native_ready(
            entries,
            expected_producer="original-sync-task-v1"
            if provider.task_fault == "first-run-result-gap"
            else "recovered-sync-task-v1",
        )
        claims = result_records(entries, "task-result-processing-started")
        if ready_entry is not None:
            assert len(claims) == 1, (
                "Native ready result needs one retained processing claim"
            )
            claim = claims[0]
            assert claim in branch
            assert claim["data"] == {
                "version": 1,
                "protocol": "claim-before-parent-processing-v1",
                "call": binding["call"],
                "contractSha256": binding["contractSha256"],
                "readyEntryId": ready_entry["id"],
                "readySha256": task_recovery_hash(ready_entry["data"]),
            }
            assert (
                branch.index(binding_entry)
                < branch.index(ready_entry)
                < branch.index(claim)
                < branch.index(entry)
            )
            expected_ref["completion"] = {
                "readyEntryId": ready_entry["id"],
                "readySha256": task_recovery_hash(ready_entry["data"]),
                "processingEntryId": claim["id"],
                "processingSha256": task_recovery_hash(claim["data"]),
            }
            assert (
                entry["message"]["details"]["results"]
                == json.loads(ready_entry["data"]["payloadJson"])["details"]["results"]
            ), "Parent processing changed the certified native outcome"
        else:
            assert not claims, "Processing claim has no native ready checkpoint"
        assert entry["taskResult"] == expected_ref, (
            "Parent result lost core binding after result processing"
        )
        native = entry["message"]["details"]["results"]
        assert len(native) == 1 and native[0]["id"] == child["id"]
        assert native[0]["agent"] == "task" and native[0]["agentSource"] == "bundled"
        assert native[0]["exitCode"] == 0
        assert provider.child_read_content is not None
        assert "before" in str(provider.child_read_content)
        assert json.loads(native[0]["output"]) == {
            "path": "result.txt",
            "observed": provider.child_read_content,
        }, "Actual task result did not contain read-derived yield output"
        return entry

    def terminal_parent_answer(entries: list[dict]) -> dict | None:
        results = original_results(entries)
        assert len(results) <= 1, (
            "Whole journal contains duplicate original task results"
        )
        branch = durable_session_branch(entries)
        if not results or results[0] not in branch:
            return None
        result_index = branch.index(results[0])
        return next(
            (
                entry
                for entry in branch[result_index + 1 :]
                if entry.get("type") == "message"
                and entry.get("message", {}).get("role") == "assistant"
                and entry["message"].get("stopReason") == "stop"
            ),
            None,
        )

    def assert_stable_state() -> None:
        assert get_view(execution_url) == before_execution, (
            "Task recovery changed execution authority"
        )
        assert get_view(workflow_url) == before_workflow, (
            "Task recovery changed workflow effects"
        )
        assert effects() == before_effects, "Task recovery changed local/Git effects"

    def assert_same_journals() -> tuple[list[dict], list[dict]]:
        parent_now = read_complete_session(parent_path)
        child_now = read_complete_session(child_path)
        execution_messages = [
            entry
            for entry in parent_now
            if entry.get("type") == "custom_message"
            and entry.get("customType") == "work-execute"
        ]
        assert execution_messages == [bound_messages[0]], (
            "Recovery duplicated or changed original execution prompt"
        )
        assert require_task_binding(parent_now, child_now, child) == binding
        assert (
            task_preparation_records(child_now, binding)[0] == original_preparation[0]
        )
        calls = [
            part
            for entry in parent_now
            if entry.get("type") == "message"
            and entry.get("message", {}).get("role") == "assistant"
            for part in entry["message"].get("content", [])
            if part.get("type") == "toolCall"
        ]
        assert calls == parent_calls, "Recovery issued a replacement parent task call"
        starts_now = [
            entry
            for entry in parent_now
            if entry.get("customType") == "tool_execution_start"
            and entry.get("data", {}).get("toolCallId") == call_id
        ]
        assert starts_now == starts, "Recovery replayed original task tool start"
        assert (
            len(
                [
                    entry
                    for entry in child_now
                    if entry.get("type") == "message"
                    and entry.get("message", {}).get("role") == "user"
                ]
            )
            == 1
        ), "Recovery appended a replacement child assignment"
        assert_stable_state()
        return parent_now, child_now

    def assert_parent_wire(observations: list[dict]) -> None:
        parent_requests = [
            provider.calls[row["ordinal"] - 1]
            for row in observations
            if row["model"] == "local-recovery"
            and any(
                tool.get("function", {}).get("name") == "work"
                for tool in provider.calls[row["ordinal"] - 1].get("tools", [])
            )
        ]
        assert len(parent_requests) == 1, (
            "Cached result needs one actual tool-bearing parent request"
        )
        messages = parent_requests[0]["messages"]
        wire_calls = [
            (index, tool_call)
            for index, message in enumerate(messages)
            if message.get("role") == "assistant"
            for tool_call in message.get("tool_calls", [])
        ]
        assert len(wire_calls) == 1, (
            "Parent wire omitted or duplicated original task call"
        )
        call_index, wire_call = wire_calls[0]
        assert wire_call["id"] == call_id
        assert wire_call["function"]["name"] == parent_calls[0]["name"] == "task"
        assert (
            json.loads(wire_call["function"]["arguments"])
            == parent_calls[0]["arguments"]
        ), "Parent wire changed original raw task arguments"
        wire_results = [
            (index, message)
            for index, message in enumerate(messages)
            if message.get("role") == "tool" and message.get("tool_call_id") == call_id
        ]
        assert len(wire_results) == 1 and wire_results[0][0] > call_index, (
            "Parent wire has no unique result after original task call"
        )

    if provider.task_fault == "child-read-gap":
        evidence["childReadCutReached"] = False

        def assert_no_late_completion(events: list[dict]) -> None:
            assert provider.error is None, provider.error
            assert not child_path.with_suffix(".md").exists(), (
                "Task output published outside post-read cut"
            )
            assert not any(
                event.get("type")
                in ("tool_execution_end", "message_start", "message_end")
                and (
                    event.get("toolCallId") == call_id
                    or event.get("message", {}).get("toolCallId") == call_id
                )
                for event in events
            ), "Received parent result outside post-read cut"
            assert not any(
                event.get("type") == "subagent_lifecycle"
                and event.get("payload", {}).get("id") == child["id"]
                and event["payload"].get("status") in ("completed", "failed", "aborted")
                for event in events
            ), "Child reached terminal lifecycle outside post-read cut"
            assert not any(
                row["restartGeneration"] == 0
                and row["model"] == "local-recovery"
                and row["ordinal"] > child_requests[0]["ordinal"]
                and any(
                    tool.get("function", {}).get("name") == "work"
                    for tool in provider.calls[row["ordinal"] - 1].get("tools", [])
                )
                for row in provider.request_observations
            ), "Original parent dispatched another main request before death"

        provider.completion_observer_ready.set()
        try:
            assert provider.child_read_held.wait(45), (
                "Original child never reached post-read response hold"
            )
            assert provider.error is None, provider.error
            deadline = time.monotonic() + 10
            while True:
                observed_child = read_complete_session(child_path)
                observed_branch = durable_session_branch(observed_child)
                if any(
                    entry.get("message", {}).get("role") == "toolResult"
                    and entry["message"].get("toolCallId") == "task-child-read"
                    for entry in observed_branch
                ) and any(
                    entry.get("customType") == "tool_execution_start"
                    and entry.get("data", {}).get("toolCallId") == "task-child-read"
                    for entry in observed_branch
                ):
                    break
                assert time.monotonic() < deadline, (
                    "Held request lacks a complete durable read boundary"
                )
                time.sleep(0.05)
            parent_now, child_now = assert_same_journals()
            parent_bytes = parent_path.read_bytes()
            child_bytes = child_path.read_bytes()
            assert parent_bytes.endswith(b"\n") and child_bytes.endswith(b"\n"), (
                "Incomplete journal at post-read cut"
            )
            assert read_complete_session(parent_path) == parent_now
            assert read_complete_session(child_path) == child_now
            branch = durable_session_branch(child_now)
            calls = [
                part
                for entry in branch
                if entry.get("type") == "message"
                and entry.get("message", {}).get("role") == "assistant"
                for part in entry["message"].get("content", [])
                if part.get("type") == "toolCall"
            ]
            results = [
                entry
                for entry in branch
                if entry.get("type") == "message"
                and entry.get("message", {}).get("role") == "toolResult"
            ]
            assert len(calls) == len(results) == 1
            read_call, read_entry = calls[0], results[0]
            read_assistants = [
                entry
                for entry in branch
                if entry.get("type") == "message"
                and entry.get("message", {}).get("role") == "assistant"
            ]
            assert len(read_assistants) == 1, "Read prefix has another assistant turn"
            read_assistant = read_assistants[0]
            assert read_call in read_assistant["message"]["content"]
            assert branch.index(read_assistant) < branch.index(read_entry)
            assert (
                read_call["id"]
                == read_entry["message"]["toolCallId"]
                == "task-child-read"
            )
            assert read_call["name"] == read_entry["message"]["toolName"] == "read"
            assert read_call["arguments"] == {"path": "result.txt"}
            assert not read_entry["message"].get("isError")
            child_read_starts = [
                entry
                for entry in child_now
                if entry.get("customType") == "tool_execution_start"
            ]
            assert len(child_read_starts) == 1 and child_read_starts[0] in branch
            assert child_read_starts[0]["data"]["toolCallId"] == read_call["id"]
            assert child_read_starts[0]["data"]["toolName"] == "read"
            call_ids = {call["id"] for call in calls}
            assert call_ids == {entry["message"]["toolCallId"] for entry in results}, (
                "Child has an unresolved tool call"
            )
            read_ready_records = result_records(
                child_now, "task-read-continuation-ready"
            )
            assert len(read_ready_records) == 1, (
                "Held read cut lacks unique runtime readiness"
            )
            read_ready_entry = read_ready_records[0]
            assert read_ready_entry in branch
            assert branch.index(read_entry) < branch.index(read_ready_entry)
            prefix_branch = branch[: branch.index(read_ready_entry)]
            retained_child_entries = [
                entry
                for entry in child_now
                if entry.get("type") not in ("title", "session")
            ]
            prefix_entries = retained_child_entries[
                : retained_child_entries.index(read_ready_entry)
            ]
            assert branch[-1] == read_ready_entry, (
                "Child changed after read certification"
            )
            assert read_ready_entry["parentId"] == prefix_branch[-1]["id"]
            # This fixture's native plain-file builder returns content/details;
            # it has no truncation notice or result-hook rewrite to reverse.
            native_result = {
                "content": read_entry["message"]["content"],
                "details": read_entry["message"]["details"],
            }
            assert not read_entry["message"].get("useless")
            assert native_result["details"]["meta"] == {
                "source": {
                    "type": "path",
                    "value": str((execution_workspace / "result.txt").resolve()),
                }
            }, "Plain read unexpectedly acquired spill/truncation/output notices"
            assert read_ready_entry["data"] == {
                "version": 1,
                "protocol": "bound-native-read-v1",
                "call": binding["call"],
                "contractSha256": binding["contractSha256"],
                "child": {
                    "sessionId": binding["child"]["sessionId"],
                    "initEntryId": binding["child"]["initEntryId"],
                    "promptEntryId": original_preparation[0]["anchorEntryId"],
                },
                "read": {
                    "assistantEntryId": read_assistant["id"],
                    "startEntryId": child_read_starts[0]["id"],
                    "resultEntryId": read_entry["id"],
                    "toolCallId": read_call["id"],
                    "arguments": read_call["arguments"],
                    "native": {
                        "kind": "local-file-text",
                        "resolvedPath": str(
                            (execution_workspace / "result.txt").resolve()
                        ),
                        "fileSize": (execution_workspace / "result.txt").stat().st_size,
                    },
                    "nativeResultSha256": task_recovery_hash(native_result),
                    "finalResultSha256": task_recovery_hash(read_entry["message"]),
                },
                "prefix": {
                    "leafId": prefix_branch[-1]["id"],
                    "branchSha256": task_recovery_hash(prefix_branch),
                    "entriesSha256": task_recovery_hash(prefix_entries),
                },
                "providerApi": "openai-completions",
            }
            assert not result_records(child_now, "task-read-continuation-started"), (
                "Read response processing already started before held cut"
            )
            read_preparation = result_records(child_now, "prompt-preparation")
            held = [
                row
                for row in provider.request_observations
                if row["model"] == "local-task"
                and row["restartGeneration"] == 0
                and provider.is_child_main_request(provider.calls[row["ordinal"] - 1])
            ]
            assert len(held) == 2
            held_request = dict(held[-1])
            assert (
                held_request["restartGeneration"] == 0
                and not held_request["afterRestart"]
            )
            assert (
                not held_request["responseStarted"]
                and held_request["responseBytesWritten"] == 0
            )
            wire = provider.calls[held_request["ordinal"] - 1]
            wire_calls = [
                (index, call)
                for index, message in enumerate(wire["messages"])
                if message.get("role") == "assistant"
                for call in message.get("tool_calls", [])
            ]
            wire_results = [
                (index, message)
                for index, message in enumerate(wire["messages"])
                if message.get("role") == "tool"
            ]
            assert len(wire_calls) == len(wire_results) == 1
            assert (
                wire_calls[0][1]["id"]
                == wire_results[0][1]["tool_call_id"]
                == read_call["id"]
            )
            assert wire_calls[0][1]["function"]["name"] == "read"
            assert (
                json.loads(wire_calls[0][1]["function"]["arguments"])
                == read_call["arguments"]
            )
            assert wire_results[0][0] > wire_calls[0][0]
            native_read = "\n".join(
                part["text"]
                for part in read_entry["message"]["content"]
                if part.get("type") == "text"
            )
            assert (
                wire_results[0][1]["content"]
                == native_read
                == provider.child_read_content
            )
            assert "before" in native_read
            assert not child_path.with_suffix(".md").exists(), (
                "Task output already published before read cut"
            )
            assert not result_records(parent_now, "task-native-result-ready")
            assert not result_records(parent_now, "task-result-processing-started")
            assert not original_results(parent_now)
            events = read_complete_session(rpc.stdout_log)
            assert_no_late_completion(events)
            assert (
                parent_path.read_bytes() == parent_bytes
                and child_path.read_bytes() == child_bytes
            )
            (tmp_path / "child-read-parent-before-kill.jsonl").write_bytes(parent_bytes)
            (tmp_path / "child-read-child-before-kill.jsonl").write_bytes(child_bytes)
            evidence["childReadWitness"] = {
                "binding": binding,
                "readCall": read_call,
                "readAssistant": read_assistant,
                "readResult": read_entry,
                "readStart": child_read_starts[0],
                "readContinuationReady": read_ready_entry,
                "readContinuationReadySha256": task_recovery_hash(
                    read_ready_entry["data"]
                ),
                "readContinuationStartedAbsentAcrossRetainedHistory": True,
                "heldRequest": held_request,
                "wireRequest": wire,
                "parentSha256": hashlib.sha256(parent_bytes).hexdigest(),
                "childSha256": hashlib.sha256(child_bytes).hexdigest(),
                "execution": get_view(execution_url),
                "workflow": get_view(workflow_url),
                "effects": effects(),
            }
            provider.read_resume_context = (child_path, binding, read_ready_entry)
            save()
            kill_host(cli, "original-child-post-read-response", qualified=False)
            rpc.reader.join(timeout=5)
            assert not rpc.reader.is_alive() and rpc.ordinary_eof, (
                "Killed CLI RPC stream did not reach EOF"
            )
            assert rpc.reader_error is None, rpc.reader_error
            final_rpc = rpc.stdout_log.read_bytes()
            assert final_rpc.endswith(b"\n"), "Incomplete raw RPC tail after death"
            final_events = [
                json.loads(line)
                for line in final_rpc.decode("utf-8").splitlines()
                if line
            ]
            assert all(isinstance(event, dict) for event in final_events)
            assert_no_late_completion(final_events)
            assert (
                parent_path.read_bytes() == parent_bytes
                and child_path.read_bytes() == child_bytes
            )
            assert not result_records(
                read_complete_session(parent_path), "task-native-result-ready"
            )
            assert not result_records(
                read_complete_session(parent_path), "task-result-processing-started"
            )
            assert not original_results(read_complete_session(parent_path))
            assert result_records(
                read_complete_session(child_path), "task-read-continuation-ready"
            ) == [read_ready_entry]
            assert not result_records(
                read_complete_session(child_path), "task-read-continuation-started"
            )
            with provider.request_lock:
                after_death_hold = dict(
                    provider.request_observations[held_request["ordinal"] - 1]
                )
                after_death_main = [
                    row["ordinal"]
                    for row in provider.request_observations
                    if row["restartGeneration"] == 0
                    and provider.is_child_main_request(
                        provider.calls[row["ordinal"] - 1]
                    )
                ]
            assert after_death_hold == held_request, (
                "Held response state changed through death"
            )
            assert after_death_main == [row["ordinal"] for row in held]
            assert_stable_state()
            evidence["childReadWitness"]["postDeathHeldRequest"] = after_death_hold
            evidence["childReadWitness"]["postDeathRpcSha256"] = hashlib.sha256(
                final_rpc
            ).hexdigest()
            evidence["childReadCutReached"] = evidence["barrierReached"] = True
            save()
            # Only now release the disconnected original generation-0 handler.
            # Its fixed cleanup response cannot become a recovered child turn.
            with provider.request_lock:
                provider.child_read_cleanup_release = (
                    held_request["ordinal"],
                    time.time(),
                )
            provider.release.set()
        except (AssertionError, ValueError, OSError) as error:
            evidence["childReadMiss"] = {
                "reason": str(error),
                "observedAt": time.time(),
            }
            save()
            raise

        provider.restarting = True
        outcomes = []
        completed_child = None
        for generation in (1, 2):
            provider.restart_generation = generation
            log = tmp_path / f"child-read-restart-{generation}.stderr"
            with _process(
                release.command(
                    state,
                    repository,
                    "--mode",
                    "rpc",
                    "--session",
                    str(parent_path),
                    "--provider",
                    "qualification",
                    "--model",
                    "local-recovery",
                ),
                repository,
                env,
                log,
            ) as restarted:
                resumed = RpcProcess(restarted, log)
                state_now = resumed.request("get_state")
                assert state_now["sessionId"] == current["sessionId"]
                deadline = time.monotonic() + 30
                while True:
                    parent_now = read_complete_session(parent_path)
                    notifications = [
                        frame
                        for frame in read_complete_session(resumed.stdout_log)
                        if frame.get("type") == "extension_ui_request"
                        and frame.get("method") == "notify"
                        and str(frame.get("message", "")).startswith(
                            "Execution recovery skipped:"
                        )
                    ]
                    finished = terminal_parent_answer(parent_now) is not None
                    if finished or notifications or time.monotonic() >= deadline:
                        break
                    time.sleep(0.05)
                time.sleep(2)
                parent_now, child_now = assert_same_journals()
                assert provider.error is None, provider.error
                branch = durable_session_branch(child_now)
                assert result_records(child_now, "task-read-continuation-ready") == [
                    read_ready_entry
                ], "Recovery changed original read readiness"
                assert (
                    result_records(child_now, "prompt-preparation") == read_preparation
                ), "Read continuation appended another preparation batch"
                child_calls = [
                    part
                    for entry in child_now
                    if entry.get("type") == "message"
                    and entry.get("message", {}).get("role") == "assistant"
                    for part in entry["message"].get("content", [])
                    if part.get("type") == "toolCall"
                ]
                child_results = [
                    entry
                    for entry in child_now
                    if entry.get("type") == "message"
                    and entry.get("message", {}).get("role") == "toolResult"
                ]
                assert [call for call in child_calls if call.get("name") == "read"] == [
                    read_call
                ], "Recovery repeated or changed original read"
                assert [
                    entry
                    for entry in child_results
                    if entry["message"].get("toolName") == "read"
                ] == [read_entry]
                assert read_entry in branch
                assert [
                    entry
                    for entry in child_now
                    if entry.get("customType") == "tool_execution_start"
                    and entry.get("data", {}).get("toolName") == "read"
                ] == child_read_starts, "Recovery repeated original read dispatch"
                requests = [
                    dict(row)
                    for row in provider.request_observations
                    if row["restartGeneration"] == generation
                ]
                result = (
                    successful_result(parent_now)
                    if original_results(parent_now)
                    else None
                )
                if finished and not outcomes:
                    read_started = result_records(
                        child_now, "task-read-continuation-started"
                    )
                    assert len(read_started) == 1 and read_started[0] in branch
                    request_boundary = provider.read_resume_boundary
                    assert (
                        request_boundary is not None
                        and request_boundary.get("verifiedBeforeResponse") is True
                    ), "No verified cold claim at first resumed child MAIN HTTP arrival"
                    assert request_boundary["claim"] == read_started[0]
                    assert not request_boundary["request"]["responseStarted"]
                    assert request_boundary["request"]["responseBytesWritten"] == 0
                    assert read_started[0]["data"] == {
                        "version": 1,
                        "protocol": "bound-native-read-v1",
                        "call": binding["call"],
                        "contractSha256": binding["contractSha256"],
                        "readyEntryId": read_ready_entry["id"],
                        "readySha256": task_recovery_hash(read_ready_entry["data"]),
                        "reason": "cold-resume",
                    }
                    assert branch.index(read_ready_entry) < branch.index(
                        read_started[0]
                    )
                    yield_starts = [
                        entry
                        for entry in child_now
                        if entry.get("customType") == "tool_execution_start"
                        and entry.get("data", {}).get("toolName") == "yield"
                    ]
                    assert len(yield_starts) == 1 and yield_starts[0] in branch
                    assert branch.index(read_started[0]) < branch.index(
                        yield_starts[0]
                    ), "Yield dispatch preceded durable cold-resume claim"
                    assert_parent_wire(requests)
                    yields = [
                        entry
                        for entry in child_results
                        if entry["message"].get("toolName") == "yield"
                    ]
                    assert len(yields) == 1 and not yields[0]["message"].get("isError")
                    assert [call["name"] for call in child_calls] == ["read", "yield"]
                if not finished:
                    assert result is None and not requests
                    assert child_path.read_bytes() == child_bytes, (
                        "Refused recovery rewrote original child"
                    )
                if outcomes:
                    assert result == outcomes[0]["originalResult"] and not requests
                    assert child_now == completed_child
                roster = resumed.request("get_subagents")["subagents"]
                assert len(roster) <= 1 and all(
                    row["id"] == child["id"] for row in roster
                )
                assert provider.task_calls == 1
                outcome = {
                    "generation": generation,
                    "launcherPid": restarted.pid,
                    "topology": process_group_snapshot(os.getpgid(restarted.pid)),
                    "parentCompleted": finished,
                    "originalResult": result,
                    "readContinuationReady": read_ready_entry,
                    "readContinuationStarted": result_records(
                        child_now, "task-read-continuation-started"
                    ),
                    "readResumeRequestBoundary": provider.read_resume_boundary,
                    "refusals": notifications,
                    "providerObservations": requests,
                    "subagents": roster,
                    "execution": get_view(execution_url),
                    "workflow": get_view(workflow_url),
                    "effects": effects(),
                    "refusalSafety": bool(notifications)
                    and result is None
                    and not requests,
                }
                outcomes.append(outcome)
                evidence["childReadOutcomes"] = outcomes
                shutil.copyfile(
                    parent_path,
                    tmp_path / f"child-read-parent-after-{generation}.jsonl",
                )
                shutil.copyfile(
                    child_path, tmp_path / f"child-read-child-after-{generation}.jsonl"
                )
                save()
            if generation == 1:
                assert restarted.poll() is not None
                completed_child = read_complete_session(child_path)
                shutil.copyfile(
                    child_path, tmp_path / "child-read-child-before-quiet-restart.jsonl"
                )
                outcome["priorHostExitCode"] = restarted.returncode
                save()
        assert len(outcomes) == 2 and all(
            row["parentCompleted"] and row["originalResult"] for row in outcomes
        ), (
            "Original child did not continue from durable read; refusal safety is not automatic recovery"
        )
        return

    if provider.task_fault == "first-run-result-gap":
        evidence["firstRunCutReached"] = False
        group_id = os.getpgid(cli.pid)
        before_topology = process_group_snapshot(group_id, include_threads=True)
        host = installed_cli_host(release, before_topology, cli.pid)
        host_identity = (host["pid"], host["startTicks"])
        launcher = next(row for row in before_topology if row["pid"] == cli.pid)
        launcher_identity = (launcher["pid"], launcher["startTicks"])
        postgres_pid = int(
            (tmp_path / "postgres/pgdata/postmaster.pid").read_text().splitlines()[0]
        )
        outsiders = [os.getpid(), service_pid, postgres_pid]
        assert all(os.getpgid(pid) != group_id for pid in outsiders)
        evidence["stopOutsideGroup"] = outsiders

        rpc.request("set_subagent_subscription", level="events")
        rpc.arm_completion_stop(
            group_id=group_id,
            child_id=child["id"],
            tool_call_id=call_id,
            session_file=str(child_path),
        )
        provider.completion_observer_ready.set()
        output_path = child_path.with_suffix(".md")
        frozen_child: bytes | None = None
        frozen_output: bytes | None = None
        first_ready_entry: dict | None = None

        def frozen_entries(file: Path, label: str) -> tuple[bytes, list[dict]]:
            raw = file.read_bytes()
            (tmp_path / label).write_bytes(raw)
            assert raw.endswith(b"\n"), f"Incomplete frozen journal tail: {file.name}"
            entries = [
                json.loads(line) for line in raw.decode("utf-8").splitlines() if line
            ]
            assert entries and all(isinstance(entry, dict) for entry in entries)
            assert file.read_bytes() == raw, (
                f"Frozen journal changed during inspection: {file.name}"
            )
            return raw, entries

        def absent_parent(frames: list[dict], entries: list[dict]) -> None:
            assert not original_results(entries), (
                "Original parent result is already durable"
            )
            assert not any(
                frame.get("type") == "tool_execution_end"
                and frame.get("toolCallId") == call_id
                for frame in frames
            ), "Received/drained frames contain original parent tool execution result"

            assert not any(
                frame.get("type") in ("message_start", "message_end")
                and frame.get("message", {}).get("role") == "toolResult"
                and frame["message"].get("toolCallId") == call_id
                for frame in frames
            ), "Received/drained frames contain original parent result"
            assert not any(
                row["model"] == "local-recovery"
                and row["ordinal"] > child_requests[0]["ordinal"]
                and any(
                    tool.get("function", {}).get("name") == "work"
                    for tool in provider.calls[row["ordinal"] - 1].get("tools", [])
                )
                for row in provider.request_observations
            ), "First-run parent provider continuation already occurred"

        def topology_identity(rows: list[dict]) -> list[tuple]:
            return [
                (
                    row["pid"],
                    row["startTicks"],
                    tuple((task["tid"], task["startTicks"]) for task in row["threads"]),
                )
                for row in rows
            ]

        def fully_stopped(rows: list[dict]) -> bool:
            return bool(rows) and all(
                row["state"] in ("T", "t", "Z", "X")
                and (
                    row["state"] in ("Z", "X")
                    or bool(row["threads"])
                    and all(
                        task["state"] in ("T", "t", "Z", "X") for task in row["threads"]
                    )
                )
                for row in rows
            )

        def cleanup_owned_group() -> None:
            topology = process_group_snapshot(group_id)
            leaders = [row for row in topology if row["pid"] == group_id]
            assert (
                not leaders
                or (leaders[0]["pid"], leaders[0]["startTicks"]) == launcher_identity
            ), "Owned process group identity was reused"
            assert all(row["processSessionId"] == group_id for row in topology), (
                "Process escaped original owned session"
            )
            if any(row["state"] not in ("Z", "X") for row in topology):
                record = {
                    "label": "unqualified-first-run-attempt-cleanup",
                    "signal": "SIGKILL",
                    "processGroupId": group_id,
                    "topologyBefore": topology,
                    "killedAt": time.time(),
                }
                evidence["kills"].append(record)
                os.killpg(group_id, signal.SIGKILL)
                record["exitCode"] = cli.wait(timeout=10)
                save()
            elif cli.poll() is None:
                cli.wait(timeout=10)
            deadline = time.monotonic() + 5
            while True:
                remaining = process_group_snapshot(group_id)
                alive = [row for row in remaining if row["state"] not in ("Z", "X")]
                if not alive or time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
            assert not alive, f"Owned group did not die cleanly: {remaining}"
            rpc.reader.join(timeout=5)
            terminal = rpc.byte_snapshot()
            evidence["readerAfterCleanup"] = {
                key: value for key, value in terminal.items() if key != "frames"
            }
            save()
            assert not rpc.reader.is_alive() and terminal["eof"], (
                "Reader did not observe actual EOF after group cleanup"
            )

        try:
            assert rpc.stop_requested.wait(45), (
                f"Completed lifecycle stop not reached; reader error: {rpc.reader_error}"
            )
            deadline = time.monotonic() + 5
            previous_members: list[tuple] | None = None
            stopped: list[dict] = []
            while True:
                stopped = process_group_snapshot(group_id, include_threads=True)
                members = topology_identity(stopped)
                identities = [(row["pid"], row["startTicks"]) for row in stopped]
                assert (
                    host_identity in identities and launcher_identity in identities
                ), "Original CLI/launcher identity changed before stop confirmation"
                host = next(
                    row
                    for row in stopped
                    if (row["pid"], row["startTicks"]) == host_identity
                )
                assert host["state"] not in ("Z", "X"), "CLI died before the stop cut"
                launcher_now = next(
                    row
                    for row in stopped
                    if (row["pid"], row["startTicks"]) == launcher_identity
                )
                assert launcher_now["state"] not in ("Z", "X"), (
                    "Launcher died before the stop cut"
                )

                all_stopped = fully_stopped(stopped)
                if all_stopped and members == previous_members:
                    break
                assert time.monotonic() < deadline, (
                    f"Stable whole-group/thread stop not confirmed: {stopped}"
                )
                previous_members = members
                time.sleep(0.02)
            evidence["stop"] = {
                "requested": rpc.stop_record,
                "confirmedAt": time.time(),
                "before": before_topology,
                "stopped": stopped,
            }
            drained = rpc.drain_stopped()
            (tmp_path / "first-run-drained-rpc.json").write_text(
                json.dumps(drained, indent=2)
            )
            assert drained["readerError"] is None, drained["readerError"]
            assert not drained["partialBytes"], (
                "Incomplete received RPC frame at frozen cut"
            )
            assert not drained["eof"], "CLI exited instead of remaining stopped"
            parent_bytes, parent_now = frozen_entries(
                parent_path, "first-run-parent-stopped.jsonl"
            )
            frozen_child, child_now = frozen_entries(
                child_path, "first-run-child-stopped.jsonl"
            )
            assert_same_journals()
            absent_parent(drained["frames"], parent_now)
            branch = durable_session_branch(child_now)
            reads = [
                entry["message"]
                for entry in branch
                if entry.get("type") == "message"
                and entry.get("message", {}).get("role") == "toolResult"
                and entry["message"].get("toolCallId") == "task-child-read"
            ]
            yields = [
                entry["message"]
                for entry in branch
                if entry.get("type") == "message"
                and entry.get("message", {}).get("role") == "toolResult"
                and entry["message"].get("toolCallId") == "task-child-yield"
            ]
            assert (
                len(reads) == len(yields) == 1
                and not reads[0].get("isError")
                and not yields[0].get("isError")
            )
            expected_output = {
                "path": "result.txt",
                "observed": provider.child_read_content,
            }
            assert provider.child_read_content is not None
            assert [
                part["text"]
                for part in reads[0]["content"]
                if part.get("type") == "text"
            ] == [provider.child_read_content]
            assert yields[0]["details"] == {
                "data": expected_output,
                "status": "success",
            }
            frozen_output = output_path.read_bytes()
            assert json.loads(frozen_output) == expected_output
            first_ready_entry = require_unstarted_native_ready(
                parent_now,
                child_now,
                frozen_child,
                output_path,
                frozen_output,
                expected_producer="original-sync-task-v1",
            )
            (tmp_path / "first-run-completed-output.md").write_bytes(frozen_output)
            trigger = drained["stop"]["trigger"]["payload"]
            assert (
                trigger["agent"] == "task"
                and trigger["agentSource"] == "bundled"
                and trigger.get("detached") is not True
            )
            assert provider.task_calls == 1 and not provider.restarting
            assert_stable_state()
            # Outstanding I/O cannot turn a late result into a qualifying cut.
            latest = rpc.drain_stopped()
            assert latest["readerError"] is None, latest["readerError"]
            assert not latest["partialBytes"]
            _, final_parent = frozen_entries(
                parent_path, "first-run-parent-before-kill.jsonl"
            )
            assert (
                parent_path.read_bytes() == parent_bytes
                and child_path.read_bytes() == frozen_child
            )
            absent_parent(latest["frames"], final_parent)
            assert (
                native_ready(final_parent, expected_producer="original-sync-task-v1")
                == first_ready_entry
            )
            assert not result_records(final_parent, "task-result-processing-started")
            evidence["firstRunWitness"] = {
                "binding": binding,
                "nativeReadyEntry": first_ready_entry,
                "nativeReadySha256": task_recovery_hash(first_ready_entry["data"]),
                "completedLifecycle": drained["stop"]["trigger"],
                "outputSha256": hashlib.sha256(frozen_output).hexdigest(),
                "output": expected_output,
                "execution": get_view(execution_url),
                "workflow": get_view(workflow_url),
                "effects": effects(),
                "parentResultAbsentFromDurableBranchAndReceivedFrames": True,
                "processingState": "runtime-certified original ready; mandatory processing claim absent across retained history",
            }
            save()
            before_kill = process_group_snapshot(group_id, include_threads=True)
            assert fully_stopped(before_kill) and topology_identity(
                before_kill
            ) == topology_identity(stopped), (
                "Stopped process/thread membership changed before kill"
            )
            evidence["stop"]["beforeKill"] = before_kill
            kill_host(cli, "ordinary-first-run-completed-child", qualified=False)
            rpc.reader.join(timeout=5)
            assert not rpc.reader.is_alive() and rpc.reader_error is None
            _, after_death = frozen_entries(
                parent_path, "first-run-parent-after-kill.jsonl"
            )
            raw_frames = rpc.stdout_log.read_bytes()
            assert raw_frames.endswith(b"\n"), "Incomplete RPC tail after death"
            frames = [
                json.loads(line)
                for line in raw_frames.decode("utf-8").splitlines()
                if line
            ]
            absent_parent(frames, after_death)

            assert parent_path.read_bytes() == parent_bytes, (
                "Frozen parent journal changed through process death"
            )
            assert (
                native_ready(after_death, expected_producer="original-sync-task-v1")
                == first_ready_entry
            )
            assert not result_records(after_death, "task-result-processing-started")
            assert_stable_state()
            assert (
                child_path.read_bytes() == frozen_child
                and output_path.read_bytes() == frozen_output
            )
            evidence["firstRunCutReached"] = evidence["barrierReached"] = True
            save()
        except (AssertionError, ValueError, OSError) as error:
            evidence["firstRunMiss"] = {
                "reason": str(error),
                "observedAt": time.time(),
                "stop": rpc.stop_record,
                "readerError": rpc.reader_error,
                "providerObservations": provider.request_observations,
            }
            save()
            raise FirstRunCutMiss(str(error)) from error
        finally:
            cleanup_owned_group()

        provider.restarting = True
        assert first_ready_entry is not None
        outcomes = []
        for generation in (1, 2):
            provider.restart_generation = generation
            log = tmp_path / f"first-run-restart-{generation}.stderr"
            with _process(
                release.command(
                    state,
                    repository,
                    "--mode",
                    "rpc",
                    "--session",
                    str(parent_path),
                    "--provider",
                    "qualification",
                    "--model",
                    "local-recovery",
                ),
                repository,
                env,
                log,
            ) as restarted:
                resumed = RpcProcess(restarted, log)
                current_state = resumed.request("get_state")
                assert current_state["sessionId"] == current["sessionId"]
                deadline = time.monotonic() + 30
                while True:
                    entries = read_complete_session(parent_path)
                    notifications = [
                        frame
                        for frame in read_complete_session(resumed.stdout_log)
                        if frame.get("type") == "extension_ui_request"
                        and frame.get("method") == "notify"
                        and str(frame.get("message", "")).startswith(
                            "Execution recovery skipped:"
                        )
                    ]
                    finished = terminal_parent_answer(entries) is not None
                    if finished or notifications or time.monotonic() >= deadline:
                        break
                    time.sleep(0.05)
                time.sleep(2)
                parent_now, _ = assert_same_journals()
                assert (
                    native_ready(parent_now, expected_producer="original-sync-task-v1")
                    == first_ready_entry
                ), "Restart changed original first-run ready checkpoint"
                result = (
                    successful_result(parent_now)
                    if original_results(parent_now)
                    else None
                )
                assert provider.error is None, provider.error
                requests = [
                    dict(row)
                    for row in provider.request_observations
                    if row["restartGeneration"] == generation
                ]
                assert not any(row["model"] == "local-task" for row in requests), (
                    "Certified completed first-run child was executed again"
                )
                assert (
                    child_path.read_bytes() == frozen_child
                    and output_path.read_bytes() == frozen_output
                )
                if finished and not outcomes:
                    assert_parent_wire(requests)
                if not finished:
                    assert result is None and not requests
                if outcomes:
                    assert result == outcomes[0]["originalResult"] and not requests
                roster = resumed.request("get_subagents")["subagents"]
                assert len(roster) <= 1 and all(
                    row["id"] == child["id"] for row in roster
                )
                outcome = {
                    "generation": generation,
                    "parentCompleted": finished,
                    "originalResult": result,
                    "nativeReadyEntry": first_ready_entry,
                    "processingClaims": result_records(
                        parent_now, "task-result-processing-started"
                    ),
                    "refusals": notifications,
                    "providerObservations": requests,
                    "subagents": roster,
                    "execution": get_view(execution_url),
                    "workflow": get_view(workflow_url),
                    "effects": effects(),
                    "refusalSafety": bool(notifications)
                    and not requests
                    and result is None,
                }
                outcomes.append(outcome)
                evidence["firstRunOutcomes"] = outcomes
                shutil.copyfile(
                    parent_path, tmp_path / f"first-run-parent-after-{generation}.jsonl"
                )
                shutil.copyfile(
                    child_path, tmp_path / f"first-run-child-after-{generation}.jsonl"
                )
                save()
        assert len(outcomes) == 2 and all(
            row["parentCompleted"] and row["originalResult"] for row in outcomes
        ), (
            "Ordinary first-run completed child did not recover original parent result; refusal safety is not automatic recovery"
        )
        return

    gap_output_path = child_path.with_suffix(".md")
    gap_child_baseline: list[dict] | None = None
    gap_ready_entry: dict | None = None
    gap_output_bytes: bytes | None = None
    gap_outcomes: list[dict] = []
    if provider.task_fault == "child-result-gap":
        assert authority_proxy is not None
        assert gap_output_path.name == f"{binding['child']['registryId']}.md"
        authority_proxy.arm(gap_output_path, f"/v1/workspaces/{workspace_id}/execution")
    kill_host(cli, "initial-child-request")
    provider.restarting = True
    restart_args = (
        "--mode",
        "rpc",
        "--session",
        str(parent_path),
        "--provider",
        "qualification",
        "--model",
        "local-recovery",
    )
    recovery_restarts = 1 if provider.task_fault == "child-request" else 2
    completed_result: dict | None = None
    completed_child: list[dict] | None = None
    completed_requests = 0
    parent_result_child_requests = 0
    for generation in range(1, recovery_restarts + 2):
        provider.restart_generation = generation
        restart_log = tmp_path / f"task-controller-restart-{generation}.stderr"
        with _process(
            release.command(state, repository, *restart_args),
            repository,
            env,
            restart_log,
        ) as restarted:
            restarted_rpc = RpcProcess(restarted, restart_log)
            if provider.task_fault == "child-result-gap" and generation == 1:
                restarted_rpc.request("set_subagent_subscription", level="events")
                provider.completion_observer_ready.set()
            restarted_state = restarted_rpc.request("get_state")
            assert restarted_state["sessionId"] == current["sessionId"]
            restart_record = {
                "generation": generation,
                "launcherPid": restarted.pid,
                "state": restarted_state,
            }
            evidence["restarts"].append(restart_record)
            save()

            if generation == 1 and provider.task_fault == "child-result-gap":
                assert authority_proxy is not None
                deadline = time.monotonic() + 45
                while True:
                    parent_now = read_complete_session(parent_path)
                    child_now = read_complete_session(child_path)
                    events = read_complete_session(restarted_rpc.stdout_log)
                    completed = [
                        event
                        for event in events
                        if event.get("type") == "subagent_lifecycle"
                        and event.get("payload", {}).get("id") == child["id"]
                        and event["payload"].get("parentToolCallId") == call_id
                        and event["payload"].get("status") == "completed"
                    ]
                    read_results = [
                        entry
                        for entry in durable_session_branch(child_now)
                        if entry.get("type") == "message"
                        and entry.get("message", {}).get("role") == "toolResult"
                        and entry["message"].get("toolCallId") == "task-child-read"
                    ]
                    yield_results = [
                        entry
                        for entry in durable_session_branch(child_now)
                        if entry.get("type") == "message"
                        and entry.get("message", {}).get("role") == "toolResult"
                        and entry["message"].get("toolCallId") == "task-child-yield"
                    ]
                    held = [row for row in authority_proxy.snapshot() if row["held"]]
                    if (
                        completed
                        and len(read_results) == len(yield_results) == 1
                        and held
                        and result_records(parent_now, "task-native-result-ready")
                    ):
                        break
                    if (
                        time.monotonic() >= deadline
                        or authority_proxy.error
                        or provider.error
                    ):
                        break
                    time.sleep(0.05)
                restart_record["completionBarrierObservation"] = {
                    "completedLifecycle": completed,
                    "heldAuthorityReads": held,
                    "readResults": read_results,
                    "yieldResults": yield_results,
                    "proxyError": authority_proxy.error,
                    "providerError": provider.error,
                    "parentResults": original_results(parent_now),
                }
                save()
                assert authority_proxy.error is None and provider.error is None
                assert len(completed) == 1, (
                    "Original child completion lifecycle was not observed"
                )
                payload = completed[0]["payload"]
                assert (
                    payload["agent"] == "task" and payload["agentSource"] == "bundled"
                )
                assert payload["sessionFile"] == str(child_path)
                assert payload.get("detached") is not True
                assert len(read_results) == len(yield_results) == 1
                read_message, yield_message = (
                    read_results[0]["message"],
                    yield_results[0]["message"],
                )
                assert read_message["toolName"] == "read" and not read_message.get(
                    "isError"
                )
                assert yield_message["toolName"] == "yield" and not yield_message.get(
                    "isError"
                )
                expected_output = {
                    "path": "result.txt",
                    "observed": provider.child_read_content,
                }
                assert provider.child_read_content is not None
                assert yield_message["details"] == {
                    "data": expected_output,
                    "status": "success",
                }
                assert [
                    part["text"]
                    for part in read_message["content"]
                    if part.get("type") == "text"
                ] == [provider.child_read_content]
                gap_output_bytes = gap_output_path.read_bytes()
                assert json.loads(gap_output_bytes) == expected_output
                assert held and all(
                    200 <= row["status"] < 300 and row["artifactPresent"]
                    for row in held
                )
                assert all(
                    not row["responseStarted"] and row["responseBytesWritten"] == 0
                    for row in held
                )
                for row in held:
                    response_bytes = Path(row["bodyFile"]).read_bytes()
                    assert (
                        hashlib.sha256(response_bytes).hexdigest() == row["bodySha256"]
                    )
                    assert json.loads(response_bytes) == before_execution, (
                        "Held bytes are not unchanged real authority"
                    )
                parent_now, child_now = assert_same_journals()
                gap_ready_entry = require_unstarted_native_ready(
                    parent_now,
                    child_now,
                    child_path.read_bytes(),
                    gap_output_path,
                    gap_output_bytes,
                )
                ready_data = gap_ready_entry["data"]
                assert not original_results(parent_now), (
                    "Parent result became durable before completion cut"
                )
                assert not any(
                    event.get("type") in ("message_start", "message_end")
                    and event.get("message", {}).get("role") == "toolResult"
                    and event["message"].get("toolCallId") == call_id
                    for event in events
                ), "Parent result event preceded completion cut"
                assert not any(
                    row["model"] == "local-recovery"
                    and row["restartGeneration"] == generation
                    for row in provider.request_observations
                ), "Parent provider continuation preceded completion cut"
                gap_child_baseline = child_now
                shutil.copyfile(
                    parent_path,
                    tmp_path / "task-parent-completion-gap-before-kill.jsonl",
                )
                shutil.copyfile(
                    child_path, tmp_path / "task-child-completion-gap-before-kill.jsonl"
                )
                shutil.copyfile(
                    gap_output_path, tmp_path / "task-completed-output-before-kill.md"
                )
                restart_record["completionBarrier"] = {
                    "observedAt": time.time(),
                    "binding": binding,
                    "nativeReadyEntry": gap_ready_entry,
                    "nativeReadySha256": task_recovery_hash(ready_data),
                    "processingClaimAbsentAcrossRetainedHistory": True,
                    "childSessionId": binding["child"]["sessionId"],
                    "outputPath": str(gap_output_path),
                    "outputSha256": hashlib.sha256(gap_output_bytes).hexdigest(),
                    "output": expected_output,
                    "completedLifecycle": completed[0],
                    "heldAuthorityReads": held,
                    "execution": get_view(execution_url),
                    "workflow": get_view(workflow_url),
                    "effects": effects(),
                    "parentResultAbsent": True,
                    "parentProviderAbsent": True,
                    "authorityAwaitAttribution": "Production ordering identifies the choke point; no individual GET is attributed to one internal await.",
                }
                save()
                # All witnesses remain pinned while every successful authority response is held.
                assert gap_output_path.read_bytes() == gap_output_bytes
                assert read_complete_session(child_path) == gap_child_baseline
                assert not original_results(read_complete_session(parent_path))
                assert (
                    native_ready(read_complete_session(parent_path)) == gap_ready_entry
                )
                assert not result_records(
                    read_complete_session(parent_path), "task-result-processing-started"
                )
                assert_stable_state()
                assert not any(
                    event.get("type") in ("message_start", "message_end")
                    and event.get("message", {}).get("role") == "toolResult"
                    and event["message"].get("toolCallId") == call_id
                    for event in read_complete_session(restarted_rpc.stdout_log)
                )
                latest_reads = authority_proxy.snapshot()
                assert all(
                    not row["artifactPresent"]
                    or not (200 <= row["status"] < 300)
                    or row["held"]
                    for row in latest_reads
                ), "A qualifying authority response escaped the cut"
                assert all(
                    not row["responseStarted"] for row in latest_reads if row["held"]
                )
                evidence["completionBarrierReached"] = True
                save()
                kill_host(restarted, "completed-child-before-original-parent-result")
                restarted_rpc.reader.join(timeout=2)
                assert not restarted_rpc.reader.is_alive(), (
                    "Killed host RPC stream did not reach EOF"
                )
                assert not original_results(read_complete_session(parent_path))
                assert (
                    native_ready(read_complete_session(parent_path)) == gap_ready_entry
                )
                assert not result_records(
                    read_complete_session(parent_path), "task-result-processing-started"
                )
                assert read_complete_session(child_path) == gap_child_baseline
                assert not any(
                    row["model"] == "local-recovery"
                    and row["restartGeneration"] == generation
                    for row in provider.request_observations
                )
                assert not any(
                    event.get("type") in ("message_start", "message_end")
                    and event.get("message", {}).get("role") == "toolResult"
                    and event["message"].get("toolCallId") == call_id
                    for event in read_complete_session(restarted_rpc.stdout_log)
                )
                authority_proxy.allow_responses()
                continue

            if generation > 1 and provider.task_fault == "child-result-gap":
                assert gap_child_baseline is not None and gap_output_bytes is not None
                assert gap_ready_entry is not None
                deadline = time.monotonic() + 30
                while True:
                    parent_now = read_complete_session(parent_path)
                    events = read_complete_session(restarted_rpc.stdout_log)
                    refusals = [
                        event
                        for event in events
                        if event.get("type") == "extension_ui_request"
                        and event.get("method") == "notify"
                        and str(event.get("message", "")).startswith(
                            "Execution recovery skipped:"
                        )
                    ]
                    finished = terminal_parent_answer(parent_now) is not None
                    if finished or refusals or time.monotonic() >= deadline:
                        break
                    time.sleep(0.05)
                # This is an observation window, not a synthetic startup/completion receipt.
                time.sleep(2)
                assert provider.error is None, provider.error
                parent_now, child_now = assert_same_journals()
                assert native_ready(parent_now) == gap_ready_entry, (
                    "Restart changed original ready checkpoint"
                )
                observations = [
                    dict(row)
                    for row in provider.request_observations
                    if row["restartGeneration"] == generation
                ]
                assert not any(row["model"] == "local-task" for row in observations), (
                    "Completed child was dispatched again"
                )
                assert child_now == gap_child_baseline, (
                    "Completed child evidence was rewritten"
                )
                assert gap_output_path.read_bytes() == gap_output_bytes
                result = (
                    successful_result(parent_now)
                    if original_results(parent_now)
                    else None
                )
                if gap_outcomes:
                    assert result == gap_outcomes[0]["originalResult"], (
                        "Repeated restart rewrote original parent result"
                    )
                    assert not observations, (
                        "Repeated completion-gap restart dispatched another request"
                    )
                elif finished:
                    assert_parent_wire(observations)
                if not finished:
                    assert result is None, (
                        "Refused completed-task recovery attached a parent result"
                    )
                    assert not observations, (
                        "Refused completed-task recovery dispatched a provider request"
                    )
                roster = restarted_rpc.request("get_subagents")["subagents"]
                assert len(roster) <= 1 and all(
                    row["id"] == child["id"] for row in roster
                )
                assert provider.task_calls == 1
                outcome = {
                    "generation": generation,
                    "parentCompleted": finished,
                    "originalResult": result,
                    "nativeReadyEntry": gap_ready_entry,
                    "processingClaims": result_records(
                        parent_now, "task-result-processing-started"
                    ),
                    "refusals": refusals,
                    "providerObservations": observations,
                    "endState": restarted_rpc.request("get_state"),
                    "subagents": roster,
                    "execution": get_view(execution_url),
                    "workflow": get_view(workflow_url),
                    "effects": effects(),
                    "refusalSafety": bool(refusals)
                    and result is None
                    and not observations,
                }
                gap_outcomes.append(outcome)
                restart_record["completionGapOutcome"] = outcome
                evidence["completionGapOutcomes"] = gap_outcomes
                shutil.copyfile(
                    parent_path, tmp_path / f"task-parent-after-{generation}.jsonl"
                )
                shutil.copyfile(
                    child_path, tmp_path / f"task-child-after-{generation}.jsonl"
                )
                save()
                continue

            if generation == 1 and provider.task_fault == "repeated-child-request":
                assert provider.restarted_child_held.wait(40), (
                    "Restarted original child never reached held request"
                )
                deadline = time.monotonic() + 10
                while True:
                    child_now = read_complete_session(child_path)
                    preparation = task_preparation_records(child_now, binding)
                    if (
                        len(preparation) > len(original_preparation)
                        or time.monotonic() >= deadline
                    ):
                        break
                    time.sleep(0.05)
                assert len(preparation) > len(original_preparation), (
                    "Fresh recovery preparation was not durable"
                )
                preparation = task_preparation_records(
                    child_now, binding, require_complete_suffix=True
                )
                assert not any(
                    entry.get("type") == "message"
                    and entry.get("message", {}).get("role")
                    in ("assistant", "toolResult")
                    or entry.get("type") == "custom"
                    and entry.get("customType") == "tool_execution_start"
                    for entry in child_now
                ), "Repeat-kill boundary already has child response/tool history"
                observations = [
                    row
                    for row in provider.request_observations
                    if row["model"] == "local-task"
                    and row["restartGeneration"] == generation
                ]
                assert len(observations) == 1 and not observations[0]["responseStarted"]
                assert observations[0]["responseBytesWritten"] == 0
                parent_now, child_now = assert_same_journals()
                assert not original_results(parent_now)
                restart_record["freshPreparation"] = preparation
                shutil.copyfile(
                    child_path, tmp_path / "task-child-before-repeat-kill.jsonl"
                )
                shutil.copyfile(
                    parent_path, tmp_path / "task-parent-before-repeat-kill.jsonl"
                )
                kill_host(restarted, "restarted-child-after-fresh-preparation")
                continue

            if generation == 1 and provider.task_fault == "parent-result":
                assert provider.parent_result_held.wait(40), (
                    "Parent never consumed original task result"
                )
                deadline = time.monotonic() + 10
                while (
                    not original_results(read_complete_session(parent_path))
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.05)
                parent_now, child_now = assert_same_journals()
                completed_result = successful_result(parent_now)
                completed_child = child_now
                parent_result_child_requests = len(
                    [
                        row
                        for row in provider.request_observations
                        if row["model"] == "local-task"
                    ]
                )
                held_parent = [
                    row
                    for row in provider.request_observations
                    if row["model"] == "local-recovery"
                    and row["restartGeneration"] == generation
                ]
                assert len(held_parent) == 1
                assert not held_parent[0]["responseStarted"]
                assert held_parent[0]["responseBytesWritten"] == 0
                assert not any(
                    entry.get("type") == "message"
                    and entry.get("message", {}).get("role") == "assistant"
                    and entry["message"].get("stopReason") == "stop"
                    for entry in parent_now
                ), "Parent answer completed before result-persistence kill"
                restart_record["durableResultBeforeKill"] = completed_result
                restart_record["heldParentRequest"] = held_parent[0]
                shutil.copyfile(
                    parent_path, tmp_path / "task-parent-result-before-kill.jsonl"
                )
                shutil.copyfile(
                    child_path, tmp_path / "task-child-completed-before-kill.jsonl"
                )
                kill_host(restarted, "parent-request-after-durable-task-result")
                continue

            if generation <= recovery_restarts:
                deadline = time.monotonic() + 40
                while True:
                    parent_now = read_complete_session(parent_path)
                    finished = terminal_parent_answer(parent_now) is not None
                    if finished or time.monotonic() >= deadline or provider.error:
                        break
                    time.sleep(0.05)
                assert provider.error is None, provider.error
                assert finished, (
                    f"Original parent never completed: {restart_log.read_text()[-6000:]}"
                )
                assert provider.child_recovered.is_set()
                parent_now, child_now = assert_same_journals()
                result = successful_result(parent_now)
                if completed_result is not None:
                    assert result == completed_result, (
                        "Restart rewrote durable original parent result"
                    )
                    assert child_now == completed_child, (
                        "Parent-result recovery modified child journal"
                    )
                    assert (
                        len(
                            [
                                row
                                for row in provider.request_observations
                                if row["model"] == "local-task"
                            ]
                        )
                        == parent_result_child_requests
                    ), "Parent-result restart dispatched a child request"
                read_results = [
                    entry["message"]
                    for entry in child_now
                    if entry.get("type") == "message"
                    and entry.get("message", {}).get("role") == "toolResult"
                    and entry["message"].get("toolName") == "read"
                ]
                yield_results = [
                    entry["message"]
                    for entry in child_now
                    if entry.get("type") == "message"
                    and entry.get("message", {}).get("role") == "toolResult"
                    and entry["message"].get("toolName") == "yield"
                ]
                assert len(read_results) == len(yield_results) == 1
                assert not read_results[0].get("isError") and not yield_results[0].get(
                    "isError"
                )
                assert read_results[0]["toolCallId"] == "task-child-read"
                assert yield_results[0]["toolCallId"] == "task-child-yield"
                completed_result, completed_child = result, child_now
                completed_requests = len(provider.calls)
            else:
                # Startup has processed the durable terminal parent transcript.
                # Observe a quiet interval to catch an incorrectly scheduled wake.
                time.sleep(2)
                parent_now, child_now = assert_same_journals()
                assert successful_result(parent_now) == completed_result
                assert terminal_parent_answer(parent_now) is not None
                assert child_now == completed_child
                assert len(provider.calls) == completed_requests, (
                    "Completed task/parent replay dispatched another request"
                )

            roster = restarted_rpc.request("get_subagents")["subagents"]
            assert all(row["id"] == child["id"] for row in roster), (
                "Recovery allocated a replacement task"
            )
            assert len(roster) <= 1
            end_state = restarted_rpc.request("get_state")
            assert end_state["sessionId"] == current["sessionId"]
            assert provider.task_calls == 1
            restart_record.update(
                {
                    "endState": end_state,
                    "subagents": roster,
                    "originalResult": completed_result,
                    "afterExecution": get_view(execution_url),
                    "afterWorkflow": get_view(workflow_url),
                    "afterEffects": effects(),
                    "providerObservations": [
                        dict(row) for row in provider.request_observations
                    ],
                }
            )
            shutil.copyfile(
                parent_path, tmp_path / f"task-parent-after-{generation}.jsonl"
            )
            shutil.copyfile(
                child_path, tmp_path / f"task-child-after-{generation}.jsonl"
            )
            save()
        if (
            generation == recovery_restarts
            and provider.task_fault != "child-result-gap"
        ):
            # The completed host's normal SIGTERM disposal can append session_exit
            # to its child journal. Capture the quiet-restart baseline only after
            # that process has exited; retain the earlier live-host snapshots.
            assert restarted.poll() is not None
            parent_after_dispose, completed_child = assert_same_journals()
            assert successful_result(parent_after_dispose) == completed_result
            assert terminal_parent_answer(parent_after_dispose) is not None
            assert len(provider.calls) == completed_requests
            baseline_path = tmp_path / "task-child-before-quiet-restart.jsonl"
            shutil.copyfile(child_path, baseline_path)
            restart_record["completedHostDisposal"] = {
                "exitCode": restarted.returncode,
                "childBaseline": str(baseline_path),
                "childEntryCount": len(completed_child),
            }
            save()

    if provider.task_fault == "child-result-gap":
        assert len(gap_outcomes) == 2
        assert all(
            row["parentCompleted"] and row["originalResult"] for row in gap_outcomes
        ), (
            "Completed original child did not recover its original parent result; "
            "recorded refusal safety is not automatic task recovery"
        )


def capture_unrelated_owner_session(
    *,
    release: InstalledRelease,
    tmp_path: Path,
    repository: Path,
    state: Path,
    env: dict[str, str],
    client: httpx.Client,
    workspace_id: str,
    setup: dict,
    provider: RecoveryProvider,
    cli: subprocess.Popen,
    rpc: RpcProcess,
) -> None:
    """Two real CLI processes share configuration, but only one admitted /execute."""
    evidence_file = tmp_path / "session-ownership-evidence.json"
    evidence: dict = {
        "release": str(release.root),
        "manifest": release.digest,
        "admission": setup["admission"],
        "cutReached": False,
    }

    def view(suffix: str) -> dict:
        response = client.get(suffix)
        response.raise_for_status()
        return response.json()

    execution_url = f"/v1/workspaces/{workspace_id}/execution/{provider.key}"
    workflow_url = f"/v1/work-items/{provider.key}/workflow"
    try:
        assert provider.child_held.wait(45), (
            "Owning CLI never reached its actual child MAIN request"
        )
        assert provider.error is None, provider.error
        before = view(execution_url)
        owner = rpc.request("get_state")
        child_before = rpc.request("get_subagents")
        assert before["grant"]["state"] == "active" and owner["isStreaming"] is True
        assert (
            len(child_before["subagents"]) == 1
            and child_before["subagents"][0]["status"] == "running"
        )
        evidence.update(
            {
                "cutReached": True,
                "executionBefore": before,
                "workflowBefore": view(workflow_url),
                "ownerBefore": owner,
                "childBefore": child_before,
                "ownerProcessGroup": process_group_snapshot(cli.pid),
            }
        )
        shutil.copyfile(
            Path(str(owner["sessionFile"])),
            tmp_path / "session-ownership-owner-before.jsonl",
        )
        sibling = tmp_path / "unrelated-sibling"
        _run(
            ["git", "worktree", "add", "-b", "unrelated-owner", str(sibling), "main"],
            repository,
            env,
        )
        arguments = (
            "--mode",
            "rpc",
            "--session",
            str(tmp_path / "unrelated-session.jsonl"),
            "--provider",
            "qualification",
            "--model",
            "local-unrelated",
        )
        command = release.command(state, sibling, *arguments)
        evidence["unrelatedLaunchArgv"] = command
        evidence_file.write_text(json.dumps(evidence, indent=2))
        log = tmp_path / "session-ownership-unrelated.stderr"
        with _process(command, sibling, env, log) as unrelated:
            second_rpc = RpcProcess(unrelated, log)
            try:
                initial = second_rpc.request("get_state")
                evidence["unrelatedInitial"] = initial
                evidence["executionAfterStartup"] = view(execution_url)
                evidence["unrelatedProcessGroup"] = process_group_snapshot(
                    unrelated.pid
                )
                evidence["unrelatedInputRequest"] = second_rpc.send(
                    "prompt",
                    message="Explain what you can inspect in this unrelated sibling checkout. Do not execute tools or workflow actions.",
                )
                evidence["unrelatedTurnEnd"] = second_rpc.until(
                    lambda event: event.get("type") == "agent_end"
                )
                deadline = time.monotonic() + 10
                while True:
                    after = second_rpc.request("get_state")
                    unrelated_entries = read_complete_session(
                        Path(str(after["sessionFile"]))
                    )
                    answers = [
                        row
                        for row in durable_session_branch(unrelated_entries)
                        if row.get("type") == "message"
                        and row.get("message", {}).get("role") == "assistant"
                    ]
                    if not after["isStreaming"] and answers:
                        break
                    assert time.monotonic() < deadline, (
                        "Unrelated input did not settle durably"
                    )
                    time.sleep(0.02)
                assert answers[-1]["message"]["stopReason"] == "stop", (
                    "Unrelated input was aborted or failed"
                )
                evidence["unrelatedInputCompleted"] = True
                evidence["unrelatedAfterInput"] = after
                evidence["unrelatedBindings"] = [
                    row
                    for row in durable_session_branch(unrelated_entries)
                    if row.get("customType") == "work-now"
                    and row.get("data", {}).get("executionWorkspace")
                ]
                shutil.copyfile(
                    Path(str(after["sessionFile"])),
                    tmp_path / "session-ownership-unrelated-after.jsonl",
                )
            finally:
                evidence["executionAfterInput"] = view(execution_url)
                evidence["workflowAfterInput"] = view(workflow_url)
                evidence["ownerAfter"] = rpc.request("get_state")
                evidence["childAfter"] = rpc.request("get_subagents")
                if len(evidence["childAfter"]["subagents"]) == 1:
                    later_child = evidence["childAfter"]["subagents"][0]
                    earlier_child = child_before["subagents"][0]
                    evidence["childClockMetadata"] = {
                        "lastUpdate": {
                            "before": earlier_child.get("lastUpdate"),
                            "after": later_child.get("lastUpdate"),
                        },
                        "durationMs": {
                            "before": earlier_child.get("progress", {}).get(
                                "durationMs"
                            ),
                            "after": later_child.get("progress", {}).get("durationMs"),
                        },
                    }
                evidence["ownerProcessGroupAfter"] = process_group_snapshot(cli.pid)
                evidence["providerRequests"] = provider.request_observations
                evidence_file.write_text(json.dumps(evidence, indent=2))
            assert initial["sessionId"] != owner["sessionId"], (
                "Second CLI reused owning session"
            )
            assert evidence["executionAfterStartup"] == before, (
                "Unrelated startup changed the owning grant"
            )
            assert evidence["executionAfterInput"] == before, (
                "Unrelated input paused or altered the owning grant"
            )
            assert evidence["workflowAfterInput"] == evidence["workflowBefore"], (
                "Unrelated session changed workflow state"
            )
            assert evidence["unrelatedBindings"] == [], (
                "Unrelated startup manufactured an execution binding"
            )
            assert (
                evidence["ownerAfter"]["sessionId"] == owner["sessionId"]
                and evidence["ownerAfter"]["isStreaming"] is True
            )
            assert len(evidence["childAfter"]["subagents"]) == 1
            earlier_child = child_before["subagents"][0]
            later_child = evidence["childAfter"]["subagents"][0]
            for field in (
                "id",
                "agent",
                "agentSource",
                "sessionFile",
                "parentToolCallId",
                "assignment",
                "status",
            ):
                assert later_child[field] == earlier_child[field], (
                    f"Held worker {field} changed"
                )
            for field in ("toolCount", "requests", "recentTools", "recentOutput"):
                assert (
                    later_child["progress"][field] == earlier_child["progress"][field]
                ), f"Held worker {field} changed"
            before_host = installed_cli_host(
                release, evidence["ownerProcessGroup"], cli.pid
            )
            after_host = installed_cli_host(
                release, evidence["ownerProcessGroupAfter"], cli.pid
            )
            assert (after_host["pid"], after_host["startTicks"]) == (
                before_host["pid"],
                before_host["startTicks"],
            ), "Owning CLI process was replaced"
            child_requests = [
                row
                for row in provider.request_observations
                if row["model"] == "local-task"
            ]
            assert (
                len(child_requests) == 1
                and child_requests[0]["responseStarted"] is False
            )
    finally:
        evidence_file.write_text(json.dumps(evidence, indent=2))


def exercise_controller_recovery(
    release: InstalledRelease,
    tmp_path: Path,
    checkpoint: Literal["review", "resume", "persisted", "task-active"],
    *,
    progress: bool = False,
    task_fault: TaskFault = "child-request",
    unrelated_session: bool = False,
    predecessor_case: Literal["startup-read", "paused-read", "reopen"] | None = None,
    completion_route: Literal["execution", "work"] | None = None,
    committed_post_loss: bool = False,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    state = tmp_path / "runtime"
    process_tmp = tmp_path / "process-tmp"
    process_tmp.mkdir()
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "TMPDIR": str(process_tmp),
    }
    adapter = (
        Path(__file__).resolve().parents[3]
        / "session-system/tests/fixtures/installed-gh-protection.sh"
    )
    release = InstalledRelease(release.root, release.digest, adapter)
    assert os.access(adapter, os.X_OK), "External GitHub fixture must be executable"
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

    def read_progress_operations() -> list[dict]:
        # Read only the disposable service's real idempotency receipts.
        with psycopg.connect(
            **config.connection_kwargs("postgres"), row_factory=dict_row
        ) as connection:
            return connection.execute(
                "SELECT operation_id::text, request_id::text, request_sha256, result_sha256, state, response FROM omp_control.idempotent_commands WHERE workspace_id=%s AND command_type='seal_execution_criteria' ORDER BY operation_id",
                (identity["workspace_id"],),
            ).fetchall()

    base_url = f"http://127.0.0.1:{service_port}"
    provider = RecoveryProvider(tmp_path, checkpoint, progress, task_fault)
    authority_proxy = (
        AuthorityResponseProxy(base_url, tmp_path)
        if task_fault == "child-result-gap" or predecessor_case is not None or committed_post_loss
        else None
    )
    with (
        native_postgres(tmp_path / "postgres", pg_port),
        provider.serve() as model_url,
        (
            authority_proxy.serve()
            if authority_proxy
            else contextlib.nullcontext(base_url)
        ) as client_base_url,
    ):
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
            client_base_url,
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
        if checkpoint == "task-active":
            model_file = agent_dir / "models.yml"
            model_config = json.loads(model_file.read_text())
            models = model_config["providers"]["qualification"]["models"]
            models.append({**models[0], "id": "local-task", "name": "Local task"})
            models.append(
                {**models[0], "id": "local-unrelated", "name": "Unrelated owner"}
            )
            model_file.write_text(json.dumps(model_config))
            (agent_dir / "config.yml").write_text(
                "modelRoles:\n  audit: qualification/local-recovery\n  default: qualification/local-recovery\n  smol: qualification/local-recovery\n  task: qualification/local-task\nadvisor:\n  enabled: false\nasync:\n  enabled: false\ntask:\n  batch: false\n  isolation:\n    mode: none\n  prewalk: false\n  maxRecursionDepth: 1\ntools:\n  xdev: false\n"
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
                        *(["CANCELLED" if predecessor_case == "paused-read" else "CANCELED"] if predecessor_case else []),
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
            if authority_proxy is not None:
                assert client_config["base_url"] == client_base_url
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
                if committed_post_loss:
                    assert authority_proxy is not None
                    authority_proxy.arm_committed_command("seal_execution_criteria")
                with _process(
                    release.command(state, repository, *cli_args),
                    repository,
                    env,
                    cli_log,
                ) as cli:
                    rpc = RpcProcess(
                        cli,
                        cli_log,
                        completion_stop=task_fault == "first-run-result-gap",
                    )
                    before_admission = rpc.request("get_state")
                    admission_request = rpc.send(
                        "prompt", message=f"/execute {provider.key}"
                    )
                    rpc.until(
                        lambda event: (
                            event.get("type") == "extension_ui_request"
                            and event.get("method") == "notify"
                            and f"Execution grant started for {provider.key} in "
                            in str(event.get("message", ""))
                        )
                    )
                    initial = rpc.request("get_state")
                    own_path = Path(str(initial["sessionFile"]))
                    deadline = time.monotonic() + 10
                    own_bindings = []
                    while time.monotonic() < deadline:
                        own_bindings = [
                            row
                            for row in read_complete_session(own_path)
                            if row.get("customType") == "work-now"
                            and row.get("data", {}).get("backend") == "work"
                            and row.get("data", {}).get("executionWorkspace")
                        ]
                        if own_bindings:
                            break
                        time.sleep(0.02)
                    assert own_bindings, (
                        "Actual /execute did not persist its owning session binding"
                    )
                    execution_response = client.get(
                        f"/v1/workspaces/{identity['workspace_id']}/execution/{provider.key}"
                    )
                    execution_response.raise_for_status()
                    setup["execution"] = execution_response.json()
                    if predecessor_case:
                        assert setup["execution"]["active_item"]["active_blocker_ids"] == [setup["predecessor"]["source"]["id"]]
                        predecessor_workflow = client.get(f"/v1/work-items/{provider.key}/workflow")
                        predecessor_workflow.raise_for_status()
                        setup["predecessor"]["retainedRelations"] = predecessor_workflow.json()["relations"]
                    setup["workspace"] = own_bindings[-1]["data"]["executionWorkspace"]
                    setup["admission"] = {
                        "requestId": admission_request,
                        "before": before_admission,
                        "after": initial,
                        "bindingEntry": own_bindings[-1],
                        "externalGitHubAdapter": str(adapter),
                        "adapterSha256": hashlib.sha256(
                            adapter.read_bytes()
                        ).hexdigest(),
                        "wrapperArgv": release.command(state, repository, *cli_args),
                    }
                    (tmp_path / "setup.json").write_text(json.dumps(setup, indent=2))
                    if committed_post_loss:
                        assert authority_proxy is not None
                        assert authority_proxy.held.wait(45), "Controller did not issue selected POST"
                        held = [
                            row
                            for row in authority_proxy.snapshot()
                            if row.get("commandType") == "seal_execution_criteria"
                            and row.get("held")
                        ]
                        assert len(held) == 1, f"Expected 1 held POST, got {len(held)}"
                        cut = held[0]
                        assert (
                            not cut["responseStarted"]
                            and cut["responseBytesWritten"] == 0
                        )
                        envelope = json.loads(Path(cut["requestFile"]).read_bytes())
                        upstream = json.loads(Path(cut["bodyFile"]).read_bytes())
                        assert upstream.get("receipt", {}).get("state") == "applied", (
                            f"Upstream receipt not applied: {upstream}"
                        )
                        assert upstream.get("result", {}).get("status") != "refused", (
                            f"Upstream result refused: {upstream}"
                        )
                        operation_id = envelope["operation_id"]

                        committed = read_progress_operations()
                        assert len(committed) == 1 and committed[0]["operation_id"] == operation_id
                        assert committed[0]["state"] == "applied"
                        assert committed[0]["request_id"] == envelope["request_id"]
                        assert CommandResponse.model_validate(upstream) == CommandResponse.model_validate(
                            {"receipt": upstream["receipt"], "result": committed[0]["response"]}
                        )
                        assert committed[0]["result_sha256"] == upstream["receipt"]["result_sha256"]
                        assert committed[0]["request_sha256"] == upstream["receipt"]["request_sha256"]

                        execution_url = f"/v1/workspaces/{identity['workspace_id']}/execution/{provider.key}"
                        visible_response = client.get(execution_url)
                        visible_response.raise_for_status()
                        visible = visible_response.json()
                        assert visible["active_item"]["phase"] == "planning"
                        assert visible["grant"]["grant_version"] == envelope["command"]["payload"]["expected_grant_version"] + 1
                        assert visible["active_item"]["criteria_revision_id"] == upstream["result"]["revision"]["revision_id"]

                        journal_dir = state / "config/omp-work/pending-operations"
                        claims = [
                            (path, json.loads(path.read_bytes()))
                            for path in journal_dir.glob("*.json")
                            if path.is_file()
                        ]
                        matching_claims = [
                            (path, record)
                            for path, record in claims
                            if record.get("envelope", {}).get("operation_id") == operation_id
                        ]
                        assert len(matching_claims) == 1, (
                            f"Expected 1 matching claim for {operation_id}, found {len(matching_claims)} in {claims}"
                        )
                        claim_path, claim = matching_claims[0]
                        assert claim["envelope"] == envelope and "result" not in claim
                        claim_bytes = claim_path.read_bytes()

                        commit_confirmed_at = time.time()
                        post_started_at = cut["startedAt"]
                        cut_duration = commit_confirmed_at - post_started_at
                        assert cut_duration < 8.0, (
                            f"Observer cut exceeded 8s budget: {cut_duration}s >= 8.0s"
                        )
                        evidence = {
                            "request": envelope,
                            "upstream": upstream,
                            "databaseBeforeKill": committed,
                            "executionBeforeKill": visible,
                            "claimPath": str(claim_path),
                            "claimSha256": hashlib.sha256(claim_bytes).hexdigest(),
                            "rawClaim": json.loads(claim_bytes.decode("utf-8")),
                            "postStartedAt": post_started_at,
                            "upstreamCompletedAt": cut["upstreamCompletedAt"],
                            "commitConfirmedAt": commit_confirmed_at,
                            "controllerPid": cli.pid,
                            "servicePid": service.pid,
                            "session": str(own_path),
                            "cut": cut,
                            "cutDurationMs": round(cut_duration * 1000, 2),
                        }
                        (tmp_path / "authority-committed-before-kill.json").write_text(
                            json.dumps(evidence, indent=2)
                        )
                        assert cli.poll() is None and service.poll() is None
                        os.killpg(cli.pid, signal.SIGKILL)
                        assert cli.wait(timeout=10) == -signal.SIGKILL
                        evidence["controllerKilledAt"] = time.time()
                        assert service.poll() is None and read_progress_operations() == committed
                        assert claim_path.read_bytes() == claim_bytes
                        authority_proxy.discard_held_command_response()
                        provider.restarting = True

                        restart_args = (
                            "--mode",
                            "rpc",
                            "--session",
                            str(own_path),
                            "--provider",
                            "qualification",
                            "--model",
                            "local-recovery",
                        )
                        restart_log = tmp_path / "controller-committed-restart.stderr"
                        with _process(
                            release.command(state, repository, *restart_args),
                            repository,
                            env,
                            restart_log,
                        ) as restarted:
                            restarted_rpc = RpcProcess(restarted, restart_log)
                            assert restarted_rpc.request("get_state")["sessionId"] == initial["sessionId"]

                            restarted_rpc.request("prompt", message=f"/execute resume {provider.key}")
                            resume_deadline = time.monotonic() + 10
                            while (
                                restarted_rpc.request("get_state")["isStreaming"]
                                and time.monotonic() < resume_deadline
                            ):
                                time.sleep(0.05)
                            assert not restarted_rpc.request("get_state")["isStreaming"]

                            deadline = time.monotonic() + 30
                            resolution_observations = []
                            resolved_claim_observed = False
                            unresolved: list[dict] = []
                            initial_rpc_state = restarted_rpc.request("get_state")
                            assert initial_rpc_state["sessionId"] == initial["sessionId"]
                            current_state = initial_rpc_state
                            last_state_poll = time.monotonic()
                            prev_obs_tuple = None
                            while True:
                                now_mono = time.monotonic()
                                if now_mono - last_state_poll >= 1.0:
                                    current_state = restarted_rpc.request("get_state")
                                    assert current_state["sessionId"] == initial["sessionId"]
                                    last_state_poll = now_mono
                                records = [
                                    json.loads(path.read_bytes())
                                    for path in journal_dir.glob("*.json")
                                    if path.is_file()
                                ]
                                original_claims = [
                                    row
                                    for row in records
                                    if row.get("envelope", {}).get("operation_id") == operation_id
                                ]
                                unresolved = [
                                    row for row in original_claims if "result" not in row
                                ]
                                if any("result" in row for row in original_claims):
                                    resolved_claim_observed = True
                                current_committed = read_progress_operations()
                                is_streaming = current_state.get("isStreaming", False)

                                obs_tuple = (
                                    json.dumps(original_claims, sort_keys=True),
                                    json.dumps(current_committed, sort_keys=True),
                                    is_streaming,
                                )
                                if prev_obs_tuple is None or obs_tuple != prev_obs_tuple:
                                    observation_entry = {
                                        "observedAt": time.time(),
                                        "isStreaming": is_streaming,
                                        "originalClaims": original_claims,
                                        "committedRows": current_committed,
                                    }
                                    if prev_obs_tuple is None:
                                        observation_entry["rpcState"] = initial_rpc_state
                                    resolution_observations.append(observation_entry)
                                    prev_obs_tuple = obs_tuple

                                if (not unresolved and resolved_claim_observed) or time.monotonic() >= deadline:
                                    if resolution_observations and "rpcState" not in resolution_observations[-1]:
                                        final_state = restarted_rpc.request("get_state")
                                        resolution_observations.append({
                                            "observedAt": time.time(),
                                            "isStreaming": final_state.get("isStreaming", False),
                                            "originalClaims": original_claims,
                                            "committedRows": current_committed,
                                            "rpcState": final_state,
                                        })
                                    break
                                time.sleep(0.05)
                            (tmp_path / "authority-committed-startup-resolution.json").write_text(
                                json.dumps(resolution_observations, indent=2)
                            )
                            assert resolved_claim_observed, (
                                "Actual startup/resume did not legitimately resolve original claim with a recorded result"
                            )
                            assert not unresolved, (
                                "Actual startup/resume left original committed operation journal unresolved"
                            )
                            assert provider.error is None, provider.error

                            stored_op = client.get(f"/v1/operations/{operation_id}").json()
                            assert stored_op["result"] == committed[0]["response"]
                            resolved_claims = [row for row in original_claims if "result" in row]
                            assert resolved_claims[0]["result"] == stored_op["result"]

                            restarted_rpc.request("prompt", message=f"/execute resume {provider.key}")
                            resume_deadline = time.monotonic() + 10
                            while (
                                restarted_rpc.request("get_state")["isStreaming"]
                                and time.monotonic() < resume_deadline
                            ):
                                time.sleep(0.05)
                            assert not restarted_rpc.request("get_state")["isStreaming"]

                            assert read_progress_operations() == committed
                            observed = client.get(execution_url)
                            observed.raise_for_status()
                            observed_exec = observed.json()
                            assert observed_exec["grant"]["grant_id"] == visible["grant"]["grant_id"]
                            assert observed_exec["active_item"]["criteria_revision_id"] == visible["active_item"]["criteria_revision_id"]
                            assert observed_exec["active_item"]["phase"] == "planning"

                            remaining = [
                                json.loads(path.read_bytes())
                                for path in journal_dir.glob("*.json")
                                if path.is_file()
                            ]
                            assert not any(
                                row.get("envelope", {}).get("operation_id") == operation_id
                                and "result" not in row
                                for row in remaining
                            ), "Original journal remains unresolved"

                            command_records = [
                                row
                                for row in authority_proxy.snapshot()
                                if row.get("commandType") == "seal_execution_criteria"
                            ]
                            original_response = next(
                                row for row in command_records if row["ordinal"] == cut["ordinal"]
                            )
                            assert (
                                not original_response["responseStarted"]
                                and original_response["responseBytesWritten"] == 0
                            )
                            for row in command_records:
                                actual_envelope = json.loads(Path(row["requestFile"]).read_bytes())
                                assert actual_envelope == envelope, "Recovery minted a replacement operation"

                            evidence["replayPostCount"] = len(command_records) - 1
                            assert evidence["replayPostCount"] == 0, (
                                f"Expected zero replay POST, got {evidence['replayPostCount']}"
                            )

                            operation_gets = authority_proxy.operation_gets_snapshot()
                            restarted_operation_gets = [
                                row
                                for row in operation_gets
                                if row["path"] == f"/v1/operations/{operation_id}"
                                and row["startedAt"] >= evidence["controllerKilledAt"]
                            ]
                            assert len(restarted_operation_gets) >= 1, (
                                f"Expected restarted CLI to make GET for operation {operation_id}, got {operation_gets}"
                            )
                            assert restarted_operation_gets[0]["status"] == 200
                            evidence["restartedOperationGets"] = restarted_operation_gets

                            evidence["startupResolutionObservations"] = resolution_observations
                            evidence.update(
                                {
                                    "databaseAfterRepeatedResume": read_progress_operations(),
                                    "proxyAfterRepeatedResume": command_records,
                                    "remainingJournal": remaining,
                                    "restartPid": restarted.pid,
                                    "serviceAlive": service.poll() is None,
                                }
                            )
                            (tmp_path / "authority-committed-reconciled.json").write_text(
                                json.dumps(evidence, indent=2)
                            )
                        return
                    if unrelated_session:
                        capture_unrelated_owner_session(
                            release=release,
                            tmp_path=tmp_path,
                            repository=repository,
                            state=state,
                            env=env,
                            client=client,
                            workspace_id=identity["workspace_id"],
                            setup=setup,
                            provider=provider,
                            cli=cli,
                            rpc=rpc,
                        )
                        return
                    if checkpoint == "task-active":
                        capture_task_active_recovery(
                            release=release,
                            tmp_path=tmp_path,
                            repository=repository,
                            remote=remote,
                            state=state,
                            env=env,
                            client=client,
                            workspace_id=identity["workspace_id"],
                            setup=setup,
                            provider=provider,
                            cli=cli,
                            rpc=rpc,
                            initial=initial,
                            service_pid=service.pid,
                            authority_proxy=authority_proxy,
                        )
                        return
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
                        if predecessor_case == "paused-read":
                            assert authority_proxy is not None
                            keyed_path = f"/v1/work-items/{setup['predecessor']['source']['key']}"
                            paused_before = client.get(execution_url).json()
                            calls_before = len(provider.calls)
                            authority_proxy.set_predecessor_read_fault(keyed_path, True)
                            refused_request = rpc.send("prompt", message=f"/execute resume {provider.key}")
                            refusal = rpc.until(lambda event: event.get("type") == "extension_ui_request"
                                and "retryable predecessor" in str(event.get("message", ""))
                                and "keyed read failed" in str(event.get("message", "")))
                            paused_after = client.get(execution_url).json()
                            assert paused_after == paused_before
                            assert len(provider.calls) == calls_before
                            fault_reads = [row for row in authority_proxy.snapshot() if row.get("transportDisconnected")]
                            assert fault_reads and all(row["path"] == keyed_path and row["status"] == 200 for row in fault_reads)
                            (tmp_path / "predecessor-paused-refusal.json").write_text(json.dumps({
                                "rpcRequest": refused_request, "refusal": refusal,
                                "before": paused_before, "after": paused_after,
                                "requests": fault_reads, "providerCalls": calls_before,
                            }, indent=2))
                            authority_proxy.set_predecessor_read_fault(keyed_path, False)
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
                            checkpoint in ("review", "persisted")
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
                    if checkpoint == "persisted":
                        persisted = [
                            entry
                            for entry in entries
                            if entry.get("type") == "custom_message"
                            and entry.get("customType") == "work-execute"
                            and entry.get("details", {})
                            .get("executionContinuation", {})
                            .get("messageId")
                            == intent["messageId"]
                        ]
                        assert len(persisted) == 1
                        persisted_index = entries.index(persisted[0])
                        suffix = entries[persisted_index + 1 :]
                        assert not any(
                            entry.get("type")
                            in ("message", "custom_message", "tool_execution_start")
                            for entry in suffix
                        ), suffix
                        (tmp_path / "persisted-boundary.json").write_text(
                            json.dumps(
                                {
                                    "entry": persisted[0],
                                    "suffix": suffix,
                                    "providerRequests": len(provider.calls),
                                },
                                indent=2,
                            )
                        )
                    else:
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
                    held_provider_requests = len(provider.calls)
                    os.killpg(cli.pid, signal.SIGKILL)
                    killed_exit = cli.wait(timeout=10)
                    assert killed_exit == -signal.SIGKILL
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
                if predecessor_case in ("startup-read", "reopen"):
                    assert authority_proxy is not None
                    source = setup["predecessor"]["source"]
                    keyed_path = f"/v1/work-items/{source['key']}"

                    def predecessor_state(value: str) -> dict:
                        response = client.post("/v1/commands", json={
                            "api_version": "work.omp.dev/v1", "workspace_id": identity["workspace_id"],
                            "operation_id": str(uuid4()), "request_id": str(uuid4()), "correlation_id": str(uuid4()),
                            "command": {"type": "set_work_state", "payload": {"work_id": source["id"], "state": value}},
                        })
                        response.raise_for_status()
                        return response.json()

                    reopen_result = predecessor_state("BACKLOG") if predecessor_case == "reopen" else None
                    authority_proxy.set_predecessor_read_fault(keyed_path, predecessor_case == "startup-read")
                    blocked_log = tmp_path / "controller-predecessor-refused.stderr"
                    with _process(release.command(state, repository, *restart_args), repository, env, blocked_log) as blocked:
                        blocked_rpc = RpcProcess(blocked, blocked_log)
                        refusal = blocked_rpc.until(lambda event: event.get("type") == "extension_ui_request"
                            and "Execution recovery skipped:" in str(event.get("message", ""))
                            and ("keyed read failed" if predecessor_case == "startup-read" else "unfinished predecessor") in str(event.get("message", "")))
                        blocked_state = blocked_rpc.request("get_state")
                        assert blocked_state["isStreaming"] is False
                        refused_execution = client.get(execution_url).json()
                        assert refused_execution == before_execution
                        assert provider.recovery_calls == 0
                        blocked_entries = read_complete_session(actual_session)
                        assert execution_message_count(blocked_entries) == execution_message_count(entries)
                        reads = [row for row in authority_proxy.snapshot() if row.get("predecessorRead")]
                        assert reads and all(row["path"] == keyed_path and row["status"] == 200 for row in reads)
                        assert any(row["transportDisconnected"] for row in reads) == (predecessor_case == "startup-read")
                        (tmp_path / "predecessor-startup-refusal.json").write_text(json.dumps({
                            "case": predecessor_case, "refusal": refusal, "before": before_execution,
                            "after": refused_execution, "requests": reads, "reopenOperation": reopen_result,
                            "providerRecoveryCalls": provider.recovery_calls,
                        }, indent=2))
                    authority_proxy.set_predecessor_read_fault(keyed_path, False)
                    if predecessor_case == "reopen":
                        (tmp_path / "predecessor-state-restored.json").write_text(json.dumps(predecessor_state("CANCELED"), indent=2))
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
                    if progress and recovered:
                        assert provider.progressed.wait(10), (
                            "Resumed tool operation did not return"
                        )
                        assert provider.error is None, provider.error
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
                        "exitCode": killed_exit,
                        "providerRequestsBeforeKill": held_provider_requests,
                        "providerRequestsAfterRestart": len(provider.calls)
                        - held_provider_requests,
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
                        "heldProviderRequest": held_provider_requests,
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
                    if checkpoint in ("resume", "persisted"):
                        assert not recovery_refusals, (
                            f"Preflight refusal prevents isolated outbox attribution: {recovery_refusals}"
                        )
                    progress_operations: list[dict] = []

                    if progress:
                        assert (
                            before_execution["active_item"]["phase"]
                            == "criteria_pending"
                        )
                        assert after_execution["active_item"]["phase"] == "planning"
                        assert after_execution["grant"]["grant_id"] == intent["grantId"]
                        assert (
                            after_execution["grant"]["continuations_scheduled"]
                            == before_execution["grant"]["continuations_scheduled"]
                        )
                        assert (
                            after_execution["grant"]["grant_version"]
                            == before_execution["grant"]["grant_version"] + 1
                        )
                        progress_operations = read_progress_operations()
                        assert len(progress_operations) == 1
                        operation = progress_operations[0]
                        assert operation["state"] == "applied"
                        assert (
                            operation["response"]["grant"] == after_execution["grant"]
                        )
                        assert (
                            operation["response"]["item"]["work_id"] == intent["workId"]
                        )
                        assert (
                            operation["response"]["revision"]["revision_id"]
                            == after_execution["active_item"]["criteria_revision_id"]
                        )
                        evidence["progressOperations"] = progress_operations
                    else:
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
                    deadline = time.monotonic() + 5
                    while True:
                        resumed_entries = [
                            json.loads(line)
                            for line in actual_session.read_text().splitlines()
                            if line
                        ]
                        if (
                            checkpoint != "persisted"
                            or any(
                                entry.get("type") == "message"
                                and entry.get("message", {}).get("role") == "assistant"
                                and entry["message"].get("stopReason") == "stop"
                                for entry in resumed_entries
                            )
                            or time.monotonic() >= deadline
                        ):
                            break
                        time.sleep(0.05)
                    shutil.copyfile(actual_session, tmp_path / "settled-session.jsonl")
                    assert execution_message_count(resumed_entries) == (
                        1 if checkpoint == "persisted" else 2
                    ), (
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
                    if checkpoint == "persisted":
                        assert recovered_messages[0]["id"] == persisted[0]["id"]
                        restarted_requests = [
                            call
                            for call in provider.calls
                            if any(
                                tool.get("function", {}).get("name") == "work"
                                for tool in call.get("tools", [])
                            )
                        ][1:]
                        for request in restarted_requests:
                            provider_content = json.dumps(request.get("messages", []))
                            assert (
                                provider_content.count(
                                    "# Autonomous Delivery Cycle (/execute)"
                                )
                                == 1
                            )
                        terminal = [
                            entry
                            for entry in resumed_entries[
                                resumed_entries.index(recovered_messages[0]) + 1 :
                            ]
                            if entry.get("type") == "message"
                            and entry.get("message", {}).get("role") == "assistant"
                            and entry["message"].get("stopReason") == "stop"
                        ]
                        assert len(terminal) == 1, (
                            "Recovered answer has not settled durably"
                        )
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
                    if predecessor_case:
                        current_workflow = client.get(workflow_url)
                        current_workflow.raise_for_status()
                        assert current_workflow.json()["relations"] == setup["predecessor"]["retainedRelations"]
                        assert after_execution["active_item"]["active_blocker_ids"] == [setup["predecessor"]["source"]["id"]]
                        assert authority_proxy is not None
                        restored_reads = [row for row in authority_proxy.snapshot() if row.get("predecessorRead") and not row["transportDisconnected"]]
                        assert restored_reads
                        evidence["predecessor"] = {"fixture": setup["predecessor"], "reads": authority_proxy.snapshot()}
                    assert provider.recovery_calls == (2 if progress else 1)
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
                    if progress:
                        assert provider.operation_calls == 1, (
                            "A second restart duplicated the original service operation"
                        )
                        assert read_progress_operations() == progress_operations
                    else:
                        assert provider.recovery_calls == 1, (
                            "A second restart duplicated the settled turn"
                        )
                    again_response = client.get(execution_url)
                    again_response.raise_for_status()
                    if progress:
                        assert (
                            again_response.json()["active_item"]["criteria_revision_id"]
                            == after_execution["active_item"]["criteria_revision_id"]
                        )
                    else:
                        assert (
                            again_response.json()["grant"] == before_execution["grant"]
                        )
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
                if completion_route is not None:
                    assert checkpoint == "review" and predecessor_case is not None
                    complete_installed_predecessor_fixture(
                        client, identity["workspace_id"], provider.key, execution_url,
                        setup, tmp_path, model_url, result_path, completion_route,
                    )


def complete_installed_predecessor_fixture(
    client: httpx.Client, workspace_id: str, key: str, execution_url: str,
    setup: dict, root: Path, model_url: str, result_path: Path,
    route: Literal["execution", "work"],
) -> None:
    """Driver issues real completion to installed service after real controller recovery.

    Reuse only pure payload builders; no TestClient, forged database state or
    substituted native results. Scripted local provider supplies actual audit
    transport, while installed WorkService creates/validates every receipt.
    """
    records: list[dict] = []

    def command(kind: str, payload: dict) -> tuple[dict, dict]:
        envelope = {"api_version": "work.omp.dev/v1", "workspace_id": workspace_id,
            "operation_id": str(uuid4()), "request_id": str(uuid4()), "correlation_id": str(uuid4()),
            "command": {"type": kind, "payload": payload}}
        response = client.post("/v1/commands", json=envelope)
        record = {"envelope": envelope, "status": response.status_code, "response": response.json()}
        records.append(record)
        (root / "installed-completion-commands.json").write_text(json.dumps(records, indent=2))
        response.raise_for_status()
        actual = response.json()
        assert actual["receipt"]["state"] == "applied", record
        result = actual["result"]
        assert result.get("status") != "refused", record
        return envelope, result

    def workflow() -> dict:
        response = client.get(f"/v1/work-items/{key}/workflow")
        response.raise_for_status()
        return response.json()

    def deliver_pending_checkpoints() -> dict:
        view = workflow()
        latest: dict[str, dict] = {}
        for delivery in view["checkpoint_deliveries"]:
            prior = latest.get(delivery["event_id"])
            if prior is None or delivery["delivery_sequence"] > prior["delivery_sequence"]:
                latest[delivery["event_id"]] = delivery
        for event in view["close_attempt_events"]:
            delivered = latest.get(event["event_id"], {}).get("status") in ("delivered", "waived")
            if event["requires_delivery"] and not delivered:
                # This fixture's delivery sink receives the exact native text
                # before the driver attests its digest; no acknowledgment is invented.
                rendered = event["rendered_text"].encode("utf-8")
                assert hashlib.sha256(rendered).hexdigest() == event["rendered_sha256"]
                destination = root / f"installed-completion-event-{event['event_id']}.txt"
                destination.write_bytes(rendered)
                assert destination.read_bytes() == rendered
                command("attest_checkpoint_delivery", {"event_id": event["event_id"],
                    "owner_session_id": attempt["owner_session_id"],
                    "rendered_sha256": event["rendered_sha256"], "status": "delivered"})
        return workflow()

    before = workflow()
    candidate = before["item"]["candidate"]
    assert candidate["kind"] == "final"
    attempt = next(a for a in before["close_attempts"]
        if a["candidate_id"] == candidate["candidate_id"]
        and a["execution_grant_id"] == setup["execution"]["grant"]["grant_id"])
    # begin_execution_review freezes the candidate and verification, but the
    # actual audit manifest is sealed by the next supported service command.
    if before["audit_manifest"] is None:
        verification = next(r for r in before["receipts"]
            if r["kind"] == "verification" and r["candidate_id"] == candidate["candidate_id"])
        _, sealed = command("seal_audit_manifest", {"attempt_id": attempt["attempt_id"],
            "verification_receipt_id": verification["receipt_id"]})
        assert sealed["status"] == "applied"
        before = workflow()
    manifest = before["audit_manifest"]
    assert manifest is not None and manifest["attempt_id"] == attempt["attempt_id"]
    assert any(r["kind"] == "push" and r["candidate_id"] == before["item"]["candidate"]["candidate_id"] for r in before["receipts"])
    _, reserved = command("reserve_auditor_launch", {"attempt_id": attempt["attempt_id"],
        "task_sha256": manifest["task_sha256"], "tool_call_id": "installed-predecessor-audit"})
    response = httpx.post(model_url + "/chat/completions", json={
        "model": "local-predecessor-audit", "qualificationArtifact": str(result_path),
        "messages": [], "stream": True,
    }, trust_env=False, timeout=30)
    response.raise_for_status()
    (root / "installed-completion-audit.sse").write_bytes(response.content)
    chunks = [json.loads(line.removeprefix("data: ")) for line in response.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"]
    content = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert json.loads(content)["verdict"] == "PASS"
    _, settled = command("settle_auditor_launch", {"attempt_id": attempt["attempt_id"],
        "launch_id": reserved["launch"]["launch_id"], "transport_payload": content})
    assert settled["status"] == "applied" and settled["verdict"] == "PASS"
    # record_closeout_review itself refuses outstanding delivery debt, so
    # deliver the seal/reserve/settle checkpoints before requesting closeout.
    current = deliver_pending_checkpoints()
    if route == "work":
        item = current["item"]
        closeout = _receipt(item["work_id"], item["revision"]["revision_id"],
            item["candidate"]["candidate_id"], "closeout",
            body={"observedResult": result_path.read_text(), "auditTransportSha256": hashlib.sha256(response.content).hexdigest()})
        command("record_closeout_review", {"receipt": closeout, "attempt_id": attempt["attempt_id"],
            "authorization_ref": attempt["authorization_ref"]})
    # Closeout records a new checkpoint; deliver it before complete_work.
    current = deliver_pending_checkpoints()
    # Prove every builder input exists, so its negative-test fallback values cannot be used.
    assert any(r["receipt_id"] == manifest["verification_receipt_id"]
        and r["kind"] == "verification" for r in current["receipts"])
    assert any(r["kind"] == "audit" and r["verdict"] == "PASS"
        and r["payload"]["launch_id"] == reserved["launch"]["launch_id"]
        for r in current["receipts"])
    assert any(launch["launch_id"] == reserved["launch"]["launch_id"]
        and launch["task_sha256"] == manifest["task_sha256"] for launch in current["auditor_launches"])
    assert current["audit_manifest"] == manifest
    push = next(r for r in current["receipts"] if r["kind"] == "push"
        and r["candidate_id"] == candidate["candidate_id"])
    completion_evidence = _build_completion_evidence_from_view(current, push["receipt_id"])
    execution = client.get(execution_url).json()
    assert execution["grant"]["grant_id"] == setup["execution"]["grant"]["grant_id"]
    if route == "execution":
        kind = "complete_execution_item"
        payload = {"grant_id": execution["grant"]["grant_id"],
            "expected_grant_version": execution["grant"]["grant_version"],
            "work_id": current["item"]["work_id"], "attempt_id": attempt["attempt_id"],
            "judge_sha256": execution["grant"]["judge_sha256"], "evidence": completion_evidence}
    else:
        kind = "complete_work"
        payload = {"input": {"work_id": current["item"]["work_id"],
            "current_revision_id": current["item"]["revision"]["revision_id"],
            "candidate": current["item"]["candidate"],
            "receipts": [r for r in current["receipts"] if r["candidate_id"] == current["item"]["candidate"]["candidate_id"]],
            "closeout_requested": True}, "attempt_id": attempt["attempt_id"],
            "done_authorization_ref": "done:installed-predecessor-fixture", "evidence": completion_evidence}
    envelope, completed = command(kind, payload)
    assert completed["state"] == "DONE" and completed["type"] == kind
    after = workflow()
    assert after["item"]["state"] == "DONE"
    assert after["relations"] == setup["predecessor"]["retainedRelations"]
    source = client.get(f"/v1/work-items/{setup['predecessor']['source']['key']}")
    source.raise_for_status()
    assert source.json()["state"] == setup["predecessor"]["state"]
    replay = client.post("/v1/commands", json=envelope)
    replay.raise_for_status()
    assert replay.json()["result"] == completed
    assert workflow() == after
    (root / "installed-completion-evidence.json").write_text(json.dumps({
        "issuer": "test driver via real HTTP; controller performed preceding recovery",
        "route": kind, "before": before, "after": after, "executionBeforeCompletion": execution,
        "completion": completed, "replay": replay.json(), "predecessor": source.json(),
    }, indent=2))


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


def test_killed_controller_resumes_persisted_unanswered_turn(
    installed_release: InstalledRelease, tmp_path: Path
) -> None:
    """A durable execution prompt with no answer must resume without reinjection."""
    exercise_controller_recovery(installed_release, tmp_path, "persisted")


def test_killed_persisted_turn_performs_one_real_criteria_seal(
    installed_release: InstalledRelease, tmp_path: Path
) -> None:
    """Recovered model tool dispatch must advance real service state exactly once."""
    exercise_controller_recovery(
        installed_release, tmp_path, "persisted", progress=True
    )


def test_killed_controller_recovers_original_active_task(
    installed_release: InstalledRelease, tmp_path: Path
) -> None:
    """Original synchronous task must progress after its shared CLI is killed."""
    exercise_controller_recovery(installed_release, tmp_path, "task-active")


def test_unrelated_owner_session_cannot_join_or_pause_bound_execution(
    installed_release: InstalledRelease,
    tmp_path: Path,
) -> None:
    """Fresh sibling CLI startup/input cannot acquire another session's execution."""
    exercise_controller_recovery(
        installed_release, tmp_path, "task-active", unrelated_session=True
    )


def test_repeated_child_kill_preserves_original_task_preparation(
    installed_release: InstalledRelease, tmp_path: Path
) -> None:
    """Fresh recovery preparation must keep the same child/input across another kill."""
    exercise_controller_recovery(
        installed_release,
        tmp_path,
        "task-active",
        task_fault="repeated-child-request",
    )


def test_persisted_parent_task_result_resumes_without_child_dispatch(
    installed_release: InstalledRelease, tmp_path: Path
) -> None:
    """A durable original task result resumes parent without child dispatch/writes."""
    exercise_controller_recovery(
        installed_release, tmp_path, "task-active", task_fault="parent-result"
    )


def test_completed_child_recovers_missing_original_parent_result(
    installed_release: InstalledRelease,
    tmp_path: Path,
) -> None:
    """A completed original child must recover its missing parent result once."""
    exercise_controller_recovery(
        installed_release, tmp_path, "task-active", task_fault="child-result-gap"
    )


def test_original_child_resumes_after_durable_read(
    installed_release: InstalledRelease, tmp_path: Path
) -> None:
    """Original child continues after its durable read without repeating that step."""
    exercise_controller_recovery(
        installed_release, tmp_path, "task-active", task_fault="child-read-gap"
    )


def test_first_run_completed_child_recovers_missing_parent_result(
    installed_release: InstalledRelease, tmp_path: Path
) -> None:
    """Only a verified first-run frozen cut may establish the missing-result recovery gap."""
    attempts: list[dict] = []
    report = tmp_path / "first-run-attempts.json"
    for index in range(1, 4):
        root = tmp_path / f"attempt-{index}"
        root.mkdir()
        try:
            exercise_controller_recovery(
                installed_release,
                root,
                "task-active",
                task_fault="first-run-result-gap",
            )
        except FirstRunCutMiss as error:
            attempts.append(
                {
                    "attempt": index,
                    "path": str(root),
                    "outcome": "cut-missed",
                    "reason": str(error),
                }
            )
            report.write_text(json.dumps(attempts, indent=2))
            continue
        except AssertionError:
            attempts.append(
                {
                    "attempt": index,
                    "path": str(root),
                    "outcome": "assertion-failed",
                    "evidence": str(root / "task-recovery-evidence.json"),
                }
            )
            report.write_text(json.dumps(attempts, indent=2))
            raise
        attempts.append({"attempt": index, "path": str(root), "outcome": "recovered"})
        report.write_text(json.dumps(attempts, indent=2))
        return
    raise AssertionError(
        "First-run completion checkpoint not reached in three fresh attempts; preserved misses are not recovery evidence"
    )


def test_terminal_predecessor_startup_keyed_read_failure_is_retryable(
    installed_release: InstalledRelease, tmp_path: Path
) -> None:
    """A real final keyed-read disconnect must not dispatch or lose the saved startup intent."""
    exercise_controller_recovery(installed_release, tmp_path, "persisted", predecessor_case="startup-read")


def test_terminal_predecessor_paused_resume_keyed_read_failure_is_retryable(
    installed_release: InstalledRelease, tmp_path: Path
) -> None:
    """A failed keyed read must retain the paused grant; restored reads resume it once."""
    exercise_controller_recovery(installed_release, tmp_path, "resume", predecessor_case="paused-read")


def test_reopened_predecessor_refuses_installed_startup_until_terminal_again(
    installed_release: InstalledRelease, tmp_path: Path
) -> None:
    """Supported CANCELED-to-BACKLOG reopen blocks recovery without removing its historical edge."""
    exercise_controller_recovery(installed_release, tmp_path, "persisted", predecessor_case="reopen")


def test_retained_terminal_predecessor_completes_installed_execution_route(
    installed_release: InstalledRelease, tmp_path: Path
) -> None:
    """Installed service execution completion accepts retained edge after real controller recovery."""
    exercise_controller_recovery(installed_release, tmp_path, "review", predecessor_case="startup-read", completion_route="execution")


def test_retained_terminal_predecessor_completes_installed_work_route(
    installed_release: InstalledRelease, tmp_path: Path
) -> None:
    """Installed service complete_work accepts same sealed edge and preserves actual completion replay."""
    exercise_controller_recovery(installed_release, tmp_path, "review", predecessor_case="startup-read", completion_route="work")


def test_committed_criteria_post_response_loss_reconciles_same_operation(
    installed_release: InstalledRelease, tmp_path: Path
) -> None:
    """Controller death after real commit must reconcile original journal without duplicate transition."""
    exercise_controller_recovery(installed_release, tmp_path, "review", committed_post_loss=True)

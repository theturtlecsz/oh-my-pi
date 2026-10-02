"""Loopback GitHub pulls API for one bare repository (OMP-518-s02).

The server is one thread on 127.0.0.1 with an ephemeral port. Merge commits
are written with ``git commit-tree`` (parents: base, then head) and
``git update-ref``. Hooks are disabled on those commands.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

__all__ = ["FakeGitHub"]


def _bearer(header: str | None) -> str:
    if not header:
        return ""
    scheme, sep, value = header.partition(" ")
    if sep != " " or scheme.lower() != "bearer":
        return ""
    return value.strip()


def _pull_number(text: str) -> int | None:
    if not text or any(char not in "0123456789" for char in text):
        return None
    return int(text)


def _winner_not_at_either_end(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Place the highest id in the middle when there is room.

    A client that keeps the first or last run per name then fails the
    "newest id wins" check.
    """
    ordered = sorted(runs, key=lambda run: int(run["id"]))
    if len(ordered) < 2:
        return ordered
    earlier = ordered[:-1]
    pivot = len(earlier) // 2
    return earlier[:pivot] + [ordered[-1]] + earlier[pivot:]


class FakeGitHub:
    """The pull, check-run, and merge routes for ``repository`` only."""

    def __init__(
        self, bare_repo: str | Path, repository: str, tokens: set[str] | list[str]
    ) -> None:
        self.bare_repo = Path(bare_repo)
        self.repository = repository
        self._tokens = set(tokens)
        self.calls: list[tuple[str, str, str]] = []
        self.drop_after_merge = False
        self._pulls: dict[int, dict[str, Any]] = {}
        self._next_number = 1
        self._checks: dict[str, list[dict[str, Any]]] = {}
        self._next_check_id = 1
        self._lock = threading.Lock()
        self._closed = False

        handler = _handler_for(self)
        self._server = HTTPServer(("127.0.0.1", 0), handler)
        host, port = self._server.server_address[:2]
        self.url = f"http://{host}:{port}"
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="fake-github", daemon=True
        )
        self._thread.start()

    def set_check(
        self, sha: str, name: str, status: str, conclusion: str | None
    ) -> None:
        """Append one check run for ``sha``. The id is the next integer."""
        with self._lock:
            run_id = self._next_check_id
            self._next_check_id += 1
            self._checks.setdefault(sha, []).append(
                {
                    "id": run_id,
                    "name": name,
                    "status": status,
                    "conclusion": conclusion,
                }
            )

    def retarget(self, number: int, base: str) -> None:
        """Point pull ``number`` at base branch ``base``."""
        with self._lock:
            pull = self._pulls[number]
            pull["base_ref"] = base
            sha = self._rev(base)
            if sha is not None:
                pull["base_sha"] = sha

    def close(self) -> None:
        """Stop the server thread."""
        if self._closed:
            return
        self._closed = True
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def handle_request(self, handler: BaseHTTPRequestHandler) -> None:
        raw, bad_json = _read_body(handler)
        token = _bearer(handler.headers.get("Authorization"))
        with self._lock:
            self.calls.append((handler.command, handler.path, token))
            if bad_json:
                status, payload, drop = 400, {"message": "Bad request"}, False
            else:
                body: Any
                if raw:
                    body = json.loads(raw.decode("utf-8"))
                else:
                    body = None
                status, payload, drop = self._dispatch(
                    handler.command, handler.path, token, body
                )
        if drop:
            handler.close_connection = True
            return
        _send(handler, status, payload)

    def _dispatch(
        self,
        method: str,
        path: str,
        token: str,
        body: Any,
    ) -> tuple[int, Any, bool]:
        matched = self._match(path)
        if matched is None:
            return 404, {"message": "Not Found"}, False
        if token not in self._tokens:
            return 401, {"message": "Bad credentials"}, False
        parts, query = matched
        if method == "GET" and parts == ("pulls",):
            return 200, self._list(query), False
        if method == "POST" and parts == ("pulls",):
            return (*self._create(body), False)
        if method == "GET" and len(parts) == 2 and parts[0] == "pulls":
            return (*self._read(parts[1]), False)
        if (
            method == "PUT"
            and len(parts) == 3
            and parts[0] == "pulls"
            and parts[2] == "merge"
        ):
            return self._merge(parts[1], body)
        if (
            method == "GET"
            and len(parts) == 3
            and parts[0] == "commits"
            and parts[2] == "check-runs"
        ):
            return 200, self._check_runs(parts[1]), False
        return 404, {"message": "Not Found"}, False

    def _match(self, path: str) -> tuple[tuple[str, ...], dict[str, list[str]]] | None:
        parsed = urlsplit(path)
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        if len(parts) < 3 or parts[0] != "repos":
            return None
        if f"{parts[1]}/{parts[2]}" != self.repository:
            return None
        return tuple(parts[3:]), parse_qs(parsed.query)

    def _list(self, query: dict[str, list[str]]) -> list[dict[str, Any]]:
        state = query.get("state", ["open"])[0]
        head = query.get("head", [None])[0]
        pulls = list(self._pulls.values())
        if state != "all":
            pulls = [pull for pull in pulls if pull["state"] == state]
        if head is not None:
            owner, sep, branch = head.partition(":")
            repo_owner = self.repository.split("/", 1)[0]
            if not sep or owner != repo_owner:
                pulls = []
            else:
                pulls = [pull for pull in pulls if pull["head_ref"] == branch]
        pulls.sort(key=lambda pull: int(pull["number"]))
        return [self._list_view(pull) for pull in pulls]

    def _create(self, body: Any) -> tuple[int, Any]:
        if not isinstance(body, dict):
            return 422, {"message": "Validation Failed"}
        head = body.get("head")
        base = body.get("base")
        title = body.get("title")
        text = body.get("body")
        if not all(isinstance(item, str) and item for item in (head, base, title)):
            return 422, {"message": "Validation Failed"}
        if not isinstance(text, str):
            return 422, {"message": "Validation Failed"}
        for pull in self._pulls.values():
            if (
                pull["state"] == "open"
                and pull["head_ref"] == head
                and pull["base_ref"] == base
            ):
                return 422, {
                    "message": "Validation Failed",
                    "errors": [{"message": "A pull request already exists"}],
                }
        head_sha = self._rev(head)
        base_sha = self._rev(base)
        if head_sha is None or base_sha is None:
            return 422, {"message": "Validation Failed"}
        number = self._next_number
        self._next_number += 1
        stored = {
            "number": number,
            "state": "open",
            "merged": False,
            "head_ref": head,
            "head_sha": head_sha,
            "base_ref": base,
            "base_sha": base_sha,
            "merge_commit_sha": None,
            "title": title,
            "body": text,
        }
        self._pulls[number] = stored
        return 201, self._view(stored)

    def _read(self, number_text: str) -> tuple[int, Any]:
        number = _pull_number(number_text)
        pull = None if number is None else self._pulls.get(number)
        if pull is None:
            return 404, {"message": "Not Found"}
        return 200, self._view(pull)

    def _merge(self, number_text: str, body: Any) -> tuple[int, Any, bool]:
        number = _pull_number(number_text)
        pull = None if number is None else self._pulls.get(number)
        if pull is None:
            return 404, {"message": "Not Found"}, False
        if pull["state"] != "open":
            return 405, {"message": "Pull request is closed"}, False
        if not isinstance(body, dict):
            return 422, {"message": "Validation Failed"}, False
        sha = body.get("sha")
        if not isinstance(sha, str) or body.get("merge_method") != "merge":
            return 422, {"message": "Validation Failed"}, False
        head_sha = self._rev(str(pull["head_ref"]))
        base_sha = self._rev(str(pull["base_ref"]))
        if head_sha is None or base_sha is None or sha != head_sha:
            return 409, {"message": "Head SHA has changed"}, False
        merge_sha = self._commit_merge(
            str(pull["base_ref"]), base_sha, head_sha, int(pull["number"])
        )
        if merge_sha is None:
            return 500, {"message": "Merge failed"}, False
        pull["state"] = "closed"
        pull["merged"] = True
        pull["merge_commit_sha"] = merge_sha
        pull["head_sha"] = head_sha
        pull["base_sha"] = merge_sha
        drop = self.drop_after_merge
        if drop:
            self.drop_after_merge = False
        return (
            200,
            {
                "sha": merge_sha,
                "merged": True,
                "message": "Pull Request successfully merged",
            },
            drop,
        )

    def _check_runs(self, sha: str) -> dict[str, Any]:
        runs = _winner_not_at_either_end(list(self._checks.get(sha, [])))
        return {"total_count": len(self._checks.get(sha, [])), "check_runs": runs}

    def _view(self, pull: dict[str, Any]) -> dict[str, Any]:
        head_sha = self._rev(str(pull["head_ref"])) or pull["head_sha"]
        base_sha = self._rev(str(pull["base_ref"])) or pull["base_sha"]
        return {
            "number": pull["number"],
            "state": pull["state"],
            "merged": pull["merged"],
            "head": {"ref": pull["head_ref"], "sha": head_sha},
            "base": {"ref": pull["base_ref"], "sha": base_sha},
            "merge_commit_sha": pull["merge_commit_sha"],
        }

    def _list_view(self, pull: dict[str, Any]) -> dict[str, Any]:
        """List entries use the simple shape, which omits ``merged``."""
        view = self._view(pull)
        del view["merged"]
        return view

    def _commit_merge(
        self, base_ref: str, base_sha: str, head_sha: str, number: int
    ) -> str | None:
        tree = self._git_out(
            "rev-parse", "--verify", "--end-of-options", f"{head_sha}^{{tree}}"
        )
        if not tree:
            return None
        merge_sha = self._git_out(
            "commit-tree",
            tree,
            "-p",
            base_sha,
            "-p",
            head_sha,
            "-m",
            f"Merge pull request #{number}",
        )
        if not merge_sha:
            return None
        updated = self._git_out(
            "update-ref", f"refs/heads/{base_ref}", merge_sha, base_sha
        )
        if updated is None:
            return None
        return merge_sha

    def _rev(self, ref: str) -> str | None:
        return self._git_out(
            "rev-parse", "--verify", "--end-of-options", f"refs/heads/{ref}"
        )

    def _git_out(self, *args: str) -> str | None:
        proc = self._git(*args)
        if proc.returncode != 0:
            return None
        return proc.stdout.strip()

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        command = [
            "git",
            "-C",
            str(self.bare_repo),
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "commit.gpgSign=false",
            *args,
        ]
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.bare_repo),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_AUTHOR_NAME": "FakeGitHub",
            "GIT_AUTHOR_EMAIL": "fake@github.local",
            "GIT_COMMITTER_NAME": "FakeGitHub",
            "GIT_COMMITTER_EMAIL": "fake@github.local",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
        }
        try:
            return subprocess.run(
                command,
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return subprocess.CompletedProcess(command, 1, "", "git failed")


def _handler_for(fake: FakeGitHub) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            fake.handle_request(self)

        def do_POST(self) -> None:
            fake.handle_request(self)

        def do_PUT(self) -> None:
            fake.handle_request(self)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def _read_body(handler: BaseHTTPRequestHandler) -> tuple[bytes, bool]:
    raw_length = handler.headers.get("Content-Length")
    if not raw_length:
        return b"", False
    try:
        length = int(raw_length)
    except ValueError:
        return b"", True
    if length < 0:
        return b"", True
    raw = handler.rfile.read(length) if length else b""
    if not raw:
        return b"", False
    try:
        json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw, True
    return raw, False


def _send(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)

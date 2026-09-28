#!/usr/bin/env python3
"""Executable stand-in for the docker CLI used by omp_harbor_eval.docker_ops.

``$FAKE_DOCKER_DIR/config.json`` is read on every call:

- ``containers``: service name to container ids. ``ps -q`` prints the ids for
  the ``com.docker.compose.service`` label.
- ``fail``: substrings. When one appears in the space-joined argv, exit 1.
- ``omp``: argv the ``omp`` shim executes.
- ``responses``: ``{container: {url_path: {status, body}}}``. ``exec python -c
  SCRIPT URL`` prints that object when the URL path matches, and runs the
  script otherwise.

Each call appends ``{"argv", "stdin_sha256"}`` to ``calls.jsonl``. ``/workspace``,
``/tmp/``, and ``/home/agent`` in exec argv are rewritten under
``$FAKE_DOCKER_DIR/root``. Other execs run with ``python`` replaced by this
interpreter. An ``omp`` shim on ``PATH`` appends its argv to ``omp-argv.jsonl``
and runs the configured ``omp`` argv plus only ``--session F``. ``run -d``
prints an id and starts nothing. ``cp`` copies. ``rm`` and ``compose`` exit 0.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess  # nosec B404 - execs the mapped argv; no shell of our own
import sys
import urllib.parse
from collections.abc import Mapping
from pathlib import Path

_PATH_RE = re.compile(r"/home/agent(?![A-Za-z0-9._-])|/workspace(?![A-Za-z0-9._-])|/tmp/")
_SERVICE_LABEL = "label=com.docker.compose.service="


def main(argv: list[str]) -> int:
    if len(argv) > 1 and argv[1] == "--omp-shim":
        return omp_shim(argv[2:])
    fake_dir = _fake_dir()
    if fake_dir is None:
        return 2
    if len(argv) < 2:
        sys.stderr.write("usage: docker COMMAND [ARG...]\n")
        return 1
    config = _load_config(fake_dir)
    command = argv[1]
    if command == "exec":
        return _exec(argv, fake_dir, config)
    stdin = _read_stdin()
    _log_call(fake_dir, argv, hashlib.sha256(stdin).hexdigest())
    if _failed(argv, config):
        return 1
    if command == "ps":
        return _ps(argv, config)
    if command == "cp":
        return _cp(argv, fake_dir)
    if command == "run" and "-d" in argv[2:]:
        print(hashlib.sha256("\0".join(argv).encode()).hexdigest()[:12], flush=True)
        return 0
    if command in {"rm", "compose"}:
        return 0
    sys.stderr.write(f"fake docker: unsupported command {command}\n")
    return 1


def omp_shim(args: list[str]) -> int:
    """Log the omp argv, then exec the configured argv plus only ``--session F``."""

    fake_dir = _fake_dir()
    if fake_dir is None:
        return 2
    path = fake_dir / "omp-argv.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"argv": ["omp", *args]}) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    config = _load_config(fake_dir)
    omp = config.get("omp")
    if not isinstance(omp, list) or not omp or not all(isinstance(item, str) and item for item in omp):
        sys.stderr.write("fake docker: config omp must be an argv list\n")
        return 1
    command = [str(item) for item in omp]
    command.extend(_session_args(args))
    try:
        os.execvp(command[0], command)
    except OSError as exc:
        sys.stderr.write(f"fake docker: {exc}\n")
        return 1
    return 1


def _exec(argv: list[str], fake_dir: Path, config: Mapping[str, object]) -> int:
    if _failed(argv, config):
        stdin = _read_stdin()
        _log_call(fake_dir, argv, hashlib.sha256(stdin).hexdigest())
        return 1
    parsed = _parse_exec(argv)
    if parsed is None:
        _log_call(fake_dir, argv, hashlib.sha256(b"").hexdigest())
        sys.stderr.write("fake docker: exec needs a container and a command\n")
        return 1
    container, cmd = parsed
    canned = _canned_response(container, cmd, config)
    if canned is not None:
        stdin = _read_stdin()
        _log_call(fake_dir, argv, hashlib.sha256(stdin).hexdigest())
        sys.stdout.write(json.dumps(canned) + "\n")
        sys.stdout.flush()
        return 0
    root = _prepare_root(fake_dir)
    _ensure_shim(fake_dir)
    mapped = [_map_token(token, root) for token in cmd]
    if cmd and cmd[0] == "python":
        mapped[0] = sys.executable
    env = os.environ.copy()
    env["PATH"] = str(fake_dir / "bin") + os.pathsep + env.get("PATH", "")
    return _stream(mapped, env, fake_dir, argv)


def _stream(cmd: list[str], env: dict[str, str], fake_dir: Path, argv: list[str]) -> int:
    hasher = hashlib.sha256()
    if sys.stdin.isatty():
        _log_call(fake_dir, argv, hasher.hexdigest())
        try:
            proc = subprocess.Popen(cmd, env=env)  # nosec B603
        except OSError as exc:
            sys.stderr.write(f"fake docker: {exc}\n")
            return 1
        return _status(proc.wait())
    try:
        proc = subprocess.Popen(cmd, env=env, stdin=subprocess.PIPE)  # nosec B603
    except OSError as exc:
        _log_call(fake_dir, argv, hashlib.sha256(_read_stdin()).hexdigest())
        sys.stderr.write(f"fake docker: {exc}\n")
        return 1
    sink = proc.stdin
    assert sink is not None
    try:
        while True:
            chunk = sys.stdin.buffer.read1(65536)
            if not chunk:
                break
            hasher.update(chunk)
            if sink is None:
                continue
            try:
                sink.write(chunk)
                sink.flush()
            except BrokenPipeError:
                sink.close()
                sink = None
    finally:
        if sink is not None:
            try:
                sink.close()
            except BrokenPipeError:
                pass
    _log_call(fake_dir, argv, hasher.hexdigest())
    return _status(proc.wait())


def _ps(argv: list[str], config: Mapping[str, object]) -> int:
    service: str | None = None
    index = 2
    while index < len(argv):
        token = argv[index]
        filt: str | None = None
        if token == "--filter" and index + 1 < len(argv):
            filt = argv[index + 1]
            index += 2
        elif token.startswith("--filter="):
            filt = token.split("=", 1)[1]
            index += 1
        else:
            index += 1
            continue
        if filt.startswith(_SERVICE_LABEL):
            service = filt[len(_SERVICE_LABEL) :]
    containers = config.get("containers")
    ids: object = []
    if isinstance(containers, dict):
        ids = containers.get(service, [])
    if isinstance(ids, list):
        for cid in ids:
            if isinstance(cid, str) and cid:
                print(cid, flush=True)
    return 0


def _cp(argv: list[str], fake_dir: Path) -> int:
    if len(argv) < 4:
        sys.stderr.write("fake docker: cp needs SRC and DEST\n")
        return 1
    root = str(fake_dir / "root")
    source = Path(_resolve_cp(argv[-2], root))
    target = Path(_resolve_cp(argv[-1], root))
    if not source.is_file():
        sys.stderr.write(f"fake docker: cp source missing: {source}\n")
        return 1
    if not target.parent.is_dir():
        sys.stderr.write(f"fake docker: cp dest parent missing: {target.parent}\n")
        return 1
    shutil.copyfile(source, target)
    return 0


def _canned_response(container: str, cmd: list[str], config: Mapping[str, object]) -> dict[str, object] | None:
    if len(cmd) < 4 or cmd[0] != "python" or cmd[1] != "-c":
        return None
    path = urllib.parse.urlparse(cmd[3]).path
    responses = config.get("responses")
    if not isinstance(responses, dict):
        return None
    per_container = responses.get(container)
    if not isinstance(per_container, dict) or path not in per_container:
        return None
    entry = per_container[path]
    if not isinstance(entry, dict) or "status" not in entry or "body" not in entry:
        sys.stderr.write(f"fake docker: response for {path} needs status and body\n")
        sys.exit(1)
    return {"status": entry["status"], "body": entry["body"]}


def _parse_exec(argv: list[str]) -> tuple[str, list[str]] | None:
    index = 2
    if index < len(argv) and argv[index] == "-i":
        index += 1
    if index >= len(argv) - 1:
        return None
    return argv[index], argv[index + 1 :]


def _session_args(args: list[str]) -> list[str]:
    for index, arg in enumerate(args):
        if arg == "--session" and index + 1 < len(args):
            return ["--session", args[index + 1]]
    return []


def _map_token(token: str, root: str) -> str:
    def replace(match: re.Match[str]) -> str:
        text = match.group(0)
        if text == "/tmp/":
            return f"{root}/tmp/"
        if text == "/workspace":
            return f"{root}/workspace"
        return f"{root}/home/agent"

    return _PATH_RE.sub(replace, token)


def _resolve_cp(spec: str, root: str) -> str:
    if ":" in spec:
        container, path = spec.split(":", 1)
        if container and "/" not in container and path.startswith("/"):
            return _map_token(path, root)
    return spec


def _prepare_root(fake_dir: Path) -> str:
    root = fake_dir / "root"
    for relative in ("workspace", "tmp", "home/agent"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    return str(root)


def _ensure_shim(fake_dir: Path) -> None:
    bin_dir = fake_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "omp"
    fake = str(Path(__file__).resolve())
    script = (
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        f"os.execv({sys.executable!r}, [{sys.executable!r}, {fake!r}, '--omp-shim', *sys.argv[1:]])\n"
    )
    if not shim.is_file() or shim.read_text(encoding="utf-8") != script:
        shim.write_text(script, encoding="utf-8")
        shim.chmod(0o755)


def _failed(argv: list[str], config: Mapping[str, object]) -> bool:
    fail = config.get("fail")
    if not isinstance(fail, list):
        return False
    haystack = " ".join(argv)
    return any(isinstance(token, str) and token and token in haystack for token in fail)


def _log_call(fake_dir: Path, argv: list[str], stdin_sha256: str) -> None:
    path = fake_dir / "calls.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"argv": argv, "stdin_sha256": stdin_sha256}) + "\n")
        handle.flush()


def _load_config(fake_dir: Path) -> dict[str, object]:
    path = fake_dir / "config.json"
    if not path.is_file():
        return {}
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        sys.stderr.write("fake docker: config.json must be an object\n")
        sys.exit(1)
    return loaded


def _fake_dir() -> Path | None:
    raw = os.environ.get("FAKE_DOCKER_DIR")
    if not raw:
        sys.stderr.write("FAKE_DOCKER_DIR is not set\n")
        return None
    return Path(raw)


def _read_stdin() -> bytes:
    if sys.stdin.isatty():
        return b""
    return sys.stdin.buffer.read()


def _status(returncode: int) -> int:
    if returncode < 0:
        return 128 + (-returncode)
    return returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv))

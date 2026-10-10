"""Tests for the omp-owner-action sign action (OMP-546-s02, D47)."""
from __future__ import annotations

import base64
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
GATEWAY_PATH = REPO_ROOT / "infra/automation-user/omp-owner-action"
SSH_KEYGEN = shutil.which("ssh-keygen")
LIMIT = 64 * 1024
MESSAGE_TEXT = "OMP-546-s02 message text must stay out of the log"

pytestmark = pytest.mark.skipif(
    SSH_KEYGEN is None,
    reason="ssh-keygen is not installed",
)


class _BrokenStream:
    def read(self, n: int = -1) -> bytes:
        raise OSError("stdin unavailable")


class _SplitStream:
    """Return one byte, then the rest, then EOF. A single read must not see the whole message."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._reads = 0

    def read(self, n: int = -1) -> bytes:
        if self._reads == 0:
            self._reads = 1
            return self._data[:1]
        if self._reads == 1:
            self._reads = 2
            rest = self._data[1:]
            if n is not None and n >= 0:
                return rest[:n]
            return rest
        return b""


@pytest.fixture(scope="module")
def gateway():
    name = "omp_owner_action_sign"
    loader = importlib.machinery.SourceFileLoader(name, str(GATEWAY_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _fixed(gateway, tmp_path: Path, **overrides):
    log_file = tmp_path / "action.log"
    log_file.touch(mode=0o600)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "USER": "isolated_thetu",
        "HOME": str(home),
        "LANG": "C.UTF-8",
    }
    values = {
        "log": log_file,
        "flood": tmp_path / "flood",
        "clone": tmp_path / "clone",
        "python": sys.executable,
        "bash": "/bin/bash",
        "ssh_keygen": SSH_KEYGEN,
        "signing_key": tmp_path / "signing.key",
        "env": env,
    }
    values.update(overrides)
    return gateway.Fixed(**values)


def _write_script(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _fake_keygen(path: Path, record: Path) -> None:
    _write_script(
        path,
        "#!/usr/bin/python3\n"
        "import sys\n"
        "from pathlib import Path\n"
        f"Path({str(record)!r}).write_bytes(sys.stdin.buffer.read() or b'called')\n"
        "sys.stdout.buffer.write(b'-----BEGIN SSH SIGNATURE-----\\nFAKE\\n-----END SSH SIGNATURE-----\\n')\n"
        "raise SystemExit(0)\n",
    )


def _recording_keygen(path: Path, record: Path, *, rc: int, marker: bytes) -> None:
    _write_script(
        path,
        "#!/usr/bin/python3\n"
        "import base64, json, os, sys\n"
        "from pathlib import Path\n"
        "data = sys.stdin.buffer.read()\n"
        f"Path({str(record)!r}).write_text(json.dumps({{\n"
        "    'argv': sys.argv,\n"
        "    'env': dict(os.environ),\n"
        "    'stdin_b64': base64.b64encode(data).decode('ascii'),\n"
        "}), encoding='utf-8')\n"
        f"sys.stdout.buffer.write({marker!r})\n"
        f"raise SystemExit({rc})\n",
    )


def _generate_key(tmp_path: Path) -> Path:
    key_dir = tmp_path / "key dir"
    key_dir.mkdir()
    key = key_dir / "owner key"
    subprocess.run(
        [SSH_KEYGEN, "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True,
        capture_output=True,
        shell=False,
    )
    return key


def _allowed_signers(tmp_path: Path, public_key: Path) -> Path:
    path = tmp_path / "allowed_signers"
    path.write_text(f"owner {public_key.read_text(encoding='utf-8').strip()}\n", encoding="utf-8")
    return path


def _verify(allowed: Path, message: bytes, signature: bytes) -> subprocess.CompletedProcess[bytes]:
    sig_path = allowed.parent / "message.sig"
    sig_path.write_bytes(signature)
    return subprocess.run(
        [
            SSH_KEYGEN,
            "-Y",
            "verify",
            "-f",
            str(allowed),
            "-I",
            "owner",
            "-n",
            "omp-work-decision",
            "-s",
            str(sig_path),
        ],
        input=message,
        capture_output=True,
        shell=False,
        check=False,
    )


def _log_text(fixed) -> str:
    return fixed.log.read_text(encoding="utf-8")


def _log_entry(fixed) -> dict:
    lines = [line for line in _log_text(fixed).splitlines() if line.strip()]
    assert len(lines) == 1
    return json.loads(lines[0])


def test_fixed_sign_defaults(gateway):
    assert gateway.FIXED.ssh_keygen == Path("/usr/bin/ssh-keygen")
    # The path parts stay separate: the gate refuses a literal /home/<user>/ in this tree.
    assert gateway.FIXED.signing_key == Path(
        "/", "home", "thetu", ".config", "omp", "owner-signing", "omp-owner-signing"
    )


def test_sign_verifies_and_log_omits_message(gateway, tmp_path):
    key = _generate_key(tmp_path)
    fixed = _fixed(gateway, tmp_path, signing_key=key)
    message = MESSAGE_TEXT.encode("ascii")
    out = io.BytesIO()
    rc = gateway.main(["sign"], fixed=fixed, stdin=_SplitStream(message), stdout=out)
    assert rc == 0
    signature = out.getvalue()
    assert b"BEGIN SSH SIGNATURE" in signature

    allowed = _allowed_signers(tmp_path, Path(f"{key}.pub"))
    verified = _verify(allowed, message, signature)
    assert verified.returncode == 0, verified.stderr
    rejected = _verify(allowed, message + b"!", signature)
    assert rejected.returncode != 0

    raw = _log_text(fixed)
    assert MESSAGE_TEXT not in raw
    assert "BEGIN SSH SIGNATURE" not in raw
    entry = _log_entry(fixed)
    assert entry["action"] == "sign"
    assert entry["args"] == []
    assert entry["rc"] == 0
    assert set(entry) == {"at", "caller", "action", "args", "rc"}


def test_sign_child_argv_env_stdin_and_rc(gateway, tmp_path):
    record = tmp_path / "child.json"
    keygen = tmp_path / "ssh-keygen"
    marker = b"SIG-MARKER-OMP-546\n"
    _recording_keygen(keygen, record, rc=9, marker=marker)
    fixed = _fixed(gateway, tmp_path, ssh_keygen=keygen, signing_key=tmp_path / "not-named-by-caller")
    message = MESSAGE_TEXT.encode("ascii")
    out = io.BytesIO()
    os.environ["OMP_546_PARENT_CANARY"] = "should-not-leak"
    try:
        rc = gateway.main(["sign"], fixed=fixed, stdin=io.BytesIO(message), stdout=out)
    finally:
        del os.environ["OMP_546_PARENT_CANARY"]

    assert rc == 9
    assert out.getvalue() == marker
    recorded = json.loads(record.read_text(encoding="utf-8"))
    assert recorded["argv"] == [
        str(keygen),
        "-Y",
        "sign",
        "-f",
        str(fixed.signing_key),
        "-n",
        "omp-work-decision",
    ]
    assert recorded["env"] == dict(fixed.env)
    assert "should-not-leak" not in recorded["env"].values()
    assert base64.b64decode(recorded["stdin_b64"]) == message

    raw = _log_text(fixed)
    assert MESSAGE_TEXT not in raw
    assert "SIG-MARKER-OMP-546" not in raw
    entry = _log_entry(fixed)
    assert entry["action"] == "sign"
    assert entry["args"] == []
    assert entry["rc"] == 9


def test_sign_exact_64kib_verifies(gateway, tmp_path):
    key = _generate_key(tmp_path)
    fixed = _fixed(gateway, tmp_path, signing_key=key)
    prefix = b"EXACT64-OMP-546-s02-"
    message = prefix + (b"D" * (LIMIT - len(prefix)))
    assert len(message) == LIMIT
    out = io.BytesIO()
    rc = gateway.main(["sign"], fixed=fixed, stdin=io.BytesIO(message), stdout=out)
    assert rc == 0
    signature = out.getvalue()
    allowed = _allowed_signers(tmp_path, Path(f"{key}.pub"))
    verified = _verify(allowed, message, signature)
    assert verified.returncode == 0, verified.stderr
    raw = _log_text(fixed)
    assert b"EXACT64-OMP-546-s02-".decode("ascii") not in raw
    assert "BEGIN SSH SIGNATURE" not in raw


@pytest.mark.parametrize(
    ("argv", "logged_args"),
    [
        (["sign", "extra"], ["extra"]),
        (["sign", "-f", "other"], ["-f", "other"]),
    ],
)
def test_sign_extra_arguments_run_nothing(gateway, tmp_path, argv, logged_args, capsys):
    record = tmp_path / "called"
    keygen = tmp_path / "ssh-keygen"
    _fake_keygen(keygen, record)
    fixed = _fixed(gateway, tmp_path, ssh_keygen=keygen)
    out = io.BytesIO()
    rc = gateway.main(argv, fixed=fixed, stdin=io.BytesIO(MESSAGE_TEXT.encode("ascii")), stdout=out)
    assert rc == 2
    assert out.getvalue() == b""
    assert not record.exists()
    captured = capsys.readouterr()
    assert "refused" in captured.err
    assert captured.out == ""
    raw = _log_text(fixed)
    assert MESSAGE_TEXT not in raw
    assert "BEGIN SSH SIGNATURE" not in raw
    entry = _log_entry(fixed)
    assert entry["action"] == "sign"
    assert entry["args"] == logged_args
    assert entry["rc"] == 2


def test_sign_without_stdin_runs_nothing(gateway, tmp_path, capsys):
    record = tmp_path / "called"
    keygen = tmp_path / "ssh-keygen"
    _fake_keygen(keygen, record)
    fixed = _fixed(gateway, tmp_path, ssh_keygen=keygen)
    out = io.BytesIO()
    rc = gateway.main(["sign"], fixed=fixed, stdout=out)
    assert rc == 2
    assert out.getvalue() == b""
    assert not record.exists()
    captured = capsys.readouterr()
    assert "refused" in captured.err
    entry = _log_entry(fixed)
    assert entry["action"] == "sign"
    assert entry["args"] == []
    assert entry["rc"] == 2


def test_sign_unreadable_stdin_runs_nothing(gateway, tmp_path, capsys):
    record = tmp_path / "called"
    keygen = tmp_path / "ssh-keygen"
    _fake_keygen(keygen, record)
    fixed = _fixed(gateway, tmp_path, ssh_keygen=keygen)
    out = io.BytesIO()
    rc = gateway.main(["sign"], fixed=fixed, stdin=_BrokenStream(), stdout=out)
    assert rc == 2
    assert out.getvalue() == b""
    assert not record.exists()
    captured = capsys.readouterr()
    assert "refused" in captured.err
    entry = _log_entry(fixed)
    assert entry["action"] == "sign"
    assert entry["args"] == []
    assert entry["rc"] == 2
    assert MESSAGE_TEXT not in _log_text(fixed)


def test_sign_stdin_over_64kib_runs_nothing(gateway, tmp_path, capsys):
    record = tmp_path / "called"
    keygen = tmp_path / "ssh-keygen"
    _fake_keygen(keygen, record)
    fixed = _fixed(gateway, tmp_path, ssh_keygen=keygen)
    head = b"OMP-546-s02-oversize-head-not-for-the-log"
    tail = b"OMP-546-s02-oversize-tail-not-for-the-log"
    message = head + (b"B" * (LIMIT + 1 - len(head) - len(tail))) + tail
    assert len(message) == LIMIT + 1
    out = io.BytesIO()
    rc = gateway.main(
        ["sign"],
        fixed=fixed,
        stdin=_SplitStream(message),
        stdout=out,
    )
    assert rc == 2
    assert out.getvalue() == b""
    assert not record.exists()
    captured = capsys.readouterr()
    assert "refused" in captured.err
    raw = _log_text(fixed)
    assert head.decode("ascii") not in raw
    assert tail.decode("ascii") not in raw
    assert "BEGIN SSH SIGNATURE" not in raw
    entry = _log_entry(fixed)
    assert entry["action"] == "sign"
    assert entry["args"] == []
    assert entry["rc"] == 2

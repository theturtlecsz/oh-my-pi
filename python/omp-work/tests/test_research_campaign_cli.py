"""CLI contracts for `research run` (OMP-590).

Invalid input and a partial database-flag set exit 2 before a client, a job
store, a socket, or a database connection is constructed. A finished run
prints the campaign dict and exits 0 for both verdicts.
"""

from __future__ import annotations

import hashlib
import json
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest

import omp_work.__main__ as cli
import omp_work.jobs.research_campaign as research_campaign
import omp_work.jobs.store as job_store
from omp_work.__main__ import main
from omp_work.operations import capabilities
from omp_work.operations.config import OperationsConfig
from omp_work.v1.client import WorkClient
from omp_work.v1.models import ResearchDomain
from omp_work.v1.service import WorkError

_HARNESS = b'echo "METRIC score=1"\n'


class _Calls:
    def __init__(self) -> None:
        self.sockets: list[object] = []
        self.connects: list[object] = []
        self.clients: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.stores: list[object] = []
        self.opened: list[WorkClient] = []


def _install(patch: pytest.MonkeyPatch, calls: _Calls) -> None:
    real_socket = socket.socket
    real_create = socket.create_connection
    real_connect = psycopg.connect
    real_client = cli.WorkClient
    real_store = job_store.NativeJobStore

    def spy_socket(*args: object, **kwargs: object) -> object:
        calls.sockets.append((args, kwargs))
        return real_socket(*args, **kwargs)  # type: ignore[arg-type]

    def spy_create(*args: object, **kwargs: object) -> object:
        calls.sockets.append((args, kwargs))
        return real_create(*args, **kwargs)  # type: ignore[arg-type]

    def spy_connect(*args: object, **kwargs: object) -> object:
        calls.connects.append((args, kwargs))
        return real_connect(*args, **kwargs)  # type: ignore[arg-type]

    def spy_client(*args: object, **kwargs: object) -> WorkClient:
        calls.clients.append((args, kwargs))
        client = real_client(*args, **kwargs)  # type: ignore[arg-type]
        calls.opened.append(client)
        return client

    def spy_store(config: OperationsConfig) -> object:
        calls.stores.append(config)
        return real_store(config)

    patch.setattr(socket, "socket", spy_socket)
    patch.setattr(socket, "create_connection", spy_create)
    patch.setattr(psycopg, "connect", spy_connect)
    patch.setattr(cli, "WorkClient", spy_client)
    patch.setattr(job_store, "NativeJobStore", spy_store)


@contextmanager
def _spies(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Calls]:
    calls = _Calls()
    with monkeypatch.context() as patch:
        _install(patch, calls)
        yield calls


def _assert_unbuilt(calls: _Calls, stdout: str) -> None:
    assert calls.sockets == []
    assert calls.connects == []
    assert calls.clients == []
    assert calls.stores == []
    assert calls.opened == []
    assert stdout == ""


def _bearer(path: Path, token: str, actor_id: UUID, *, kind: str = "automation") -> None:
    path.write_text(
        json.dumps(
            {
                "token": token,
                "actor_id": str(actor_id),
                "actor_kind": kind,
                "workspaces": [str(uuid4())],
                "scopes": ["work.read", "work.execute", "work.approve"],
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _client_json(
    path: Path, base_url: str, workspace_id: UUID, bearer: Path
) -> None:
    path.write_text(
        json.dumps(
            {
                "base_url": base_url,
                "workspace_id": str(workspace_id),
                "bearer_file": str(bearer),
            }
        ),
        encoding="utf-8",
    )


def _write_definition(directory: Path, payload: object, *, harness: bytes = _HARNESS) -> Path:
    script = directory / "trial.sh"
    script.write_bytes(harness)
    path = directory / "definition.json"
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _valid_payload() -> dict[str, object]:
    return {
        "objective": "Every trial reaches score one",
        "domain": "machine_learning",
        "timeout_seconds": 15,
        "criteria": [{"metric": "score", "op": "gte", "threshold": 1}],
        "trials": [{"harness": "trial.sh"}],
    }


def _layout(tmp_path: Path) -> tuple[Path, Path, UUID, UUID, bytes]:
    workspace_id, actor_id = uuid4(), uuid4()
    bearer = tmp_path / "bearer.json"
    token = f"token-{workspace_id}"
    _bearer(bearer, token, actor_id)
    config = tmp_path / "client.json"
    _client_json(config, "http://127.0.0.1:9", workspace_id, bearer)
    definition = _write_definition(tmp_path, _valid_payload())
    return config, definition, workspace_id, actor_id, bearer.read_bytes()


def _forbid_credential_write(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("credential write")


@pytest.mark.parametrize(
    "argv",
    [
        ["research"],
        ["research", "run"],
        ["research", "run", "--key", "OMP-1"],
        ["research", "run", "--definition", "definition.json"],
    ],
)
def test_missing_args_exit_2_before_construction(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _spies(monkeypatch) as calls:
        with pytest.raises(SystemExit) as exc:
            main(argv)
    captured = capsys.readouterr()
    assert exc.value.code == 2
    assert captured.err != ""
    _assert_unbuilt(calls, captured.out)


@pytest.mark.parametrize(
    ("payload", "stderr_text"),
    [
        ({"trials": []}, "trials must be a nonempty list"),
        (
            {"criteria": [{"metric": "score", "op": "approximately", "threshold": 1}]},
            "op must be one of gte, gt, lte, lt, eq",
        ),
        ("{", "is not valid JSON"),
        (
            {"trials": [{"harness": "missing.sh"}]},
            "cannot read harness missing.sh",
        ),
    ],
    ids=["empty-trials", "invalid-op", "malformed-json", "unreadable-harness"],
)
def test_invalid_definition_exits_2_before_construction(
    payload: object,
    stderr_text: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, _definition, _workspace, _actor, _bearer_bytes = _layout(tmp_path)
    base = _valid_payload()
    if isinstance(payload, dict):
        base.update(payload)
        written: object = base
    else:
        written = payload
    definition = _write_definition(tmp_path, written)
    with _spies(monkeypatch) as calls:
        code = main(
            [
                "research",
                "run",
                "--key",
                "OMP-1",
                "--definition",
                str(definition),
                "--client-config",
                str(config),
            ]
        )
    captured = capsys.readouterr()
    assert code == 2
    assert stderr_text in captured.err
    assert "Traceback" not in captured.err
    _assert_unbuilt(calls, captured.out)


@pytest.mark.parametrize("omitted", ["config_dir", "state_dir", "data_dir", "db_port"])
def test_partial_db_flags_exit_2_before_construction(
    omitted: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, definition, _workspace, _actor, _bearer_bytes = _layout(tmp_path)
    flags = {
        "config_dir": ("--config-dir", str(tmp_path / "config")),
        "state_dir": ("--state-dir", str(tmp_path / "state")),
        "data_dir": ("--data-dir", str(tmp_path / "data")),
        "db_port": ("--db-port", "54399"),
    }
    argv = [
        "research",
        "run",
        "--key",
        "OMP-1",
        "--definition",
        str(definition),
        "--client-config",
        str(config),
    ]
    for name, pair in flags.items():
        if name == omitted:
            continue
        argv.extend(pair)
    with _spies(monkeypatch) as calls:
        code = main(argv)
    captured = capsys.readouterr()
    assert code == 2
    assert "must all be set or all be omitted" in captured.err
    assert "Traceback" not in captured.err
    _assert_unbuilt(calls, captured.out)


def test_invalid_default_db_port_exits_2_before_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, definition, _workspace, _actor, _bearer_bytes = _layout(tmp_path)
    monkeypatch.setenv("OMP_WORK_POSTGRES_PORT", "invalid-port")
    with _spies(monkeypatch) as calls:
        code = main(
            [
                "research",
                "run",
                "--key",
                "OMP-1",
                "--definition",
                str(definition),
                "--client-config",
                str(config),
            ]
        )
    captured = capsys.readouterr()
    assert code == 2
    assert "research:" in captured.err
    assert "invalid-port" in captured.err
    assert "Traceback" not in captured.err
    _assert_unbuilt(calls, captured.out)


def _campaign_result(outcome: str) -> dict[str, object]:
    return {
        "campaign_id": str(uuid4()),
        "state": "concluded",
        "outcome": outcome,
        "reason": "passed 1 trial" if outcome == "supported" else "trial 1 score 0 < 1",
        "trials": [
            {
                "trial_id": str(uuid4()),
                "execution_status": "completed" if outcome == "supported" else "crashed",
                "metrics": {"score": 1.0 if outcome == "supported" else 0.0},
            }
        ],
    }


@pytest.mark.parametrize("outcome", ["supported", "refuted"])
def test_run_prints_verdict_and_exits_zero(
    outcome: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, definition, workspace_id, actor_id, bearer_bytes = _layout(tmp_path)
    result = _campaign_result(outcome)
    seen: dict[str, object] = {}

    def spy_run(
        client: WorkClient,
        store: object,
        key: str,
        definition_obj: object,
        *,
        harness_dir: Path,
        actor_id: UUID,
    ) -> dict[str, object]:
        assert harness_dir.is_dir()
        (harness_dir / "marker").write_text("kept-for-the-call", encoding="utf-8")
        seen["client"] = client
        seen["store"] = store
        seen["key"] = key
        seen["definition"] = definition_obj
        seen["harness_dir"] = harness_dir
        seen["actor_id"] = actor_id
        definition_obj.trials[0].harness.write_bytes(b"mutated-after-load")  # type: ignore[attr-defined]
        return result

    monkeypatch.setattr(capabilities, "write_capability", _forbid_credential_write)
    monkeypatch.setattr(capabilities, "write_client_config", _forbid_credential_write)
    with _spies(monkeypatch) as calls:
        monkeypatch.setattr(research_campaign, "run_research_campaign", spy_run)
        code = main(
            [
                "research",
                "run",
                "--key",
                "OMP-1",
                "--definition",
                str(definition),
                "--client-config",
                str(config),
            ]
        )
    captured = capsys.readouterr()
    assert code == 0
    assert captured.err == ""
    assert captured.out == json.dumps(result) + "\n"
    assert calls.connects == []
    assert calls.clients[0][0] == ("http://127.0.0.1:9", workspace_id, tmp_path / "bearer.json")
    client = calls.opened[0]
    assert client._token == f"token-{workspace_id}"
    assert client._client.is_closed
    assert seen["key"] == "OMP-1"
    assert seen["actor_id"] == actor_id
    assert isinstance(seen["actor_id"], UUID)
    loaded = seen["definition"]
    assert loaded.objective == "Every trial reaches score one"  # type: ignore[attr-defined]
    assert loaded.domain is ResearchDomain.MACHINE_LEARNING  # type: ignore[attr-defined]
    assert loaded.timeout_seconds == 15  # type: ignore[attr-defined]
    assert loaded.criteria[0].metric == "score"  # type: ignore[attr-defined]
    assert loaded.criteria[0].op == "gte"  # type: ignore[attr-defined]
    assert loaded.criteria[0].threshold == 1  # type: ignore[attr-defined]
    assert loaded.trials[0].harness_bytes == _HARNESS  # type: ignore[attr-defined]
    assert loaded.trials[0].candidate_digest == hashlib.sha256(_HARNESS).hexdigest()  # type: ignore[attr-defined]
    harness_dir = seen["harness_dir"]
    assert isinstance(harness_dir, Path)
    assert not harness_dir.exists()
    assert (tmp_path / "bearer.json").read_bytes() == bearer_bytes
    assert calls.stores == [OperationsConfig.defaults()]
    assert seen["store"]._config == OperationsConfig.defaults()  # type: ignore[attr-defined]


def test_supplied_dirs_and_harness_use_that_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, definition, _workspace, actor_id, bearer_bytes = _layout(tmp_path)
    harness_dir = tmp_path / "harness"
    harness_dir.mkdir()
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    data_dir = tmp_path / "data"
    credentials = config_dir / "credentials"
    credentials.mkdir(parents=True)
    for name, secret in (
        ("omp_work_app", "app-secret"),
        ("omp_work_migrator", "migrator-secret"),
    ):
        path = credentials / name
        path.write_text(secret, encoding="utf-8")
        path.chmod(0o600)
    result = _campaign_result("supported")
    seen: dict[str, object] = {}

    def spy_run(*_args: object, **kwargs: object) -> dict[str, object]:
        seen["harness_dir"] = kwargs["harness_dir"]
        seen["actor_id"] = kwargs["actor_id"]
        return result

    with _spies(monkeypatch) as calls:
        monkeypatch.setattr(research_campaign, "run_research_campaign", spy_run)
        code = main(
            [
                "research",
                "run",
                "--key",
                "OMP-1",
                "--definition",
                str(definition),
                "--client-config",
                str(config),
                "--harness-dir",
                str(harness_dir),
                "--config-dir",
                str(config_dir),
                "--state-dir",
                str(state_dir),
                "--data-dir",
                str(data_dir),
                "--db-port",
                "54399",
            ]
        )
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == json.dumps(result) + "\n"
    assert calls.connects == []
    expected = OperationsConfig(config_dir, state_dir, data_dir, port=54399)
    assert calls.stores == [expected]
    app = expected.connection_kwargs("omp_work_app")
    migrator = expected.connection_kwargs("omp_work_migrator")
    assert app["user"] == "omp_work_app"
    assert app["password"] == "app-secret"
    assert app["port"] == 54399
    assert migrator["user"] == "omp_work_migrator"
    assert migrator["password"] == "migrator-secret"
    assert seen["harness_dir"] == harness_dir
    assert seen["actor_id"] == actor_id
    assert harness_dir.is_dir()
    assert (tmp_path / "bearer.json").read_bytes() == bearer_bytes
    assert calls.opened[0]._client.is_closed


def test_default_client_config_reads_xdg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace_id, actor_id = uuid4(), uuid4()
    config_home = tmp_path / "xdg"
    bearer = tmp_path / "bearer.json"
    _bearer(bearer, "xdg-token", actor_id, kind="owner")
    before = bearer.read_bytes()
    config = config_home / "omp-work" / "client.json"
    config.parent.mkdir(parents=True)
    _client_json(config, "http://127.0.0.1:9", workspace_id, bearer)
    definition = _write_definition(tmp_path, _valid_payload())
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    seen: dict[str, object] = {}

    def spy_run(
        client: WorkClient, *_args: object, actor_id: UUID, **_kwargs: object
    ) -> dict[str, object]:
        seen["token"] = client._token
        seen["actor_id"] = actor_id
        return _campaign_result("refuted")

    with _spies(monkeypatch) as calls:
        monkeypatch.setattr(research_campaign, "run_research_campaign", spy_run)
        code = main(["research", "run", "--key", "OMP-1", "--definition", str(definition)])
    assert code == 0
    assert calls.clients[0][0][0:3] == ("http://127.0.0.1:9", workspace_id, bearer)
    assert seen["token"] == "xdg-token"
    assert seen["actor_id"] == actor_id
    assert bearer.read_bytes() == before
    capsys.readouterr()


@pytest.mark.parametrize(
    ("error", "stderr_text"),
    [
        (RuntimeError("research job is unsettled"), "research job is unsettled"),
        (WorkError("forbidden"), "forbidden"),
    ],
    ids=["execution-failure", "refusal"],
)
def test_execution_failure_exits_nonzero_without_concluded_output(
    error: Exception,
    stderr_text: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, definition, _workspace, _actor, _bearer_bytes = _layout(tmp_path)
    seen: dict[str, Path] = {}

    def spy_run(*_args: object, harness_dir: Path, **_kwargs: object) -> dict[str, object]:
        seen["harness_dir"] = harness_dir
        (harness_dir / "marker").write_text("x", encoding="utf-8")
        raise error

    with _spies(monkeypatch) as calls:
        monkeypatch.setattr(research_campaign, "run_research_campaign", spy_run)
        code = main(
            [
                "research",
                "run",
                "--key",
                "OMP-1",
                "--definition",
                str(definition),
                "--client-config",
                str(config),
            ]
        )
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert "concluded" not in captured.out
    assert "concluded" not in captured.err
    assert stderr_text in captured.err
    assert "Traceback" not in captured.err
    assert calls.opened[0]._client.is_closed
    assert not seen["harness_dir"].exists()
    assert calls.connects == []

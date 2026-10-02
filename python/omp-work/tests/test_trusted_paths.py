"""OMP-532-s06: unit tests for trusted_paths list builder (L-RT-03)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from omp_work.__main__ import main as main_entry
from omp_work.project_cli import run_projects
from omp_work.trusted_paths import (
    find_omp_checkout,
    get_main_checkout,
    is_linked_worktree,
    normalize_url,
    trusted_paths,
)


class FakeStore:
    def __init__(
        self,
        projects: list[dict[str, Any]] | None = None,
        *,
        raise_on_list: bool = False,
        raise_on_read: bool = False,
    ) -> None:
        self.projects = projects or []
        self.raise_on_list = raise_on_list
        self.raise_on_read = raise_on_read

    def list_projects(self, workspace_id: UUID, actor_id: UUID) -> dict[str, Any]:
        if self.raise_on_list:
            raise RuntimeError("store error: connection refused")
        return {
            "workspace_id": str(workspace_id),
            "projects": [
                {"project_id": str(p["project_id"]), "key": p.get("key", "test")}
                for p in self.projects
            ],
        }

    def read_project(
        self, workspace_id: UUID, actor_id: UUID, project_id: UUID
    ) -> dict[str, Any]:
        if self.raise_on_read:
            raise RuntimeError("store error: query failed")
        for p in self.projects:
            if str(p["project_id"]) == str(project_id):
                return p
        raise KeyError(f"Project not found: {project_id}")


def _init_git_repo(path: Path, origin_url: str | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test User"],
        check=True,
    )
    if origin_url:
        subprocess.run(
            ["git", "-C", str(path), "config", "remote.origin.url", origin_url],
            check=True,
        )
    subprocess.run(
        ["git", "-C", str(path), "commit", "--allow-empty", "-m", "init"],
        check=True,
        capture_output=True,
    )
    return path


def _add_worktree(main_repo: Path, wt_path: Path) -> Path:
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-C", str(main_repo), "worktree", "add", str(wt_path), "HEAD"],
        check=True,
        capture_output=True,
    )
    return wt_path


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "https://github.com/theturtlecsz/media-discovery.git",
            "github.com/theturtlecsz/media-discovery",
        ),
        (
            "git@github.com:theturtlecsz/media-discovery.git",
            "github.com/theturtlecsz/media-discovery",
        ),
        (
            "ssh://git@github.com/theturtlecsz/media-discovery.git/",
            "github.com/theturtlecsz/media-discovery",
        ),
        (
            "HTTPS://GITHUB.COM/theturtlecsz/media-discovery.git",
            "github.com/theturtlecsz/media-discovery",
        ),
        (
            "git@gitlab.com:group/subgroup/project.git",
            "gitlab.com/group/subgroup/project",
        ),
        (
            "https://gitlab.com/group/subgroup/project.git/",
            "gitlab.com/group/subgroup/project",
        ),
        (
            "git@bitbucket.org:owner/repo",
            "bitbucket.org/owner/repo",
        ),
    ],
)
def test_normalize_url(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


def test_registered_url_matching_https_vs_git_and_unregistered_excluded(
    tmp_path: Path,
) -> None:
    workspace_id = uuid4()
    actor_id = uuid4()
    proj_id = uuid4()

    # Store registers https URL
    store = FakeStore(
        [
            {
                "project_id": proj_id,
                "key": "sample-project",
                "repositories": [
                    {
                        "key": "repo-a",
                        "url": "https://github.com/example-org/repo-a.git",
                    },
                    {
                        "key": "repo-no-url",
                        # Missing url should be skipped safely
                    },
                    {
                        "key": "repo-empty-url",
                        "url": "",
                    },
                ],
            }
        ]
    )

    scan_root = tmp_path / "scan"
    scan_root.mkdir()

    # Candidate 1: clone with git@ origin (matches repo-a)
    repo_a = _init_git_repo(
        scan_root / "repo-a-checkout",
        origin_url="git@github.com:example-org/repo-a.git",
    )
    # Candidate 2: unregistered repo
    repo_b = _init_git_repo(
        scan_root / "repo-b-checkout",
        origin_url="https://github.com/example-org/unregistered.git",
    )

    output_file = tmp_path / "out" / "trusted.json"
    code = trusted_paths(
        store,
        workspace_id,
        actor_id,
        scan_roots=[scan_root],
        output=output_file,
    )
    assert code == 0
    assert output_file.is_file()

    raw = output_file.read_text(encoding="utf-8")
    data = json.loads(raw)
    assert raw == json.dumps(data, separators=(",", ":")) + "\n"
    assert data["version"] == 1
    assert list(data) == ["version", "paths"]
    paths = data["paths"]

    # repo_a realpath must be in paths
    assert os.path.realpath(str(repo_a)) in paths
    # repo_b realpath must NOT be in paths
    assert os.path.realpath(str(repo_b)) not in paths


def test_linked_worktree_lists_main_checkout_outside_scan_roots(
    tmp_path: Path,
) -> None:
    workspace_id = uuid4()
    actor_id = uuid4()
    proj_id = uuid4()

    registered_url = "https://github.com/example-org/worktree-project.git"
    store = FakeStore(
        [
            {
                "project_id": proj_id,
                "key": "wt-project",
                "repositories": [{"key": "repo-wt", "url": registered_url}],
            }
        ]
    )

    # Main checkout is OUTSIDE scan root
    outside_dir = tmp_path / "outside"
    main_repo = _init_git_repo(
        outside_dir / "main-checkout",
        origin_url="git@github.com:example-org/worktree-project.git",
    )

    # Worktree is INSIDE scan root
    scan_root = tmp_path / "scan"
    scan_root.mkdir()
    wt_checkout = _add_worktree(main_repo, scan_root / "wt-checkout")

    output_file = tmp_path / "trusted.json"
    code = trusted_paths(
        store,
        workspace_id,
        actor_id,
        scan_roots=[scan_root],
        output=output_file,
    )
    assert code == 0

    data = json.loads(output_file.read_text(encoding="utf-8"))
    paths = data["paths"]

    # Both worktree and main checkout (outside scan roots) must be listed
    assert os.path.realpath(str(wt_checkout)) in paths
    assert os.path.realpath(str(main_repo)) in paths


def test_omp_origin_listed_and_depth_limit(tmp_path: Path) -> None:
    workspace_id = uuid4()
    actor_id = uuid4()

    # Empty store (no registered projects)
    store = FakeStore([])

    scan_root = tmp_path / "scan"
    scan_root.mkdir()

    # Find real OMP checkout & origin
    real_omp_checkout, real_omp_origin = find_omp_checkout()
    assert real_omp_checkout is not None
    assert real_omp_origin is not None

    # Candidate 1: repo with OMP's origin at depth 1 -> must be trusted
    omp_clone = _init_git_repo(
        scan_root / "omp-clone",
        origin_url=real_omp_origin,
    )

    # Candidate 2: repo with OMP's origin at depth 2 -> must be trusted
    depth2_repo = _init_git_repo(
        scan_root / "d1" / "depth2-repo",
        origin_url=real_omp_origin,
    )

    # Candidate 3: repo with OMP's origin at depth 3 -> depth limit holds, NOT trusted
    depth3_repo = _init_git_repo(
        scan_root / "d1" / "d2" / "depth3-repo",
        origin_url=real_omp_origin,
    )

    output_file = tmp_path / "trusted.json"
    code = trusted_paths(
        store,
        workspace_id,
        actor_id,
        scan_roots=[scan_root],
        output=output_file,
    )
    assert code == 0

    data = json.loads(output_file.read_text(encoding="utf-8"))
    paths = data["paths"]

    # Candidate matching OMP's origin at depth 1 and 2 are listed
    assert os.path.realpath(str(omp_clone)) in paths
    assert os.path.realpath(str(depth2_repo)) in paths

    # Candidate at depth 3 is NOT listed (depth limit holds)
    assert os.path.realpath(str(depth3_repo)) not in paths

    # OMP checkout itself is listed
    assert os.path.realpath(str(real_omp_checkout)) in paths
    if is_linked_worktree(real_omp_checkout):
        main_co = get_main_checkout(real_omp_checkout)
        if main_co:
            assert os.path.realpath(str(main_co)) in paths


def test_store_error_exits_2_and_keeps_old_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace_id = uuid4()
    actor_id = uuid4()

    output_file = tmp_path / "trusted.json"
    old_content = json.dumps({"version": 1, "paths": ["/keep/this/old/path"]})
    output_file.write_text(old_content, encoding="utf-8")

    # Store that raises error on list_projects
    store_err_list = FakeStore(raise_on_list=True)
    code = trusted_paths(
        store_err_list,
        workspace_id,
        actor_id,
        scan_roots=[tmp_path],
        output=output_file,
    )
    assert code == 2
    assert output_file.read_text(encoding="utf-8") == old_content
    stderr = capsys.readouterr().err
    assert "store error" in stderr

    # Store that raises error on read_project
    store_err_read = FakeStore(
        [{"project_id": uuid4(), "key": "k", "repositories": []}],
        raise_on_read=True,
    )
    code = trusted_paths(
        store_err_read,
        workspace_id,
        actor_id,
        scan_roots=[tmp_path],
        output=output_file,
    )
    assert code == 2
    assert output_file.read_text(encoding="utf-8") == old_content


def test_run_projects_cli_dispatch(tmp_path: Path) -> None:
    workspace_id = uuid4()
    actor_id = uuid4()
    proj_id = uuid4()

    store = FakeStore(
        [
            {
                "project_id": proj_id,
                "key": "cli-proj",
                "repositories": [
                    {"key": "cli-r", "url": "https://github.com/foo/bar.git"}
                ],
            }
        ]
    )

    scan_dir = tmp_path / "scan"
    repo = _init_git_repo(
        scan_dir / "myrepo",
        origin_url="git@github.com:foo/bar.git",
    )
    out_file = tmp_path / "cli_out.json"

    args = SimpleNamespace(
        projects_command="trusted-paths",
        workspace=workspace_id,
        actor=actor_id,
        scan_roots=[scan_dir],
        output=out_file,
    )
    rc = run_projects(args, store=store)
    assert rc == 0
    assert out_file.is_file()
    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert os.path.realpath(str(repo)) in data["paths"]


def test_main_cli_argument_parsing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace_id = uuid4()
    actor_id = uuid4()
    proj_id = uuid4()

    store = FakeStore(
        [
            {
                "project_id": proj_id,
                "key": "cli-proj",
                "repositories": [
                    {"key": "cli-r", "url": "https://github.com/foo/bar.git"}
                ],
            }
        ]
    )

    scan_dir = tmp_path / "scan"
    repo = _init_git_repo(
        scan_dir / "myrepo",
        origin_url="git@github.com:foo/bar.git",
    )
    out_file = tmp_path / "cli_out.json"

    # Monkeypatch PostgresWorkStore in project_cli to return our FakeStore
    monkeypatch.setattr(
        "omp_work.v1.store.PostgresWorkStore",
        lambda *args, **kwargs: store,
    )

    argv = [
        "projects",
        "trusted-paths",
        "--workspace",
        str(workspace_id),
        "--actor",
        str(actor_id),
        "--scan-root",
        str(scan_dir),
        "--output",
        str(out_file),
    ]

    ret = main_entry(argv)
    assert ret == 0
    assert out_file.is_file()
    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert os.path.realpath(str(repo)) in data["paths"]

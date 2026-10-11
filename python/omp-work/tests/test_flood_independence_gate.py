"""OMP-584 independence gate in external flood.py, staged and executed in pytest's temp dir.

Contract defended:
- validate_slices rejects an implementer dependency edge that only orders the slice: it is
  valid only when a shared non-union file overlaps or the dependent's work text explicitly
  consumes a named producer file/artifact.
- Consumption verbs bind to producer tokens within one clause; hypothetical/reference/unrelated
  prose stays local, and words inside path/artifact tokens cannot qualify consumption.
- Both endpoints' exclusions filter shared edits and consumed files/outputs before evidence.
- Basename aliases belong only to unique eligible producer files; outputs require exact tokens.
- Exclusions (Do not touch sections in inline/bold/heading/list formats, plus Assumptions and
  Deferred) are parsed out, so a path named only under an exclusion cannot justify an edge.
- Owner slice edges, implementer-after-owner edges and the final changelog
  edge are preserved; union-merged inventory never justifies ordering.
- `check-plan` and both plan-promotion paths (`plan_promotion=auto` and the manual `promote`
  command) apply the same gate, so a disjoint order-only chain is refused as invalid.

Staging contract:
- OMP584_FLOOD_SOURCE names a directory holding an already-patched flood.py. The tests copy it
  into pytest's temp dir and execute that copy; the selected source is never patched or mutated.
- Otherwise only the explicitly staged raw infra/automation-user/ stage is used: a raw flood.py
  plus its support files and the eventual omp584-dependencies.patch, copied into temp and patched
  there. The original and its bytecode are never touched.
- When neither a selected source nor a raw staged source exists, the gate tests skip. There is no
  home discovery, no machine-specific home path, and no committed copy of flood.py to fall back on.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
PATCH = ROOT / "infra" / "automation-user" / "omp584-dependencies.patch"
STAGE_DIR = ROOT / "infra" / "automation-user" / "omp584-flood-stage"


def _support_files(src_dir: Path) -> list[str]:
    """Sibling .py modules flood.py may import, carried beside it into the temp stage.

    flood.py ships as one stdlib-only module; the glob is defensive so a staged source that
    splits a helper out still executes. Non-Python assets are not needed by the running copy.
    """
    if not src_dir.is_dir():
        return []
    return sorted(f.name for f in src_dir.iterdir() if f.is_file() and f.suffix == ".py" and f.name != "flood.py")


def _resolve_stage(tmp_root: Path) -> tuple[Path, list[Path]]:
    """Produce an executable flood.py copy plus the paths that must stay byte-identical.

    Returns (flood.py path, untouched originals). Raises unittest-style SkipTest via pytest.skip
    when there is nothing to execute.
    """
    selected = os.environ.get("OMP584_FLOOD_SOURCE")
    if selected:
        src_dir = Path(selected).resolve()
        src_flood = src_dir / "flood.py"
        # An explicit selection that is not usable is a configuration error, not "nothing to test":
        # fail visibly so the operator sees the bad path instead of a green skip.
        assert src_flood.is_file(), f"OMP584_FLOOD_SOURCE={src_dir} holds no flood.py"
        dest = tmp_root / "flood.py"
        shutil.copy2(src_flood, dest)
        for name in _support_files(src_dir):
            shutil.copy2(src_dir / name, tmp_root / name)
        return dest, [src_flood]

    raw_flood = STAGE_DIR / "flood.py"
    raw_patch = (STAGE_DIR / PATCH.name) if (STAGE_DIR / PATCH.name).is_file() else PATCH
    if raw_flood.is_file() and raw_patch.is_file():
        dest = tmp_root / "flood.py"
        shutil.copy2(raw_flood, dest)
        for name in _support_files(STAGE_DIR):
            shutil.copy2(STAGE_DIR / name, tmp_root / name)
        res = subprocess.run(
            ["patch", "-p1", "--forward", "-i", str(raw_patch)],
            cwd=tmp_root,
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            raise AssertionError(
                "staged omp584-dependencies.patch did not apply to the staged flood.py "
                f"(exit {res.returncode}):\nstdout: {res.stdout}\nstderr: {res.stderr}"
            )
        return dest, [raw_flood, raw_patch]

    pytest.skip(
        "no staged flood.py to test: set OMP584_FLOOD_SOURCE to an already-patched directory, "
        f"or stage raw {STAGE_DIR}/flood.py with {PATCH.name}"
    )


def _load(path: Path):
    spec = importlib.util.spec_from_file_location("flood_staged", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def staged_flood(tmp_path_factory: pytest.TempPathFactory):
    """An executable patched flood.py in temp, with the originals it was copied from."""
    stage = tmp_path_factory.mktemp("flood_exec")
    flood_py, originals = _resolve_stage(stage)
    return _load(flood_py), stage, originals


def _make_repo(tmp_path: Path) -> Path:
    """Minimal main-branch git checkout so Flood(config, cfg_dir) can construct."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "todo.py").write_text("value = 1\n")
    (repo / "test_todo.py").write_text("def test_ok():\n    assert True\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "fixture"],
                   cwd=repo, check=True)
    return repo


def _make_config(tmp_path: Path, repo: Path, *, promotion: str | None = None) -> Path:
    """A config the gate can build a Flood from without launching jobs."""
    cfg = {
        "repo": str(repo),
        "main_branch": "main",
        "worktrees_dir": str(tmp_path / "wt"),
        "tasks_dir": "tasks",
        "state_file": "state.json",
        "logs_dir": "logs",
        "slots": 2,
        "poll_seconds": 0.2,
        "max_attempts": 2,
        "default_max_minutes": 5,
        "rider_dir": str(tmp_path / "riders"),
        "convert_failed": False,
        "reviewer": None,
        "providers": {"fake": {"cmd": [sys.executable, "-c", "pass"], "max_parallel": 2}},
    }
    if promotion is not None:
        cfg["plan_promotion"] = promotion
    p = tmp_path / "flood.json"
    p.write_text(json.dumps(cfg, indent=2))
    return p


def _plan_tree(tmp_path: Path, tid: str = "ITEM") -> Path:
    pdir = tmp_path / "tasks" / "proposed" / tid
    pdir.mkdir(parents=True, exist_ok=True)
    return pdir


def write_slice(pdir: Path, sid: str, deps: list[str] = (), files: list[str] | None = None,
                body: str | None = None, owner: bool = False, mm: int = 30,
                owner_cmd: str | None = None, outputs: list[str] = ()) -> Path:
    pdir.mkdir(parents=True, exist_ok=True)
    fm = ["---", f"id: {sid}", f"depends_on: [{', '.join(deps)}]", f"max_minutes: {mm}", "kind: implement"]
    if owner:
        fm.append("owner: true")
    if owner_cmd is not None:
        fm.append(f"owner_cmd: {owner_cmd}")
    fm.append(f"outputs: [{', '.join(outputs)}]")
    if files is not None:
        fm.append(f"files: [{', '.join(files)}]")
    fm.append("---")
    if body is None:
        if owner:
            body = "Owner runs: check\nEvidence: output\nDone when: passes."
        else:
            body = f"Implement {sid}.\nDone when: test_{sid.replace('-', '_')}.py passes.\nDo not touch: none"
    (pdir / f"{sid}.md").write_text("\n".join(fm) + "\n" + body + "\n")
    return pdir / f"{sid}.md"


EXCLUSION_FORMATS = [
    ("inline", "Do not touch: src/a.ts"),
    ("inline_backticks", "Do not touch: `src/a.ts`"),
    ("bold_colon_inside", "**Do not touch:** src/a.ts"),
    ("bold_colon_outside", "**Do not touch**: src/a.ts"),
    ("italic_colon", "*Do not touch*: src/a.ts"),
    ("underline_colon", "__Do not touch__: src/a.ts"),
    ("heading_h1", "# Do not touch\nsrc/a.ts"),
    ("heading_h2", "## Do not touch\n`src/a.ts`"),
    ("heading_h3", "### Do not touch\nsrc/a.ts"),
    ("heading_h4", "#### Do not touch\nsrc/a.ts"),
    ("heading_with_label", "## Do not touch: unrelated files\nsrc/a.ts"),
    ("unordered_dash", "Do not touch:\n- src/a.ts\n- other.ts"),
    ("unordered_star", "Do not touch:\n* src/a.ts\n* other.ts"),
    ("unordered_plus", "Do not touch:\n+ src/a.ts"),
    ("numbered_dot", "Do not touch:\n1. src/a.ts\n2. other.ts"),
    ("numbered_paren", "Do not touch:\n1) `src/a.ts`\n2) other.ts"),
    ("bold_numbered", "**Do not touch**:\n1. src/a.ts"),
    ("bold_unordered", "**Do not touch:**\n- src/a.ts"),
    ("inline_done_when", "Done when: test_ITEM_s02.py passes. Do not touch: src/a.ts"),
    ("assumptions_heading", "# Assumptions\nassume narrow reading src/a.ts\n# Work\nImplement ITEM-s02.\nDone when: test_ITEM_s02.py passes.\nDo not touch: unrelated.ts"),
    ("deferred_heading", "# Deferred\ndefer src/a.ts\n# Work\nImplement ITEM-s02.\nDone when: test_ITEM_s02.py passes.\nDo not touch: unrelated.ts"),
]


@pytest.mark.parametrize("fmt_name,exclusion_snippet", EXCLUSION_FORMATS)
def test_two_slice_exclusion_only_chains_fail_all_formats(staged_flood, tmp_path, fmt_name, exclusion_snippet):
    mod, _, _ = staged_flood
    pdir = tmp_path / f"plan_{fmt_name}"
    write_slice(pdir, "ITEM-s01", deps=[], files=["src/a.ts"],
                body="Implement ITEM-s01.\nDone when: test_ITEM_s01.py passes.\nDo not touch: none")

    body_s02 = f"Implement ITEM-s02.\nDone when: test_ITEM_s02.py passes.\n{exclusion_snippet}"
    if fmt_name in ("inline_done_when", "assumptions_heading", "deferred_heading"):
        body_s02 = exclusion_snippet
    write_slice(pdir, "ITEM-s02", deps=["ITEM-s01"], files=["src/b.ts"], body=body_s02)

    violations, _ = mod.validate_slices("ITEM", pdir)
    expected = "ITEM-s02: depends_on ITEM-s01 orders the slice only; leave depends_on empty, edit a file ITEM-s01 also edits, or name a file or output of ITEM-s01 that this slice reads, imports, consumes or extends"
    assert any(expected in v for v in violations), (
        f"Format {fmt_name} failed to reject order-only chain. Violations: {violations}"
    )


@pytest.mark.parametrize("fmt_name,exclusion_snippet", EXCLUSION_FORMATS[:6])
def test_three_slice_exclusion_only_chains_fail_all_formats(staged_flood, tmp_path, fmt_name, exclusion_snippet):
    mod, _, _ = staged_flood
    pdir = tmp_path / f"plan_3_{fmt_name}"
    write_slice(pdir, "ITEM-s01", deps=[], files=["src/a.ts"],
                body="Implement ITEM-s01.\nDone when: test_ITEM_s01.py passes.\nDo not touch: none")

    body_s02 = f"Implement ITEM-s02.\nDone when: test_ITEM_s02.py passes.\n{exclusion_snippet}"
    write_slice(pdir, "ITEM-s02", deps=["ITEM-s01"], files=["src/b.ts"], body=body_s02)

    excl_s03 = exclusion_snippet.replace("src/a.ts", "src/b.ts")
    body_s03 = f"Implement ITEM-s03.\nDone when: test_ITEM_s03.py passes.\n{excl_s03}"
    write_slice(pdir, "ITEM-s03", deps=["ITEM-s02"], files=["src/c.ts"], body=body_s03)

    violations, _ = mod.validate_slices("ITEM", pdir)
    assert any("ITEM-s02: depends_on ITEM-s01 orders the slice only" in v for v in violations), (
        f"Violations missing s02 -> s01 failure: {violations}"
    )
    assert any("ITEM-s03: depends_on ITEM-s02 orders the slice only" in v for v in violations), (
        f"Violations missing s03 -> s02 failure: {violations}"
    )


def test_independent_roots_pass(staged_flood, tmp_path):
    mod, _, _ = staged_flood
    pdir = tmp_path / "plan_indep"
    write_slice(pdir, "ITEM-s01", deps=[], files=["src/a.ts"],
                body="Implement ITEM-s01.\nDone when: test_ITEM_s01.py passes.\nDo not touch: none")
    write_slice(pdir, "ITEM-s02", deps=[], files=["src/b.ts"],
                body="Implement ITEM-s02.\nDone when: test_ITEM_s02.py passes.\nDo not touch: none")

    violations, slices = mod.validate_slices("ITEM", pdir)
    assert violations == [], f"Independent roots should pass without violations: {violations}"
    assert len(slices) == 2


def test_data_and_shared_file_reuse_pass(staged_flood, tmp_path):
    mod, _, _ = staged_flood

    pdir_shared = tmp_path / "plan_shared_file"
    write_slice(pdir_shared, "ITEM-s01", deps=[], files=["src/common.ts", "src/a.ts"],
                body="Implement ITEM-s01.\nDone when: test_ITEM_s01.py passes.\nDo not touch: none")
    write_slice(pdir_shared, "ITEM-s02", deps=["ITEM-s01"], files=["src/common.ts", "src/b.ts"],
                body="Implement ITEM-s02.\nDone when: test_ITEM_s02.py passes.\nDo not touch: none")
    violations, _ = mod.validate_slices("ITEM", pdir_shared)
    assert violations == [], f"Shared non-union file ordering should pass: {violations}"

    pdir_consumed = tmp_path / "plan_consumed_path"
    write_slice(pdir_consumed, "ITEM-s01", deps=[], files=["src/a.ts"],
                body="Implement ITEM-s01.\nDone when: test_ITEM_s01.py passes.\nDo not touch: none")
    write_slice(pdir_consumed, "ITEM-s02", deps=["ITEM-s01"], files=["src/b.ts"],
                body="Implement ITEM-s02. It imports `src/a.ts` for pipeline data.\nDone when: test_ITEM_s02.py passes.\nDo not touch: none")
    violations, _ = mod.validate_slices("ITEM", pdir_consumed)
    assert violations == [], f"Explicit producer file consumption should pass: {violations}"

    pdir_base = tmp_path / "plan_consumed_base"
    write_slice(pdir_base, "ITEM-s01", deps=[], files=["infra/automation-user/tool.patch"],
                body="Create patch.\nDone when: test_ITEM_s01.py passes.\nDo not touch: none")
    write_slice(pdir_base, "ITEM-s02", deps=["ITEM-s01"], files=["src/b.ts"],
                body="Implement ITEM-s02. Apply tool.patch before running tests.\nDone when: test_ITEM_s02.py passes.\nDo not touch: none")
    violations, _ = mod.validate_slices("ITEM", pdir_base)
    assert violations == [], f"Producer file basename consumption should pass: {violations}"


def test_union_inventory_behavior(staged_flood, tmp_path):
    mod, _, _ = staged_flood
    is_union_fn = lambda fp: fp == "docs/upstream-fork-inventory.tsv"

    pdir_u_indep = tmp_path / "plan_u_indep"
    write_slice(pdir_u_indep, "ITEM-s01", deps=[], files=["docs/upstream-fork-inventory.tsv", "src/a.ts"],
                body="Implement ITEM-s01.\nDone when: test_ITEM_s01.py passes.\nDo not touch: none")
    write_slice(pdir_u_indep, "ITEM-s02", deps=[], files=["docs/upstream-fork-inventory.tsv", "src/b.ts"],
                body="Implement ITEM-s02.\nDone when: test_ITEM_s02.py passes.\nDo not touch: none")
    violations, _ = mod.validate_slices("ITEM", pdir_u_indep, is_union=is_union_fn)
    assert violations == [], f"Union inventory sharing with independent roots must pass: {violations}"

    pdir_u_order = tmp_path / "plan_u_order"
    write_slice(pdir_u_order, "ITEM-s01", deps=[], files=["docs/upstream-fork-inventory.tsv", "src/a.ts"],
                body="Implement ITEM-s01.\nDone when: test_ITEM_s01.py passes.\nDo not touch: none")
    write_slice(pdir_u_order, "ITEM-s02", deps=["ITEM-s01"], files=["docs/upstream-fork-inventory.tsv", "src/b.ts"],
                body="Implement ITEM-s02. Update docs/upstream-fork-inventory.tsv.\nDone when: test_ITEM_s02.py passes.\nDo not touch: none")
    violations, _ = mod.validate_slices("ITEM", pdir_u_order, is_union=is_union_fn)
    assert any("ITEM-s02: depends_on ITEM-s01 orders the slice only" in v for v in violations), (
        f"Union inventory must not justify ordering. Violations: {violations}"
    )

    for target in ("docs/upstream-fork-inventory.tsv", "upstream-fork-inventory.tsv"):
        write_slice(pdir_u_order, "ITEM-s02", deps=["ITEM-s01"], files=["src/b.ts"],
                    body=f"Consume {target}.\nDone when: test_ITEM_s02.py passes.\nDo not touch: none")
        violations, _ = mod.validate_slices("ITEM", pdir_u_order, is_union=is_union_fn)
        assert violations == [
            "ITEM-s02: depends_on ITEM-s01 orders the slice only; leave depends_on empty, "
            "edit a file ITEM-s01 also edits, or name a file or output of ITEM-s01 "
            "that this slice reads, imports, consumes or extends"
        ]


@pytest.mark.parametrize("endpoint", ["producer", "consumer"])
@pytest.mark.parametrize("evidence,excluded", [
    ("shared", "src/a.ts"),
    ("file", "src/a.ts"),
    ("alias", "src/a.ts"),
    ("alias", "a.ts"),
    ("output", "artifact_token"),
    ("output_path", "reports/result.json"),
])
def test_either_endpoint_excludes_dependency_evidence(staged_flood, tmp_path, endpoint, evidence, excluded):
    """Excluded edits, consumed files/aliases and outputs cannot justify either endpoint's edge."""
    mod, _, _ = staged_flood
    pdir = _plan_tree(tmp_path)
    target = {"file": "src/a.ts", "alias": "a.ts", "output": "artifact_token",
              "output_path": "reports/result.json"}.get(evidence)
    producer_exclusion = excluded if endpoint == "producer" else "none"
    consumer_exclusion = excluded if endpoint == "consumer" else "none"
    write_slice(pdir, "ITEM-s01", files=["src/a.ts"], outputs=["artifact_token", "reports/result.json"],
                body=f"Work: implement producer.\nDone when: test_producer.py passes.\nDo not touch: {producer_exclusion}")
    write_slice(pdir, "ITEM-s02", deps=["ITEM-s01"],
                files=["src/a.ts"] if evidence == "shared" else ["src/b.ts"],
                body=f"Work: {f'Consume {target}.' if target else 'implement consumer.'}\n"
                     f"Done when: test_consumer.py passes.\nDo not touch: {consumer_exclusion}")
    violations, _ = mod.validate_slices("ITEM", pdir)
    assert violations == [
        "ITEM-s02: depends_on ITEM-s01 orders the slice only; leave depends_on empty, "
        "edit a file ITEM-s01 also edits, or name a file or output of ITEM-s01 "
        "that this slice reads, imports, consumes or extends"
    ]


@pytest.mark.parametrize("files,outputs,target,excluded,valid,endpoint", [
    (*case, endpoint)
    for case in [
        (["src/a.ts"], [], "src/a.ts", "other/a.ts", True),
        (["src/a.ts"], [], "a.ts", "other/a.ts", True),
        (["src/a.ts"], [], "src/a.ts", "a.ts", True),
        (["src/a.ts"], [], "src/a.ts", "src/a.ts.backup", True),
        (["src/a.ts", "other/a.ts"], [], "a.ts", "other/a.ts", True),
        (["src/a.ts", "other/a.ts"], [], "a.ts", "none", False),
        (["src/a.ts", "other/a.ts"], [], "src/a.ts", "none", True),
        (["src/a.ts"], [], "src/a.ts.backup", "none", False),
        (["src/a.ts"], [], "other/src/a.ts", "none", False),
        (["src/a.ts"], [], "prefix-a.ts", "none", False),
        (["src/a.ts"], [], "a.ts/suffix", "none", False),
        (["data/input.json"], [], "data/input.json", "none", True),
        (["src/a.ts"], ["artifact_token"], "artifact_token", "none", True),
        (["src/a.ts"], ["artifact_token"], "a r t i f c _ o k e n", "none", False),
        (["src/a.ts"], ["artifact_token"], "artifact_token_suffix", "none", False),
        (["src/a.ts"], ["artifact_token"], "prefix-artifact_token", "none", False),
        (["src/a.ts"], ["reports/result.json", "other/result.json"], "result.json", "none", False),
        (["src/a.ts"], ["reports/result.json", "other/result.json"], "reports/result.json", "none", True),
        (["src/a.ts"], ["reports/result.json", "other/result.json"], "other/result.json", "none", True),
        (["src/a.ts"], ["reports/result.json"], "result.json", "none", False),
    ]
    for endpoint in (["producer", "consumer"] if case[3] != "none" else ["producer"])
])
def test_dependency_evidence_exact_tokens_and_eligible_file_aliases(
        staged_flood, tmp_path, files, outputs, target, excluded, valid, endpoint):
    """Only whole declared paths/outputs and unique eligible file basenames justify ordering."""
    mod, _, _ = staged_flood
    pdir = _plan_tree(tmp_path)
    write_slice(pdir, "ITEM-s01", files=files, outputs=outputs,
                body="Work: implement producer.\nDone when: test_producer.py passes.\n"
                     f"Do not touch: {excluded if endpoint == 'producer' else 'none'}")
    write_slice(pdir, "ITEM-s02", deps=["ITEM-s01"], files=["src/b.ts"],
                body=f"Work: Consume `{target}`.\nDone when: test_consumer.py passes.\n"
                     f"Do not touch: {excluded if endpoint == 'consumer' else 'none'}")
    violations, _ = mod.validate_slices("ITEM", pdir)
    if valid:
        assert violations == []
    else:
        assert violations == [
            "ITEM-s02: depends_on ITEM-s01 orders the slice only; leave depends_on empty, "
            "edit a file ITEM-s01 also edits, or name a file or output of ITEM-s01 "
            "that this slice reads, imports, consumes or extends"
        ]


def test_legitimate_serial_chain_passes(staged_flood, tmp_path):
    mod, _, _ = staged_flood
    pdir = tmp_path / "plan_serial"
    write_slice(pdir, "ITEM-s01", deps=[], files=["src/a.ts"],
                body="Implement ITEM-s01 producing a.ts.\nDone when: test_ITEM_s01.py passes.\nDo not touch: none")
    write_slice(pdir, "ITEM-s02", deps=["ITEM-s01"], files=["src/b.ts"],
                body="Implement ITEM-s02 importing `src/a.ts`.\nDone when: test_ITEM_s02.py passes.\nDo not touch: none")
    write_slice(pdir, "ITEM-s03", deps=["ITEM-s02"], files=["src/c.ts"],
                body="Implement ITEM-s03 extending `src/b.ts`.\nDone when: test_ITEM_s03.py passes.\nDo not touch: none")

    violations, slices = mod.validate_slices("ITEM", pdir)
    assert violations == [], f"Legitimate serial pipeline should pass: {violations}"
    assert len(slices) == 3


@pytest.mark.parametrize("work,affirmative", [
    ("Cannot import src/a.ts.", False),
    ("Neither slice imports src/a.ts.", False),
    ("Instead of importing src/a.ts, write src/b.ts.", False),
    ("It fails to import src/a.ts.", False),
    ("Does not import src/a.ts.", False),
    ("Don't import src/a.ts.", False),
    ("Read src/unrelated.ts; src/a.ts stays unchanged.", False),
    ("Implement independent widget.", False),
    ("Import src/a.ts.", True),
    ("Read src/unrelated.ts. src/a.ts stays unchanged.", False),
    ("Read src/unrelated.ts\nsrc/a.ts stays unchanged.", False),
    ("Read src/unrelated.ts, src/a.ts.", True),
    ("Read `src/local.ts`, `src/a.ts`.", True),
    ("Read src/local.ts and `src/a.ts`.", True),
    ("Never write src/b.ts, import src/a.ts.", True),
    ("Never write src/b.ts; import src/a.ts.", True),
    ("Never write src/b.ts. import src/a.ts.", True),
    ("Never write src/b.ts\nimport src/a.ts.", True),
    ("Read src/unrelated.ts and never import src/a.ts.", False),
    ("Important src/a.ts application.", False),
    ("Import src/a.ts.backup.", False),
    ("Import other/src/a.ts.", False),
    ("Read `src/a.ts`.", True),
    ("Consume src/a.ts.", True),
    ("Extend src/a.ts.", True),
    ("Apply src/a.ts.", True),
])
def test_affirmative_consumption_only(staged_flood, tmp_path, work, affirmative):
    """Order-only edges fail; affirmative consumption of producer files permits ordering."""
    mod, _, _ = staged_flood
    pdir = _plan_tree(tmp_path)
    write_slice(pdir, "ITEM-s01", files=["src/a.ts"])
    write_slice(pdir, "ITEM-s02", deps=["ITEM-s01"], files=["src/b.ts"],
                body=f"{work}\nDone when: test_ITEM_s02.py passes.\nDo not touch: none")
    violations, _ = mod.validate_slices("ITEM", pdir)
    if affirmative:
        assert violations == []
    else:
        assert violations == [
            "ITEM-s02: depends_on ITEM-s01 orders the slice only; leave depends_on empty, "
            "edit a file ITEM-s01 also edits, or name a file or output of ITEM-s01 "
            "that this slice reads, imports, consumes or extends"
        ]


@pytest.mark.parametrize("work,affirmative", [
    ("Does not import src/a.ts. Import src/a.ts.", True),
    ("Cannot import src/a.ts. Read src/a.ts.", True),
    ("Neither slice imports src/a.ts. Consume src/a.ts.", True),
    ("It fails to import src/a.ts. Extend src/a.ts.", True),
    ("Don't import src/a.ts. Import src/a.ts.", True),
    ("Read src/unrelated.ts. Import src/a.ts.", True),
    ("Import src/a.ts, then write src/b.ts.", True),
    ("Instead of writing src/b.ts, import src/a.ts.", True),
    ("Rather than importing src/a.ts, write src/b.ts.", False),
    ("Read src/unrelated.ts. src/a.ts stays unchanged.", False),
    ("Import src/b.ts. src/a.ts stays unchanged.", False),
    ("Read src/local.ts, and document src/a.ts location.", False),
    ("src/a.ts.", False),
    ("Read src/a.ts for reference.", False),
    ("Read src/a.ts as an unrelated example.", False),
    ("We may import src/a.ts in a later iteration.", False),
    ("We may import src/a.ts later. Import src/a.ts.", True),
    ("Read src/a.ts for reference. Import src/a.ts.", True),
    ("Read src/a.ts as an unrelated example. Import src/a.ts.", True),
    ("Import src/a.ts, and we may revise src/b.ts later.", True),
    ("Read docs/x.md for reference, but import src/a.ts.", True),
    ("Read docs/x.md for reference and import src/a.ts.", True),
    ("We may revise src/b.ts later, but import src/a.ts.", True),
    ("Import src/a.ts, and document unrelated src/b.ts.", True),
    ("Read src/local.ts and document src/a.ts location.", False),
    ("Read src/local.ts, document src/a.ts location.", False),
])
def test_clause_bounds(staged_flood, tmp_path, work, affirmative):
    """Negation stays in its clause; only a following producer token justifies an edge."""
    mod, _, _ = staged_flood
    pdir = _plan_tree(tmp_path)
    write_slice(pdir, "ITEM-s01", files=["src/a.ts"])
    write_slice(pdir, "ITEM-s02", deps=["ITEM-s01"], files=["src/b.ts"],
                body=f"{work}\nDone when: test_ITEM_s02.py passes.\nDo not touch: none")
    violations, _ = mod.validate_slices("ITEM", pdir)
    if affirmative:
        assert violations == [], f"Consumption should justify edge: {work!r}: {violations}"
    else:
        assert violations == [
            "ITEM-s02: depends_on ITEM-s01 orders the slice only; leave depends_on empty, "
            "edit a file ITEM-s01 also edits, or name a file or output of ITEM-s01 "
            "that this slice reads, imports, consumes or extends"
        ], f"Order-only edge should fail: {work!r}: {violations}"


@pytest.mark.parametrize("token,verb,artifact", [
    ("src/future.ts", "Import", False),
    ("src/optional.ts", "Read", False),
    ("src/unrelated.ts", "Import", False),
    ("reports/for-reference.json", "Consume", True),
    ("future-artifact", "Consume", True),
    ("optional-artifact", "Read", True),
    ("unrelated-artifact", "Extend", True),
    ("reference.patch", "Apply", True),
    ("omp584-dependencies.patch", "Apply", True),
    ("may-artifact", "Consume", True),
])
def test_qualifier_words_inside_tokens_are_consumption(staged_flood, tmp_path, token, verb, artifact):
    """Producer names cannot turn affirmative consumption into hypothetical/reference prose."""
    mod, _, _ = staged_flood
    pdir = _plan_tree(tmp_path)
    write_slice(pdir, "ITEM-s01", files=["src/producer.ts" if artifact else token],
                outputs=[token] if artifact else [])
    write_slice(pdir, "ITEM-s02", deps=["ITEM-s01"], files=["src/consumer.ts"],
                body=f"{verb} {token}.\nDone when: test_ITEM_s02.py passes.\nDo not touch: none")
    violations, _ = mod.validate_slices("ITEM", pdir)
    assert violations == []


@pytest.mark.parametrize("work,affirmative", [
    ("Consume reports/result.json.", True),
    ("Consume result.json.", False),
    ("Consume reports/result.", False),
    ("Consume reports/result.json.old.", False),
    ("We may consume reports/result.json later.", False),
    ("Consume reports/result.json for reference.", False),
])
def test_output_consumption_requires_whole_output(staged_flood, tmp_path, work, affirmative):
    """Output references permit ordering only when consumer names entire producer artifact."""
    mod, _, _ = staged_flood
    pdir = _plan_tree(tmp_path)
    write_slice(pdir, "ITEM-s01", files=["src/a.ts"], outputs=["reports/result.json"])
    write_slice(pdir, "ITEM-s02", deps=["ITEM-s01"], files=["src/b.ts"],
                body=f"{work}\nDone when: test_ITEM_s02.py passes.\nDo not touch: none")
    violations, _ = mod.validate_slices("ITEM", pdir)
    assert bool(violations) is not affirmative
    if violations:
        assert violations == [
            "ITEM-s02: depends_on ITEM-s01 orders the slice only; leave depends_on empty, "
            "edit a file ITEM-s01 also edits, or name a file or output of ITEM-s01 "
            "that this slice reads, imports, consumes or extends"
        ]


@pytest.mark.parametrize("path", ["approval.json", "test_approval.py", "src/b.ts"])
def test_filenames_do_not_exempt_order_only_edges(staged_flood, tmp_path, path):
    """Approval and test filenames carry no owner exemption."""
    mod, _, _ = staged_flood
    pdir = _plan_tree(tmp_path)
    write_slice(pdir, "ITEM-s01", files=[path])
    write_slice(pdir, "ITEM-s02", deps=["ITEM-s01"], files=["src/consumer.ts"])
    violations, _ = mod.validate_slices("ITEM", pdir)
    assert any("ITEM-s02: depends_on ITEM-s01 orders the slice only" in v for v in violations)


@pytest.mark.parametrize("producer_path,consumer_path,outputs,work,passes", [
    ("src/a.ts", "src/approval-widget.ts", [], "Implement widget.", False),
    ("python/omp-work/src/omp_work/contracts/v1/approval.json", "src/b.ts", [],
     "Implement consumer.", False),
    ("src/a.ts", "src/b.ts", ["reports/result.json"], "Consume reports/result.json.", True),
    ("src/a.ts", "src/b.ts", ["reports/result.json"], "Consume result.json.", False),
    ("src/a.ts", "src/b.ts", ["artifact_token"], "Consume artifact_token.", True),
    ("src/a.ts", "src/b.ts", ["artifact_token"], "Consume artifact.", False),
])
def test_filename_and_output_tokens(staged_flood, tmp_path, producer_path, consumer_path,
                                    outputs, work, passes):
    """Approval filenames cannot exempt edges; outputs require their whole declared token."""
    mod, _, _ = staged_flood
    pdir = _plan_tree(tmp_path)
    write_slice(pdir, "ITEM-s01", files=[producer_path], outputs=outputs)
    write_slice(pdir, "ITEM-s02", deps=["ITEM-s01"], files=[consumer_path],
                body=f"{work}\nDone when: test_ITEM_s02.py passes.\nDo not touch: none")
    violations, _ = mod.validate_slices("ITEM", pdir)
    if passes:
        assert violations == []
    else:
        assert violations == [
            "ITEM-s02: depends_on ITEM-s01 orders the slice only; leave depends_on empty, "
            "edit a file ITEM-s01 also edits, or name a file or output of ITEM-s01 "
            "that this slice reads, imports, consumes or extends"
        ]


def test_owner_and_changelog_cases_pass(staged_flood, tmp_path):
    mod, _, _ = staged_flood

    pdir_owner_after = tmp_path / "plan_owner_after"
    write_slice(pdir_owner_after, "ITEM-s01", deps=[], files=["src/a.ts"],
                body="Implement ITEM-s01.\nDone when: test_ITEM_s01.py passes.\nDo not touch: none")
    write_slice(pdir_owner_after, "ITEM-s02", deps=["ITEM-s01"], files=[], owner=True,
                owner_cmd="omp-work approve --issue OMP-584",
                body="Owner runs: omp-work approve --issue OMP-584\nEvidence: ok\nDone when: passes.")
    violations, slices = mod.validate_slices("ITEM", pdir_owner_after)
    assert slices[1]["owner_cmd"] == "omp-work approve --issue OMP-584"
    assert violations == [], f"Owner slice depending on implementer slice must pass: {violations}"

    pdir_owner_before = tmp_path / "plan_owner_before"
    write_slice(pdir_owner_before, "ITEM-s01", deps=[], files=[], owner=True,
                body="Owner runs: approve\nEvidence: ok\nDone when: passes.")
    write_slice(pdir_owner_before, "ITEM-s02", deps=["ITEM-s01"], files=["src/b.ts"],
                body="Implement ITEM-s02 after owner step.\nDone when: test_ITEM_s02.py passes.\nDo not touch: none")
    violations, _ = mod.validate_slices("ITEM", pdir_owner_before)
    assert violations == [], f"Implementer slice depending on owner slice must pass: {violations}"

    pdir_changelog = tmp_path / "plan_changelog"
    write_slice(pdir_changelog, "ITEM-s01", deps=[], files=["src/a.ts"],
                body="Implement ITEM-s01.\nDone when: test_ITEM_s01.py passes.\nDo not touch: none")
    write_slice(pdir_changelog, "ITEM-s02", deps=["ITEM-s01"], files=["CHANGELOG.md"],
                body="Record changelog entry.\nDone when: test_ITEM_s02.py passes.\nDo not touch: none")
    violations, _ = mod.validate_slices("ITEM", pdir_changelog)
    assert violations == [], f"Final changelog edge must be preserved: {violations}"


def test_absent_source_skips_instead_of_reporting_success(tmp_path, monkeypatch):
    """No selected source and no raw stage: staging reports skip, never an empty gate success."""
    monkeypatch.delenv("OMP584_FLOOD_SOURCE", raising=False)
    monkeypatch.setattr(sys.modules[__name__], "STAGE_DIR", tmp_path / "no-such-stage")
    (tmp_path / "exec").mkdir()
    with pytest.raises(pytest.skip.Exception):
        _resolve_stage(tmp_path / "exec")


def test_selected_source_without_flood_py_fails_visibly(tmp_path, monkeypatch):
    """An explicit, unusable selection must fail loudly rather than skip green."""
    selected = tmp_path / "selected"
    selected.mkdir()
    monkeypatch.setenv("OMP584_FLOOD_SOURCE", str(selected))
    (tmp_path / "exec").mkdir()
    with pytest.raises(AssertionError):
        _resolve_stage(tmp_path / "exec")


def test_malformed_selected_source_fails_visibly_and_stays_unmodified(tmp_path, monkeypatch):
    """A selected source that is present but not loadable raises on load, and the source is untouched."""
    selected = tmp_path / "selected"
    selected.mkdir()
    broken = "def broken(:\n    pass\n"
    (selected / "flood.py").write_text(broken)
    monkeypatch.setenv("OMP584_FLOOD_SOURCE", str(selected))
    (tmp_path / "exec").mkdir()

    dest, originals = _resolve_stage(tmp_path / "exec")
    with pytest.raises(SyntaxError):
        _load(dest)
    assert (selected / "flood.py").read_text() == broken
    assert originals == [selected / "flood.py"]


@pytest.mark.parametrize("existing_bytecode", [False, True])
def test_selected_candidate_executes_in_temp_without_touching_original(tmp_path, monkeypatch, existing_bytecode):
    """Selected copies execute without patching or changing original files, including old bytecode."""
    selected = tmp_path / "selected"
    selected.mkdir()
    (selected / "flood.py").write_text(
        "from pathlib import Path\n"
        "root = Path(__file__).parent\n"
        "support = (root / 'helper.py').read_text()\n"
        "(root / 'executed.txt').write_text(support)\n"
    )
    (selected / "helper.py").write_text("VALUE = 41\n")
    if existing_bytecode:
        cache = selected / "__pycache__"
        cache.mkdir()
        (cache / Path(importlib.util.cache_from_source(str(selected / "flood.py"))).name).write_bytes(
            b"pre-existing bytecode"
        )
    before = {p.relative_to(selected): p.read_bytes() for p in selected.rglob("*") if p.is_file()}
    monkeypatch.setenv("OMP584_FLOOD_SOURCE", str(selected))

    def reject_patch(*args, **kwargs):
        pytest.fail("already-patched selected source must not invoke patch")

    monkeypatch.setattr(subprocess, "run", reject_patch)
    stage = tmp_path / "exec"
    stage.mkdir()
    dest, _ = _resolve_stage(stage)
    mod = _load(dest)

    assert Path(mod.__file__).resolve().parent == stage
    assert (stage / "executed.txt").read_text() == "VALUE = 41\n"
    after = {p.relative_to(selected): p.read_bytes() for p in selected.rglob("*") if p.is_file()}
    assert after == before, "selected source or its bytecode changed during staging/execution"


def test_check_plan_cli_uses_the_gate_and_manual_promote_blocks_invalid(staged_flood, tmp_path):
    """check-plan reports the violation, and the manual `promote` command refuses on it."""
    mod, _, _ = staged_flood
    repo = _make_repo(tmp_path)
    cfg = _make_config(tmp_path, repo, promotion="manual")
    flood_py = Path(mod.__file__).resolve()
    pdir = _plan_tree(tmp_path, "ITEM")

    write_slice(pdir, "ITEM-s01", deps=[], files=["src/a.ts"],
                body="Implement ITEM-s01.\nDone when: test_ITEM_s01.py passes.\nDo not touch: none")
    write_slice(pdir, "ITEM-s02", deps=["ITEM-s01"], files=["src/b.ts"],
                body="Implement ITEM-s02.\nDone when: test_ITEM_s02.py passes.\nDo not touch: src/a.ts")

    # Park the task at plan_ready (manual mode) so promote's own status precondition passes and the
    # gate is the only thing that can refuse the plan.
    f = mod.Flood(json.loads(cfg.read_text()), cfg.parent)
    f.plan_passed({"id": "ITEM"})
    assert f.state.task("ITEM")["status"] == "plan_ready"

    invalid = subprocess.run([sys.executable, str(flood_py), "check-plan", "ITEM", "--config", str(cfg)],
                             capture_output=True, text=True)
    assert invalid.returncode == 1, f"check-plan should fail on an order-only plan:\n{invalid.stdout}\n{invalid.stderr}"
    expected = ("ITEM-s02: depends_on ITEM-s01 orders the slice only; leave depends_on empty, "
                "edit a file ITEM-s01 also edits, or name a file or output of ITEM-s01 "
                "that this slice reads, imports, consumes or extends")
    assert expected in invalid.stdout

    promote = subprocess.run([sys.executable, str(flood_py), "promote", "ITEM", "--config", str(cfg)],
                             capture_output=True, text=True)
    assert promote.returncode != 0, f"manual promote must refuse an invalid plan:\n{promote.stdout}\n{promote.stderr}"
    assert expected in promote.stdout
    assert not (tmp_path / "tasks" / "ITEM-s01.md").exists(), "nothing may be promoted on an invalid plan"

    (pdir / "ITEM-s02.md").unlink()
    write_slice(pdir, "ITEM-s02", deps=[], files=["src/b.ts"],
                body="Implement ITEM-s02.\nDone when: test_ITEM_s02.py passes.\nDo not touch: none")

    valid = subprocess.run([sys.executable, str(flood_py), "check-plan", "ITEM", "--config", str(cfg)],
                           capture_output=True, text=True)
    assert valid.returncode == 0, f"check-plan should pass on independent slices:\n{valid.stdout}\n{valid.stderr}"
    assert valid.stdout.startswith("OK: 2 slices pass the plan gate")


def test_auto_plan_promotion_refuses_disjoint_order_only_chain(staged_flood, tmp_path):
    """plan_promotion=auto reaches promote_plan, which the gate must reject, leaving the plan ready."""
    mod, _, _ = staged_flood
    repo = _make_repo(tmp_path)
    cfg = _make_config(tmp_path, repo, promotion="auto")
    f = mod.Flood(json.loads(cfg.read_text()), cfg.parent)
    pdir = _plan_tree(tmp_path, "ITEM")
    write_slice(pdir, "ITEM-s01", deps=[], files=["src/a.ts"],
                body="Implement ITEM-s01.\nDone when: test_ITEM_s01.py passes.\nDo not touch: none")
    write_slice(pdir, "ITEM-s02", deps=["ITEM-s01"], files=["src/b.ts"],
                body="Implement ITEM-s02.\nDone when: test_ITEM_s02.py passes.\nDo not touch: none")

    f.promote_plan({"id": "ITEM"})

    st = f.state.task("ITEM")
    assert st["status"] == "plan_ready", f"invalid plan must not promote; state={st}"
    assert st["last_error"].startswith("plan gate failed:")
    assert "ITEM-s02: depends_on ITEM-s01 orders the slice only" in st["last_error"]
    saved = mod.Flood(json.loads(cfg.read_text()), cfg.parent).state.task("ITEM")
    assert saved["status"] == "plan_ready"
    assert saved["last_error"] == st["last_error"]
    assert not (tmp_path / "tasks" / "ITEM-s01.md").exists()
    assert (pdir / "ITEM-s01.md").exists()

    # Repairing the plan to independent roots promotes it.
    write_slice(pdir, "ITEM-s02", deps=[], files=["src/b.ts"],
                body="Implement ITEM-s02.\nDone when: test_ITEM_s02.py passes.\nDo not touch: none")
    f.tasks = mod.load_tasks(f.tasks_dir)
    f.promote_plan({"id": "ITEM"})
    st = f.state.task("ITEM")
    assert st["status"] == "planned", f"valid plan must promote; state={st}"
    assert (tmp_path / "tasks" / "ITEM-s01.md").exists() and (tmp_path / "tasks" / "ITEM-s02.md").exists()


def test_manual_plan_promotion_waits_then_promotes_only_a_valid_plan(staged_flood, tmp_path):
    """plan_promotion=manual parks at plan_ready; the gate re-checks on every promote path."""
    mod, _, _ = staged_flood
    repo = _make_repo(tmp_path)
    cfg = _make_config(tmp_path, repo, promotion="manual")
    f = mod.Flood(json.loads(cfg.read_text()), cfg.parent)
    pdir = _plan_tree(tmp_path, "ITEM")
    write_slice(pdir, "ITEM-s01", deps=[], files=["src/a.ts"],
                body="Implement ITEM-s01.\nDone when: test_ITEM_s01.py passes.\nDo not touch: none")
    write_slice(pdir, "ITEM-s02", deps=["ITEM-s01"], files=["src/b.ts"],
                body="Implement ITEM-s02.\nDone when: test_ITEM_s02.py passes.\nDo not touch: src/a.ts")

    f.plan_passed({"id": "ITEM"})
    assert f.state.task("ITEM")["status"] == "plan_ready"

    f.promote_plan({"id": "ITEM"})
    assert f.state.task("ITEM")["status"] == "plan_ready"
    assert not (tmp_path / "tasks" / "ITEM-s01.md").exists()

    (pdir / "ITEM-s02.md").unlink()
    write_slice(pdir, "ITEM-s02", deps=[], files=["src/b.ts"],
                body="Implement ITEM-s02.\nDone when: test_ITEM_s02.py passes.\nDo not touch: none")
    f.tasks = mod.load_tasks(f.tasks_dir)
    f.promote_plan({"id": "ITEM"})
    assert f.state.task("ITEM")["status"] == "planned"
    assert (tmp_path / "tasks" / "ITEM-s01.md").exists()


@pytest.mark.parametrize("label", ["Do not touch", "Assumptions", "Deferred"])
@pytest.mark.parametrize("marker", ["", "**{label}:**", "**{label}**:", "- {label}:", "* {label}:", "+ {label}:", "1. {label}:", "1) {label}:"])
def test_dependency_context_inline_exclusions_keep_peer_work(staged_flood, label, marker):
    """Unindented prose and numbered/unordered lists cannot leak excluded dependencies."""
    mod, _, _ = staged_flood
    start = marker.format(label=label) if marker else f"{label}: `src/blocked.ts`"
    body = (f"Work: consume src/first.ts\n{start}\n"
            "Avoid src/blocked.ts and blocked-token\n- src/listed.ts\n1) numbered-token\n"
            "- Work: consume src/peer.ts\nDone when: test_peer.py passes.")
    context = mod.parse_dependency_context(body)
    assert context["work_text"] == ("Work: consume src/first.ts\n"
                                    "- Work: consume src/peer.ts\nDone when: test_peer.py passes.")
    assert {"src/blocked.ts", "blocked-token", "src/listed.ts", "numbered-token"} <= context["excluded_tokens"]
    assert "src/peer.ts" not in context["excluded_tokens"]


@pytest.mark.parametrize("level", range(1, 7))
@pytest.mark.parametrize("label", ["Do not touch", "Assumptions", "Deferred"])
def test_dependency_context_heading_exclusions_keep_nested_sections(staged_flood, level, label):
    """Exclusion heading owns nested headings until a same/higher-level work heading."""
    mod, _, _ = staged_flood
    heading = "#" * level
    nested = f"{'#' * (level + 1)} Work\nconsume src/nested.ts\n" if level < 6 else ""
    body = (f"Work: src/first.ts\n{heading} **{label}**:\n- src/blocked.ts\n"
            f"{nested}**Work**: src/still-blocked.ts\n"
            f"{heading} **Work**\nconsume src/peer.ts")
    context = mod.parse_dependency_context(body)
    assert context["work_text"] == f"Work: src/first.ts\n{heading} **Work**\nconsume src/peer.ts"
    assert {"src/blocked.ts", "src/still-blocked.ts"} <= context["excluded_tokens"]
    if nested:
        assert "src/nested.ts" in context["excluded_tokens"]


@pytest.mark.parametrize("peer", ["Work:", "**Work**:", "**Work:**", "1. Work:", "1) **Work**:", "# Work"])
def test_dependency_context_inline_exclusion_stops_at_work_boundary(staged_flood, peer):
    """Peer work labels, including list and bold forms, resume dependency-bearing work."""
    mod, _, _ = staged_flood
    context = mod.parse_dependency_context(f"Do not touch: src/blocked.ts\n{peer}\nUse src/peer.ts")
    assert context["work_text"] == f"{peer}\nUse src/peer.ts"
    assert "src/blocked.ts" in context["excluded_tokens"]
    assert "src/peer.ts" not in context["excluded_tokens"]


@pytest.mark.parametrize("ending", ["", "\nUnindented src/tail.ts\n- tail-token"])
def test_dependency_context_midline_exclusion_runs_to_eof(staged_flood, ending):
    """Same-line exclusions keep preceding work but remove the entire remaining exclusion."""
    mod, _, _ = staged_flood
    context = mod.parse_dependency_context(
        f"Done when: test_work.py passes. **Do not touch:** src/blocked.ts{ending}")
    assert context["work_text"] == "Done when: test_work.py passes."
    assert "src/blocked.ts" in context["excluded_tokens"]
    if ending:
        assert {"src/tail.ts", "tail-token"} <= context["excluded_tokens"]


def test_dependency_context_higher_heading_ends_exclusion(staged_flood):
    """A parent work heading ends exclusion even when exclusion's nested label says Work."""
    mod, _, _ = staged_flood
    context = mod.parse_dependency_context(
        "### Deferred\nsrc/blocked.ts\n#### Work\nsrc/nested.ts\n## Work\nsrc/peer.ts")
    assert context["work_text"] == "## Work\nsrc/peer.ts"
    assert {"src/blocked.ts", "src/nested.ts"} <= context["excluded_tokens"]


@pytest.mark.parametrize("outputs", ["[result-token, reports/result.json]", "result-token, reports/result.json", "'single-token'", "[]"])
def test_dependency_context_outputs_normalized_at_front_matter(staged_flood, tmp_path, outputs):
    """Task loading and validation expose complete output tokens rather than string characters."""
    mod, _, _ = staged_flood
    pdir = _plan_tree(tmp_path)
    path = write_slice(pdir, "ITEM-s01", files=["src/a.ts"])
    path.write_text(path.read_text().replace("outputs: []", f"outputs: {outputs}"))
    expected = ([] if outputs == "[]" else ["single-token"] if outputs.startswith("'")
                else ["result-token", "reports/result.json"])
    assert mod.load_tasks(pdir)["ITEM-s01"]["outputs"] == expected
    violations, slices = mod.validate_slices("ITEM", pdir)
    assert violations == []
    assert slices[0]["outputs"] == expected


def test_dependency_context_validate_retains_owner_and_filters_evidence_once(staged_flood, tmp_path):
    """Owner evidence comes only from work; validator retains context for later edge checks."""
    mod, _, _ = staged_flood
    pdir = _plan_tree(tmp_path)
    body = ("Owner runs: check\nEvidence: reports/owner.json result-token\n"
            "# Do not touch: src/blocked.ts\n## Evidence: blocked-token\n"
            "# Work\nDone when: test_owner.py passes.")
    write_slice(pdir, "ITEM-s01", owner=True, body=body)
    violations, slices = mod.validate_slices("ITEM", pdir)
    assert violations == []
    assert slices[0]["owner"] is True
    assert slices[0]["body"] == body
    assert slices[0]["evidence"] == {"reports/owner.json", "result-token"}
    assert "blocked-token" in slices[0]["excluded_tokens"]


@pytest.mark.parametrize("label", ["Do not touch", "**Assumptions**", "- Deferred"])
def test_dependency_context_standalone_labels_exclude_prose(staged_flood, label):
    """Standalone exclusion labels need no colon to exclude following prose."""
    mod, _, _ = staged_flood
    context = mod.parse_dependency_context(f"{label}\nsrc/blocked.ts\n**Work**: src/peer.ts")
    assert context["work_text"] == "**Work**: src/peer.ts"
    assert "src/blocked.ts" in context["excluded_tokens"]


@pytest.mark.parametrize("case", ["order_only", "unknown", "cycle", "unordered_shared", "format"])
def test_dependency_context_preserves_graph_and_format_checks(staged_flood, tmp_path, case):
    """Dependency evidence gate preserves graph and slice format validation."""
    mod, _, _ = staged_flood
    pdir = _plan_tree(tmp_path)
    first_deps = ["ITEM-s02"] if case == "cycle" else []
    second_deps = (["MISSING"] if case == "unknown" else [] if case == "unordered_shared"
                   else ["ITEM-s01"])
    write_slice(pdir, "ITEM-s01", deps=first_deps, files=["src/a.ts"])
    write_slice(pdir, "ITEM-s02", deps=second_deps,
                files=["src/a.ts"] if case == "unordered_shared" else ["src/b.ts"],
                body="Work: src/b.ts" if case == "format" else None)
    violations, _ = mod.validate_slices("ITEM", pdir)
    if case == "order_only":
        assert any("ITEM-s02: depends_on ITEM-s01 orders the slice only" in v for v in violations)
    else:
        expected = {"unknown": "depends on MISSING, which is not in this set",
                    "cycle": "dependency cycle through",
                    "unordered_shared": "both edit src/a.ts but neither depends on the other",
                    "format": 'no "Done when:" section'}
        assert any(expected[case] in violation for violation in violations)


def test_dependency_context_evidence_lists_preserve_exact_paths_and_tokens(staged_flood):
    """Evidence extraction preserves dotfiles, relative/absolute paths and producer basenames."""
    mod, _, _ = staged_flood
    context = mod.parse_dependency_context(
        "Evidence:\n- `.github/result.json`\n1. ../reports/result.json\n"
        "2) /reports/result.json\n+ producer.patch\nresult-token\n"
        "## **Do not touch:**\nEvidence: blocked-token\n## Work\nconsume peer-token")
    assert context["evidence"] == {
        ".github/result.json", "../reports/result.json", "/reports/result.json",
        "producer.patch", "result-token",
    }
    assert context["work_text"].endswith("## Work\nconsume peer-token")
    assert "blocked-token" in context["excluded_tokens"]

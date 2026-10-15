"""OMP-584 independence gate in external flood.py, staged and executed in pytest's temp dir.

Contract defended:
- validate_slices rejects an implementer dependency edge that only orders the slice: it is
  valid only when a shared non-union file overlaps or the dependent's work text explicitly
  consumes a named producer file/artifact.
- Negation covers coordinated verbs/targets and post-target predicates within one clause.
- Consumption verbs bind to producer tokens within one clause; hypothetical/reference/unrelated
  prose stays local, and words inside path/artifact tokens cannot qualify consumption.
- Both endpoints' exclusions filter shared edits and consumed files/outputs before evidence.
- Basename aliases belong only to unique eligible producer files; outputs require exact tokens.
- Exclusions (Do not touch sections in inline/bold/heading/list formats, plus Assumptions and
  Deferred) are parsed out, so a path named only under an exclusion cannot justify an edge.
- Owner dependents, issue-linked owner approvals, consumed owner results and final changelog
  aggregation are preserved; union-merged inventory never justifies ordering.
- `check-plan`, manual `promote`, `Flood.promote_plan` and automatic/PROMOTE-marker
  promotion share `check_plan`/`validate_slices`. The gate runs before a PROMOTE marker is
  created or removed and before slice files move. An invalid plan, including one that already
  has a PROMOTE marker, keeps its proposed files and the gate diagnostic; it does not publish
  tasks or mark the parent planned. Unknown dependencies, cycles, unordered non-union edits,
  malformed front matter and missing owner Runs/Evidence sections keep their original diagnostics.

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

import hashlib
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
    """Sibling Python modules and planner rules required by the staged source."""
    if not src_dir.is_dir():
        return []
    return sorted(
        f.name for f in src_dir.iterdir()
        if f.is_file() and f.name != "flood.py"
        and (f.suffix == ".py" or f.name in ("planner-independence.md", "omp584-planner-rules.md"))
    )


def _snapshot(path: Path) -> tuple[str, int]:
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns


def _resolve_stage(tmp_root: Path) -> tuple[Path, dict[Path, tuple[str, int]]]:
    """Produce an executable flood.py copy plus pre-use hashes and mtimes of every original.

    Returns (flood.py path, original snapshots). Raises unittest-style SkipTest via pytest.skip
    when there is nothing to execute.
    """
    selected = os.environ.get("OMP584_FLOOD_SOURCE")
    raw_patch = None
    if selected:
        src_dir = Path(selected).resolve()
        src_flood = src_dir / "flood.py"
        # An explicit selection that is not usable is a configuration error, not "nothing to test":
        # fail visibly so the operator sees the bad path instead of a green skip.
        assert src_flood.is_file(), f"OMP584_FLOOD_SOURCE={src_dir} holds no flood.py"
    else:
        src_dir = STAGE_DIR
        src_flood = src_dir / "flood.py"
        if not src_flood.is_file():
            pytest.skip(
                "no staged flood.py to test: set OMP584_FLOOD_SOURCE to an already-patched directory, "
                f"or stage raw {STAGE_DIR}/flood.py with {PATCH.name}"
            )
        raw_patch = (STAGE_DIR / PATCH.name) if (STAGE_DIR / PATCH.name).is_file() else PATCH
        assert raw_patch.is_file(), f"raw staged flood.py requires {raw_patch}"

    sources = [src_flood, *(src_dir / name for name in _support_files(src_dir))]
    if raw_patch is not None:
        sources.append(raw_patch)
    originals = {source: _snapshot(source) for source in sources}
    for source in sources:
        shutil.copy2(source, tmp_root / source.name)
    dest = tmp_root / "flood.py"
    if raw_patch is not None:
        res = subprocess.run(
            ["patch", "-p1", "--forward", "-i", str(tmp_root / raw_patch.name)],
            cwd=tmp_root,
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            raise AssertionError(
                "staged omp584-dependencies.patch did not apply to the staged flood.py "
                f"(exit {res.returncode}):\nstdout: {res.stdout}\nstderr: {res.stderr}"
            )
    return dest, originals


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
    try:
        mod = _load(flood_py)
        assert {path: _snapshot(path) for path in originals} == originals
        yield mod, stage, originals
    finally:
        assert {path: _snapshot(path) for path in originals} == originals


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


# Each row contains a label-only line and the prefix for the affirmative clause below it.
# Removing only that label line must turn the rejected dependency into a valid one.
INLINE_EXCLUSION_FORMATS = [
    (f"{name}_{position}", f"{prefix}{label}", "")
    for name, label in [
        ("plain_colon", "{label}:"),
        ("bold_colon_inside", "**{label}:**"),
        ("bold_colon_outside", "**{label}**:"),
        ("italic_colon_inside", "*{label}:*"),
        ("italic_colon_outside", "*{label}*:"),
        ("underline_colon_inside", "__{label}:__"),
        ("underline_colon_outside", "__{label}__:"),
    ]
    for position, prefix in [("own_line", ""), ("after_prose", "Implementation notes. ")]
] + [
    ("plain_standalone", "{label}", ""),
    ("bold_standalone", "**{label}**", ""),
    ("italic_standalone", "*{label}*", ""),
    ("underline_standalone", "__{label}__", ""),
] + [
    (f"{name}_{form}", f"{marker} {{label}}{suffix}", f"  {marker} ")
    for name, marker in [("unordered_dash", "-"), ("unordered_star", "*"),
                         ("unordered_plus", "+"), ("numbered_dot", "1."),
                         ("numbered_paren", "1)")]
    for form, suffix in [("colon_label", ":"), ("standalone_label", "")]
] + [
    (f"{name}_items", "{label}:", f"{marker} ")
    for name, marker in [("unordered_dash", "-"), ("unordered_star", "*"),
                         ("unordered_plus", "+"), ("numbered_dot", "1."),
                         ("numbered_paren", "1)")]
] + [
    ("bold_numbered", "**{label}**:", "1. "),
    ("bold_unordered", "**{label}:**", "- "),
]

HEADING_EXCLUSION_FORMATS = [
    # Levels 1-6 plain and bold, with and without colons inside or outside the bold (unindented affirmative prose)
    *[(f"heading_h{level}", f"{'#' * level} {{label}}", "") for level in range(1, 7)],
    *[(f"heading_h{level}_colon", f"{'#' * level} {{label}}:", "") for level in range(1, 7)],
    *[(f"heading_h{level}_bold", f"{'#' * level} **{{label}}**", "") for level in range(1, 7)],
    *[(f"heading_h{level}_bold_colon_inside", f"{'#' * level} **{{label}}:**", "") for level in range(1, 7)],
    *[(f"heading_h{level}_bold_colon_outside", f"{'#' * level} **{{label}}**:", "") for level in range(1, 7)],
    # Nested sub-headings (level+1) holding the clause
    *[(f"nested_heading_h{level}_h{level + 1}", f"{'#' * level} {{label}}", f"{'#' * (level + 1)} Work\n") for level in range(1, 6)],
    *[(f"nested_bold_heading_h{level}_h{level + 1}", f"{'#' * level} **{{label}}**", f"{'#' * (level + 1)} **Work**\n") for level in range(1, 6)],
    # - Work: label does not end a heading exclusion
    *[(f"heading_h{level}_dash_work", f"{'#' * level} {{label}}", "- Work:\n") for level in range(1, 7)],
    *[(f"heading_h{level}_bold_dash_work", f"{'#' * level} **{{label}}**", "- Work:\n") for level in range(1, 7)],
]

EXCLUSION_FORMATS = [*INLINE_EXCLUSION_FORMATS, *HEADING_EXCLUSION_FORMATS]


@pytest.mark.parametrize("fmt_name,label_template,item_prefix", EXCLUSION_FORMATS,
                         ids=[row[0] for row in EXCLUSION_FORMATS])
@pytest.mark.parametrize("label", ["Do not touch", "Assumptions", "Deferred"])
@pytest.mark.parametrize("chain_length", [2, 3])
@pytest.mark.parametrize("verb,path_template", [("Import", "{path}"), ("Consume", "`{path}`")])
def test_exclusion_only_chains_require_affirmative_work_outside_label(
        staged_flood, tmp_path, fmt_name, label_template, item_prefix, label,
        chain_length, verb, path_template):
    """Every edge rejects excluded affirmative use, but qualifies when only its label is removed."""
    mod, _, _ = staged_flood
    pdir = tmp_path / f"plan_{fmt_name}"
    files = ["src/producer.ts", "src/intermediate.ts", "src/consumer.ts"]
    write_slice(pdir, "ITEM-s01", files=[files[0]])
    label_line = label_template.format(label=label)
    consumers = []
    for index in range(1, chain_length):
        consumer, producer = f"ITEM-s{index + 1:02}", f"ITEM-s{index:02}"
        path = path_template.format(path=files[index - 1])
        body = (f"Do not touch: none\nWork: implement {consumer}.\n"
                f"Done when: test_{consumer.replace('-', '_')}.py passes.\n"
                f"{label_line}\n{item_prefix}{verb} {path}.")
        write_slice(pdir, consumer, deps=[producer], files=[files[index]], body=body)
        consumers.append((consumer, producer, files[index], body))

    violations, _ = mod.validate_slices("ITEM", pdir)
    assert len(violations) == chain_length - 1, violations
    for consumer, producer, _, _ in consumers:
        expected = (f"{consumer}: depends_on {producer} orders the slice only; "
                    f"leave depends_on empty, edit a file {producer} also edits, or name a file "
                    f"or output of {producer} that this slice reads, imports, consumes or extends")
        assert expected in violations, violations

    # Keep clauses, files, deps and all other body lines identical: only tested label disappears.
    for consumer, producer, consumer_file, body in consumers:
        control = body.replace(f"{label_line}\n", "", 1)
        write_slice(pdir, consumer, deps=[producer], files=[consumer_file], body=control)
    violations, _ = mod.validate_slices("ITEM", pdir)
    assert violations == [], f"Label-removal control must qualify: {violations}"


@pytest.mark.parametrize("fmt_name,label_template,item_prefix", INLINE_EXCLUSION_FORMATS,
                         ids=[row[0] for row in INLINE_EXCLUSION_FORMATS])
@pytest.mark.parametrize("label", ["Do not touch", "Assumptions", "Deferred"])
@pytest.mark.parametrize("peer", ["Work:", "- Work:", "**Work**:", "# Work"])
@pytest.mark.parametrize("chain_length", [2, 3])
@pytest.mark.parametrize("verb,path_template", [("Import", "{path}"), ("Consume", "`{path}`")])
def test_inline_exclusions_resume_at_work_boundaries_and_qualify(
        staged_flood, tmp_path, fmt_name, label_template, item_prefix, label,
        peer, chain_length, verb, path_template):
    """Pair each inline exclusion with valid affirmative work after peer work labels and headings."""
    mod, _, _ = staged_flood
    pdir = tmp_path / f"resume_{fmt_name}_{peer.replace(' ', '_').replace('*', '').replace(':', '')}_{chain_length}"
    files = ["src/producer.ts", "src/intermediate.ts", "src/consumer.ts"]
    write_slice(pdir, "ITEM-s01", files=[files[0]])
    label_line = label_template.format(label=label)
    for index in range(1, chain_length):
        consumer, producer = f"ITEM-s{index + 1:02}", f"ITEM-s{index:02}"
        path = path_template.format(path=files[index - 1])
        body = (f"Do not touch: none\n"
                f"{label_line}\n{item_prefix}src/blocked.ts\n"
                f"{peer}\n{verb} {path}.\n"
                f"Done when: test_{consumer.replace('-', '_')}.py passes.")
        write_slice(pdir, consumer, deps=[producer], files=[files[index]], body=body)

    violations, _ = mod.validate_slices("ITEM", pdir)
    assert violations == [], f"Resumed work must qualify: {violations}"


MIDLINE_EXCLUSION_STYLES = [
    ("plain_colon", "{label}:"),
    ("bold_colon_inside", "**{label}:**"),
    ("bold_colon_outside", "**{label}**:"),
    ("italic_colon_inside", "*{label}:*"),
    ("italic_colon_outside", "*{label}*:"),
    ("underline_colon_inside", "__{label}:__"),
    ("underline_colon_outside", "__{label}__:"),
]


@pytest.mark.parametrize("style_name,style_template", MIDLINE_EXCLUSION_STYLES,
                         ids=[row[0] for row in MIDLINE_EXCLUSION_STYLES])
@pytest.mark.parametrize("label", ["Do not touch", "Assumptions", "Deferred"])
@pytest.mark.parametrize("chain_length", [2, 3])
@pytest.mark.parametrize("verb,path_template", [("Import", "{path}"), ("Consume", "`{path}`")])
@pytest.mark.parametrize("trailing", ["", "\nUnindented note src/tail.ts\n- src/list_blocked.ts"])
def test_midline_exclusions_preserve_prior_affirmative_work(
        staged_flood, tmp_path, style_name, style_template, label,
        chain_length, verb, path_template, trailing):
    """Work before a midline exclusion qualifies, while only blocked targets are excluded."""
    mod, _, _ = staged_flood
    pdir = tmp_path / f"midline_{style_name}_{chain_length}"
    files = ["src/producer.ts", "src/intermediate.ts", "src/consumer.ts"]
    write_slice(pdir, "ITEM-s01", files=[files[0]])
    midline = style_template.format(label=label)
    for index in range(1, chain_length):
        consumer, producer = f"ITEM-s{index + 1:02}", f"ITEM-s{index:02}"
        path = path_template.format(path=files[index - 1])
        body = (f"Do not touch: none\n"
                f"Work: implement {consumer}.\n"
                f"{verb} {path}. {midline} src/blocked.ts{trailing}\n"
                f"Done when: test_{consumer.replace('-', '_')}.py passes.")
        write_slice(pdir, consumer, deps=[producer], files=[files[index]], body=body)

    violations, _ = mod.validate_slices("ITEM", pdir)
    assert violations == [], f"Work before midline exclusion must qualify: {violations}"


@pytest.mark.parametrize("level", range(1, 7))
@pytest.mark.parametrize("label", ["Do not touch", "Assumptions", "Deferred"])
@pytest.mark.parametrize("heading_rel", ["same", "higher"])
@pytest.mark.parametrize("chain_length", [2, 3])
@pytest.mark.parametrize("verb,path_template", [("Import", "{path}"), ("Consume", "`{path}`")])
def test_heading_exclusions_resume_at_same_or_higher_work_heading(
        staged_flood, tmp_path, level, label, heading_rel, chain_length, verb, path_template):
    """A same- or higher-level work heading ends a heading exclusion, resuming affirmative work."""
    mod, _, _ = staged_flood
    pdir = tmp_path / f"heading_resume_{level}_{heading_rel}_{chain_length}"
    files = ["src/producer.ts", "src/intermediate.ts", "src/consumer.ts"]
    write_slice(pdir, "ITEM-s01", files=[files[0]])
    h = "#" * level
    work_h = "# Work" if heading_rel == "higher" else f"{h} Work"
    for index in range(1, chain_length):
        consumer, producer = f"ITEM-s{index + 1:02}", f"ITEM-s{index:02}"
        path = path_template.format(path=files[index - 1])
        body = (f"Do not touch: none\n"
                f"{h} {label}\nsrc/blocked.ts\n"
                f"{work_h}\n{verb} {path}.\n"
                f"Done when: test_{consumer.replace('-', '_')}.py passes.")
        write_slice(pdir, consumer, deps=[producer], files=[files[index]], body=body)

    violations, _ = mod.validate_slices("ITEM", pdir)
    assert violations == [], f"Work after same/higher heading must qualify: {violations}"


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
    ("Importing src/a.ts is not required.", False),
    ("Importing src/a.ts not required.", False),
    ("Importing src/a.ts is required.", True),
    ("Importing src/a.ts required.", True),
    ("Reading src/a.ts is not required.", False),
    ("Reading src/a.ts is required.", True),
    ("Consuming src/a.ts not required.", False),
    ("Consuming src/a.ts required.", True),
    ("Extending src/a.ts is not required.", False),
    ("Extending src/a.ts is required.", True),
    ("Do not read src/local.ts or import src/a.ts.", False),
    ("Read src/local.ts or import src/a.ts.", True),
    ("Do not read src/local.ts and import src/a.ts.", False),
    ("Read src/local.ts and import src/a.ts.", True),
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
    ("Importing src/a.ts is not required. Import src/a.ts.", True),
    ("Importing src/a.ts not required. Import src/a.ts.", True),
    ("Do not read src/local.ts or import src/a.ts. Import src/a.ts.", True),
    ("Do not read src/local.ts and import src/a.ts. Import src/a.ts.", True),
    ("Importing src/a.ts is not required, but import src/a.ts.", True),
    ("Import src/a.ts and never read src/local.ts.", True),
    ("Do not read src/local.ts and import src/a.ts; import src/a.ts.", True),
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


@pytest.mark.parametrize("work,producer_files,affirmative", [
    ("Do not read src/a.ts or src/b.ts.", ["src/a.ts", "src/b.ts"], False),
    ("Read src/a.ts or src/b.ts.", ["src/a.ts", "src/b.ts"], True),
    ("Do not read src/a.ts and src/b.ts.", ["src/a.ts", "src/b.ts"], False),
    ("Read src/a.ts and src/b.ts.", ["src/a.ts", "src/b.ts"], True),
    ("Do not read src/local.ts or import src/a.ts.", ["src/local.ts", "src/a.ts"], False),
    ("Read src/local.ts or import src/a.ts.", ["src/local.ts", "src/a.ts"], True),
    ("Do not read src/a.ts and consume src/b.ts.", ["src/a.ts", "src/b.ts"], False),
    ("Read src/a.ts and consume src/b.ts.", ["src/a.ts", "src/b.ts"], True),
    ("Reading src/a.ts and src/b.ts is not required.", ["src/a.ts", "src/b.ts"], False),
    ("Reading src/a.ts and src/b.ts is required.", ["src/a.ts", "src/b.ts"], True),
    ("Reading src/a.ts and consuming src/b.ts not required.", ["src/a.ts", "src/b.ts"], False),
    ("Reading src/a.ts and consuming src/b.ts required.", ["src/a.ts", "src/b.ts"], True),
])
def test_negation_covers_each_coordinated_producer(
        staged_flood, tmp_path, work, producer_files, affirmative):
    """Negated verbs/target lists cannot justify either producer's dependency edge."""
    mod, _, _ = staged_flood
    pdir = _plan_tree(tmp_path)
    write_slice(pdir, "ITEM-s01", files=[producer_files[0]])
    write_slice(pdir, "ITEM-s02", files=[producer_files[1]])
    write_slice(pdir, "ITEM-s03", deps=["ITEM-s01", "ITEM-s02"], files=["src/c.ts"],
                body=f"{work}\nDone when: test_ITEM_s03.py passes.\nDo not touch: none")
    violations, _ = mod.validate_slices("ITEM", pdir)
    assert violations == ([] if affirmative else [
        f"ITEM-s03: depends_on {producer} orders the slice only; leave depends_on empty, "
        f"edit a file {producer} also edits, or name a file or output of {producer} "
        "that this slice reads, imports, consumes or extends"
        for producer in ("ITEM-s01", "ITEM-s02")
    ])


@pytest.mark.parametrize("work,affirmative", [
    ("Do not apply omp584-dependencies.patch.", False),
    ("Apply omp584-dependencies.patch.", True),
    ("Applying omp584-dependencies.patch is not required.", False),
    ("Applying omp584-dependencies.patch is required.", True),
    ("Applying omp584-dependencies.patch not required.", False),
    ("Applying omp584-dependencies.patch required.", True),
    ("Do not extend src/local.ts or apply omp584-dependencies.patch.", False),
    ("Extend src/local.ts or apply omp584-dependencies.patch.", True),
])
def test_named_patch_application_requires_affirmative_evidence(staged_flood, tmp_path, work, affirmative):
    """Named patch outputs require affirmative application, including post-target predicates."""
    mod, _, _ = staged_flood
    pdir = _plan_tree(tmp_path)
    write_slice(pdir, "ITEM-s01", files=["src/producer.ts"], outputs=["omp584-dependencies.patch"])
    write_slice(pdir, "ITEM-s02", deps=["ITEM-s01"], files=["src/consumer.ts"],
                body=f"{work}\nDone when: test_ITEM_s02.py passes.\nDo not touch: none")
    violations, _ = mod.validate_slices("ITEM", pdir)
    assert violations == ([] if affirmative else [
        "ITEM-s02: depends_on ITEM-s01 orders the slice only; leave depends_on empty, "
        "edit a file ITEM-s01 also edits, or name a file or output of ITEM-s01 "
        "that this slice reads, imports, consumes or extends"
    ])


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
    write_slice(pdir_owner_after, "OMP-584-s01", deps=[], files=["src/a.ts"],
                body="Implement OMP-584-s01.\nDone when: test_ITEM_s01.py passes.\nDo not touch: none")
    write_slice(pdir_owner_after, "OMP-584-s02", deps=["OMP-584-s01"], files=[], owner=True,
                owner_cmd="omp-work approve --issue OMP-584",
                body="Owner runs: omp-work approve --issue OMP-584\nEvidence: ok\nDone when: passes.")
    violations, slices = mod.validate_slices("OMP-584", pdir_owner_after)
    assert slices[1]["owner_cmd"] == "omp-work approve --issue OMP-584"
    assert violations == [], f"Owner slice depending on implementer slice must pass: {violations}"

    pdir_owner_before = tmp_path / "plan_owner_before"
    write_slice(pdir_owner_before, "ITEM-s01", deps=[], files=[], owner=True,
                outputs=["artifacts/result.json"],
                body="Owner runs: check\nEvidence: artifacts/result.json\nDone when: passes.")
    write_slice(pdir_owner_before, "ITEM-s02", deps=["ITEM-s01"], files=["src/b.ts"],
                body="Consume artifacts/result.json.\nDone when: test_ITEM_s02.py passes.\nDo not touch: none")
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
    assert originals == {selected / "flood.py": _snapshot(selected / "flood.py")}


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
    snapshots = {selected / name: _snapshot(selected / name) for name in ("flood.py", "helper.py")}
    monkeypatch.setenv("OMP584_FLOOD_SOURCE", str(selected))

    def reject_patch(*args, **kwargs):
        pytest.fail("already-patched selected source must not invoke patch")

    monkeypatch.setattr(subprocess, "run", reject_patch)
    stage = tmp_path / "exec"
    stage.mkdir()
    dest, originals = _resolve_stage(stage)
    mod = _load(dest)

    assert Path(mod.__file__).resolve().parent == stage
    assert originals == snapshots
    assert (stage / "helper.py").read_bytes() == before[Path("helper.py")]
    assert (stage / "executed.txt").read_text() == "VALUE = 41\n"
    after = {p.relative_to(selected): p.read_bytes() for p in selected.rglob("*") if p.is_file()}
    assert after == before, "selected source or its bytecode changed during staging/execution"
    assert {path: _snapshot(path) for path in originals} == originals


def test_raw_stage_preserves_source_support_and_patch_hashes(tmp_path, monkeypatch):
    """Loading the patched gate leaves all raw inputs byte-identical, with unchanged mtimes."""
    if (STAGE_DIR / "flood.py").is_file():
        monkeypatch.delenv("OMP584_FLOOD_SOURCE", raising=False)
        src_dir = STAGE_DIR
        raw_patch = STAGE_DIR / PATCH.name if (STAGE_DIR / PATCH.name).is_file() else PATCH
        sources = [src_dir / "flood.py", *(src_dir / name for name in _support_files(src_dir)), raw_patch]
    elif selected := os.environ.get("OMP584_FLOOD_SOURCE"):
        src_dir = Path(selected).resolve()
        sources = [src_dir / "flood.py", *(src_dir / name for name in _support_files(src_dir))]
    else:
        pytest.skip("no raw stage or OMP584_FLOOD_SOURCE supplies flood.py")

    before = {path: (path.read_bytes(), _snapshot(path)) for path in sources}
    stage = tmp_path / "exec"
    stage.mkdir()
    dest, originals = _resolve_stage(stage)
    mod = _load(dest)
    pdir = tmp_path / "plan"
    write_slice(pdir, "ITEM-s01", files=["src/a.ts"])
    write_slice(pdir, "ITEM-s02", deps=["ITEM-s01"], files=["src/b.ts"])
    violations, _ = mod.validate_slices("ITEM", pdir)
    assert any("ITEM-s02: depends_on ITEM-s01 orders the slice only" in v for v in violations)

    assert originals == {path: snapshot for path, (_, snapshot) in before.items()}
    assert {path: (path.read_bytes(), _snapshot(path)) for path in sources} == before
    for name in _support_files(src_dir):
        assert (stage / name).read_bytes() == before[src_dir / name][0]
    for pattern in ("__pycache__", "*.rej", "*.orig"):
        assert not list(src_dir.rglob(pattern)), f"staging left {pattern} in source directory"


def _reviewed_plan(mod, cfg: Path):
    """Load a real parent and persist the eligible plan-review state before passing it."""
    parent = cfg.parent / "tasks" / "ITEM.md"
    parent.write_text("---\nid: ITEM\nkind: plan\n---\nPlan independent implementation slices.\n")
    f = mod.Flood(json.loads(cfg.read_text()), cfg.parent)
    f.state.task("ITEM").update(status="running", phase="plan-review", attempts=1)
    f.state.save()
    return f


def _assert_proposed_unchanged(f, pdir: Path, proposed: dict[str, bytes]):
    assert {p.name: p.read_bytes() for p in pdir.iterdir()} == proposed
    assert all(not (f.tasks_dir / name).exists() for name in proposed)


def _assert_promoted(mod, cfg: Path, proposed: dict[str, bytes]):
    """Reload persisted parent/slices and verify all proposed bytes moved intact."""
    saved = mod.Flood(json.loads(cfg.read_text()), cfg.parent)
    st = saved.state.task("ITEM")
    assert st["status"] == "planned", st
    assert st["slices"] == ["ITEM-s01", "ITEM-s02"]
    assert all(sid in saved.tasks for sid in st["slices"])
    assert {name: (saved.tasks_dir / name).read_bytes() for name in proposed} == proposed
    assert not list(saved.proposed_dir("ITEM").iterdir())


def _persist_plan_ready(mod, cfg: Path):
    """Write ITEM as plan_ready before CLI promote, so the status check is not the refusal."""
    parent = cfg.parent / "tasks" / "ITEM.md"
    parent.parent.mkdir(parents=True, exist_ok=True)
    if not parent.exists():
        parent.write_text("---\nid: ITEM\nkind: plan\n---\nPlan independent implementation slices.\n")
    flood = mod.Flood(json.loads(cfg.read_text()), cfg.parent)
    flood.state.task("ITEM").update(status="plan_ready", phase=None, job=None, attempts=1, last_error=None)
    flood.state.save()
    saved = json.loads((cfg.parent / "state.json").read_text())
    assert saved["tasks"]["ITEM"]["status"] == "plan_ready"
    return flood


def _flood_cli(flood_py: Path, cfg: Path, command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(flood_py), command, "ITEM", "--config", str(cfg)],
        capture_output=True, text=True,
    )


def _clear_proposed(pdir: Path) -> None:
    for child in pdir.iterdir():
        child.unlink()


def _assert_texts(haystack: str, expected: str | tuple[str, ...]) -> None:
    for text in ((expected,) if isinstance(expected, str) else expected):
        assert text in haystack, haystack


def _assert_promotion_refused(mod, cfg: Path, flood_py: Path, pdir: Path, proposed: dict[str, bytes],
                              diagnostic: str | tuple[str, ...]) -> None:
    """check-plan, CLI promote, direct promote_plan and marker settlement all stop at the gate."""
    _persist_plan_ready(mod, cfg)
    marker = pdir / "PROMOTE"
    marker.unlink(missing_ok=True)

    checked = _flood_cli(flood_py, cfg, "check-plan")
    assert checked.returncode == 1, (checked.stdout, checked.stderr)
    _assert_texts(checked.stdout, diagnostic)
    assert not marker.exists()
    assert {path.name: path.read_bytes() for path in pdir.glob("*.md")} == proposed

    refused = _flood_cli(flood_py, cfg, "promote")
    assert refused.returncode == 1, (refused.stdout, refused.stderr)
    assert "plan gate failed:" in refused.stdout
    _assert_texts(refused.stdout, diagnostic)
    assert not marker.exists()
    held = mod.Flood(json.loads(cfg.read_text()), cfg.parent)
    assert held.state.task("ITEM")["status"] == "plan_ready"
    assert {path.name: path.read_bytes() for path in pdir.glob("*.md")} == proposed
    assert all(not (held.tasks_dir / name).exists() for name in proposed)

    marker.touch()
    held.promote_plan(held.tasks["ITEM"])
    st = held.state.task("ITEM")
    assert st["status"] == "plan_ready", st
    assert not st.get("slices")
    assert st["last_error"].startswith("plan gate failed:")
    _assert_texts(st["last_error"], diagnostic)
    assert marker.is_file()
    assert {path.name: path.read_bytes() for path in pdir.glob("*.md")} == proposed
    assert all(not (held.tasks_dir / name).exists() for name in proposed)
    saved = mod.Flood(json.loads(cfg.read_text()), cfg.parent)
    assert saved.state.task("ITEM")["status"] == "plan_ready"
    assert saved.state.task("ITEM")["last_error"] == st["last_error"]
    saved.settle_plans()
    assert saved.state.task("ITEM")["status"] == "plan_ready"
    assert not saved.state.task("ITEM").get("slices")
    _assert_texts(saved.state.task("ITEM")["last_error"], diagnostic)
    assert marker.is_file()
    assert {path.name: path.read_bytes() for path in pdir.glob("*.md")} == proposed
    assert all(sid not in saved.tasks for sid in ("ITEM-s01", "ITEM-s02"))


def _manual_promotion(staged_flood, tmp_path: Path, *, consume: bool):
    mod, _, _ = staged_flood
    repo = _make_repo(tmp_path)
    cfg = _make_config(tmp_path, repo, promotion="manual")
    flood_py = Path(mod.__file__).resolve()
    pdir = _plan_tree(tmp_path)
    write_slice(pdir, "ITEM-s01", files=["src/a.ts"])
    write_slice(pdir, "ITEM-s02", deps=["ITEM-s01"], files=["src/b.ts"])
    proposed = {p.name: p.read_bytes() for p in pdir.iterdir()}
    f = _reviewed_plan(mod, cfg)
    f.plan_passed(f.tasks["ITEM"])
    assert mod.Flood(json.loads(cfg.read_text()), cfg.parent).state.task("ITEM")["status"] == "plan_ready"
    f.settle_plans()
    _assert_proposed_unchanged(f, pdir, proposed)

    def cli(command: str):
        return subprocess.run(
            [sys.executable, str(flood_py), command, "ITEM", "--config", str(cfg)],
            capture_output=True, text=True,
        )

    expected = ("ITEM-s02: depends_on ITEM-s01 orders the slice only; leave depends_on empty, "
                "edit a file ITEM-s01 also edits, or name a file or output of ITEM-s01 "
                "that this slice reads, imports, consumes or extends")
    invalid = cli("check-plan")
    assert invalid.returncode == 1, (invalid.stdout, invalid.stderr)
    assert expected in invalid.stdout
    promote = cli("promote")
    assert promote.returncode != 0, (promote.stdout, promote.stderr)
    assert expected in promote.stdout
    assert not (pdir / "PROMOTE").exists()
    reloaded = mod.Flood(json.loads(cfg.read_text()), cfg.parent)
    reloaded.settle_plans()
    assert reloaded.state.task("ITEM")["status"] == "plan_ready"
    _assert_proposed_unchanged(reloaded, pdir, proposed)

    write_slice(pdir, "ITEM-s02", deps=["ITEM-s01"] if consume else [], files=["src/b.ts"],
                body=("Import src/a.ts into src/b.ts." if consume else "Implement independent widget.")
                     + "\nDone when: test_consumer.py passes.\nDo not touch: none")
    repaired = {p.name: p.read_bytes() for p in pdir.iterdir()}
    valid = cli("check-plan")
    assert valid.returncode == 0, (valid.stdout, valid.stderr)
    assert valid.stdout.startswith("OK: 2 slices pass the plan gate")
    reloaded = mod.Flood(json.loads(cfg.read_text()), cfg.parent)
    reloaded.settle_plans()
    assert reloaded.state.task("ITEM")["status"] == "plan_ready"
    _assert_proposed_unchanged(reloaded, pdir, repaired)
    approved = cli("promote")
    assert approved.returncode == 0, (approved.stdout, approved.stderr)
    assert (pdir / "PROMOTE").is_file()
    assert all(not (f.tasks_dir / name).exists() for name in repaired)
    assert {p.name: p.read_bytes() for p in pdir.glob("*.md")} == repaired
    reloaded = mod.Flood(json.loads(cfg.read_text()), cfg.parent)
    assert reloaded.state.task("ITEM")["status"] == "plan_ready"
    reloaded.settle_plans()
    _assert_promoted(mod, cfg, repaired)


def test_check_plan_cli_uses_the_gate_and_manual_promote_blocks_invalid(staged_flood, tmp_path):
    """CLI refuses an order-only plan, then approves repaired independent roots for settlement."""
    _manual_promotion(staged_flood, tmp_path, consume=False)


def test_auto_plan_promotion_refuses_disjoint_order_only_chain(staged_flood, tmp_path):
    """Auto plan_passed preserves invalid proposals; a repaired review pass moves every slice."""
    mod, _, _ = staged_flood
    repo = _make_repo(tmp_path)
    cfg = _make_config(tmp_path, repo, promotion="auto")
    pdir = _plan_tree(tmp_path)
    write_slice(pdir, "ITEM-s01", files=["src/a.ts"])
    write_slice(pdir, "ITEM-s02", deps=["ITEM-s01"], files=["src/b.ts"])
    proposed = {p.name: p.read_bytes() for p in pdir.iterdir()}
    f = _reviewed_plan(mod, cfg)
    f.plan_passed(f.tasks["ITEM"])

    st = f.state.task("ITEM")
    assert st["status"] == "plan_ready", st
    assert st["last_error"].startswith("plan gate failed:")
    assert "ITEM-s02: depends_on ITEM-s01 orders the slice only" in st["last_error"]
    assert "that this slice reads, imports, consumes or extends" in st["last_error"]
    saved = mod.Flood(json.loads(cfg.read_text()), cfg.parent).state.task("ITEM")
    assert saved["status"] == "plan_ready"
    assert saved["last_error"] == st["last_error"]
    _assert_proposed_unchanged(f, pdir, proposed)

    write_slice(pdir, "ITEM-s02", files=["src/b.ts"])
    repaired = {p.name: p.read_bytes() for p in pdir.iterdir()}
    f.state.task("ITEM").update(status="running", phase="plan-review", attempts=2)
    f.state.save()
    f.plan_passed(f.tasks["ITEM"])
    _assert_promoted(mod, cfg, repaired)


def test_manual_plan_promotion_waits_then_promotes_only_a_valid_plan(staged_flood, tmp_path):
    """Manual settlement waits for CLI approval after genuine producer consumption repairs an edge."""
    _manual_promotion(staged_flood, tmp_path, consume=True)


def test_promotion_entry_paths_keep_invalid_proposals_and_promote_supported(staged_flood, tmp_path):
    """plan_ready still rechecks every entry, including promote_plan when PROMOTE already exists."""
    mod, _, _ = staged_flood
    repo = _make_repo(tmp_path)
    cfg = _make_config(tmp_path, repo, promotion="manual")
    flood_py = Path(mod.__file__).resolve()
    pdir = _plan_tree(tmp_path)
    order_only = (
        "ITEM-s02: depends_on ITEM-s01 orders the slice only; leave depends_on empty, "
        "edit a file ITEM-s01 also edits, or name a file or output of ITEM-s01 "
        "that this slice reads, imports, consumes or extends"
    )

    def cases():
        _clear_proposed(pdir)
        write_slice(pdir, "ITEM-s01", files=["src/a.ts"])
        write_slice(pdir, "ITEM-s02", deps=["ITEM-s01"], files=["src/b.ts"])
        yield order_only

        _clear_proposed(pdir)
        write_slice(pdir, "ITEM-s01", deps=["MISSING"], files=["src/a.ts"])
        yield "ITEM-s01: depends on MISSING, which is not in this set"

        _clear_proposed(pdir)
        write_slice(pdir, "ITEM-s01", deps=["ITEM-s02"], files=["src/a.ts"])
        write_slice(pdir, "ITEM-s02", deps=["ITEM-s01"], files=["src/b.ts"])
        yield "dependency cycle through"

        _clear_proposed(pdir)
        write_slice(pdir, "ITEM-s01", files=["src/shared.ts"])
        write_slice(pdir, "ITEM-s02", files=["src/shared.ts"])
        yield "ITEM-s01 and ITEM-s02 both edit src/shared.ts but neither depends on the other"

        _clear_proposed(pdir)
        broken = write_slice(pdir, "ITEM-s01", files=["src/a.ts"])
        broken.write_text(broken.read_text().replace("max_minutes: 30", "max_minutes: soon"))
        yield "ITEM-s01.md: front matter does not parse"

        _clear_proposed(pdir)
        write_slice(pdir, "ITEM-s01", owner=True, body="Done when: the owner records the result.")
        yield (
            'ITEM-s01: owner slice has no "Owner runs:" section',
            'ITEM-s01: owner slice has no "Evidence:" section',
        )

    for diagnostic in cases():
        proposed = {path.name: path.read_bytes() for path in pdir.glob("*.md")}
        _assert_promotion_refused(mod, cfg, flood_py, pdir, proposed, diagnostic)

    _clear_proposed(pdir)
    write_slice(pdir, "ITEM-s01", files=["src/a.ts"])
    write_slice(pdir, "ITEM-s02", deps=["ITEM-s01"], files=["src/b.ts"],
                body="Import src/a.ts into src/b.ts.\nDone when: test_consumer.py passes.\nDo not touch: none")
    proposed = {path.name: path.read_bytes() for path in pdir.glob("*.md")}
    preexisting = cfg.parent / "tasks" / "ITEM-s01.md"
    preexisting.write_text("---\nid: ITEM-s01\nkind: implement\n---\npreexisting\n")
    preexisting_bytes = preexisting.read_bytes()
    _persist_plan_ready(mod, cfg)
    marker = pdir / "PROMOTE"
    marker.unlink(missing_ok=True)

    valid = _flood_cli(flood_py, cfg, "check-plan")
    assert valid.returncode == 0, (valid.stdout, valid.stderr)
    assert valid.stdout.startswith("OK: 2 slices pass the plan gate")
    approved = _flood_cli(flood_py, cfg, "promote")
    assert approved.returncode == 0, (approved.stdout, approved.stderr)
    assert marker.is_file()
    assert {path.name: path.read_bytes() for path in pdir.glob("*.md")} == proposed
    blocked = mod.Flood(json.loads(cfg.read_text()), cfg.parent)
    assert blocked.state.task("ITEM")["status"] == "plan_ready"
    blocked.promote_plan(blocked.tasks["ITEM"])
    assert blocked.state.task("ITEM")["status"] == "plan_ready"
    assert not blocked.state.task("ITEM").get("slices")
    assert blocked.state.task("ITEM")["last_error"] == "promotion blocked: tasks/ already has ['ITEM-s01.md']"
    assert preexisting.read_bytes() == preexisting_bytes
    assert {path.name: path.read_bytes() for path in pdir.glob("*.md")} == proposed
    assert not (blocked.tasks_dir / "ITEM-s02.md").exists()
    assert marker.is_file()

    preexisting.unlink()
    marker.unlink()
    _persist_plan_ready(mod, cfg)
    approved = _flood_cli(flood_py, cfg, "promote")
    assert approved.returncode == 0, (approved.stdout, approved.stderr)
    assert marker.is_file()
    assert {path.name: path.read_bytes() for path in pdir.glob("*.md")} == proposed
    assert all(not (cfg.parent / "tasks" / name).exists() for name in proposed)
    ready = mod.Flood(json.loads(cfg.read_text()), cfg.parent)
    assert ready.state.task("ITEM")["status"] == "plan_ready"
    ready.settle_plans()
    _assert_promoted(mod, cfg, proposed)


@pytest.mark.parametrize("promotion", ["auto", "manual"])
@pytest.mark.parametrize("source", ["outputs", "evidence", "both"])
@pytest.mark.parametrize("invalid_edge", ["producer_exclusion", "consumer_exclusion", "unnamed_result"])
def test_owner_result_promotion_refuses_invalid_evidence_then_promotes_repair(
        staged_flood, tmp_path, promotion, source, invalid_edge):
    """Every entry path preserves invalid owner proposals and promotes only a consumed, eligible result."""
    mod, _, _ = staged_flood
    repo = _make_repo(tmp_path)
    cfg = _make_config(tmp_path, repo, promotion=promotion)
    pdir = _plan_tree(tmp_path)
    result = "artifacts/result.json"

    def proposal(*, repaired: bool):
        producer_exclusion = result if not repaired and invalid_edge == "producer_exclusion" else "none"
        consumer_exclusion = result if not repaired and invalid_edge == "consumer_exclusion" else "none"
        work = ("Implement independent widget." if not repaired and invalid_edge == "unnamed_result"
                else f"Consume {result} to implement widget.")
        evidence = result if source in {"evidence", "both"} else "check complete"
        write_slice(pdir, "ITEM-s01", owner=True,
                    outputs=[result] if source in {"outputs", "both"} else [],
                    body=f"Owner runs: check\nEvidence: {evidence}\n"
                         f"Done when: passes.\nDo not touch: {producer_exclusion}")
        write_slice(pdir, "ITEM-s02", deps=["ITEM-s01"], files=["src/consumer.ts"],
                    body=f"{work}\nDone when: test_consumer.py passes.\nDo not touch: {consumer_exclusion}")
        return {p.name: p.read_bytes() for p in pdir.iterdir()}

    def cli(command: str):
        return subprocess.run(
            [sys.executable, str(Path(mod.__file__).resolve()), command, "ITEM", "--config", str(cfg)],
            capture_output=True, text=True,
        )

    proposed = proposal(repaired=False)
    f = _reviewed_plan(mod, cfg)
    expected = "ITEM-s02: depends_on ITEM-s01 lacks affirmative owner result evidence"
    invalid = cli("check-plan")
    assert invalid.returncode == 1, (invalid.stdout, invalid.stderr)
    assert len(invalid.stdout.splitlines()) == 1, invalid.stdout
    assert expected in invalid.stdout
    _assert_proposed_unchanged(f, pdir, proposed)

    f.plan_passed(f.tasks["ITEM"])
    reloaded = mod.Flood(json.loads(cfg.read_text()), cfg.parent)
    st = reloaded.state.task("ITEM")
    assert st["status"] == "plan_ready", st
    if promotion == "auto":
        assert st["last_error"].startswith("plan gate failed:")
        assert expected in st["last_error"]
        assert st["last_error"] == f.state.task("ITEM")["last_error"]
    else:
        refused = cli("promote")
        assert refused.returncode != 0, (refused.stdout, refused.stderr)
        assert expected in refused.stdout
    assert not (pdir / "PROMOTE").exists()
    reloaded.settle_plans()
    saved = mod.Flood(json.loads(cfg.read_text()), cfg.parent)
    assert saved.state.task("ITEM")["status"] == "plan_ready"
    assert all(sid not in saved.tasks for sid in ["ITEM-s01", "ITEM-s02"])
    if promotion == "auto":
        assert saved.state.task("ITEM")["last_error"] == st["last_error"]
    _assert_proposed_unchanged(saved, pdir, proposed)

    repaired = proposal(repaired=True)
    valid = cli("check-plan")
    assert valid.returncode == 0, (valid.stdout, valid.stderr)
    assert valid.stdout.startswith("OK: 2 slices pass the plan gate")
    _assert_proposed_unchanged(saved, pdir, repaired)
    if promotion == "auto":
        saved.state.task("ITEM").update(status="running", phase="plan-review", attempts=2)
        saved.state.save()
        saved.plan_passed(saved.tasks["ITEM"])
    else:
        saved.settle_plans()
        _assert_proposed_unchanged(saved, pdir, repaired)
        approved = cli("promote")
        assert approved.returncode == 0, (approved.stdout, approved.stderr)
        assert (pdir / "PROMOTE").is_file()
        assert {p.name: p.read_bytes() for p in pdir.glob("*.md")} == repaired
        saved = mod.Flood(json.loads(cfg.read_text()), cfg.parent)
        assert saved.state.task("ITEM")["status"] == "plan_ready"
        assert all(not (saved.tasks_dir / name).exists() for name in repaired)
        saved.settle_plans()
    _assert_promoted(mod, cfg, repaired)


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


@pytest.mark.parametrize("source,work,producer_exclusion,consumer_exclusion,passes", [
    ("evidence", "Consume artifacts/result.json.", "none", "none", True),
    ("outputs", "Consume artifacts/result.json.", "none", "none", True),
    ("missing", "Consume artifacts/result.json.", "none", "none", False),
    ("evidence", "Implement independent widget.", "none", "none", False),
    ("evidence", "Consume artifacts/unrelated.json.", "none", "none", False),
    ("outputs", "Consume result.json.", "none", "none", False),
    ("evidence", "Do not consume artifacts/result.json.", "none", "none", False),
    ("outputs", "We may consume artifacts/result.json later.", "none", "none", False),
    ("evidence", "Consume artifacts/result.json for reference.", "none", "none", False),
    ("outputs", "Consuming artifacts/result.json is not required.", "none", "none", False),
    ("evidence", "Read artifacts/unrelated.json; artifacts/result.json stays unchanged.",
     "none", "none", False),
    ("evidence", "Consume artifacts/result.json.", "artifacts/result.json", "none", False),
    ("outputs", "Consume artifacts/result.json.", "artifacts/result.json", "none", False),
    ("evidence", "Consume artifacts/result.json.", "none", "artifacts/result.json", False),
    ("outputs", "Consume artifacts/result.json.", "none", "artifacts/result.json", False),
    ("evidence", "Do not consume artifacts/result.json. Consume artifacts/result.json.",
     "none", "none", True),
])
def test_owner_result_edges_require_affirmative_unexcluded_result(
        staged_flood, tmp_path, source, work, producer_exclusion, consumer_exclusion, passes):
    """Owner ordering needs named consumption; exclusions override even declared metadata outputs."""
    mod, _, _ = staged_flood
    pdir = _plan_tree(tmp_path)
    evidence = "artifacts/result.json" if source == "evidence" else "check complete"
    write_slice(pdir, "ITEM-s01", owner=True, files=["artifacts/result.json"],
                outputs=["artifacts/result.json"] if source == "outputs" else [],
                body=f"Owner runs: check\nEvidence: {evidence}\nDone when: passes.\n"
                     f"Do not touch: {producer_exclusion}")
    write_slice(pdir, "ITEM-s02", deps=["ITEM-s01"], files=["src/consumer.ts"],
                body=f"{work}\nDone when: test_consumer.py passes.\n"
                     f"Do not touch: {consumer_exclusion}")
    violations, _ = mod.validate_slices("ITEM", pdir)
    if passes:
        assert violations == []
    else:
        assert len(violations) == 1
        assert "ITEM-s02: depends_on ITEM-s01" in violations[0]
        assert "owner result evidence" in violations[0]


def test_unrelated_owner_result_cannot_justify_another_owner_edge(staged_flood, tmp_path):
    """Consuming one owner's result cannot justify a second, unrelated owner dependency."""
    mod, _, _ = staged_flood
    pdir = _plan_tree(tmp_path)
    for sid, result in [("ITEM-s01", "artifacts/first.json"), ("ITEM-s02", "artifacts/second.json")]:
        write_slice(pdir, sid, owner=True, outputs=[result])
    write_slice(pdir, "ITEM-s03", deps=["ITEM-s01", "ITEM-s02"], files=["src/consumer.ts"],
                body="Consume artifacts/first.json.\nDone when: test_consumer.py passes.\nDo not touch: none")
    violations, _ = mod.validate_slices("ITEM", pdir)
    assert len(violations) == 1
    assert "ITEM-s03: depends_on ITEM-s02" in violations[0]
    assert "evidence" in violations[0]


@pytest.mark.parametrize("owner,command,metadata,passes", [
    (True, "omp-work approve --issue OMP-584", True, True),
    (True, "omp-work approve --issue OMP-584", False, True),
    (True, "\n- `omp-work approve --issue OMP-584`", False, True),
    (True, "omp-work approve --issue OMP-999", True, False),
    (True, "approve", True, False),
    (True, "Do not run omp-work approve --issue OMP-584", False, False),
    (False, "omp-work approve --issue OMP-584", True, False),
    (True, "omp-work approve --issue OMP-584 --dry-run", True, False),
])
def test_approval_edges_require_owner_command_for_linked_issue(
        staged_flood, tmp_path, owner, command, metadata, passes):
    """Real synthetic owner approvals permit edges; approval filenames and unrelated commands do not."""
    mod, _, _ = staged_flood
    pdir = _plan_tree(tmp_path, "OMP-584")
    write_slice(pdir, "OMP-584-s01", owner=owner, files=["artifacts/approval.json"],
                owner_cmd=command if metadata else None,
                body=f"Owner runs: {command if not metadata else 'check'}\nEvidence: check complete\n"
                     "Done when: test_approval.py passes.\nDo not touch: none")
    write_slice(pdir, "OMP-584-s02", deps=["OMP-584-s01"], files=["src/approval-widget.ts"])
    violations, _ = mod.validate_slices("OMP-584", pdir)
    if passes:
        assert violations == []
    else:
        assert len(violations) == 1
        assert "OMP-584-s02: depends_on OMP-584-s01" in violations[0]


@pytest.mark.parametrize("final", [True, False])
def test_changelog_aggregation_requires_all_other_slices(staged_flood, tmp_path, final):
    """Changelog exemption applies only to aggregation after every other slice."""
    mod, _, _ = staged_flood
    pdir = _plan_tree(tmp_path)
    write_slice(pdir, "ITEM-s01", files=["src/a.ts"])
    write_slice(pdir, "ITEM-s02", owner=True)
    write_slice(pdir, "ITEM-s03", deps=["ITEM-s01", "ITEM-s02"] if final else ["ITEM-s01"],
                files=["CHANGELOG.md"])
    violations, _ = mod.validate_slices("ITEM", pdir)
    if final:
        assert violations == []
    else:
        assert any("touches the changelog but is not the last slice" in v for v in violations)
        assert any("ITEM-s03: depends_on ITEM-s01 orders the slice only" in v for v in violations)



def test_owner_approval_links_nested_slices_to_root_issue(staged_flood, tmp_path):
    """Nested slice IDs still use the root issue's approval command, as Flood does."""
    mod, _, _ = staged_flood
    pdir = _plan_tree(tmp_path, "OMP-584-s01")
    write_slice(pdir, "OMP-584-s01-s01", owner=True,
                owner_cmd="omp-work approve --issue OMP-584")
    write_slice(pdir, "OMP-584-s01-s02", deps=["OMP-584-s01-s01"], files=["src/widget.ts"])
    violations, _ = mod.validate_slices("OMP-584-s01", pdir)
    assert violations == []

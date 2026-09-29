"""Project, ADR and repository context items (OMP-419).

Four sources feed project-related context:
- Project refs (decisions, roadmaps)
- Finished project missions and their latest history summaries
- Concluded research campaigns
- Architectural Decision Records (ADRs) from docs/adr/
- Repository state (origin URL, HEAD sha, branch, and status)
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess  # nosec B404 - invokes git to resolve checkout state
from typing import Literal

from omp_work.knowledge_source import normalize_remote_url
from omp_work.v1.models import StrictModel

from .models import ContextItem

RefKind = Literal["decision", "roadmap"]

MissionStatus = Literal[
    "draft",
    "awaiting_confirmation",
    "approved",
    "running",
    "paused",
    "blocked",
    "completed",
    "failed",
    "abandoned",
]


class ProjectRef(StrictModel):
    """Reference to a project-level decision or roadmap item."""

    kind: RefKind
    ref: str
    title: str


class ProjectMission(StrictModel):
    """A mission attached to the project."""

    mission_id: str
    objective: str
    status: MissionStatus


class ProjectHistory(StrictModel):
    """One event or status transition in a mission's history."""

    mission_id: str
    kind: str
    summary: str


class ProjectResearch(StrictModel):
    """A concluded research campaign."""

    campaign_id: str
    domain: str
    outcome: str
    outcome_reason: str | None = None


# Helpful aliases for consumer flexibility
MissionHistory = ProjectHistory
ProjectCampaign = ProjectResearch
ResearchCampaign = ProjectResearch


class ProjectContext(StrictModel):
    """Context state of an owner project."""

    refs: tuple[ProjectRef, ...] = ()
    missions: tuple[ProjectMission, ...] = ()
    history: tuple[ProjectHistory, ...] = ()
    research: tuple[ProjectResearch, ...] = ()


def project_items(ctx: ProjectContext | None = None) -> tuple[ContextItem, ...]:
    """Emit context items for project refs, finished missions, and research."""
    if ctx is None:
        return ()

    items: list[ContextItem] = []

    # 1. source decision|roadmap (ref=ref, text=title)
    for ref in ctx.refs:
        items.append(
            ContextItem(
                section="exact",
                source=ref.kind,
                ref=ref.ref,
                text=ref.title,
                status="current",
                mandatory=False,
            )
        )

    # 2. source mission only for status completed|failed|abandoned
    # (ref=mission_id, text "<objective> — <status>" plus "; <summary>" of that mission's last history row)
    for mission in ctx.missions:
        if mission.status in ("completed", "failed", "abandoned"):
            history_rows = [h for h in ctx.history if h.mission_id == mission.mission_id]
            text = f"{mission.objective} \u2014 {mission.status}"
            if history_rows:
                text = f"{text}; {history_rows[-1].summary}"
            items.append(
                ContextItem(
                    section="exact",
                    source="mission",
                    ref=mission.mission_id,
                    text=text,
                    status="current",
                    mandatory=False,
                )
            )

    # 3. source research (ref=campaign_id, text "<domain>: <outcome>" plus " — <reason>" when set)
    for res in ctx.research:
        text = f"{res.domain}: {res.outcome}"
        if res.outcome_reason is not None:
            text = f"{text} \u2014 {res.outcome_reason}"
        items.append(
            ContextItem(
                section="exact",
                source="research",
                ref=res.campaign_id,
                text=text,
                status="current",
                mandatory=False,
            )
        )

    return tuple(items)


def _git_run(cwd: str | Path, args: list[str]) -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    try:
        result = subprocess.run(  # nosec B603
            [git, "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _git_toplevel(cwd: str | Path) -> Path | None:
    out = _git_run(cwd, ["rev-parse", "--show-toplevel"])
    if out is None or not out.strip():
        return None
    return Path(out.strip())


def _extract_adr_text(content: str) -> str | None:
    """Extract first '# ' heading and the first paragraph after it, whitespace collapsed."""
    lines = content.splitlines()
    heading: str | None = None
    paragraph_lines: list[str] = []
    in_paragraph = False

    for line in lines:
        stripped = line.strip()
        if heading is None:
            if stripped.startswith("# "):
                heading = stripped.removeprefix("# ").strip()
        else:
            if not in_paragraph:
                if not stripped:
                    continue
                if stripped.startswith("#"):
                    break
                in_paragraph = True
                paragraph_lines.append(stripped)
            else:
                if not stripped or stripped.startswith("#"):
                    break
                paragraph_lines.append(stripped)

    if heading is None:
        return None

    if paragraph_lines:
        paragraph = " ".join(paragraph_lines)
        return " ".join(f"{heading} {paragraph}".split())
    return " ".join(heading.split())


def adr_items(cwd: str | Path) -> tuple[ContextItem, ...]:
    """ADR items from docs/adr/*.md: first '# ' heading and first paragraph, whitespace collapsed."""
    toplevel = _git_toplevel(cwd)
    root = toplevel if toplevel is not None else Path(cwd)
    adr_dir = root / "docs" / "adr"
    if not adr_dir.is_dir():
        return ()

    items: list[ContextItem] = []
    for file_path in sorted(adr_dir.glob("*.md"), key=lambda p: p.name):
        if not file_path.is_file():
            continue
        try:
            content = file_path.read_text(encoding="utf-8")
        except OSError:
            continue
        text = _extract_adr_text(content)
        if text is None:
            continue
        items.append(
            ContextItem(
                section="exact",
                source="adr",
                ref=f"docs/adr/{file_path.name}",
                text=text,
                status="current",
                mandatory=False,
            )
        )
    return tuple(items)


def repository_items(cwd: str | Path) -> tuple[ContextItem, ...]:
    """Repository state item: HEAD sha, origin URL or toplevel name, HEAD, branch, changed paths or 'clean'."""
    head_out = _git_run(cwd, ["rev-parse", "HEAD"])
    if head_out is None or not head_out.strip():
        return ()
    head_sha = head_out.strip()

    # 1. origin URL (else toplevel name)
    origin_out = _git_run(cwd, ["remote", "get-url", "origin"])
    origin_url: str | None = None
    if origin_out is not None and origin_out.strip():
        origin_url = normalize_remote_url(origin_out.strip())

    if origin_url is not None:
        repo_name = origin_url
    else:
        top = _git_toplevel(cwd)
        repo_name = top.name if top is not None else Path(cwd).name

    # 2. branch
    branch_out = _git_run(cwd, ["rev-parse", "--abbrev-ref", "HEAD"])
    branch = branch_out.strip() if branch_out is not None and branch_out.strip() else "HEAD"

    # 3. sorted git status --porcelain paths or "clean"
    status_out = _git_run(cwd, ["status", "--porcelain"])
    changed_paths: list[str] = []
    if status_out is not None:
        for line in status_out.splitlines():
            if not line.strip():
                continue
            entry = line[3:].strip()
            if " -> " in entry:
                entry = entry.split(" -> ")[-1].strip()
            entry = entry.strip('"')
            if entry:
                changed_paths.append(entry)

    lines = [repo_name, head_sha, branch]
    if changed_paths:
        lines.extend(sorted(set(changed_paths)))
    else:
        lines.append("clean")

    text = "\n".join(lines)
    return (
        ContextItem(
            section="exact",
            source="repository",
            ref=head_sha,
            text=text,
            status="current",
            mandatory=False,
        ),
    )


__all__ = [
    "MissionHistory",
    "MissionStatus",
    "ProjectCampaign",
    "ProjectContext",
    "ProjectHistory",
    "ProjectMission",
    "ProjectRef",
    "ProjectResearch",
    "RefKind",
    "ResearchCampaign",
    "adr_items",
    "project_items",
    "repository_items",
]

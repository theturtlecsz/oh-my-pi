"""In-process stage compiler (OMP-419).

A service calls :func:`compile_stage` with :class:`ContextSettings` loaded from
``context.json``. The compile gathers exact, project, structural and semantic
items, then reranks, compiles and persists through the same helpers as the
context CLI. Procedural lessons are compiled when an approved learning store is provided.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from omp_work.v1.models import StrictModel
from pydantic import Field, field_validator, model_validator

from omp_knowledge.context.cli import (
    DEFAULT_SEMANTIC_LIMIT,
    DEFAULT_STRUCTURAL_LIMIT,
    _resolve_repository,
    append_semantic_items,
    append_structural_items,
    finish_compile,
    resolve_reranker,
)
from omp_knowledge.context.models import ContextItem, Stage
from omp_knowledge.context.project_sources import (
    ProjectContext,
    adr_items,
    project_items,
    repository_items,
)
from omp_knowledge.context.rerank import Reranker
from omp_knowledge.context.sources import (
    ViewInput,
    _view_mapping,
    exact_items,
    identity_from_view,
)
from omp_knowledge.engine.protocol import KnowledgeEngine
from omp_knowledge.learning.promotion import (
    ensure_promotion_tables,
    get_promoted_procedures,
)
from omp_knowledge.learning.store import LearningStore
from omp_knowledge.learning.uses import supply

EngineName = Literal["none", "cognee"]


class ContextSettings(StrictModel):
    """Settings for one in-process stage compile.

    Unknown keys are refused, so a ``learning_db`` entry cannot turn procedural
    lessons back on. ``reranker_model`` is required when ``reranker_url`` is set.
    """

    command: list[str] = Field(min_length=1)
    token_cmd: list[str] = Field(min_length=1)
    state_dir: str | None = None
    encoding: str | None = None
    token_budget: int = Field(gt=0)
    engine: EngineName = "none"
    reranker_url: str | None = None
    reranker_model: str | None = None
    structural_state_dir: str | None = None

    @field_validator("command", "token_cmd")
    @classmethod
    def _non_empty_strings(cls, value: list[str]) -> list[str]:
        if any(not isinstance(part, str) or not part for part in value):
            raise ValueError("must be a non-empty list of non-empty strings")
        return value

    @model_validator(mode="after")
    def _reranker_model_with_url(self) -> ContextSettings:
        if self.reranker_url and not self.reranker_model:
            raise ValueError("reranker_model is required with reranker_url")
        return self


def load_settings(path: str | Path) -> ContextSettings:
    """Read ``context.json`` and refuse any key this model does not declare."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("context settings must be a JSON object")
    return ContextSettings.model_validate(payload)


def _env_value(env: Mapping[str, str], key: str) -> str | None:
    value = env.get(key)
    if value is None or value == "":
        return None
    return value


def default_settings_path(env: Mapping[str, str]) -> str:
    """``$OMP_KNOWLEDGE_CONFIG_DIR`` else ``${XDG_CONFIG_HOME:-$HOME/.config}/omp-knowledge``, plus ``context.json``."""
    configured = _env_value(env, "OMP_KNOWLEDGE_CONFIG_DIR")
    if configured is not None:
        root = configured
    else:
        xdg = _env_value(env, "XDG_CONFIG_HOME")
        if xdg is not None:
            root = str(Path(xdg) / "omp-knowledge")
        else:
            home = _env_value(env, "HOME")
            if home is None:
                home = str(Path.home())
            root = str(Path(home) / ".config" / "omp-knowledge")
    return str(Path(root) / "context.json")


def _revision_title(view: ViewInput) -> str:
    if isinstance(view, Mapping):
        return str(view["item"]["revision"]["title"])
    return str(view.item.revision.title)


def _project_context(
    project: ProjectContext | Mapping[str, Any] | None,
) -> ProjectContext | None:
    if project is None or isinstance(project, ProjectContext):
        return project
    return ProjectContext.model_validate(project)


def compile_stage(
    settings: ContextSettings,
    *,
    view: ViewInput,
    stage: Stage,
    attempt_id: str,
    cwd: str,
    project: ProjectContext | Mapping[str, Any] | None = None,
    snapshots: Sequence[tuple[UUID, UUID, str]] = (),
    permitted_repositories: Collection[str | UUID] = (),
    engine: KnowledgeEngine | None = None,
    reranker: Reranker | None = None,
    learning_store: LearningStore | None = None,
) -> dict[str, Any]:
    """Compile one stage for ``view`` and persist the bundle.

    Items are the exact workflow view, repository, ADR and project items,
    structural items when ``structural_state_dir`` and ``snapshots`` are set,
    procedural items when ``learning_store`` is set and procedures are approved
    under journey-pass and promotion gates, and semantic items when an engine
    (or ``settings.engine == "cognee"``) and ``snapshots`` are set. The return
    carries the same keys as the context CLI compile; a procedural compile also
    carries ``lesson_supply_ids`` (the retained supply ids) and
    ``lesson_supplies`` (their proposed records). Compilation proposes lessons
    but does not record delivery, so it leaves no ``supplies`` rows.
    """
    if not settings.state_dir:
        raise ValueError("state_dir is required")
    if not settings.encoding:
        raise ValueError("encoding is required")

    identity = identity_from_view(view, stage, attempt_id)
    query = _revision_title(view)
    selection = tuple(snapshots)
    items: list[ContextItem] = list(exact_items(view))
    items.extend(repository_items(cwd))
    items.extend(adr_items(cwd))
    items.extend(project_items(_project_context(project)))
    append_structural_items(
        items,
        structural_state_dir=settings.structural_state_dir,
        selection=selection,
        permitted=permitted_repositories,
        limit=DEFAULT_STRUCTURAL_LIMIT,
    )

    if learning_store is not None:
        ensure_promotion_tables(learning_store)
        view_dict = _view_mapping(view)
        item_dict = view_dict["item"]
        workspace_id = item_dict.get("workspace_id")
        work_key = str(item_dict["alias"]["key"])
        project_id = item_dict.get("project_id")

        approved_pairs = get_promoted_procedures(
            learning_store,
            workspace_id=workspace_id,
        )

        learning_context: dict[str, str] = {}
        if project_id is not None:
            learning_context["project_id"] = str(project_id)
        repository = _resolve_repository(cwd)
        if repository is not None:
            learning_context["repository"] = repository

        with learning_store.transaction():
            supply_result = supply(
                learning_store,
                workspace_id=str(workspace_id) if workspace_id is not None else "",
                work_key=work_key,
                context=learning_context,
                limit=0,
                allowed=approved_pairs,
                record=False,
            )
            for supply_id, line in zip(supply_result.supply_ids, supply_result.lines):
                items.append(
                    ContextItem(
                        section="procedural",
                        source="lesson",
                        ref=supply_id,
                        text=line,
                        status="current",
                    )
                )
            append_semantic_items(
                items,
                engine_name=settings.engine,
                engine=engine,
                selection=selection,
                permitted=permitted_repositories,
                query=query,
                limit=DEFAULT_SEMANTIC_LIMIT,
            )
            active_reranker, reranker_route = resolve_reranker(
                injected=reranker,
                reranker_url=settings.reranker_url,
                reranker_model=settings.reranker_model,
                route_set=None,
            )
            result = finish_compile(
                items=items,
                identity=identity,
                token_budget=settings.token_budget,
                encoding=settings.encoding,
                token_cmd=settings.token_cmd,
                state_dir=settings.state_dir,
                query=query,
                reranker=active_reranker,
                routes=[reranker_route],
            )
            excluded_supply_ids = {
                exclusion["ref"] if isinstance(exclusion, dict) else exclusion.ref
                for exclusion in result["exclusions"]
                if (exclusion.get("section") if isinstance(exclusion, dict) else exclusion.section) == "procedural"
            }
            retained_ids = [
                sid for sid in supply_result.supply_ids
                if sid not in excluded_supply_ids
            ]
            retained_records = [
                record for record in supply_result.records
                if record.supply_id not in excluded_supply_ids
            ]
            result["lesson_supply_ids"] = retained_ids
            result["lesson_supplies"] = [
                record.model_dump(mode="json") for record in retained_records
            ]
            return result

    append_semantic_items(
        items,
        engine_name=settings.engine,
        engine=engine,
        selection=selection,
        permitted=permitted_repositories,
        query=query,
        limit=DEFAULT_SEMANTIC_LIMIT,
    )
    active_reranker, reranker_route = resolve_reranker(
        injected=reranker,
        reranker_url=settings.reranker_url,
        reranker_model=settings.reranker_model,
        route_set=None,
    )
    return finish_compile(
        items=items,
        identity=identity,
        token_budget=settings.token_budget,
        encoding=settings.encoding,
        token_cmd=settings.token_cmd,
        state_dir=settings.state_dir,
        query=query,
        reranker=active_reranker,
        routes=[reranker_route],
    )


__all__ = [
    "ContextSettings",
    "compile_stage",
    "default_settings_path",
    "load_settings",
]

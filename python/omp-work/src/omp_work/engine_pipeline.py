"""Retrieve -> compile -> campaign pipeline under a WorkService job id."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import json
import os

from omp_work.cognee_adapter import query_graph
from omp_work.context_compile import compile_context, CompiledContext
from omp_work.research_campaign import run_campaign, CampaignResult


def _active_dir() -> Path:
    val = os.environ.get("OMP_ECONOMY_ACTIVE_DIR")
    if val:
        return Path(val)
    return Path.home() / ".codex/workflows/economy/ACTIVE"


DEFAULT_INTAKE_BAR = 1200
DEFAULT_ACTIVE_DIR = _active_dir()
DEFAULT_COGNEE_STORE = DEFAULT_ACTIVE_DIR / "cognee-store.json"
DEFAULT_ENOLA_STORE = DEFAULT_ACTIVE_DIR / "enola-store.json"
DEFAULT_BUDGET_CAPS = DEFAULT_ACTIVE_DIR / "BUDGET-CAPS.json"


@dataclass(frozen=True)
class PipelineResult:
    job_id: str
    query: str
    findings: list[str]
    compiled: CompiledContext
    campaign: CampaignResult
    context_tokens_est: int
    truncated: bool
    budget_note: str
    packet_section: str
    store_path: str | None = None
    enola_trace_key: str | None = None
    enola_finding: str | None = None
    ledger_event: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "query": self.query,
            "findings": self.findings,
            "worker_context": self.compiled.worker_context,
            "campaign_finding": self.campaign.finding,
            "context_tokens_est": self.context_tokens_est,
            "truncated": self.truncated,
            "budget_note": self.budget_note,
            "spent_tokens": self.campaign.spent_tokens,
            "stopped": self.campaign.stopped,
            "store_path": self.store_path,
            "enola_trace_key": self.enola_trace_key,
            "enola_finding": self.enola_finding,
            "ledger_event": self.ledger_event,
        }


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def read_budget_note(caps_path: Path | str | None = None) -> str:
    path = Path(caps_path) if caps_path else (_active_dir() / "BUDGET-CAPS.json")
    if not path.is_file():
        return "budget_caps: missing — prefer E1; stop on quota"
    data = json.loads(path.read_text())
    soft = data.get("soft_warn_fraction")
    hard = data.get("hard_refuse_fraction")
    ms = data.get("post_program_milestone_soft_tokens")
    mh = data.get("post_program_milestone_hard_tokens")
    return (
        f"budget_caps: soft_warn={soft} hard_refuse={hard}; "
        f"post_program milestone soft={ms} hard={mh}; period $ UNKNOWN"
    )


def _resolve_store(store, store_path):
    if store is not None:
        return store, None
    path = Path(store_path) if store_path is not None else (_active_dir() / "cognee-store.json")
    if path.is_file():
        from omp_work.cognee_store import CogneeStore
        return CogneeStore.open(path).nodes, str(path)
    return None, None


def _fit_section(*, job_id, findings, objective, max_context_tokens):
    truncated = False
    work = list(findings)
    while True:
        compiled = compile_context(job_id=job_id, findings=work, objective=objective)
        section = compiled.as_packet_section()
        est = estimate_tokens(section)
        if est <= max_context_tokens:
            return compiled, section, est, truncated, work
        if len(work) > 1:
            work = work[:-1]
            truncated = True
            continue
        keep_chars = max(32, max_context_tokens * 3)
        work = [work[0][:keep_chars] + " [truncated_to_intake_bar]"]
        compiled = compile_context(job_id=job_id, findings=work, objective=objective)
        section = compiled.as_packet_section()
        while estimate_tokens(section) > max_context_tokens and len(section) > 64:
            target = max(64, max_context_tokens * 4 - 32)
            section = section[:target].rstrip() + "\n[truncated_to_intake_bar]"
        budget_chars = max(1, max_context_tokens * 4 - 3)
        if len(section) > budget_chars:
            marker = "\n[truncated_to_intake_bar]"
            section = section[: max(0, budget_chars - len(marker))] + marker
        est = estimate_tokens(section)
        compiled = CompiledContext(
            job_id=job_id,
            findings=work,
            worker_context=(compiled.worker_context[: max(32, budget_chars // 2)]
                           + " [truncated_to_intake_bar]"),
        )
        return compiled, section, est, True, work


def _maybe_enola_trace(*, job_id, objective, findings, enola_store_path, enola_enabled):
    if not enola_enabled:
        return None, None
    path = Path(enola_store_path) if enola_store_path is not None else (_active_dir() / "enola-store.json")
    from omp_work.enola_store import EnolaStore
    store = EnolaStore.open(path)
    result = store.record_trace(job_id=job_id, goal=objective, facts=list(findings)[:5])
    keys = [k for k, v in store.traces.items() if v.get("job_id") == job_id]
    key = keys[-1] if keys else None
    return key, result.finding()


def run_retrieve_compile_campaign(
    *,
    job_id: str,
    query: str,
    objective: str,
    max_context_tokens: int = DEFAULT_INTAKE_BAR,
    parent_budget_tokens: int = 8000,
    store: dict[str, Any] | None = None,
    store_path: Path | str | None = None,
    caps_path: Path | str | None = None,
    enola_store_path: Path | str | None = None,
    enola_enabled: bool = True,
    ledger_attach: bool = True,
    ledger_path: Path | str | None = None,
) -> PipelineResult:
    if not job_id.strip():
        raise ValueError("job_id required — pipeline runs via WorkService")
    resolved, used_path = _resolve_store(store, store_path)
    cg = query_graph(query=query, job_id=job_id, store=resolved)
    findings = [n.get("claim", "") for n in cg.nodes if n.get("claim")]
    findings = [f for f in findings if f]
    if not findings:
        findings = [f"No graph hit for {query!r}; use write-first + E1 default"]
    compiled, section, est, truncated, work = _fit_section(
        job_id=job_id,
        findings=findings,
        objective=objective,
        max_context_tokens=max_context_tokens,
    )
    enola_key, enola_finding = _maybe_enola_trace(
        job_id=job_id,
        objective=objective,
        findings=work,
        enola_store_path=enola_store_path,
        enola_enabled=enola_enabled,
    )
    ledger_event = None
    if ledger_attach:
        from omp_work.research_ledger import run_campaign_with_ledger, append_ledger_event
        led = run_campaign_with_ledger(
            campaign_id=f"{job_id}-camp",
            question=objective,
            retrieved_facts=work,
            parent_budget_tokens=parent_budget_tokens,
        )
        campaign = led.campaign
        ledger_event = led.ledger_event
        if ledger_path is not None:
            append_ledger_event(ledger_path, ledger_event)
    else:
        campaign = run_campaign(
            campaign_id=f"{job_id}-camp",
            question=objective,
            retrieved_facts=work,
            parent_budget_tokens=parent_budget_tokens,
        )
    note = read_budget_note(caps_path)
    return PipelineResult(
        job_id=job_id,
        query=query,
        findings=list(work),
        compiled=compiled,
        campaign=campaign,
        context_tokens_est=est,
        truncated=truncated,
        budget_note=note,
        packet_section=section,
        store_path=used_path,
        enola_trace_key=enola_key,
        enola_finding=enola_finding,
        ledger_event=ledger_event,
    )

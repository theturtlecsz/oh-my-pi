"""Parent-budget guard for research campaigns."""
from __future__ import annotations
from omp_work.research_campaign import run_campaign


def run_campaign_guarded(**kwargs):
    """Run campaign; re-raise clear error if parent budget too small for BoN=2."""
    parent = int(kwargs.get("parent_budget_tokens") or 8000)
    if parent < 8:
        raise ValueError(f"parent_budget_tokens too small for BoN=2: {parent}")
    try:
        return run_campaign(**kwargs)
    except ValueError as e:
        raise ValueError(f"campaign exceeded parent budget ({parent}): {e}") from e

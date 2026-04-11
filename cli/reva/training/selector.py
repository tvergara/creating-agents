"""Select survivors from an evaluated generation of agents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from reva.training.evaluator import EvalResult


@dataclass
class AgentResult:
    """An evaluated agent ready for selection."""
    config: dict          # AgentConfig fields (role, persona, interests, methodology, review_format)
    eval: EvalResult


def select(agents: list[AgentResult], n_survivors: int = 4) -> list[AgentResult]:
    """Select exactly n_survivors agents using top-2-by-each-metric rule.

    Algorithm:
      1. Take top 2 ranked by citation_corr.
      2. Take top 2 ranked by acceptance_corr.
      3. Deduplicate (union of both sets).
      4. If union < n_survivors, fill remaining slots from the combined
         ranking (average of both correlations, descending), excluding
         already-selected agents.

    Agents with eval (-1, -1) are always ranked last.

    Args:
        agents: List of evaluated agents from a generation.
        n_survivors: Number of survivors to return (default 4).

    Returns:
        List of exactly n_survivors AgentResult objects.
    """
    if len(agents) <= n_survivors:
        return list(agents)

    def citation_key(a: AgentResult) -> float:
        return a.eval.citation_corr

    def acceptance_key(a: AgentResult) -> float:
        return a.eval.acceptance_corr

    def avg_key(a: AgentResult) -> float:
        return (a.eval.citation_corr + a.eval.acceptance_corr) / 2.0

    by_citation = sorted(agents, key=citation_key, reverse=True)
    by_acceptance = sorted(agents, key=acceptance_key, reverse=True)

    survivors: list[AgentResult] = []
    seen_indices: set[int] = set()

    def _add(agent: AgentResult) -> None:
        idx = id(agent)
        if idx not in seen_indices:
            seen_indices.add(idx)
            survivors.append(agent)

    # Top 2 from each metric
    for agent in by_citation[:2]:
        _add(agent)
    for agent in by_acceptance[:2]:
        _add(agent)

    # Fill remaining slots from average ranking
    if len(survivors) < n_survivors:
        by_avg = sorted(agents, key=avg_key, reverse=True)
        for agent in by_avg:
            if len(survivors) >= n_survivors:
                break
            _add(agent)

    return survivors[:n_survivors]

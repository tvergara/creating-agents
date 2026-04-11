"""Correlation-based evaluation of agent review scores against ground truth."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from scipy.stats import pointbiserialr, spearmanr

from reva.training.runner import ScoreEntry

logger = logging.getLogger(__name__)


@dataclass
class EvalResult:
    citation_corr: float
    acceptance_corr: float


_FAILURE = EvalResult(citation_corr=-1.0, acceptance_corr=-1.0)


def evaluate(scores: list[ScoreEntry], ground_truth: dict[int, dict]) -> EvalResult:
    """Compute citation and acceptance correlations for an agent's 10 scores.

    Args:
        scores: List of ScoreEntry from runner.run_agent(). Expected to have
                exactly 10 entries. Returns failure result if fewer.
        ground_truth: Dict mapping paper_id → {"citation_count": int, "accepted": bool}.

    Returns:
        EvalResult with Spearman ρ (citations) and point-biserial r (acceptance).
        Returns EvalResult(-1.0, -1.0) when scores is empty or < 10 valid entries.
    """
    if not scores:
        return _FAILURE

    agent_scores: list[float] = []
    citations: list[float] = []
    accepted: list[float] = []

    for entry in scores:
        gt = ground_truth.get(entry.paper_id)
        if gt is None:
            logger.warning("paper_id %d not in ground truth — skipping", entry.paper_id)
            continue
        agent_scores.append(entry.score)
        citations.append(float(gt["citation_count"]))
        accepted.append(1.0 if gt["accepted"] else 0.0)

    if len(agent_scores) < 10:
        return _FAILURE

    citation_corr, _ = spearmanr(agent_scores, citations)
    acceptance_corr, _ = pointbiserialr(accepted, agent_scores)

    return EvalResult(
        citation_corr=float(citation_corr),
        acceptance_corr=float(acceptance_corr),
    )

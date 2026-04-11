"""Run a reviewer agent against the paper set via direct Anthropic API call."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Default model for training runs
DEFAULT_MODEL = "claude-opus-4-6"

# Max chars of full_text included per paper to avoid exceeding context limits
MAX_TEXT_CHARS = 8_000

_SCORE_INSTRUCTION = """\
You are participating in a paper-review training exercise.

Below are {n_papers} papers submitted to ICLR 2024. You must select exactly \
10 of them to review and assign each a score.

For each paper you choose, provide:
- paper_id: the integer ID shown before the paper
- score: a float from 0.0 to 10.0 (0 = reject outright, 10 = exceptional accept)
- reasoning: one sentence justifying the score

Respond with a JSON array and nothing else — no prose, no markdown fences, \
no explanation outside the array. Example:
[
  {{"paper_id": 3, "score": 7.5, "reasoning": "Strong methodology but limited novelty."}},
  {{"paper_id": 17, "score": 4.0, "reasoning": "Incremental contribution over prior work."}}
]

Select papers that fall within your research interests and expertise. \
You must choose exactly 10.

--- PAPERS ---

{papers_block}"""


@dataclass
class ScoreEntry:
    paper_id: int
    score: float
    reasoning: str


def run_agent(
    system_prompt: str,
    papers: list[dict],
    *,
    model: str = DEFAULT_MODEL,
    max_retries: int = 3,
    client: Any = None,
) -> list[ScoreEntry]:
    """Run one agent against all papers, returning exactly 10 scored entries.

    Args:
        system_prompt: The compiled agent system prompt.
        papers: List of agent-visible paper dicts (id, title, abstract,
                domains, pdf_url, full_text).
        model: Anthropic model identifier.
        max_retries: Number of parse/validation retries before giving up.
        client: Optional pre-constructed anthropic.Anthropic client (for
                testing). If None, one is constructed from env.

    Returns:
        List of exactly 10 ScoreEntry objects, or empty list on failure.
    """
    if client is None:
        import anthropic
        client = anthropic.Anthropic()

    papers_block = _format_papers_block(papers)
    user_message = _SCORE_INSTRUCTION.format(
        n_papers=len(papers),
        papers_block=papers_block,
    )

    valid_ids = {p["id"] for p in papers}

    for attempt in range(1, max_retries + 1):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=4096,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
            )
            text = response.content[0].text.strip()
            entries = _parse_and_validate(text, valid_ids)
            if entries is not None:
                return entries
            logger.warning("Attempt %d/%d: invalid response, retrying", attempt, max_retries)
        except Exception as exc:
            logger.warning("Attempt %d/%d: API error: %s", attempt, max_retries, exc)

    logger.error("All %d attempts failed — returning empty result", max_retries)
    return []


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _format_papers_block(papers: list[dict]) -> str:
    parts = []
    for p in papers:
        full_text = p.get("full_text", "")
        if len(full_text) > MAX_TEXT_CHARS:
            full_text = full_text[:MAX_TEXT_CHARS] + "\n[... truncated ...]"
        domains = ", ".join(p.get("domains", []))
        parts.append(
            f"[{p['id']}] {p['title']}\n"
            f"Domains: {domains}\n"
            f"Abstract: {p['abstract']}\n"
            f"Full text:\n{full_text}"
        )
    return "\n\n---\n\n".join(parts)


def _parse_and_validate(text: str, valid_ids: set[int]) -> list[ScoreEntry] | None:
    """Parse JSON from model response and validate structure.

    Returns a list of exactly 10 ScoreEntry objects, or None if invalid.
    """
    # Strip markdown code fences if present
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s*```$", "", text, flags=re.MULTILINE)
    text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.debug("JSON parse error: %s", exc)
        return None

    if not isinstance(data, list):
        logger.debug("Response is not a JSON array")
        return None

    if len(data) != 10:
        logger.debug("Expected 10 entries, got %d", len(data))
        return None

    entries: list[ScoreEntry] = []
    seen_ids: set[int] = set()

    for item in data:
        if not isinstance(item, dict):
            logger.debug("Entry is not a dict: %r", item)
            return None

        try:
            paper_id = int(item["paper_id"])
            score = float(item["score"])
            reasoning = str(item.get("reasoning", ""))
        except (KeyError, ValueError, TypeError) as exc:
            logger.debug("Entry parse error: %s", exc)
            return None

        if paper_id not in valid_ids:
            logger.debug("paper_id %d not in valid range", paper_id)
            return None

        if paper_id in seen_ids:
            logger.debug("Duplicate paper_id: %d", paper_id)
            return None

        seen_ids.add(paper_id)
        # Clamp score to [0, 10]
        score = max(0.0, min(10.0, score))
        entries.append(ScoreEntry(paper_id=paper_id, score=score, reasoning=reasoning))

    return entries

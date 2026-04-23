"""Run a reviewer agent against the paper set via a CLI backend (claude or gemini)."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_GEMINI_NODE20_BIN = "/Users/tom/.nvm/versions/node/v20.20.2/bin"

# Default model per backend
DEFAULT_MODEL: dict[str, str] = {
    "claude-code": "claude-sonnet-4-6",
    "gemini-cli": "gemini-2.5-pro",
}
BACKEND_CHOICES = ("claude-code", "gemini-cli")

# Max chars of full_text included per paper to avoid exceeding context limits
MAX_TEXT_CHARS = 8_000

_SCORE_INSTRUCTION = """\
You are participating in a paper-review training exercise.

Below are {n_papers} papers submitted to ICLR 2024. You must review ALL of them.

For each paper, provide:
- paper_id: the integer ID shown before the paper
- score: a float from 0.0 to 10.0 (0 = reject outright, 10 = exceptional accept)
- reasoning: one sentence justifying the score

Respond with a JSON array and nothing else — no prose, no markdown fences, \
no explanation outside the array. Example:
[
  {{"paper_id": 3, "score": 7.5, "reasoning": "Strong methodology but limited novelty."}},
  {{"paper_id": 17, "score": 4.0, "reasoning": "Incremental contribution over prior work."}}
]

You must include an entry for every one of the {n_papers} papers.

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
    model: str | None = None,
    backend: str = "claude-code",
    max_retries: int = 3,
    client: Any = None,
) -> list[ScoreEntry]:
    """Run one agent against the given papers, returning one score per paper.

    Args:
        system_prompt: The compiled agent system prompt.
        papers: List of agent-visible paper dicts (id, title, abstract,
                domains, pdf_url, full_text).
        model: Model identifier (defaults to the backend's default model).
        backend: "claude-code" or "gemini-cli".
        max_retries: Number of parse/validation retries before giving up.
        client: Unused (kept for test-mock compatibility).

    Returns:
        List of exactly 10 ScoreEntry objects, or empty list on failure.
    """
    if model is None:
        model = DEFAULT_MODEL.get(backend, DEFAULT_MODEL["claude-code"])

    papers_block = _format_papers_block(papers)
    user_message = _SCORE_INSTRUCTION.format(
        n_papers=len(papers),
        papers_block=papers_block,
    )

    valid_ids = {p["id"] for p in papers}

    _call = _call_claude if backend == "claude-code" else _call_gemini

    for attempt in range(1, max_retries + 1):
        try:
            text = _call(system_prompt, user_message, model)
            entries = _parse_and_validate(text, valid_ids)
            if entries is not None:
                return entries
            logger.warning("Attempt %d/%d: invalid response, retrying", attempt, max_retries)
        except Exception as exc:
            logger.warning("Attempt %d/%d: %s error: %s", attempt, max_retries, backend, exc)

    logger.error("All %d attempts failed — returning empty result", max_retries)
    return []


# ---------------------------------------------------------------------------
# Backend CLI invocations
# ---------------------------------------------------------------------------


def _call_claude(system_prompt: str, user_message: str, model: str) -> str:
    """Invoke ``claude -p`` in a temp directory with CLAUDE.md as system prompt."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Write system prompt as CLAUDE.md (Claude Code reads it automatically)
        claude_md = Path(tmpdir) / "CLAUDE.md"
        claude_md.write_text(system_prompt, encoding="utf-8")

        # Write user message to a file (avoids shell ARG_MAX limits)
        msg_file = Path(tmpdir) / "user_message.txt"
        msg_file.write_text(user_message, encoding="utf-8")

        # Use shell=True so $(cat ...) is expanded
        shell_cmd = " ".join([
            "claude",
            "-p", f'"$(cat {msg_file})"',
            "--model", model,
            "--output-format", "text",
        ])

        result = subprocess.run(
            shell_cmd,
            shell=True,
            capture_output=True,
            text=True,
            cwd=tmpdir,
            timeout=300,
        )

        if result.returncode != 0:
            raise RuntimeError(f"claude exited {result.returncode}: {result.stderr[:500]}")

        return result.stdout.strip()


def _call_gemini(system_prompt: str, user_message: str, model: str) -> str:
    """Invoke ``gemini -p`` in a temp directory with GEMINI.md as system prompt."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Gemini CLI reads GEMINI.md as system context automatically
        gemini_md = Path(tmpdir) / "GEMINI.md"
        gemini_md.write_text(system_prompt, encoding="utf-8")

        # Write user message to a file (avoids shell ARG_MAX limits)
        msg_file = Path(tmpdir) / "user_message.txt"
        msg_file.write_text(user_message, encoding="utf-8")

        shell_cmd = " ".join([
            "gemini",
            "-p", f'"$(cat {msg_file})"',
            "--model", model,
        ])

        env = os.environ.copy()
        if Path(_GEMINI_NODE20_BIN).is_dir():
            env["PATH"] = f"{_GEMINI_NODE20_BIN}:{env.get('PATH', '')}"

        result = subprocess.run(
            shell_cmd,
            shell=True,
            capture_output=True,
            text=True,
            cwd=tmpdir,
            timeout=300,
            env=env,
        )

        if result.returncode != 0:
            raise RuntimeError(f"gemini exited {result.returncode}: {result.stderr[:500]}")

        return result.stdout.strip()


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

    Returns a list with one ScoreEntry per paper (len == len(valid_ids)), or None if invalid.
    """
    data = _load_candidate_json(text)
    if data is None:
        return None

    if isinstance(data, dict):
        for key in ("scores", "results", "papers", "entries"):
            if isinstance(data.get(key), list):
                data = data[key]
                break

    if not isinstance(data, list):
        logger.debug("Response is not a JSON array")
        return None

    if len(data) != len(valid_ids):
        logger.debug("Expected %d entries, got %d", len(valid_ids), len(data))
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


def _load_candidate_json(text: str) -> Any | None:
    """Best-effort JSON loader for model outputs.

    Gemini sometimes wraps the array in prose, markdown fences, or a simple
    object wrapper. Try the raw text first, then a bracket-extracted array.
    """
    # Strip markdown code fences if present.
    cleaned = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    cleaned = re.sub(r"\s*```$", "", cleaned, flags=re.MULTILINE).strip()

    for candidate in (cleaned, _extract_outer_json_array(cleaned)):
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            logger.debug("JSON parse error: %s", exc)

    return None


def _extract_outer_json_array(text: str) -> str | None:
    """Return the first top-level JSON array substring from text, if any."""
    start = text.find("[")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape = False

    for i in range(start, len(text)):
        ch = text[i]

        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]

    return None

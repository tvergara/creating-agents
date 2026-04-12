"""Deploy trained agents: score papers in training-style batches, then post verdicts."""

from __future__ import annotations

import json
import logging
import random
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PLATFORM_BASE = "https://coale.science"
BATCH_SIZE = 40
PICKS_PER_BATCH = 10

_DEPLOY_SCORE_INSTRUCTION = """\
You are participating in a paper-review exercise.

Below are {n_papers} papers. You must select exactly {picks} of them to review.

For each paper you choose, provide:
- paper_id: the UUID shown in brackets before the paper
- score: a float from 0.0 to 10.0 (0 = reject outright, 10 = exceptional accept)
- review: a full review of 2-3 paragraphs justifying your score

Respond with a JSON array and nothing else — no prose, no markdown fences. Example:
[
  {{"paper_id": "fc8548a0-dce6-4076-b50d-edffe28d20e9", "score": 7.5, "review": "This paper presents a novel approach..."}},
  {{"paper_id": "a1b2c3d4-...", "score": 4.0, "review": "The contribution is incremental..."}}
]

Select papers that fall within your research interests and expertise.
You must choose exactly {picks}.

--- PAPERS ---

{papers_block}"""

MAX_TEXT_CHARS = 8_000


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class DeployReview:
    paper_id: str   # platform UUID
    score: float
    review: str     # full review text


# ---------------------------------------------------------------------------
# Scoring (mirrors training runner, fresh context per batch)
# ---------------------------------------------------------------------------


def score_batch(
    system_prompt: str,
    papers: list[dict],
    *,
    backend: str = "claude-code",
    model: str = "claude-sonnet-4-6",
    max_retries: int = 3,
) -> list[DeployReview]:
    """Score one batch of papers in a fresh subprocess. Returns up to PICKS_PER_BATCH reviews."""
    papers_block = _format_papers_block(papers)
    user_message = _DEPLOY_SCORE_INSTRUCTION.format(
        n_papers=len(papers),
        picks=PICKS_PER_BATCH,
        papers_block=papers_block,
    )
    valid_ids = {p["id"] for p in papers}

    for attempt in range(1, max_retries + 1):
        try:
            if backend == "gemini-cli":
                text = _call_gemini(system_prompt, user_message)
            else:
                text = _call_claude(system_prompt, user_message, model)
            result = _parse_reviews(text, valid_ids)
            if result is not None:
                return result
            logger.warning("Attempt %d/%d: invalid response, retrying", attempt, max_retries)
        except Exception as exc:
            logger.warning("Attempt %d/%d: %s error: %s", attempt, max_retries, backend, exc)

    logger.error("All %d attempts failed for batch", max_retries)
    return []


def collect_reviews(
    system_prompt: str,
    papers: list[dict],
    *,
    n_target: int = 50,
    backend: str = "claude-code",
    model: str = "claude-sonnet-4-6",
    seed: int | None = None,
    parallel: int = 4,
) -> list[DeployReview]:
    """Run scoring in BATCH_SIZE batches (fresh context each) until n_target reviews collected.

    Batches are dispatched in parallel (up to `parallel` concurrent calls).
    """
    rng = random.Random(seed)
    paper_pool = list(papers)
    rng.shuffle(paper_pool)

    # Pre-split into non-overlapping batches
    batches = [paper_pool[i:i + BATCH_SIZE] for i in range(0, len(paper_pool), BATCH_SIZE)]
    n_needed = (n_target + PICKS_PER_BATCH - 1) // PICKS_PER_BATCH  # batches needed
    batches = batches[:n_needed]

    logger.info("Dispatching %d batches (parallel=%d)...", len(batches), parallel)

    results_by_idx: dict[int, list[DeployReview]] = {}

    def _run_batch(idx: int, batch: list[dict]) -> tuple[int, list[DeployReview]]:
        logger.info("Batch %d/%d: scoring %d papers", idx + 1, len(batches), len(batch))
        reviews = score_batch(system_prompt, batch, backend=backend, model=model)
        logger.info("Batch %d/%d done: %d reviews", idx + 1, len(batches), len(reviews))
        return idx, reviews

    with ThreadPoolExecutor(max_workers=min(parallel, len(batches))) as executor:
        futures = {executor.submit(_run_batch, i, b): i for i, b in enumerate(batches)}
        for future in as_completed(futures):
            idx, reviews = future.result()
            results_by_idx[idx] = reviews

    # Merge in order, dedup
    collected: list[DeployReview] = []
    seen_ids: set[str] = set()
    for i in range(len(batches)):
        for r in results_by_idx.get(i, []):
            if r.paper_id not in seen_ids and len(collected) < n_target:
                collected.append(r)
                seen_ids.add(r.paper_id)

    if len(collected) < n_target:
        logger.warning("Only collected %d reviews (target was %d)", len(collected), n_target)

    return collected


# ---------------------------------------------------------------------------
# Platform posting
# ---------------------------------------------------------------------------


class CoalescencePoster:
    """REST client for posting comments, votes, and verdicts."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.headers = {
            "Authorization": api_key,
            "Content-Type": "application/json",
        }

    def _post(self, path: str, body: dict) -> dict:
        url = f"{PLATFORM_BASE}/api/v1{path}"
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, headers=self.headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body_text = e.read().decode(errors="replace")
            raise RuntimeError(f"POST {path} → {e.code}: {body_text}") from e

    def _get(self, path: str) -> Any:
        url = f"{PLATFORM_BASE}/api/v1{path}"
        req = urllib.request.Request(url, headers=self.headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())

    def post_comment(self, paper_id: str, text: str) -> dict:
        return self._post("/comments/", {"paper_id": paper_id, "content_markdown": text})

    def get_comments(self, paper_id: str) -> list:
        return self._get(f"/comments/paper/{paper_id}")

    def cast_vote(self, target_id: str, target_type: str, vote_value: int) -> dict:
        return self._post("/votes/", {
            "target_id": target_id,
            "target_type": target_type,
            "vote_value": vote_value,
        })

    def post_verdict(self, paper_id: str, content_markdown: str, score: float) -> dict:
        return self._post("/verdicts/", {
            "paper_id": paper_id,
            "content_markdown": content_markdown,
            "score": score,
        })


def post_all_verdicts(
    reviews: list[DeployReview],
    api_key: str,
    *,
    delay: float = 1.0,
) -> list[dict]:
    """Post comment + vote (if possible) + verdict for each review. Returns results."""
    client = CoalescencePoster(api_key)
    results = []

    for i, review in enumerate(reviews):
        logger.info("Posting verdict %d/%d for paper %s", i + 1, len(reviews), review.paper_id[:8])
        entry = {"paper_id": review.paper_id, "score": review.score, "status": None, "error": None}

        try:
            # 1. Post a comment (required before verdict)
            comment_text = f"**Review notes**\n\n{review.review}"
            client.post_comment(review.paper_id, comment_text)

            # 2. Vote on an existing comment if any
            comments = client.get_comments(review.paper_id)
            if comments:
                client.cast_vote(comments[0]["id"], "COMMENT", 1)

            # 3. Post verdict
            client.post_verdict(review.paper_id, review.review, review.score)
            entry["status"] = "ok"
            logger.info("  posted verdict (score=%.1f)", review.score)

        except Exception as exc:
            entry["status"] = "error"
            entry["error"] = str(exc)
            logger.error("  failed: %s", exc)

        results.append(entry)
        time.sleep(delay)

    return results


# ---------------------------------------------------------------------------
# Internal: subprocess calls (mirrors runner.py)
# ---------------------------------------------------------------------------


def _call_claude(system_prompt: str, user_message: str, model: str) -> str:
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "CLAUDE.md").write_text(system_prompt, encoding="utf-8")
        msg_file = Path(tmpdir) / "user_message.txt"
        msg_file.write_text(user_message, encoding="utf-8")
        shell_cmd = f'claude -p "$(cat {msg_file})" --model {model} --output-format text'
        result = subprocess.run(shell_cmd, shell=True, capture_output=True, text=True,
                                cwd=tmpdir, timeout=600)
        if result.returncode != 0:
            raise RuntimeError(f"claude exited {result.returncode}: {result.stderr[:500]}")
        return result.stdout.strip()


def _call_gemini(system_prompt: str, user_message: str, model: str = "gemini-2.5-pro") -> str:
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "GEMINI.md").write_text(system_prompt, encoding="utf-8")
        msg_file = Path(tmpdir) / "user_message.txt"
        msg_file.write_text(user_message, encoding="utf-8")
        shell_cmd = f'gemini -p "$(cat {msg_file})" --model {model}'
        result = subprocess.run(shell_cmd, shell=True, capture_output=True, text=True,
                                cwd=tmpdir, timeout=600)
        if result.returncode != 0:
            raise RuntimeError(f"gemini exited {result.returncode}: {result.stderr[:500]}")
        return result.stdout.strip()


# ---------------------------------------------------------------------------
# Internal: formatting and parsing
# ---------------------------------------------------------------------------


def _format_papers_block(papers: list[dict]) -> str:
    parts = []
    for p in papers:
        full_text = p.get("full_text", "")
        if len(full_text) > MAX_TEXT_CHARS:
            full_text = full_text[:MAX_TEXT_CHARS] + "\n[... truncated ...]"
        domains = ", ".join(p.get("domains", []))
        section = (
            f"[{p['id']}] {p['title']}\n"
            f"Domains: {domains}\n"
            f"Abstract: {p['abstract']}"
        )
        if full_text:
            section += f"\nFull text:\n{full_text}"
        parts.append(section)
    return "\n\n---\n\n".join(parts)


def _parse_reviews(text: str, valid_ids: set[str]) -> list[DeployReview] | None:
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s*```$", "", text, flags=re.MULTILINE).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.debug("JSON parse error: %s", exc)
        return None

    if not isinstance(data, list) or len(data) != PICKS_PER_BATCH:
        logger.debug("Expected list of %d, got %s", PICKS_PER_BATCH, type(data))
        return None

    reviews: list[DeployReview] = []
    seen: set[str] = set()

    for item in data:
        try:
            paper_id = str(item["paper_id"])
            score = max(0.0, min(10.0, float(item["score"])))
            review = str(item.get("review", "")).strip()
        except (KeyError, ValueError, TypeError) as exc:
            logger.debug("Entry parse error: %s", exc)
            return None

        if paper_id not in valid_ids or paper_id in seen or not review:
            logger.debug("Invalid entry: paper_id=%s", paper_id)
            return None

        seen.add(paper_id)
        reviews.append(DeployReview(paper_id=paper_id, score=score, review=review))

    return reviews

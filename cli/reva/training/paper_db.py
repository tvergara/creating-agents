"""Platform paper database: fetch, cache, and format for deployment prompts."""

from __future__ import annotations

import io
import json
import logging
import random
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

logger = logging.getLogger(__name__)

PLATFORM_BASE_URL = "https://coale.science"
PAPERS_API = f"{PLATFORM_BASE_URL}/api/v1/papers/"
MAX_TEXT_CHARS = 8_000  # same truncation as training runner
BIGBANG_SUBMITTER_ID = "dd8deb9a-5d20-4b81-aa5b-5dffc43757c6"

_DEPLOY_INITIAL_PROMPT = """\
You are an agent on the Coalescence scientific paper evaluation platform.
Your role, research interests, and persona are described in your instructions.

IMPORTANT — Identity and authentication:
1. Read `.agent_name` to get your platform username.
2. Check if `.api_key` exists. If it does, use it to authenticate (verify with GET {base}/api/v1/users/me \
using Authorization header with your key).
   Do NOT register again — you are already registered.
3. If `.api_key` does NOT exist, read {base}/skill.md, register using EXACTLY \
the name from `.agent_name`, and save the returned API key to `.api_key` immediately.

---

Below are {{n_papers}} papers currently on the platform. \
Select exactly 10 that fall within your research interests and expertise.

For each paper you select, complete the full verdict workflow:
1. Post at least one comment (POST {base}/api/v1/comments/ with paper_id, content_markdown)
2. If other comments exist, upvote at least one (POST {base}/api/v1/votes/ with target_id, \
target_type="COMMENT", vote_value=1)
3. Post your verdict (POST {base}/api/v1/verdicts/ with paper_id, content_markdown, score)
   - score: 0.0 (reject) to 10.0 (strong accept)
   - content_markdown: full review, at least 2-3 paragraphs
   - One verdict per paper, immutable — make it count

Use your API key in every request: Authorization header set to your key value (no "Bearer" prefix).

--- PAPERS ---

{{papers_block}}"""


def fetch_platform_papers(limit: int = 200, submitter_id: str | None = BIGBANG_SUBMITTER_ID) -> list[dict]:
    """Fetch papers from the Coalescence REST API, optionally filtered by submitter."""
    url = f"{PAPERS_API}?sort=new&limit={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": "reva/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        papers = json.loads(resp.read())
    if submitter_id:
        papers = [p for p in papers if p.get("submitter_id") == submitter_id]
    return papers


def cache_paper_text(paper_id: str, pdf_url: str, cache_dir: Path, delay: float = 1.0) -> str:
    """Fetch and cache plain text for one platform paper. Returns the text."""
    cache_file = cache_dir / f"{paper_id}.txt"
    if cache_file.exists():
        return cache_file.read_text(encoding="utf-8")

    if not pdf_url:
        return ""

    # Resolve relative URLs
    if pdf_url.startswith("/"):
        pdf_url = PLATFORM_BASE_URL + pdf_url

    try:
        import pdfplumber
        req = urllib.request.Request(pdf_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            pdf_bytes = resp.read()
        time.sleep(delay)
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            pages = [page.extract_text() or "" for page in pdf.pages]
        text = "\n".join(pages)
    except Exception as exc:
        logger.warning("PDF fetch failed for %s: %s", paper_id, exc)
        text = ""

    cache_file.write_text(text, encoding="utf-8")
    return text


def build_paper_db(
    db_path: str | Path,
    cache_dir: str | Path,
    *,
    fetch_pdfs: bool = True,
    limit: int = 200,
    delay: float = 0.5,
    parallel: int = 8,
    submitter_id: str | None = BIGBANG_SUBMITTER_ID,
) -> list[dict]:
    """Fetch all platform papers, optionally cache their PDFs, save DB.

    Returns the list of paper dicts saved to db_path.
    """
    db_path = Path(db_path)
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Fetching platform papers (limit=%d, submitter_id=%s)...", limit, submitter_id or "all")
    papers = fetch_platform_papers(limit=limit, submitter_id=submitter_id)
    logger.info("Fetched %d papers", len(papers))

    # Resume: load existing DB entries to skip already-processed papers
    existing: dict[str, dict] = {}
    if db_path.exists():
        for entry in json.loads(db_path.read_text(encoding="utf-8")):
            existing[entry["id"]] = entry
        logger.info("Resuming — %d papers already in DB", len(existing))

    pending = [p for p in papers if p["id"] not in existing]
    logger.info("%d papers to process", len(pending))

    lock = threading.Lock()
    db: list[dict] = list(existing.values())
    done = [0]

    def _process(p: dict) -> None:
        full_text = ""
        if fetch_pdfs:
            full_text = cache_paper_text(p["id"], p.get("pdf_url", ""), cache_dir, delay=delay)

        entry = {
            "id": p["id"],
            "title": p["title"],
            "abstract": p.get("abstract", ""),
            "domains": p.get("domains", []),
            "pdf_url": p.get("pdf_url", ""),
            "comment_count": p.get("comment_count", 0),
            "submitter_name": p.get("submitter_name", ""),
            "full_text": full_text,
        }
        with lock:
            db.append(entry)
            done[0] += 1
            _atomic_write(db_path, db)
            logger.info("Cached %d/%d: %s", done[0], len(pending), p["title"][:60])

    workers = max(1, min(parallel, len(pending))) if pending else 1
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_process, p) for p in pending]
        for f in as_completed(futures):
            f.result()  # re-raise any exception

    logger.info("Saved paper DB to %s (%d papers)", db_path, len(db))
    return db


def _atomic_write(path: Path, data: list) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_paper_db(db_path: str | Path) -> list[dict]:
    return json.loads(Path(db_path).read_text(encoding="utf-8"))


def sample_papers(db: list[dict], n: int = 40, seed: int | None = None) -> list[dict]:
    rng = random.Random(seed)
    return rng.sample(db, min(n, len(db)))


def build_deploy_initial_prompt(papers: list[dict]) -> str:
    """Build a training-style initial prompt with platform papers embedded."""
    papers_block = _format_papers_block(papers)
    auth_section = _DEPLOY_INITIAL_PROMPT.format(base=PLATFORM_BASE_URL)
    return auth_section.format(n_papers=len(papers), papers_block=papers_block)


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

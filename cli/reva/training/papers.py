"""Paper loading, ground truth building, and PDF/LaTeX caching for training."""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from pathlib import Path

import pdfplumber
from pylatexenc.latex2text import LatexNodes2Text

logger = logging.getLogger(__name__)

# Fields agents are allowed to see (no citation_count, poisoned, or decision)
_AGENT_FIELDS = {"title", "abstract", "domains", "pdf_url"}

_LATEX_CONVERTER = LatexNodes2Text(strict_latex_spaces=False)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_papers(data_dir: str | Path) -> list[dict]:
    """Load the 40 papers, returning only agent-visible fields plus 'id'.

    Args:
        data_dir: Path to the directory containing final_dataset.json.

    Returns:
        List of dicts with keys: id, title, abstract, domains, pdf_url.
        citation_count, poisoned, and decision are stripped.
    """
    data_dir = Path(data_dir)
    raw = json.loads((data_dir / "final_dataset.json").read_text(encoding="utf-8"))
    return [
        {"id": i, **{k: v for k, v in paper.items() if k in _AGENT_FIELDS}}
        for i, paper in enumerate(raw)
    ]


def build_ground_truth(data_dir: str | Path) -> dict[int, dict]:
    """Build the ground-truth map for all 40 papers.

    Args:
        data_dir: Path to the directory containing final_dataset.json and
                  iclr_2024_papers.json.

    Returns:
        Dict mapping paper_id → {"citation_count": int, "accepted": bool}.
        accepted is True only when the ICLR decision contains "Accept".
        All other cases (Reject, Withdrawn, no ICLR match, poisoned) → False.
    """
    data_dir = Path(data_dir)
    raw = json.loads((data_dir / "final_dataset.json").read_text(encoding="utf-8"))
    iclr = json.loads((data_dir / "iclr_2024_papers.json").read_text(encoding="utf-8"))

    iclr_by_url: dict[str, dict] = {p["pdf_url"]: p for p in iclr}

    ground_truth: dict[int, dict] = {}
    for i, paper in enumerate(raw):
        iclr_entry = iclr_by_url.get(paper["pdf_url"])
        decision = iclr_entry.get("decision", "") if iclr_entry else ""
        accepted = "Accept" in decision
        ground_truth[i] = {
            "citation_count": paper["citation_count"],
            "accepted": accepted,
        }
    return ground_truth


def cache_papers(
    data_dir: str | Path,
    flaws_dir: str | Path,
    cache_dir: str | Path,
    delay: float = 1.0,
) -> None:
    """Fetch/extract all 40 papers to plain-text cache files.

    Non-poisoned papers: fetch PDF from pdf_url, extract text via pdfplumber.
    Poisoned papers: read combined.tex from the FLAWS directory, strip LaTeX.

    Both types produce identical-format output in cache_dir/<id>.txt.
    Already-cached files are skipped.

    Args:
        data_dir: Directory containing final_dataset.json.
        flaws_dir: Root of the FLAWS/data/papers/ directory.
        cache_dir: Destination directory for .txt cache files.
        delay: Seconds to sleep between PDF fetches (rate-limit protection).
    """
    data_dir = Path(data_dir)
    flaws_dir = Path(flaws_dir)
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    raw = json.loads((data_dir / "final_dataset.json").read_text(encoding="utf-8"))

    for i, paper in enumerate(raw):
        out_path = cache_dir / f"{i}.txt"
        if out_path.exists():
            logger.debug("Cache hit: paper %d", i)
            continue

        if paper.get("poisoned"):
            text = _extract_latex(paper["title"], flaws_dir)
        else:
            text = _fetch_pdf_text(paper["pdf_url"], delay=delay)

        out_path.write_text(text, encoding="utf-8")
        logger.info("Cached paper %d: %s", i, paper["title"][:60])


def load_cached_paper_text(cache_dir: str | Path, paper_id: int) -> str:
    """Return the cached full text for a paper, or empty string if missing."""
    path = Path(cache_dir) / f"{paper_id}.txt"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _title_to_slug(title: str) -> str:
    """Convert a paper title to a filesystem-safe slug."""
    slug = title.replace(" ", "_")
    for ch in ":,?!'\"":
        slug = slug.replace(ch, "")
    return slug


def _find_flaws_dir(title: str, flaws_root: Path) -> Path | None:
    """Find the FLAWS directory for a poisoned paper by title.

    FLAWS directory names are slugified titles, sometimes truncated at ~80
    chars due to filesystem limits. We match by checking whether the full
    slug starts with the candidate directory name (allowing truncation).
    """
    slug = _title_to_slug(title)
    # Exact match first
    exact = flaws_root / slug
    if exact.is_dir():
        return exact
    # Prefix match: find a dir whose name is a prefix of our slug
    for candidate in flaws_root.iterdir():
        if not candidate.is_dir():
            continue
        name = candidate.name
        if slug.startswith(name) or name.startswith(slug):
            return candidate
    return None


def _extract_latex(title: str, flaws_root: Path) -> str:
    """Extract plain text from a poisoned paper's combined.tex source."""
    paper_dir = _find_flaws_dir(title, flaws_root)
    if paper_dir is None:
        logger.warning("No FLAWS directory found for: %s", title)
        return ""

    tex_path = paper_dir / "combined.tex"
    if not tex_path.exists():
        logger.warning("combined.tex not found in: %s", paper_dir)
        return ""

    raw_tex = tex_path.read_text(encoding="utf-8", errors="replace")
    try:
        return _LATEX_CONVERTER.latex_to_text(raw_tex)
    except Exception as exc:
        logger.warning("LaTeX extraction failed for %s: %s", title, exc)
        return raw_tex  # fall back to raw LaTeX


def _fetch_pdf_text(pdf_url: str, delay: float = 1.0) -> str:
    """Fetch a PDF from url and extract text via pdfplumber."""
    if not pdf_url:
        return ""
    try:
        import io
        req = urllib.request.Request(pdf_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            pdf_bytes = resp.read()
        time.sleep(delay)
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            pages = [page.extract_text() or "" for page in pdf.pages]
        return "\n".join(pages)
    except Exception as exc:
        logger.warning("PDF fetch/extract failed for %s: %s", pdf_url, exc)
        return ""

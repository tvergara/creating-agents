"""Full evolutionary training loop with state persistence and resume support."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from reva.compiler import compile_agent_prompt
from reva.config import load_config
from reva.training.evaluator import evaluate, EvalResult
from reva.training.mutator import AgentConfig, AxisPools, generate_children, sample_random
from reva.training.papers import build_ground_truth, load_cached_paper_text, load_papers
from reva.training.runner import ScoreEntry, run_agent
from reva.training.selector import AgentResult, select

logger = logging.getLogger(__name__)


@dataclass
class TrainingConfig:
    population: int = 15
    n_survivors: int = 4
    max_generations: int = 50
    papers_per_agent: int = 10
    model: str = "claude-sonnet-4-6"
    run_id: Optional[str] = None
    data_dir: str = "data"
    flaws_dir: str = "FLAWS/data/papers"
    cache_dir: str = "training/paper_cache"
    runs_dir: str = "training/runs"
    config_path: Optional[str] = None  # reva config.toml path
    seed: Optional[int] = None


def run(cfg: TrainingConfig) -> list[AgentConfig]:
    """Execute the full evolutionary training loop.

    Returns the 4 final survivor AgentConfig objects.
    """
    if cfg.run_id is None:
        cfg.run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]

    run_dir = Path(cfg.runs_dir) / cfg.run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # Persist run config
    config_path = run_dir / "config.json"
    if not config_path.exists():
        config_path.write_text(json.dumps(asdict(cfg), indent=2), encoding="utf-8")

    # Load data
    data_dir = Path(cfg.data_dir)
    ground_truth = build_ground_truth(data_dir)
    papers = _load_papers_with_text(data_dir, Path(cfg.cache_dir))

    # Persist ground truth
    gt_path = run_dir / "ground_truth.json"
    if not gt_path.exists():
        gt_path.write_text(
            json.dumps({str(k): v for k, v in ground_truth.items()}, indent=2),
            encoding="utf-8",
        )

    # Build axis pools from reva config
    pools = _build_pools(cfg.config_path)

    prev_survivor_keys: Optional[frozenset] = None
    survivors: list[AgentConfig] = []

    for gen_idx in range(cfg.max_generations):
        gen_dir = run_dir / f"gen_{gen_idx:03d}"
        gen_dir.mkdir(exist_ok=True)

        logger.info("=== Generation %d ===", gen_idx)

        # --- Build population ---
        population = _get_or_build_population(
            gen_dir, gen_idx, survivors, pools, cfg.population, cfg.seed
        )

        # --- Score agents (skip already-completed) ---
        results = _score_generation(
            gen_dir, population, papers, ground_truth, cfg.model
        )

        # --- Select survivors ---
        survivors_result = select(results, n_survivors=cfg.n_survivors)
        survivors = [r.config_obj for r in survivors_result]

        _save_survivors(gen_dir, survivors_result)
        logger.info(
            "Gen %d survivors: citation_corr=%s acceptance_corr=%s",
            gen_idx,
            [f"{r.eval.citation_corr:.3f}" for r in survivors_result],
            [f"{r.eval.acceptance_corr:.3f}" for r in survivors_result],
        )

        # --- Convergence check ---
        survivor_keys = frozenset(_config_key(c) for c in survivors)
        if prev_survivor_keys is not None and survivor_keys == prev_survivor_keys:
            logger.info("Converged at generation %d — survivors unchanged", gen_idx)
            break
        prev_survivor_keys = survivor_keys

    else:
        logger.warning("Reached max_generations=%d without convergence", cfg.max_generations)

    # Write final survivors
    final_path = run_dir / "final_survivors.json"
    final_path.write_text(
        json.dumps([c.as_dict() for c in survivors], indent=2), encoding="utf-8"
    )
    logger.info("Run %s complete. Final survivors written to %s", cfg.run_id, final_path)
    return survivors


def load_survivors(run_dir: str | Path) -> list[AgentConfig]:
    """Load final survivors from a completed run directory."""
    path = Path(run_dir) / "final_survivors.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return [AgentConfig.from_dict(d) for d in data]


def latest_run_dir(runs_dir: str | Path = "training/runs") -> Optional[Path]:
    """Return the most recently created run directory, or None."""
    base = Path(runs_dir)
    if not base.exists():
        return None
    dirs = sorted(d for d in base.iterdir() if d.is_dir())
    return dirs[-1] if dirs else None


def run_status(run_dir: str | Path) -> dict:
    """Return a status summary for a run directory."""
    run_dir = Path(run_dir)
    config = json.loads((run_dir / "config.json").read_text()) if (run_dir / "config.json").exists() else {}
    gen_dirs = sorted(d for d in run_dir.iterdir() if d.is_dir() and d.name.startswith("gen_"))
    final = (run_dir / "final_survivors.json").exists()
    return {
        "run_id": run_dir.name,
        "generations_completed": len(gen_dirs),
        "converged": final,
        "config": config,
        "latest_gen": gen_dirs[-1].name if gen_dirs else None,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class _AgentResultWithConfig(AgentResult):
    """AgentResult that also carries the original AgentConfig object."""
    def __init__(self, config: dict, eval: EvalResult, config_obj: AgentConfig):
        super().__init__(config=config, eval=eval)
        self.config_obj = config_obj


def _load_papers_with_text(data_dir: Path, cache_dir: Path) -> list[dict]:
    papers = load_papers(data_dir)
    for p in papers:
        p["full_text"] = load_cached_paper_text(cache_dir, p["id"])
    return papers


def _build_pools(config_path: Optional[str]) -> AxisPools:
    cfg = load_config(config_path)
    roles = sorted(str(f) for f in cfg.roles_dir.glob("*.md") if f.name != "README.md")
    personas = sorted(str(f) for f in cfg.personas_dir.glob("*.json") if not f.name.startswith("all_"))
    interests = sorted(str(f) for f in cfg.interests_dir.glob("**/*.md") if f.name != "README.md")
    methodologies = sorted(str(f) for f in cfg.review_methodology_dir.glob("*.md") if f.name != "README.md")
    review_formats = sorted(str(f) for f in cfg.review_format_dir.glob("*.md") if f.name != "README.md")
    return AxisPools(
        roles=roles,
        personas=personas,
        interests=interests,
        methodologies=methodologies,
        review_formats=review_formats,
    )


def _get_or_build_population(
    gen_dir: Path,
    gen_idx: int,
    survivors: list[AgentConfig],
    pools: AxisPools,
    population: int,
    seed: Optional[int],
) -> list[AgentConfig]:
    agents_path = gen_dir / "agents.json"
    if agents_path.exists():
        data = json.loads(agents_path.read_text())
        return [AgentConfig.from_dict(d) for d in data]

    gen_seed = None if seed is None else seed + gen_idx
    if gen_idx == 0:
        pop = sample_random(pools, n=population, seed=gen_seed)
    else:
        pop = generate_children(survivors, pools, n_children=population, seed=gen_seed)

    agents_path.write_text(json.dumps([c.as_dict() for c in pop], indent=2), encoding="utf-8")
    return pop


def _score_generation(
    gen_dir: Path,
    population: list[AgentConfig],
    papers: list[dict],
    ground_truth: dict,
    model: str,
) -> list[_AgentResultWithConfig]:
    results_path = gen_dir / "results.json"
    completed: dict[int, dict] = {}

    if results_path.exists():
        for entry in json.loads(results_path.read_text()):
            completed[entry["agent_idx"]] = entry

    results: list[_AgentResultWithConfig] = []

    for i, config in enumerate(population):
        if i in completed:
            entry = completed[i]
            eval_result = EvalResult(
                citation_corr=entry["citation_corr"],
                acceptance_corr=entry["acceptance_corr"],
            )
            results.append(_AgentResultWithConfig(
                config=config.as_dict(), eval=eval_result, config_obj=config
            ))
            logger.debug("Agent %d: resume from cache (citation=%.3f)", i, eval_result.citation_corr)
            continue

        logger.info("Scoring agent %d/%d", i + 1, len(population))
        system_prompt = _compile_prompt(config)
        scores = run_agent(system_prompt, papers, model=model)
        eval_result = evaluate(scores, ground_truth)

        # Persist this agent's result immediately
        completed[i] = {
            "agent_idx": i,
            "config": config.as_dict(),
            "scores": [{"paper_id": s.paper_id, "score": s.score, "reasoning": s.reasoning} for s in scores],
            "citation_corr": eval_result.citation_corr,
            "acceptance_corr": eval_result.acceptance_corr,
        }
        _atomic_write(results_path, list(completed.values()))

        results.append(_AgentResultWithConfig(
            config=config.as_dict(), eval=eval_result, config_obj=config
        ))
        logger.info(
            "Agent %d: citation_corr=%.3f, acceptance_corr=%.3f",
            i, eval_result.citation_corr, eval_result.acceptance_corr,
        )

    return results


def _compile_prompt(config: AgentConfig) -> str:
    from pathlib import Path as P
    return compile_agent_prompt(
        role_path=P(config.role),
        persona_path=P(config.persona),
        interest_path=P(config.interests),
        review_methodology_path=P(config.methodology),
        review_format_path=P(config.review_format),
    )


def _save_survivors(gen_dir: Path, survivors: list[_AgentResultWithConfig]) -> None:
    data = [
        {
            "config": r.config_obj.as_dict(),
            "citation_corr": r.eval.citation_corr,
            "acceptance_corr": r.eval.acceptance_corr,
        }
        for r in survivors
    ]
    (gen_dir / "survivors.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def _config_key(c: AgentConfig) -> tuple:
    return (c.role, c.persona, c.interests, c.methodology, c.review_format)


def _atomic_write(path: Path, data: Any) -> None:
    """Write JSON to a temp file then rename for crash safety."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)

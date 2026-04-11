"""reva train — evolutionary training CLI subgroup."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import click

from reva.backends import BACKEND_CHOICES, get_backend
from reva.compiler import compile_agent_prompt
from reva.config import DEFAULT_INITIAL_PROMPT, load_config

logger = logging.getLogger(__name__)


@click.group("train")
def train_group():
    """Evolutionary training system for reviewer agents."""
    pass


# ---------------------------------------------------------------------------
# reva train fetch-pdfs
# ---------------------------------------------------------------------------


@train_group.command("fetch-pdfs")
@click.option("--data-dir", default="data", show_default=True, help="Directory containing final_dataset.json.")
@click.option("--flaws-dir", default="FLAWS/data/papers", show_default=True, help="Root of FLAWS poisoned paper sources.")
@click.option("--cache-dir", default="training/paper_cache", show_default=True, help="Output directory for cached .txt files.")
@click.option("--delay", type=float, default=1.0, show_default=True, help="Seconds between PDF fetches.")
def fetch_pdfs(data_dir, flaws_dir, cache_dir, delay):
    """Pre-fetch and cache all 40 paper texts (run before training)."""
    from reva.training.papers import cache_papers

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    click.echo(f"Caching papers to {cache_dir} ...")
    cache_papers(data_dir, flaws_dir, cache_dir, delay=delay)
    count = len(list(Path(cache_dir).glob("*.txt")))
    click.echo(f"Done. {count} paper(s) cached in {cache_dir}")


# ---------------------------------------------------------------------------
# reva train run
# ---------------------------------------------------------------------------


@train_group.command("run")
@click.option("--population", type=int, default=15, show_default=True)
@click.option("--survivors", "n_survivors", type=int, default=4, show_default=True)
@click.option("--max-generations", type=int, default=50, show_default=True)
@click.option("--model", default="claude-opus-4-6", show_default=True)
@click.option("--run-id", default=None, help="Custom run ID (auto-generated if omitted).")
@click.option("--data-dir", default="data", show_default=True)
@click.option("--flaws-dir", default="FLAWS/data/papers", show_default=True)
@click.option("--cache-dir", default="training/paper_cache", show_default=True)
@click.option("--runs-dir", default="training/runs", show_default=True)
@click.option("--seed", type=int, default=None)
@click.option("--parallel", type=int, default=1, show_default=True, help="Number of agents to score concurrently.")
@click.option("--config", "config_path", default=None, help="Path to reva config.toml.")
def train_run(population, n_survivors, max_generations, model, run_id, data_dir,
              flaws_dir, cache_dir, runs_dir, seed, parallel, config_path):
    """Run the full evolutionary training loop."""
    from reva.training.orchestrator import TrainingConfig, run

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    cfg = TrainingConfig(
        population=population,
        n_survivors=n_survivors,
        max_generations=max_generations,
        model=model,
        run_id=run_id,
        data_dir=data_dir,
        flaws_dir=flaws_dir,
        cache_dir=cache_dir,
        runs_dir=runs_dir,
        seed=seed,
        parallel=parallel,
        config_path=config_path,
    )

    click.echo(f"Starting training run (population={population}, survivors={n_survivors}, max_gen={max_generations}, parallel={parallel})")
    survivors = run(cfg)
    click.echo(f"\nTraining complete. Run ID: {cfg.run_id}")
    click.echo(f"Final survivors written to training/runs/{cfg.run_id}/final_survivors.json")
    click.echo(f"  Use `reva train results --run-id {cfg.run_id}` to view results.")
    click.echo(f"  Use `reva train export --run-id {cfg.run_id}` to export agents.")


# ---------------------------------------------------------------------------
# reva train status
# ---------------------------------------------------------------------------


@train_group.command("status")
@click.option("--run-id", default=None, help="Run ID (defaults to most recent).")
@click.option("--runs-dir", default="training/runs", show_default=True)
def train_status(run_id, runs_dir):
    """Show status of a training run."""
    from reva.training.orchestrator import latest_run_dir, run_status

    run_dir = _resolve_run_dir(run_id, runs_dir)
    status = run_status(run_dir)

    click.echo(f"Run ID:               {status['run_id']}")
    click.echo(f"Generations complete: {status['generations_completed']}")
    click.echo(f"Converged:            {status['converged']}")
    click.echo(f"Latest generation:    {status['latest_gen'] or 'none'}")

    cfg = status.get("config", {})
    if cfg:
        click.echo(f"Population:           {cfg.get('population')}")
        click.echo(f"Survivors:            {cfg.get('n_survivors')}")
        click.echo(f"Max generations:      {cfg.get('max_generations')}")

    if status['converged']:
        _print_survivors(run_dir)


# ---------------------------------------------------------------------------
# reva train results
# ---------------------------------------------------------------------------


@train_group.command("results")
@click.option("--run-id", default=None, help="Run ID (defaults to most recent).")
@click.option("--runs-dir", default="training/runs", show_default=True)
def train_results(run_id, runs_dir):
    """Print final survivors with their correlation scores."""
    run_dir = _resolve_run_dir(run_id, runs_dir)
    final_path = run_dir / "final_survivors.json"
    if not final_path.exists():
        raise click.ClickException("No final_survivors.json found. Has the run completed?")

    survivors = json.loads(final_path.read_text())

    # Try to find correlation scores from the last generation's survivors.json
    gen_dirs = sorted(d for d in run_dir.iterdir() if d.is_dir() and d.name.startswith("gen_"))
    scores_by_config: dict[str, tuple] = {}
    if gen_dirs:
        last_survivors_path = gen_dirs[-1] / "survivors.json"
        if last_survivors_path.exists():
            for entry in json.loads(last_survivors_path.read_text()):
                key = json.dumps(entry["config"], sort_keys=True)
                scores_by_config[key] = (entry["citation_corr"], entry["acceptance_corr"])

    click.echo(f"\nFinal survivors for run: {run_dir.name}\n")
    click.echo(f"{'#':<3} {'citation_corr':>14} {'acceptance_corr':>16}  config")
    click.echo("-" * 80)
    for i, cfg in enumerate(survivors):
        key = json.dumps(cfg, sort_keys=True)
        cit, acc = scores_by_config.get(key, (float("nan"), float("nan")))
        click.echo(f"{i:<3} {cit:>14.4f} {acc:>16.4f}  {_short_config(cfg)}")


# ---------------------------------------------------------------------------
# reva train export
# ---------------------------------------------------------------------------


@train_group.command("export")
@click.option("--run-id", default=None, help="Run ID (defaults to most recent).")
@click.option("--runs-dir", default="training/runs", show_default=True)
@click.option("--output-dir", default="./agents/", show_default=True, type=click.Path())
@click.option("--backend", default="claude-code", type=click.Choice(BACKEND_CHOICES))
@click.option("--config", "config_path", default=None, help="Path to reva config.toml.")
def train_export(run_id, runs_dir, output_dir, backend, config_path):
    """Export final survivors as reva agent directories (ready for reva batch launch)."""
    from reva.training.mutator import AgentConfig
    from reva.training.orchestrator import load_survivors

    run_dir = _resolve_run_dir(run_id, runs_dir)
    survivors = load_survivors(run_dir)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    backend_obj = get_backend(backend)

    for i, cfg in enumerate(survivors):
        agent_name = f"trained_{run_dir.name}_{i:02d}"
        agent_dir = out / agent_name
        agent_dir.mkdir(exist_ok=True)

        prompt = compile_agent_prompt(
            role_path=Path(cfg.role),
            persona_path=Path(cfg.persona),
            interest_path=Path(cfg.interests),
            review_methodology_path=Path(cfg.methodology),
            review_format_path=Path(cfg.review_format),
        )

        (agent_dir / "prompt.md").write_text(prompt, encoding="utf-8")
        (agent_dir / backend_obj.prompt_filename).write_text(prompt, encoding="utf-8")
        (agent_dir / "initial_prompt.txt").write_text(DEFAULT_INITIAL_PROMPT, encoding="utf-8")
        (agent_dir / ".agent_name").write_text(agent_name, encoding="utf-8")

        config_data = {
            "name": agent_name,
            "backend": backend,
            "role": cfg.role,
            "persona": cfg.persona,
            "interest": cfg.interests,
            "review_methodology": cfg.methodology,
            "review_format": cfg.review_format,
            "trained_from_run": run_dir.name,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        (agent_dir / "config.json").write_text(json.dumps(config_data, indent=2), encoding="utf-8")
        click.echo(f"  exported: {agent_name}")

    click.echo(f"\n{len(survivors)} agent(s) written to {out}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_run_dir(run_id: str | None, runs_dir: str) -> Path:
    from reva.training.orchestrator import latest_run_dir
    if run_id:
        run_dir = Path(runs_dir) / run_id
    else:
        run_dir = latest_run_dir(runs_dir)
    if run_dir is None or not run_dir.exists():
        raise click.ClickException(f"Run directory not found: {run_dir}")
    return run_dir


def _print_survivors(run_dir: Path) -> None:
    final_path = run_dir / "final_survivors.json"
    if not final_path.exists():
        return
    survivors = json.loads(final_path.read_text())
    click.echo("\nFinal survivors:")
    for i, cfg in enumerate(survivors):
        click.echo(f"  {i}: {_short_config(cfg)}")


def _short_config(cfg: dict) -> str:
    return " | ".join(
        Path(cfg.get(k, "?")).stem
        for k in ("role", "persona", "interests", "methodology", "review_format")
    )

"""reva train — evolutionary training CLI subgroup."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import click

from reva.backends import BACKEND_CHOICES, get_backend, write_setup_files
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
@click.option("--survivors", "n_survivors", type=int, default=6, show_default=True)
@click.option("--max-generations", type=int, default=6, show_default=True)
@click.option("--backend", default="claude-code", type=click.Choice(["claude-code", "gemini-cli"]), show_default=True)
@click.option("--model", default=None, help="Model override (default: backend's default model).")
@click.option("--run-id", default=None, help="Custom run ID (auto-generated if omitted).")
@click.option("--data-dir", default="data", show_default=True)
@click.option("--flaws-dir", default="FLAWS/data/papers", show_default=True)
@click.option("--cache-dir", default="training/paper_cache", show_default=True)
@click.option("--runs-dir", default="training/runs", show_default=True)
@click.option("--seed", type=int, default=None)
@click.option("--parallel", type=int, default=4, show_default=True, help="Number of agents to score concurrently.")
@click.option("--seed-from-run", default=None, help="Seed gen 0 with survivors from this run ID.")
@click.option("--seed-from-gen", type=int, default=None, help="Generation to load seed survivors from (default: final).")
@click.option("--config", "config_path", default=None, help="Path to reva config.toml.")
def train_run(population, n_survivors, max_generations, backend, model, run_id, data_dir,
              flaws_dir, cache_dir, runs_dir, seed, parallel, seed_from_run, seed_from_gen, config_path):
    """Run the full evolutionary training loop."""
    from reva.training.orchestrator import TrainingConfig, run

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    # Load seed configs from a previous run if requested
    seed_configs = []
    if seed_from_run:
        seed_run_dir = Path(runs_dir) / seed_from_run
        if seed_from_gen is not None:
            raw_survivors = _load_gen_survivors(seed_run_dir, seed_from_gen)
        else:
            from reva.training.orchestrator import load_survivors
            raw_survivors = load_survivors(seed_run_dir)
        # Resolve default selection strategy path for old configs missing that axis
        from reva.config import load_config as _lc
        _cfg = _lc(config_path)
        default_ss = str(_cfg.selection_strategy_dir / "default.md")
        from reva.training.mutator import AgentConfig
        seed_configs = [
            AgentConfig.from_dict(c.as_dict() if hasattr(c, 'as_dict') else c, default_selection_strategy=default_ss).as_dict()
            for c in raw_survivors
        ]
        click.echo(f"Seeding gen 0 with {len(seed_configs)} agent(s) from {seed_from_run}")

    cfg = TrainingConfig(
        population=population,
        n_survivors=n_survivors,
        max_generations=max_generations,
        backend=backend,
        model=model,
        run_id=run_id,
        data_dir=data_dir,
        flaws_dir=flaws_dir,
        cache_dir=cache_dir,
        runs_dir=runs_dir,
        seed=seed,
        parallel=parallel,
        config_path=config_path,
        seed_configs=seed_configs,
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
@click.option("--gen", "gen_idx", default=None, type=int, help="Export survivors from a specific generation (default: use final_survivors.json).")
@click.option("--config", "config_path", default=None, help="Path to reva config.toml.")
def train_export(run_id, runs_dir, output_dir, backend, gen_idx, config_path):
    """Export final survivors as reva agent directories (ready for reva batch launch)."""
    from reva.training.mutator import AgentConfig
    from reva.training.orchestrator import load_survivors

    run_dir = _resolve_run_dir(run_id, runs_dir)

    if gen_idx is not None:
        survivors = _load_gen_survivors(run_dir, gen_idx)
    else:
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
            selection_strategy_path=Path(cfg.selection_strategy),
        )

        (agent_dir / "prompt.md").write_text(prompt, encoding="utf-8")
        (agent_dir / backend_obj.prompt_filename).write_text(prompt, encoding="utf-8")
        (agent_dir / "initial_prompt.txt").write_text(DEFAULT_INITIAL_PROMPT, encoding="utf-8")
        (agent_dir / ".agent_name").write_text(agent_name, encoding="utf-8")
        write_setup_files(backend_obj, agent_dir)

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
        for k in ("role", "persona", "interests", "methodology", "review_format", "selection_strategy")
    )


# ---------------------------------------------------------------------------
# reva train build-paper-db
# ---------------------------------------------------------------------------


@train_group.command("build-paper-db")
@click.option("--db-path", default="training/paper_db.json", show_default=True)
@click.option("--cache-dir", default="training/platform_paper_cache", show_default=True)
@click.option("--limit", type=int, default=200, show_default=True, help="Max papers to fetch from platform.")
@click.option("--fetch-pdfs/--no-fetch-pdfs", default=True, show_default=True)
@click.option("--delay", type=float, default=0.5, show_default=True, help="Seconds between PDF fetches per worker.")
@click.option("--parallel", type=int, default=8, show_default=True, help="Number of concurrent PDF fetches.")
@click.option("--submitter-id", default=None, help="Filter by submitter ID (default: BigBang). Pass empty string to disable.")
def build_paper_db(db_path, cache_dir, limit, fetch_pdfs, delay, parallel, submitter_id):
    """Fetch platform papers and cache their full text for deployment prompts."""
    from reva.training.paper_db import build_paper_db as _build, BIGBANG_SUBMITTER_ID

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sid = submitter_id if submitter_id is not None else BIGBANG_SUBMITTER_ID
    papers = _build(db_path, cache_dir, fetch_pdfs=fetch_pdfs, limit=limit, delay=delay, parallel=parallel, submitter_id=sid or None)
    cached = sum(1 for p in papers if p.get("full_text"))
    click.echo(f"\n{len(papers)} papers saved to {db_path} ({cached} with full text)")


# ---------------------------------------------------------------------------
# reva train build-eval-csv
# ---------------------------------------------------------------------------


@train_group.command("build-eval-csv")
@click.option("--db-path", default="training/paper_db.json", show_default=True)
@click.option("--output", default="training/eval_papers.csv", show_default=True)
def build_eval_csv(db_path, output):
    """Build an eval CSV from the paper DB (mirrors training dataset format)."""
    import csv
    from reva.training.paper_db import load_paper_db

    db_file = Path(db_path)
    if not db_file.exists():
        raise click.ClickException(f"Paper DB not found: {db_path}\nRun `reva train build-paper-db` first.")

    db = load_paper_db(db_file)

    fields = ["id", "title", "abstract", "domains", "pdf_url", "comment_count", "has_full_text"]

    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for p in db:
            writer.writerow({
                "id": p["id"],
                "title": p["title"],
                "abstract": p["abstract"],
                "domains": "; ".join(p.get("domains", [])),
                "pdf_url": p.get("pdf_url", ""),
                "comment_count": p.get("comment_count", 0),
                "has_full_text": bool(p.get("full_text")),
            })

    click.echo(f"Wrote {len(db)} rows to {out}")


# ---------------------------------------------------------------------------
# reva train deploy
# ---------------------------------------------------------------------------


@train_group.command("deploy")
@click.option("--agent-name", required=True, help="Name of the exported agent directory.")
@click.option("--db-path", default="training/paper_db.json", show_default=True)
@click.option("--n-reviews", type=int, default=70, show_default=True, help="Target number of reviews to collect (ignored with --review-all).")
@click.option("--backend", default=None, help="Override backend (default: from agent config).")
@click.option("--model", default="claude-sonnet-4-6", show_default=True, help="Model for claude-code backend.")
@click.option("--seed", type=int, default=None)
@click.option("--parallel", type=int, default=4, show_default=True, help="Concurrent batch calls.")
@click.option("--output", default=None, help="Path to save reviews JSON (default: <agent_dir>/deploy_reviews.json).")
@click.option("--review-all", is_flag=True, default=False, help="Review every paper in the DB (no selection, single call).")
@click.option("--config", "config_path", default=None)
def train_deploy(agent_name, db_path, n_reviews, backend, model, seed, parallel, output, review_all, config_path):
    """Score papers in training-style batches of 40 and save reviews for later posting."""
    from reva.training.deployer import collect_reviews, review_all_papers
    from reva.training.paper_db import load_paper_db

    cfg = load_config(config_path)
    agent_dir = cfg.agents_dir / agent_name
    if not agent_dir.exists():
        raise click.ClickException(f"Agent not found: {agent_dir}")

    db_file = Path(db_path)
    if not db_file.exists():
        raise click.ClickException(f"Paper DB not found: {db_path}\nRun `reva train build-paper-db` first.")

    db = load_paper_db(db_file)

    agent_config = json.loads((agent_dir / "config.json").read_text())
    resolved_backend = backend or agent_config.get("backend", "claude-code")
    system_prompt = (agent_dir / "prompt.md").read_text(encoding="utf-8")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if review_all:
        click.echo(f"Reviewing all {len(db)} papers via {resolved_backend} (parallel={parallel}, no selection)...")
        reviews = review_all_papers(system_prompt, db, backend=resolved_backend, model=model, parallel=parallel)
    else:
        eligible = [p for p in db if p.get("comment_count", 0) >= 1]
        click.echo(f"Papers in DB: {len(db)} total, {len(eligible)} with ≥1 comment")
        if len(eligible) < 40:
            raise click.ClickException(f"Need at least 40 eligible papers, only have {len(eligible)}.")
        click.echo(f"Collecting {n_reviews} reviews via {resolved_backend} (batches of 40, pick 10 each)...")
        reviews = collect_reviews(
            system_prompt, eligible,
            n_target=n_reviews, backend=resolved_backend, model=model, seed=seed, parallel=parallel,
        )

    click.echo(f"Collected {len(reviews)} reviews")

    out_path = Path(output) if output else agent_dir / "deploy_reviews.json"
    out_path.write_text(
        json.dumps([{"paper_id": r.paper_id, "score": r.score, "review": r.review} for r in reviews], indent=2),
        encoding="utf-8",
    )
    click.echo(f"Saved to {out_path}")
    click.echo(f"\nNext: reva train post-verdicts --agent-name {agent_name}")


# ---------------------------------------------------------------------------
# reva train post-verdicts
# ---------------------------------------------------------------------------


@train_group.command("post-verdicts")
@click.option("--agent-name", required=True)
@click.option("--api-key", default=None, help="Coalescence API key (default: read from <agent_dir>/.api_key).")
@click.option("--reviews-path", default=None, help="Path to reviews JSON (default: <agent_dir>/deploy_reviews.json).")
@click.option("--delay", type=float, default=1.0, show_default=True, help="Seconds between verdict posts.")
@click.option("--github-file-url", default=None, help="URL to the agent's prompt file on GitHub (for comment transparency).")
@click.option("--config", "config_path", default=None)
def post_verdicts(agent_name, api_key, reviews_path, delay, github_file_url, config_path):
    """Post collected reviews as verdicts to the Coalescence platform."""
    from reva.training.deployer import DeployReview, post_all_verdicts

    cfg = load_config(config_path)
    agent_dir = cfg.agents_dir / agent_name
    if not agent_dir.exists():
        raise click.ClickException(f"Agent not found: {agent_dir}")

    # Resolve API key
    if not api_key:
        key_file = agent_dir / ".api_key"
        if not key_file.exists():
            raise click.ClickException(
                "No API key found. Pass --api-key or ensure .api_key exists in the agent directory."
            )
        api_key = key_file.read_text(encoding="utf-8").strip()

    # Resolve github_file_url from agent config if not provided
    if not github_file_url:
        try:
            agent_config = json.loads((agent_dir / "config.json").read_text())
            repo = agent_config.get("github_repo", "")
            agent_name_slug = agent_dir.name
            if repo:
                github_file_url = f"{repo}/blob/main/agents/{agent_name_slug}/prompt.md"
        except Exception:
            pass

    # Load reviews
    rp = Path(reviews_path) if reviews_path else agent_dir / "deploy_reviews.json"
    if not rp.exists():
        raise click.ClickException(f"Reviews not found: {rp}\nRun `reva train deploy` first.")

    raw = json.loads(rp.read_text())
    reviews = [DeployReview(paper_id=r["paper_id"], score=r["score"], review=r["review"]) for r in raw]
    click.echo(f"Posting {len(reviews)} verdicts (github_file_url={github_file_url or 'none'})...")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    results = post_all_verdicts(reviews, api_key, delay=delay, github_file_url=github_file_url or "")

    ok = sum(1 for r in results if r["status"] == "ok")
    errors = sum(1 for r in results if r["status"] == "error")
    click.echo(f"\nDone: {ok} posted, {errors} errors")
    if errors:
        for r in results:
            if r["status"] == "error":
                click.echo(f"  {r['paper_id'][:8]}: {r['error']}")


# ---------------------------------------------------------------------------
# reva train history
# ---------------------------------------------------------------------------


@train_group.command("history")
@click.option("--run-id", default=None, help="Run ID (defaults to most recent).")
@click.option("--runs-dir", default="training/runs", show_default=True)
@click.option("--output", default=None, help="Optionally save consolidated results to a JSON file.")
def train_history(run_id, runs_dir, output):
    """Show all agents scored across every generation in a run."""
    run_dir = _resolve_run_dir(run_id, runs_dir)
    gen_dirs = sorted(d for d in run_dir.iterdir() if d.is_dir() and d.name.startswith("gen_"))

    all_entries = []
    for gen_dir in gen_dirs:
        results_path = gen_dir / "results.json"
        if not results_path.exists():
            continue
        for entry in json.loads(results_path.read_text()):
            all_entries.append({"gen": gen_dir.name, **entry})

    if not all_entries:
        click.echo("No scored agents found yet.")
        return

    click.echo(f"Run: {run_dir.name}  —  {len(all_entries)} agents scored across {len(gen_dirs)} generation(s)\n")
    click.echo(f"{'gen':<10} {'idx':<5} {'citation_corr':>14} {'acceptance_corr':>16}  config")
    click.echo("-" * 95)
    for e in all_entries:
        click.echo(
            f"{e['gen']:<10} {e['agent_idx']:<5} {e['citation_corr']:>14.4f} "
            f"{e['acceptance_corr']:>16.4f}  {_short_config(e['config'])}"
        )

    if output:
        Path(output).write_text(json.dumps(all_entries, indent=2), encoding="utf-8")
        click.echo(f"\nSaved to {output}")


# ---------------------------------------------------------------------------
# reva train validate
# ---------------------------------------------------------------------------


@train_group.command("validate")
@click.option("--run-id", default=None, help="Run ID (defaults to most recent).")
@click.option("--runs-dir", default="training/runs", show_default=True)
@click.option("--gen", "gen_idx", default=None, type=int, help="Use survivors from a specific generation (default: final_survivors.json).")
@click.option("--backend", default="claude-code", type=click.Choice(["claude-code", "gemini-cli"]), show_default=True)
@click.option("--model", default=None, help="Model override (default: backend's default model).")
@click.option("--data-dir", default="data", show_default=True)
@click.option("--cache-dir", default="training/paper_cache", show_default=True)
@click.option("--papers-per-agent", type=int, default=10, show_default=True)
@click.option("--parallel", type=int, default=1, show_default=True)
@click.option("--output", default=None, help="Path to save validation results JSON.")
@click.option("--config", "config_path", default=None)
def train_validate(run_id, runs_dir, gen_idx, backend, model, data_dir, cache_dir,
                   papers_per_agent, parallel, output, config_path):
    """Evaluate trained survivors on the val split and report correlation scores."""
    from reva.training.evaluator import evaluate
    from reva.training.orchestrator import _compile_prompt, _score_generation
    from reva.training.papers import build_ground_truth, load_cached_paper_text, load_papers
    from reva.training.runner import run_agent

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    run_dir = _resolve_run_dir(run_id, runs_dir)

    if gen_idx is not None:
        survivors = _load_gen_survivors(run_dir, gen_idx)
        source_label = f"gen_{gen_idx:03d}"
    else:
        from reva.training.orchestrator import load_survivors
        try:
            survivors = load_survivors(run_dir)
            source_label = "final"
        except FileNotFoundError:
            raise click.ClickException(
                "No final_survivors.json found. Use --gen to specify a generation."
            )

    click.echo(f"Validating {len(survivors)} survivor(s) from {run_dir.name} ({source_label}) on val split...")

    # Load val data
    data_path = Path(data_dir)
    val_papers = load_papers(data_path, split="val")
    for p in val_papers:
        p["full_text"] = load_cached_paper_text(cache_dir, p["id"])

    ground_truth = build_ground_truth(data_path, split="val")

    if not val_papers:
        raise click.ClickException("No val papers found in dataset.")

    click.echo(f"Val set: {len(val_papers)} papers, {sum(1 for p in val_papers if p.get('full_text'))} with cached text")

    results = []
    for i, cfg in enumerate(survivors):
        click.echo(f"\nScoring survivor {i} ({_short_config(cfg.as_dict())})...")
        system_prompt = _compile_prompt(cfg)
        scores = run_agent(system_prompt, val_papers, model=model, backend=backend)
        eval_result = evaluate(scores, ground_truth)
        results.append({
            "survivor_idx": i,
            "config": cfg.as_dict(),
            "citation_corr": eval_result.citation_corr,
            "acceptance_corr": eval_result.acceptance_corr,
            "n_scored": len(scores),
        })
        click.echo(f"  citation_corr={eval_result.citation_corr:.4f}  acceptance_corr={eval_result.acceptance_corr:.4f}")

    click.echo(f"\n{'#':<3} {'citation_corr':>14} {'acceptance_corr':>16}  config")
    click.echo("-" * 80)
    for r in results:
        click.echo(
            f"{r['survivor_idx']:<3} {r['citation_corr']:>14.4f} {r['acceptance_corr']:>16.4f}  "
            f"{_short_config(r['config'])}"
        )

    if output:
        out_path = Path(output)
    else:
        out_path = run_dir / f"val_results_{source_label}.json"
    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    click.echo(f"\nResults saved to {out_path}")


# ---------------------------------------------------------------------------
# helpers (existing)
# ---------------------------------------------------------------------------


def _load_gen_survivors(run_dir: Path, gen_idx: int) -> list:
    """Load survivors from a specific generation's survivors.json."""
    from reva.training.mutator import AgentConfig

    gen_dir = run_dir / f"gen_{gen_idx:03d}"
    survivors_path = gen_dir / "survivors.json"
    if not survivors_path.exists():
        raise click.ClickException(f"No survivors.json found for gen {gen_idx}: {survivors_path}")
    data = json.loads(survivors_path.read_text())
    return [AgentConfig.from_dict(entry["config"]) for entry in data]

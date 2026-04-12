# Spec: Evolutionary Training System for Reviewer Agents

## 1. Objective

Build an automated evolutionary training loop that finds optimal reviewer agent configurations by correlating agent paper scores against ground-truth citation counts and acceptance decisions. The system runs fully locally, without the Coalescence platform, producing 4 final survivor agents ready for production deployment.

**Target users:** Researchers running the `reva` CLI to train and deploy reviewer agents.

---

## 2. Background & Existing Infrastructure

An agent is defined by 5 component axes, each a file path:

| Axis | Pool location | Current examples |
|------|--------------|-----------------|
| `role` | `agent_definition/roles/*.md` | novelty evaluator, technical soundness, etc. |
| `persona` | Personas dir (`*.json`) | trait vectors |
| `interests` | Interests dir (`**/*.md`) | research area profiles |
| `review_methodology` | `agent_definition/review_methodology/*.md` | generic, three-stage, etc. |
| `review_format` | `agent_definition/review_formats/*.md` | generic, adversarial, etc. |

The compiled system prompt is assembled by `cli/reva/compiler.py:compile_agent_prompt()`.

**Dataset:** `data/final_dataset.json` — 40 ICLR 2024 papers. Fields: `title`, `abstract`, `domains`, `pdf_url`, `github_repo`, `arxiv_id`, `poisoned`, `citation_count`.

**Ground truth** (hidden from agents, used only for evaluation):
- `citation_count` — from the dataset directly
- `accepted` — cross-referenced from `data/iclr_2024_papers.json` via `pdf_url`; `True` if `decision` contains `"Accept"`, `False` for all others (Reject, Withdrawn, no match, poisoned)

---

## 3. Changes to Existing Agents

### 3.1 Add Verdict Section to Review Formats

All review format files (`agent_definition/review_formats/*.md`) must include a mandatory **Verdict** section appended at the end:

```
## Verdict

**Score: X.X / 10**

[One sentence justifying the score. 0 = reject outright, 10 = exceptional accept.]
```

This section must appear in every review an agent writes. The score is a float in `[0.0, 10.0]`.

---

## 4. Training System

### 4.1 Overview

```
Generation loop:
  1. Create population of N=15 agents (random on gen 0; seeded from survivors on gen > 0)
  2. For each agent: run locally via Anthropic API → collect scores for exactly 10 papers
  3. Evaluate: correlate scores against ground truth for each agent
  4. Select: top 2 by citation correlation + top 2 by acceptance correlation → 4 survivors
  5. Convergence check: if survivors identical to previous generation → stop
  6. Mutate survivors → next generation of 15 agents
  7. Repeat until convergence OR max_generations reached
```

Final output: the 4 survivor agent configurations, exported as `reva`-compatible agent directories.

### 4.2 Paper Presentation to Agents

Agents see only:
- `title`
- `abstract`
- `domains`
- `pdf_url`
- Full PDF text (fetched from `pdf_url`, cached locally)

Agents do **not** see: `citation_count`, `poisoned`, or `decision`.

### 4.3 Local Training Runner

Training does **not** use tmux or the Coalescence platform. Each agent is invoked as a single direct Anthropic API call:

- **System prompt:** the compiled agent prompt (identical to production)
- **User message:** lists all 40 papers with agent-visible fields + full text; instructs agent to select exactly 10 and return structured JSON
- **Expected response format:**

```json
[
  {"paper_id": 0, "score": 7.5, "reasoning": "..."},
  ...
]
```

Exactly 10 entries required. The runner retries up to 3 times if the response is malformed or count != 10.

### 4.4 PDF Caching

Before training begins, all 40 papers are extracted to plain text and cached in `training/paper_cache/<paper_id>.txt`. Sources differ by paper type, but the cache format is identical — agents cannot distinguish poisoned from non-poisoned papers based on how content is served:

- **Non-poisoned papers:** fetch PDF from `pdf_url` (OpenReview) → extract text via `pdfplumber`
- **Poisoned papers:** read `combined.tex` from `FLAWS/data/papers/<paper_slug>/` → strip LaTeX markup via `pylatexenc`

The slug mapping from paper title to FLAWS directory is resolved at cache-build time. Cached files are reused across all generations and runs.

### 4.5 Evaluation (per agent)

After collecting 10 scores from an agent:

1. **Citation correlation:** Spearman ρ between agent's 10 scores and ground-truth `citation_count` for those papers.
2. **Acceptance correlation:** Point-biserial r between agent's 10 scores and ground-truth `accepted` (bool) for those papers.

Agents that fail to produce valid output receive `ρ = -1` for both metrics.

### 4.6 Selection

From the 15 evaluated agents:

1. Rank all agents by citation correlation → take top 2
2. Rank all agents by acceptance correlation → take top 2
3. Deduplicate (same agent may rank highly on both) → union set
4. If union < 4: fill remaining slots from the combined ranking (average of both correlations, descending)
5. Result: exactly 4 survivors

### 4.7 Mutation (next generation)

Distribute 15 children among 4 survivors (round-robin until 15 filled). For each child:

1. Inherit all 5 axes from the parent survivor
2. With 50% probability, randomly select 1 axis and replace it with a uniformly random file from that axis's pool (must differ from parent's current file)

If an axis pool has only 1 option, that axis cannot be mutated.

### 4.8 Convergence

Stop when either:
- **Population fixed:** the 4 survivors in generation `g` are identical (same component file paths) to generation `g-1`
- **Safety cap:** `max_generations` reached (configurable, default 50)

### 4.9 Run State Persistence

Each training run writes state to `training/runs/<run_id>/`:

```
training/runs/<run_id>/
  config.json              # run parameters
  ground_truth.json        # paper → {citation_count, accepted}
  gen_000/
    agents.json            # 15 agent configs for this generation
    results.json           # per-agent {scores, citation_corr, acceptance_corr}
    survivors.json         # 4 selected survivors (component paths + correlations)
  gen_001/
    ...
  final_survivors.json     # copy of last survivors.json
```

A run can be resumed if interrupted (skip already-completed agents in a generation by checking `results.json`).

---

## 5. File & Module Structure

```
training/
  __init__.py
  papers.py        # Load dataset, build ground truth, fetch/cache PDFs
  runner.py        # Direct Anthropic API call per agent; parse+validate JSON scores
  evaluator.py     # Spearman + point-biserial correlations per agent
  selector.py      # Top-2-by-each selection logic
  mutator.py       # Generate next generation from survivors
  orchestrator.py  # Full training loop, convergence detection, state persistence
  cli.py           # Click CLI subgroup `reva train`

training/paper_cache/   # <paper_id>.txt — one file per paper
training/runs/          # Per-run output directories
```

---

## 6. CLI

Added as a `reva train` subgroup:

```bash
# Run full training loop
reva train run [--population 15] [--survivors 4] [--max-generations 50] [--run-id <id>]

# Show status of a run in progress or completed
reva train status [--run-id <id>]

# Print final survivors with their correlation scores
reva train results [--run-id <id>]

# Export final survivors as reva agent directories (ready for reva batch launch)
# Writes the same structure as `reva batch create`: prompt.md, initial_prompt.txt,
# config.json, .agent_name, and the backend-specific prompt file.
reva train export [--run-id <id>] [--output-dir ./agents/] [--backend claude-code]

# Pre-fetch and cache all PDFs (useful to do before a long run)
reva train fetch-pdfs
```

If `--run-id` is omitted, uses the most recent run.

---

## 7. Dependencies

New dependencies to add to `pyproject.toml`:
- `pdfplumber` — PDF text extraction (non-poisoned papers)
- `pylatexenc` — LaTeX-to-text stripping (poisoned papers)
- `scipy` — Spearman and point-biserial correlation
- `anthropic` — direct API client (if not already present)

---

## 8. Code Style & Constraints

- Pure Python, no new CLI frameworks beyond existing `click`
- All new modules go under `training/` at the project root
- `reva train` is wired into the existing `cli/reva/cli.py` as a subgroup
- Do not modify `reva batch create/launch` or any existing agent-running flow
- No backwards-compatibility shims
- No test files unless explicitly requested

---

## 9. Acceptance Criteria

- [ ] All 40 papers are text-extracted and cached before training starts: non-poisoned via `pdfplumber` from `pdf_url`, poisoned via `pylatexenc` from `FLAWS/data/papers/<slug>/combined.tex`; all output to `training/paper_cache/<id>.txt` with identical format
- [ ] Ground truth correctly maps all 40 papers: citation_count (direct) + accepted (cross-ref iclr_2024_papers.json; Withdrawn/no-match/poisoned → False)
- [ ] Each agent reviews exactly 10 papers; malformed responses are retried up to 3 times
- [ ] Spearman and point-biserial correlations computed correctly per agent
- [ ] Selection produces exactly 4 survivors: top-2 citation + top-2 acceptance, deduplicated, filled if needed
- [ ] Mutation distributes 15 children from 4 parents, each child mutating 0 or 1 axis
- [ ] Convergence stops the loop when survivors match the previous generation or max_generations is hit
- [ ] Run state is persisted per generation; interrupted runs resume correctly
- [ ] `reva train export` writes 4 agent directories with full `reva batch create`-compatible structure (`prompt.md`, `initial_prompt.txt`, `config.json`, `.agent_name`, backend prompt file), ready for `reva batch launch`
- [ ] All 5 review format files in `agent_definition/review_formats/` have the Verdict section added
- [ ] `reva train run` works end-to-end on the 40-paper dataset

---

## 10. Out of Scope

- Crossover between two different parent agents (only mutation from one parent)
- Training agents that use the Coalescence platform (training is local API-only)
- Adaptive population sizes
- Anything requiring GPU or distributed compute

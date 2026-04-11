# reva — reviewer agent cli

## Quickstart

Three commands to go from nothing to a live agent:

```bash
uv run reva batch create     # sample 1 random agent (wipes old ones automatically)
uv run reva batch launch     # launch it indefinitely
uv run reva watch            # watch it work in real time
```

All arguments default — roles, interests, and personas are picked up from paths in `config.toml`, one agent is sampled at random, and duration is indefinite.

Keep existing agents and add more with `--no-clean`:

```bash
uv run reva batch create --no-clean --n 5
```

## Setup

```bash
uv sync
```

Config is read from `config.toml` in the project root (walked up from cwd, like git finds `.git/`):

```toml
agents_dir         = "./agent_configs/"
personas_dir       = "./agent_definition/personas/"
roles_dir          = "./agent_definition/roles/"
interests_dir      = "./agent_definition/research_interests/generated_personas/"
global_rules       = "./agent_definition/GLOBAL_RULES.md"
platform_skills    = "./agent_definition/platform_skills.md"
```

Config resolution order (first match wins):

1. `--config /path/to/config.toml` (per-command flag)
2. `REVA_CONFIG` env var
3. Walk up from cwd looking for `config.toml`
4. `~/.reva/config.toml` (global default)

## Batch operations

```bash
# defaults: n=1, random sampling, auto-wipe existing agents
reva batch create

# larger batch, stratified, keep existing agents
reva batch create --n 50 --strategy stratified --no-clean

# launch all agents in parallel (indefinite by default)
reva batch launch

# launch for a fixed duration
reva batch launch --duration 8    # hours

# stop all running agents
reva batch kill
```

## Watch

```bash
reva watch            # stream the most recent agent's activity
reva watch --all      # interleave all running agents
```

Output shows tool calls, thinking blocks, and text responses in real time, parsed from the claude stream-json format.

## Single agent

```bash
# create
reva create \
    --name my-agent \
    --backend claude-code \
    --role path/to/role.md \
    --persona path/to/persona.json \
    --interest path/to/research-interest.md

# launch (indefinite)
reva launch --name my-agent

# stop
reva kill --name my-agent

# see what's running
reva status
```

## List available components

```bash
reva list roles
reva list personas
reva list interests
```

## Debug

```bash
# preview compiled prompts before launching
reva debug --n 3 --strategy random
```

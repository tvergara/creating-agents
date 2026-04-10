"""
run_agents.py

Launches multiple Claude Code agents in parallel, each running against the
Moltbook platform. Each agent has its own directory containing a CLAUDE.md
(assembled by agent_definition/prompt_builder.py) and an .mcp.json config
pointing at the Moltbook MCP server.

Usage:
    python run_agents.py --agent-dirs agents/agent_0 agents/agent_1 ...
    python run_agents.py --agent-dirs agents/agent_0 agents/agent_1 --duration 3600
"""

import argparse
import subprocess
import time
import threading
from pathlib import Path

INITIAL_PROMPT = (
    "You are starting a new session on the Moltbook scientific paper evaluation platform. "
    "Your role, research interests, and persona are described in your instructions. "
    "Begin by browsing recent papers, identify ones that need attention in your area, "
    "and start contributing — whether that means writing a review, engaging with an "
    "existing one, or casting a vote. Use your available platform skills."
)


def run_agent(agent_dir: Path, duration: float | None) -> None:
    """Run a single agent in a loop until duration expires (or forever if None)."""
    start = time.time()
    while True:
        proc = subprocess.Popen(
            ["claude", "-p", INITIAL_PROMPT, "--dangerously-skip-permissions"],
            cwd=agent_dir,
        )
        proc.wait()

        if duration is not None and time.time() - start >= duration:
            break


def launch_agents(agent_dirs: list[Path], duration: float | None) -> None:
    """Launch all agents in parallel and wait for all to finish."""
    threads = [
        threading.Thread(target=run_agent, args=(agent_dir, duration))
        for agent_dir in agent_dirs
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--agent-dirs", nargs="+", required=True,
        help="Paths to agent directories (each must contain a CLAUDE.md and .mcp.json)",
    )
    parser.add_argument(
        "--duration", type=float, default=None,
        help="How long to run each agent in seconds (omit to run indefinitely)",
    )
    args = parser.parse_args()

    agent_dirs = [Path(d) for d in args.agent_dirs]
    launch_agents(agent_dirs, args.duration)

"""
debug_prompts.py

Samples a few agent configs and prints their assembled prompts.
Useful for inspecting what agents will actually see before launching a run.

Usage:
    python debug_prompts.py
    python debug_prompts.py --n 3 --strategy random
    python debug_prompts.py --n 1 --role agent_definition/roles/01_novelty_and_originality.md
"""

import argparse
import glob
from pathlib import Path

from agent_definition.prompt_builder import build_prompt
from launcher.sampler import sample
from launcher.prepare_agents import load, persona_to_prompt

SEPARATOR = "\n" + "=" * 80 + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=3, help="Number of agents to sample")
    parser.add_argument("--strategy", default="stratified", choices=["random", "stratified"])
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--roles", nargs="+", default=sorted(glob.glob("agent_definition/roles/*.md")))
    parser.add_argument("--interests", nargs="+", default=sorted(glob.glob("agent_definition/research_interests/*.json")))
    parser.add_argument("--personas", nargs="+", default=sorted(glob.glob("agent_definition/personas/*.json")))
    parser.add_argument("--scaffolding", default="agent_definition/harness/scaffolding.md")
    args = parser.parse_args()

    scaffolding_prompt = load(args.scaffolding)

    samples = sample(
        roles=args.roles,
        interests=args.interests,
        personas=args.personas,
        n=args.n,
        strategy=args.strategy,
        seed=args.seed,
    )

    for i, agent in enumerate(samples):
        prompt = build_prompt(
            role_prompt=load(agent.role),
            research_interests_prompt=load(agent.interests),
            persona_prompt=persona_to_prompt(agent.persona),
            scaffolding_prompt=scaffolding_prompt,
        )

        print(SEPARATOR)
        print(f"Agent {i+1}/{len(samples)}: {agent.name}")
        print(f"  role:      {Path(agent.role).name}")
        print(f"  interests: {Path(agent.interests).name}")
        print(f"  persona:   {Path(agent.persona).name}")
        print(SEPARATOR)
        print(prompt)

    print(SEPARATOR)
    print(f"Total prompt chars: {sum(len(build_prompt(load(a.role), load(a.interests), persona_to_prompt(a.persona), scaffolding_prompt)) for a in samples)}")


if __name__ == "__main__":
    main()

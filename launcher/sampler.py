"""
sampler.py

Samples agent configurations from the Cartesian product of role × interests × persona.

Two strategies:
  - random:     uniformly sample N combinations (with a seed for reproducibility)
  - stratified: ensure each role and each persona appears at least once,
                then fill remaining slots randomly
"""

import random
from dataclasses import dataclass
from itertools import product
from pathlib import Path


@dataclass
class AgentSample:
    role: str
    interests: str
    persona: str

    @property
    def name(self) -> str:
        return (
            f"{Path(self.role).stem}__{Path(self.interests).stem}__{Path(self.persona).stem}"
        )


def sample(
    roles: list[str],
    interests: list[str],
    personas: list[str],
    n: int,
    strategy: str = "stratified",
    seed: int = 42,
) -> list[AgentSample]:
    """
    Sample n agent configurations.

    Args:
        roles:     Paths to role .md files
        interests: Paths to research interest .md files
        personas:  Paths to persona .json files
        n:         Number of agents to sample
        strategy:  "random" or "stratified"
        seed:      Random seed for reproducibility

    Returns:
        List of AgentSample dataclasses, one per agent.
    """
    rng = random.Random(seed)
    all_combinations = [
        AgentSample(role=r, interests=i, persona=p)
        for r, i, p in product(roles, interests, personas)
    ]

    if n >= len(all_combinations):
        return all_combinations

    if strategy == "random":
        return rng.sample(all_combinations, n)

    if strategy == "stratified":
        return _stratified_sample(all_combinations, roles, personas, n, rng)

    raise ValueError(f"Unknown strategy: {strategy!r}. Choose 'random' or 'stratified'.")


def _stratified_sample(
    all_combinations: list[AgentSample],
    roles: list[str],
    personas: list[str],
    n: int,
    rng: random.Random,
) -> list[AgentSample]:
    """Ensure each role and persona appears at least once, fill remainder randomly."""
    selected = set()

    # Pick one representative per role
    for role in roles:
        candidates = [c for c in all_combinations if c.role == role]
        selected.add(id(pick := rng.choice(candidates)))
        selected_list = [pick]

    # Pick one representative per persona (if not already covered)
    selected_items = [c for c in all_combinations if id(c) in selected]
    covered_personas = {c.persona for c in selected_items}
    for persona in personas:
        if persona not in covered_personas:
            candidates = [c for c in all_combinations if c.persona == persona]
            pick = rng.choice(candidates)
            selected.add(id(pick))
            covered_personas.add(persona)

    selected_items = [c for c in all_combinations if id(c) in selected]

    # Fill remaining slots randomly
    remaining = [c for c in all_combinations if id(c) not in selected]
    rng.shuffle(remaining)
    selected_items += remaining[: max(0, n - len(selected_items))]

    return selected_items[:n]

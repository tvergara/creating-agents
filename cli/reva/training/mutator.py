"""Generate the next generation of agents by mutating survivors."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class AgentConfig:
    """The 5-axis configuration of one agent."""
    role: str
    persona: str
    interests: str
    methodology: str
    review_format: str

    def as_dict(self) -> dict:
        return {
            "role": self.role,
            "persona": self.persona,
            "interests": self.interests,
            "methodology": self.methodology,
            "review_format": self.review_format,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AgentConfig":
        return cls(
            role=d["role"],
            persona=d["persona"],
            interests=d["interests"],
            methodology=d["methodology"],
            review_format=d["review_format"],
        )


@dataclass
class AxisPools:
    """Available files for each agent axis."""
    roles: list[str]
    personas: list[str]
    interests: list[str]
    methodologies: list[str]
    review_formats: list[str]


_AXES = ("role", "persona", "interests", "methodology", "review_format")
_POOL_ATTRS = ("roles", "personas", "interests", "methodologies", "review_formats")


def generate_children(
    survivors: list[AgentConfig],
    pools: AxisPools,
    n_children: int = 15,
    seed: Optional[int] = None,
) -> list[AgentConfig]:
    """Generate the next generation by mutating survivors.

    Children are distributed round-robin across survivors. Each child
    inherits all 5 axes from its parent. With 50% probability, exactly
    one randomly chosen axis is replaced with a different file from that
    axis's pool. Axes with only one pool option are never mutated.

    Args:
        survivors: The selected parent agents (typically 4).
        pools: Available files for each axis.
        n_children: Total children to generate (default 15).
        seed: Optional RNG seed for reproducibility.

    Returns:
        List of n_children AgentConfig objects.
    """
    rng = random.Random(seed)
    children: list[AgentConfig] = []

    for i in range(n_children):
        parent = survivors[i % len(survivors)]
        child = _maybe_mutate(parent, pools, rng)
        children.append(child)

    return children


def sample_random(pools: AxisPools, n: int, seed: Optional[int] = None) -> list[AgentConfig]:
    """Sample n random agent configurations from the pools (for generation 0)."""
    rng = random.Random(seed)
    configs = []
    for _ in range(n):
        configs.append(AgentConfig(
            role=rng.choice(pools.roles),
            persona=rng.choice(pools.personas),
            interests=rng.choice(pools.interests),
            methodology=rng.choice(pools.methodologies),
            review_format=rng.choice(pools.review_formats),
        ))
    return configs


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _maybe_mutate(parent: AgentConfig, pools: AxisPools, rng: random.Random) -> AgentConfig:
    """Return a child that is the parent with at most 1 axis mutated (50% chance)."""
    if rng.random() < 0.5:
        return AgentConfig(**parent.as_dict())  # no mutation

    # Find axes that can be mutated (pool has more than 1 option)
    mutable: list[tuple[str, str, list[str]]] = []
    for axis, pool_attr in zip(_AXES, _POOL_ATTRS):
        pool = getattr(pools, pool_attr)
        current = getattr(parent, axis)
        alternatives = [p for p in pool if p != current]
        if alternatives:
            mutable.append((axis, current, alternatives))

    if not mutable:
        return AgentConfig(**parent.as_dict())  # nothing to mutate

    axis, _, alternatives = rng.choice(mutable)
    new_val = rng.choice(alternatives)
    child_dict = parent.as_dict()
    child_dict[axis] = new_val
    return AgentConfig(**child_dict)

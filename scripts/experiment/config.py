"""Experiment configuration for Graphiti vs Seshat comparison."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class LLMConfig:
    """Configuration for a single LLM provider in the experiment."""

    name: str
    medium_model: str
    small_model: str
    embedder_model: str = "text-embedding-3-small"
    embedding_dim: int = 1024


OPENAI_CONFIG = LLMConfig(
    name="graphiti-openai",
    medium_model="gpt-4.1-mini",
    small_model="gpt-4.1-nano",
)

ANTHROPIC_CONFIG = LLMConfig(
    name="graphiti-anthropic",
    medium_model="claude-haiku-4-5-latest",
    small_model="claude-haiku-4-5-latest",
)

LLM_CONFIGS: dict[str, LLMConfig] = {
    "openai": OPENAI_CONFIG,
    "anthropic": ANTHROPIC_CONFIG,
}


@dataclass(frozen=True)
class ExperimentConfig:
    """Top-level experiment configuration."""

    llm: str = "openai"
    scenarios: list[int] = field(default_factory=lambda: [1, 2, 3, 4, 5, 6])
    episodes: int = 50
    scale_episodes: int = 500
    output_dir: Path = Path("telemetry/evaluation/graphiti")
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "neo4j_dev_password"
    graphiti_neo4j_uri: str = "bolt://localhost:7688"
    graphiti_neo4j_user: str = "neo4j"
    graphiti_neo4j_password: str = "graphiti_experiment"

    @property
    def llm_config(self) -> LLMConfig:
        return LLM_CONFIGS[self.llm]

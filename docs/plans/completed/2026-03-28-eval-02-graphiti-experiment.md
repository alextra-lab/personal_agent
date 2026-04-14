# Graphiti Experiment Implementation Plan

> **ARCHIVED INFRASTRUCTURE (Apr 2026)**
> The ephemeral second Neo4j (`neo4j-experiment`, port `7688`) and the Graphiti
> harness (`scripts/graphiti_experiment.py` + `scripts/experiment/`) have been
> archived to `scripts/archive/graphiti_experiment/` and removed from compose.
> The procedural steps below are historical record — **do not re-run** them as-is.
> See `docs/research/GRAPHITI_EXPERIMENT_REPORT.md` for conclusions.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run a comparative experiment between Graphiti and the current Seshat Neo4j backend across 6 scenarios (episodic retrieval, semantic consolidation, temporal queries, entity dedup, consolidation lifecycle, scaling) and produce a recommendation report.

**Architecture:** Single rerunnable Python script (`scripts/graphiti_experiment.py`) that populates both backends with identical data, runs queries against each, captures metrics, and outputs JSON + markdown results. Seshat side uses the existing `MemoryService` API. Graphiti side uses `graphiti-core` with a fresh Neo4j container on port 7688.

**Tech Stack:** Python 3.12, graphiti-core, openai, neo4j, asyncio, argparse, orjson

**Spec:** `docs/superpowers/specs/2026-03-28-graphiti-experiment-design.md`

---

## File Structure

| File | Responsibility |
|------|---------------|
| `scripts/graphiti_experiment.py` | CLI entry point, argument parsing, orchestrates scenario runs, outputs results |
| `scripts/experiment/config.py` | Experiment configuration dataclass, LLM model matrix, Neo4j URIs |
| `scripts/experiment/data_loader.py` | Loads real telemetry data + generates synthetic episodes |
| `scripts/experiment/seshat_runner.py` | Runs scenarios against current Seshat MemoryService |
| `scripts/experiment/graphiti_runner.py` | Runs scenarios against Graphiti |
| `scripts/experiment/metrics.py` | Timing, precision/recall calculation, cost tracking |
| `scripts/experiment/report.py` | Formats results as JSON + markdown tables |
| `scripts/experiment/__init__.py` | Package init |
| `docker-compose.yml` | Modified: add neo4j-experiment service |

---

## Task 1: Infrastructure — Docker + Dependencies

**Files:**
- Modify: `docker-compose.yml` (add neo4j-experiment service)
- Create: `scripts/experiment/__init__.py`
- Create: `scripts/experiment/config.py`

- [ ] **Step 1: Add experiment Neo4j container to docker-compose.yml**

Add after the existing `neo4j` service block:

```yaml
  neo4j-experiment:
    image: neo4j:5.26-community
    environment:
      NEO4J_AUTH: neo4j/graphiti_experiment
      NEO4J_PLUGINS: '["apoc"]'
    ports:
      - "7688:7687"
      - "7475:7474"
    volumes:
      - neo4j_experiment_data:/data
    healthcheck:
      test: ["CMD-SHELL", "wget --no-verbose --tries=1 --spider http://localhost:7474 || exit 1"]
      interval: 10s
      timeout: 5s
      retries: 5
```

Add to the `volumes:` section at the bottom:

```yaml
  neo4j_experiment_data:
```

- [ ] **Step 2: Start the experiment container and verify**

Run:
```bash
docker compose up -d neo4j-experiment
docker compose ps neo4j-experiment
```

Expected: neo4j-experiment running, healthy. Verify browser at `http://localhost:7475`.

- [ ] **Step 3: Install experiment dependencies**

Run:
```bash
uv pip install graphiti-core openai
```

Verify:
```bash
python -c "import graphiti_core; print(graphiti_core.__version__)"
python -c "import openai; print(openai.__version__)"
```

Expected: Both import without error.

- [ ] **Step 4: Create experiment package init**

Create `scripts/experiment/__init__.py`:

```python
"""Graphiti vs Seshat experiment package (EVAL-02 / FRE-147)."""
```

- [ ] **Step 5: Create experiment config module**

Create `scripts/experiment/config.py`:

```python
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
```

- [ ] **Step 6: Commit**

```bash
git add docker-compose.yml scripts/experiment/__init__.py scripts/experiment/config.py
git commit -m "feat(eval-02): add experiment infrastructure — docker + config"
```

---

## Task 2: Data Loader — Real Telemetry + Synthetic Generator

**Files:**
- Create: `scripts/experiment/data_loader.py`

- [ ] **Step 1: Create data_loader.py with telemetry parser**

Create `scripts/experiment/data_loader.py`:

```python
"""Load real telemetry data and generate synthetic episodes for the experiment."""

from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import orjson

from personal_agent.memory.models import TurnNode
from personal_agent.memory.protocol import Episode


def load_real_episodes(
    telemetry_dir: Path,
    max_episodes: int = 50,
) -> list[Episode]:
    """Parse evaluation telemetry JSON files into Episode objects.

    Scans run-* directories for evaluation_results.json files and extracts
    conversation turns as Episode objects.

    Args:
        telemetry_dir: Path to telemetry/evaluation/ directory.
        max_episodes: Maximum number of episodes to return.

    Returns:
        List of Episode objects parsed from real evaluation data.
    """
    episodes: list[Episode] = []

    for run_dir in sorted(telemetry_dir.glob("run-*")):
        results_file = run_dir / "evaluation_results.json"
        if not results_file.exists():
            continue

        data = orjson.loads(results_file.read_bytes())
        paths = data.get("paths", [])

        for path in paths:
            session_id = path.get("session_id", str(uuid.uuid4()))
            turns = path.get("turns", [])

            for turn in turns:
                user_msg = turn.get("user_message", "")
                response = turn.get("response_text", "")
                trace_id = turn.get("trace_id", str(uuid.uuid4()))

                if not user_msg or not response:
                    continue

                episode = Episode(
                    turn_id=trace_id,
                    session_id=session_id,
                    timestamp=datetime.now(timezone.utc),
                    user_message=user_msg,
                    assistant_response=response,
                )
                episodes.append(episode)

                if len(episodes) >= max_episodes:
                    return episodes

    return episodes


# -- Synthetic data generation ------------------------------------------------

# Entity clusters with deliberate name variations for dedup testing
ENTITY_CLUSTERS: list[dict[str, Any]] = [
    {
        "canonical": "Neo4j",
        "type": "Technology",
        "variations": ["Neo4j", "neo4j", "Neo4J", "neo4j graph database"],
        "description": "Graph database for knowledge storage",
    },
    {
        "canonical": "Claude Code",
        "type": "Technology",
        "variations": ["Claude Code", "claude-code", "CC", "Claude Code CLI"],
        "description": "Anthropic CLI for AI-assisted development",
    },
    {
        "canonical": "Python",
        "type": "Technology",
        "variations": ["Python", "python", "Python 3.12", "CPython"],
        "description": "Programming language used for the agent",
    },
    {
        "canonical": "Elasticsearch",
        "type": "Technology",
        "variations": ["Elasticsearch", "ElasticSearch", "elastic search", "ES"],
        "description": "Search and analytics engine for telemetry",
    },
    {
        "canonical": "Machine Learning",
        "type": "Concept",
        "variations": ["Machine Learning", "ML", "machine learning", "deep learning"],
        "description": "AI training and inference methodology",
    },
    {
        "canonical": "FastAPI",
        "type": "Technology",
        "variations": ["FastAPI", "fast api", "fastapi", "Fast API"],
        "description": "Python web framework for the agent service",
    },
    {
        "canonical": "Graphiti",
        "type": "Technology",
        "variations": ["Graphiti", "graphiti", "Graphiti by Zep", "graphiti-core"],
        "description": "Temporal knowledge graph framework",
    },
    {
        "canonical": "Memory Consolidation",
        "type": "Concept",
        "variations": [
            "Memory Consolidation",
            "memory consolidation",
            "consolidation",
            "episodic to semantic promotion",
        ],
        "description": "Process of promoting episodic memories to semantic knowledge",
    },
    {
        "canonical": "Docker",
        "type": "Technology",
        "variations": ["Docker", "docker", "Docker Compose", "docker-compose"],
        "description": "Container runtime for infrastructure services",
    },
    {
        "canonical": "Cognitive Architecture",
        "type": "Concept",
        "variations": [
            "Cognitive Architecture",
            "cognitive architecture",
            "cognitive agent architecture",
            "agent architecture",
        ],
        "description": "Biologically-inspired agent design framework",
    },
]

# Conversation templates that reference entities naturally
CONVERSATION_TEMPLATES: list[dict[str, str]] = [
    {
        "user": "How does {e1} handle entity deduplication compared to {e2}?",
        "assistant": "The {e1} approach uses {e3} for dedup while {e2} relies on name-based MERGE in the graph database. The key difference is that {e1} adds vector similarity matching before the LLM reasoning step.",
    },
    {
        "user": "What's the current status of the {e1} integration?",
        "assistant": "The {e1} integration is operational. It connects to {e2} for storage and uses {e3} for the extraction pipeline. We recently improved the query performance.",
    },
    {
        "user": "Can you explain how {e1} works in our system?",
        "assistant": "In our {e2}, {e1} serves as the foundation for persistent knowledge. When a conversation is processed, entities are extracted via {e3} and stored as nodes with relationships.",
    },
    {
        "user": "I want to compare {e1} and {e2} for our memory backend.",
        "assistant": "Good question. {e1} offers built-in temporal queries and entity resolution, while {e2} gives us more control over the schema. Both use {e3} under the hood, so the infrastructure cost is similar.",
    },
    {
        "user": "What did we discuss about {e1} last week?",
        "assistant": "Last week we explored how {e1} handles {e2} at scale. The main finding was that {e3} performance degrades above 10K entities without proper indexing.",
    },
    {
        "user": "How should we test the {e1} pipeline?",
        "assistant": "For testing {e1}, I recommend creating fixtures with known entities using {e2}, then verifying extraction quality with {e3}. Run the full pipeline end-to-end with synthetic data first.",
    },
    {
        "user": "Is {e1} better than our current approach for handling temporal data?",
        "assistant": "For temporal queries, {e1} has a clear advantage: it uses a bi-temporal model with valid_at and invalid_at timestamps on every edge. Our current {e2} only tracks recency via {e3} timestamps on turn nodes.",
    },
    {
        "user": "Tell me about the relationship between {e1} and {e2}.",
        "assistant": "{e1} and {e2} are complementary in our architecture. {e1} provides the high-level patterns while {e2} handles the low-level storage. {e3} bridges the two with its extraction pipeline.",
    },
]


def generate_synthetic_episodes(
    count: int = 500,
    days_span: int = 30,
) -> list[Episode]:
    """Generate synthetic episodes with known entities and temporal anchors.

    Creates realistic multi-topic conversations with deliberate entity name
    variations for dedup testing and known temporal references for verification.

    Args:
        count: Number of episodes to generate.
        days_span: Time span in days for the generated episodes.

    Returns:
        List of Episode objects with ground truth in properties.
    """
    episodes: list[Episode] = []
    base_time = datetime.now(timezone.utc) - timedelta(days=days_span)
    session_id = str(uuid.uuid4())

    for i in range(count):
        # Pick 3 entity clusters and a random variation from each
        clusters = random.sample(ENTITY_CLUSTERS, k=min(3, len(ENTITY_CLUSTERS)))
        e1_var = random.choice(clusters[0]["variations"])
        e2_var = random.choice(clusters[1]["variations"])
        e3_var = random.choice(clusters[2]["variations"])

        # Pick a conversation template and fill it
        template = random.choice(CONVERSATION_TEMPLATES)
        user_msg = template["user"].format(e1=e1_var, e2=e2_var, e3=e3_var)
        assistant_msg = template["assistant"].format(e1=e1_var, e2=e2_var, e3=e3_var)

        # Distribute timestamps across the time span
        offset = timedelta(seconds=i * (days_span * 86400 / count))
        timestamp = base_time + offset

        # Rotate session_id every 10 episodes
        if i > 0 and i % 10 == 0:
            session_id = str(uuid.uuid4())

        episode = Episode(
            turn_id=str(uuid.uuid4()),
            session_id=session_id,
            timestamp=timestamp,
            user_message=user_msg,
            assistant_response=assistant_msg,
            entities=[c["canonical"] for c in clusters],
        )
        episodes.append(episode)

    return episodes


def get_ground_truth() -> dict[str, Any]:
    """Return ground truth for precision/recall measurement.

    Returns:
        Dict with canonical entities, expected dedup mappings, and
        expected relationships.
    """
    return {
        "canonical_entities": {c["canonical"]: c for c in ENTITY_CLUSTERS},
        "variation_to_canonical": {
            var: c["canonical"]
            for c in ENTITY_CLUSTERS
            for var in c["variations"]
        },
        "total_canonical_count": len(ENTITY_CLUSTERS),
    }
```

- [ ] **Step 2: Verify data loader works**

Run:
```bash
cd /Users/Alex/Dev/personal_agent
python -c "
import sys; sys.path.insert(0, 'src')
from scripts.experiment.data_loader import load_real_episodes, generate_synthetic_episodes, get_ground_truth
from pathlib import Path

real = load_real_episodes(Path('telemetry/evaluation'), max_episodes=5)
print(f'Real episodes: {len(real)}')
for ep in real[:2]:
    print(f'  {ep.turn_id[:8]}... user={ep.user_message[:50]}...')

synth = generate_synthetic_episodes(count=10, days_span=7)
print(f'Synthetic episodes: {len(synth)}')
for ep in synth[:2]:
    print(f'  entities={ep.entities}, msg={ep.user_message[:60]}...')

gt = get_ground_truth()
print(f'Ground truth: {gt[\"total_canonical_count\"]} canonical entities, {len(gt[\"variation_to_canonical\"])} variations')
"
```

Expected: Real episodes parsed from telemetry, synthetic episodes generated with entity references, ground truth returned.

- [ ] **Step 3: Commit**

```bash
git add scripts/experiment/data_loader.py
git commit -m "feat(eval-02): add data loader — telemetry parser + synthetic generator"
```

---

## Task 3: Metrics Module — Timing, Precision/Recall, Cost

**Files:**
- Create: `scripts/experiment/metrics.py`

- [ ] **Step 1: Create metrics.py**

Create `scripts/experiment/metrics.py`:

```python
"""Metrics collection for the Graphiti experiment."""

from __future__ import annotations

import statistics
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator


@dataclass
class TimingResult:
    """Latency measurements for a set of operations."""

    label: str
    latencies_ms: list[float] = field(default_factory=list)

    @property
    def p50(self) -> float:
        if not self.latencies_ms:
            return 0.0
        return statistics.median(self.latencies_ms)

    @property
    def p95(self) -> float:
        if not self.latencies_ms:
            return 0.0
        sorted_vals = sorted(self.latencies_ms)
        idx = int(len(sorted_vals) * 0.95)
        return sorted_vals[min(idx, len(sorted_vals) - 1)]

    @property
    def mean(self) -> float:
        if not self.latencies_ms:
            return 0.0
        return statistics.mean(self.latencies_ms)

    def to_dict(self) -> dict[str, float]:
        return {
            "p50_ms": round(self.p50, 2),
            "p95_ms": round(self.p95, 2),
            "mean_ms": round(self.mean, 2),
            "count": len(self.latencies_ms),
        }


@asynccontextmanager
async def timed(timing: TimingResult) -> AsyncIterator[None]:
    """Async context manager that records elapsed time in milliseconds."""
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000
        timing.latencies_ms.append(elapsed_ms)


@dataclass
class DedupMetrics:
    """Entity deduplication accuracy metrics."""

    raw_mentions: int = 0
    unique_entities_created: int = 0
    expected_canonical: int = 0
    false_positives: int = 0  # incorrectly merged distinct entities
    false_negatives: int = 0  # failed to merge same entity

    @property
    def dedup_ratio(self) -> float:
        if self.raw_mentions == 0:
            return 0.0
        return self.unique_entities_created / self.raw_mentions

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_mentions": self.raw_mentions,
            "unique_entities_created": self.unique_entities_created,
            "expected_canonical": self.expected_canonical,
            "dedup_ratio": round(self.dedup_ratio, 3),
            "false_positives": self.false_positives,
            "false_negatives": self.false_negatives,
        }


@dataclass
class RetrievalMetrics:
    """Precision and recall for retrieval scenarios."""

    queries: list[dict[str, Any]] = field(default_factory=list)

    def add_query(
        self,
        query_text: str,
        expected_ids: set[str],
        returned_ids: set[str],
        latency_ms: float,
    ) -> None:
        true_positives = len(expected_ids & returned_ids)
        precision = true_positives / len(returned_ids) if returned_ids else 0.0
        recall = true_positives / len(expected_ids) if expected_ids else 0.0

        self.queries.append({
            "query": query_text,
            "precision": precision,
            "recall": recall,
            "latency_ms": latency_ms,
            "expected_count": len(expected_ids),
            "returned_count": len(returned_ids),
        })

    @property
    def avg_precision(self) -> float:
        if not self.queries:
            return 0.0
        return statistics.mean(q["precision"] for q in self.queries)

    @property
    def avg_recall(self) -> float:
        if not self.queries:
            return 0.0
        return statistics.mean(q["recall"] for q in self.queries)

    def to_dict(self) -> dict[str, Any]:
        return {
            "avg_precision": round(self.avg_precision, 3),
            "avg_recall": round(self.avg_recall, 3),
            "query_count": len(self.queries),
            "queries": self.queries,
        }


@dataclass
class CostTracker:
    """Track LLM token usage and estimated cost."""

    input_tokens: int = 0
    output_tokens: int = 0
    embedding_tokens: int = 0
    input_cost_per_mtok: float = 0.0
    output_cost_per_mtok: float = 0.0
    embedding_cost_per_mtok: float = 0.02  # text-embedding-3-small default

    @property
    def estimated_cost_usd(self) -> float:
        input_cost = (self.input_tokens / 1_000_000) * self.input_cost_per_mtok
        output_cost = (self.output_tokens / 1_000_000) * self.output_cost_per_mtok
        embed_cost = (self.embedding_tokens / 1_000_000) * self.embedding_cost_per_mtok
        return round(input_cost + output_cost + embed_cost, 4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "embedding_tokens": self.embedding_tokens,
            "estimated_cost_usd": self.estimated_cost_usd,
        }


@dataclass
class ScenarioResult:
    """Aggregated results for a single scenario."""

    scenario_name: str
    seshat: dict[str, Any] = field(default_factory=dict)
    graphiti: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario_name,
            "seshat": self.seshat,
            "graphiti": self.graphiti,
            "notes": self.notes,
        }
```

- [ ] **Step 2: Verify metrics module**

Run:
```bash
cd /Users/Alex/Dev/personal_agent
python -c "
import sys; sys.path.insert(0, 'scripts')
from experiment.metrics import TimingResult, DedupMetrics, CostTracker

t = TimingResult(label='test')
t.latencies_ms = [10, 20, 30, 40, 50]
print(f'Timing: {t.to_dict()}')

d = DedupMetrics(raw_mentions=100, unique_entities_created=40, expected_canonical=10)
print(f'Dedup: {d.to_dict()}')

c = CostTracker(input_tokens=10000, output_tokens=5000, input_cost_per_mtok=0.40, output_cost_per_mtok=1.60)
print(f'Cost: {c.to_dict()}')
"
```

Expected: Timing p50=30, dedup_ratio=0.4, cost calculated.

- [ ] **Step 3: Commit**

```bash
git add scripts/experiment/metrics.py
git commit -m "feat(eval-02): add metrics module — timing, precision/recall, cost tracking"
```

---

## Task 4: Seshat Runner — Scenarios Against Current Neo4j

**Files:**
- Create: `scripts/experiment/seshat_runner.py`

- [ ] **Step 1: Create seshat_runner.py**

Create `scripts/experiment/seshat_runner.py`:

```python
"""Run experiment scenarios against the current Seshat Neo4j backend."""

from __future__ import annotations

import time
from typing import Any

from personal_agent.memory.models import Entity, MemoryQuery, Relationship, TurnNode
from personal_agent.memory.service import MemoryService

from .config import ExperimentConfig
from .data_loader import ENTITY_CLUSTERS, get_ground_truth
from .metrics import (
    CostTracker,
    DedupMetrics,
    RetrievalMetrics,
    ScenarioResult,
    TimingResult,
    timed,
)

from personal_agent.memory.protocol import Episode


async def create_seshat_service(config: ExperimentConfig) -> MemoryService:
    """Create and connect a MemoryService to the existing Neo4j."""
    service = MemoryService()
    connected = await service.connect()
    if not connected:
        msg = f"Failed to connect to Seshat Neo4j at {config.neo4j_uri}"
        raise ConnectionError(msg)
    return service


def episode_to_turn_node(episode: Episode) -> TurnNode:
    """Convert a protocol Episode to a TurnNode for MemoryService."""
    return TurnNode(
        turn_id=episode.turn_id,
        trace_id=episode.turn_id,
        session_id=episode.session_id,
        timestamp=episode.timestamp,
        user_message=episode.user_message,
        assistant_response=episode.assistant_response,
        key_entities=list(episode.entities),
    )


async def ingest_episodes(
    service: MemoryService,
    episodes: list[Episode],
    timing: TimingResult,
) -> None:
    """Store episodes in Seshat, recording timing per episode."""
    for episode in episodes:
        turn = episode_to_turn_node(episode)
        async with timed(timing):
            await service.create_conversation(turn)
            # Create entity nodes for each entity in the episode
            for entity_name in episode.entities:
                entity = Entity(
                    name=entity_name,
                    entity_type="Technology",  # default; real extraction would classify
                )
                await service.create_entity(entity)


async def run_scenario_1_episodic(
    service: MemoryService,
    episodes: list[Episode],
) -> dict[str, Any]:
    """Scenario 1: Episodic Memory — Store + Retrieve.

    Ingest episodes, then query by entity name and measure retrieval quality.
    """
    ingest_timing = TimingResult(label="seshat_ingest")
    query_timing = TimingResult(label="seshat_query")
    retrieval = RetrievalMetrics()

    # Ingest
    await ingest_episodes(service, episodes, ingest_timing)

    # Query by each canonical entity name
    ground_truth = get_ground_truth()
    for canonical, cluster in ground_truth["canonical_entities"].items():
        query = MemoryQuery(entity_names=[canonical], limit=20)

        start = time.perf_counter()
        async with timed(query_timing):
            result = await service.query_memory(query)
        latency = (time.perf_counter() - start) * 1000

        # Expected: episodes that mention this canonical entity
        expected_ids = {
            ep.turn_id for ep in episodes if canonical in ep.entities
        }
        returned_ids = {c.turn_id for c in result.conversations}

        retrieval.add_query(
            query_text=canonical,
            expected_ids=expected_ids,
            returned_ids=returned_ids,
            latency_ms=latency,
        )

    return {
        "ingest": ingest_timing.to_dict(),
        "query": query_timing.to_dict(),
        "retrieval": retrieval.to_dict(),
    }


async def run_scenario_2_semantic(
    service: MemoryService,
    episodes: list[Episode],
) -> dict[str, Any]:
    """Scenario 2: Semantic Memory — Consolidation Quality.

    Examine what entities exist after ingestion. Since Seshat relies on
    explicit entity creation during ingestion, we check what was stored.
    """
    # Query all entities via broad recall
    broad = await service.query_memory_broad(entity_types=None, recency_days=90, limit=100)
    entities = broad.get("entities", [])
    turns = broad.get("turns_summary", [])

    return {
        "entity_count": len(entities),
        "entities": [
            {"name": e.get("name", ""), "type": e.get("type", ""), "mentions": e.get("mentions", 0)}
            for e in entities[:20]
        ],
        "turns_with_entities": len(turns),
        "notes": "Seshat stores entities explicitly during ingestion; no auto-extraction in this test path.",
    }


async def run_scenario_3_temporal(
    service: MemoryService,
    episodes: list[Episode],
) -> dict[str, Any]:
    """Scenario 3: Temporal Queries.

    Test recency-based filtering at different time windows.
    """
    query_timing = TimingResult(label="seshat_temporal")
    results = []

    for days in [7, 14, 30]:
        query = MemoryQuery(recency_days=days, limit=50)
        async with timed(query_timing):
            result = await service.query_memory(query)

        results.append({
            "recency_days": days,
            "results_count": len(result.conversations),
            "has_relevance_scores": bool(result.relevance_scores),
        })

    return {
        "query_timing": query_timing.to_dict(),
        "temporal_queries": results,
        "notes": "Seshat supports recency_days filtering only — no point-in-time or bi-temporal queries.",
    }


async def run_scenario_4_dedup(
    service: MemoryService,
    episodes: list[Episode],
) -> dict[str, Any]:
    """Scenario 4: Entity Deduplication.

    Ingest episodes with entity name variations and count unique entities created.
    """
    dedup = DedupMetrics()
    ground_truth = get_ground_truth()

    # Count raw mentions across all episodes
    all_entity_names: list[str] = []
    for ep in episodes:
        all_entity_names.extend(ep.entities)

    # Also ingest with variation names to test dedup
    variation_timing = TimingResult(label="seshat_dedup_ingest")
    variation_count = 0
    for cluster in ENTITY_CLUSTERS:
        for variation in cluster["variations"]:
            entity = Entity(name=variation, entity_type=cluster["type"])
            async with timed(variation_timing):
                await service.create_entity(entity)
            variation_count += 1

    # Count unique entities in the graph
    all_entities = await service.query_memory_broad(entity_types=None, recency_days=90, limit=500)
    unique_count = len(all_entities.get("entities", []))

    dedup.raw_mentions = variation_count
    dedup.unique_entities_created = unique_count
    dedup.expected_canonical = ground_truth["total_canonical_count"]
    # Seshat dedup is name-based MERGE — variations with different names create separate nodes
    dedup.false_negatives = max(0, unique_count - ground_truth["total_canonical_count"])

    return {
        "dedup": dedup.to_dict(),
        "ingest_timing": variation_timing.to_dict(),
        "notes": "Seshat uses Neo4j MERGE on exact entity name — no fuzzy/semantic dedup.",
    }


async def run_scenario_5_lifecycle(
    service: MemoryService,
    episodes: list[Episode],
) -> dict[str, Any]:
    """Scenario 5: Consolidation Lifecycle.

    Simulate the working → episodic → semantic lifecycle.
    Steps measured: store episode, create entities, promote to semantic.
    """
    from personal_agent.memory.fact import PromotionCandidate
    from personal_agent.memory.promote import run_promotion_pipeline

    store_timing = TimingResult(label="seshat_store")
    promote_timing = TimingResult(label="seshat_promote")

    # Store episodes (episodic encoding)
    for episode in episodes[:10]:
        turn = episode_to_turn_node(episode)
        async with timed(store_timing):
            await service.create_conversation(turn)
            for name in episode.entities:
                await service.create_entity(Entity(name=name, entity_type="Technology"))

    # Build promotion candidates
    candidates = [
        PromotionCandidate(
            entity_name=c["canonical"],
            entity_type=c["type"],
            mention_count=len(c["variations"]) * 5,
            first_seen=episodes[0].timestamp,
            last_seen=episodes[-1].timestamp,
            source_turn_ids=[ep.turn_id for ep in episodes[:3]],
            description=c["description"],
        )
        for c in ENTITY_CLUSTERS[:5]
    ]

    # Run promotion (semantic integration)
    async with timed(promote_timing):
        promotion_result = await run_promotion_pipeline(service, candidates, trace_id="experiment")

    return {
        "store_timing": store_timing.to_dict(),
        "promote_timing": promote_timing.to_dict(),
        "promoted_count": promotion_result.promoted_count,
        "skipped_count": promotion_result.skipped_count,
        "errors": promotion_result.errors,
        "lifecycle_steps": [
            "1. Store episode (create_conversation + create_entity)",
            "2. Build PromotionCandidate from entity stats",
            "3. Run run_promotion_pipeline()",
            "4. Entity gains memory_type='semantic'",
        ],
    }


async def run_scenario_6_scaling(
    service: MemoryService,
    episodes: list[Episode],
) -> dict[str, Any]:
    """Scenario 6: Scaling.

    Ingest episodes in batches and measure throughput + query latency at checkpoints.
    """
    ingest_timing = TimingResult(label="seshat_scale_ingest")
    checkpoints = [100, 250, 500]
    checkpoint_results = []

    for i, episode in enumerate(episodes):
        turn = episode_to_turn_node(episode)
        async with timed(ingest_timing):
            await service.create_conversation(turn)

        if (i + 1) in checkpoints:
            # Measure query latency at this checkpoint
            query_timing = TimingResult(label=f"seshat_query_at_{i+1}")
            for canonical in ["Neo4j", "Python", "FastAPI"]:
                query = MemoryQuery(entity_names=[canonical], limit=10)
                async with timed(query_timing):
                    await service.query_memory(query)

            checkpoint_results.append({
                "episodes_ingested": i + 1,
                "ingest_mean_ms": round(ingest_timing.mean, 2),
                "query": query_timing.to_dict(),
            })

    return {
        "ingest_total": ingest_timing.to_dict(),
        "checkpoints": checkpoint_results,
    }


async def clean_experiment_data(service: MemoryService) -> None:
    """Remove experiment data from Seshat Neo4j.

    Deletes all Turn nodes created during the experiment (identified by
    experiment session patterns). Does NOT touch production data.
    """
    if service.driver is None:
        return

    async with service.driver.session() as session:
        # Delete only nodes created in this experiment run
        # We use a DETACH DELETE on turns that have no session linkage
        # (experiment turns are not linked to real sessions)
        await session.run(
            "MATCH (t:Turn) WHERE t.properties CONTAINS 'experiment' DETACH DELETE t"
        )
```

- [ ] **Step 2: Commit**

```bash
git add scripts/experiment/seshat_runner.py
git commit -m "feat(eval-02): add Seshat runner — 6 scenarios against current Neo4j"
```

---

## Task 5: Graphiti Runner — Scenarios Against Graphiti

**Files:**
- Create: `scripts/experiment/graphiti_runner.py`

- [ ] **Step 1: Create graphiti_runner.py**

Create `scripts/experiment/graphiti_runner.py`:

```python
"""Run experiment scenarios against Graphiti."""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

# Disable Graphiti telemetry before importing
os.environ["GRAPHITI_TELEMETRY_ENABLED"] = "false"

from graphiti_core import Graphiti
from graphiti_core.llm_client.config import LLMConfig as GraphitiLLMConfig
from graphiti_core.nodes import EpisodeType
from graphiti_core.search.search_config_recipes import (
    NODE_HYBRID_SEARCH_RRF,
)

from personal_agent.memory.protocol import Episode

from .config import ExperimentConfig, LLMConfig
from .data_loader import ENTITY_CLUSTERS, get_ground_truth
from .metrics import (
    CostTracker,
    DedupMetrics,
    RetrievalMetrics,
    ScenarioResult,
    TimingResult,
    timed,
)


def _create_graphiti_llm_client(
    llm_config: LLMConfig,
) -> Any:
    """Create the appropriate Graphiti LLM client based on config."""
    if "claude" in llm_config.medium_model:
        from graphiti_core.llm_client.anthropic_client import AnthropicClient

        return AnthropicClient(
            config=GraphitiLLMConfig(
                model=llm_config.medium_model,
                small_model=llm_config.small_model,
            )
        )
    else:
        from graphiti_core.llm_client.openai_client import OpenAIClient

        return OpenAIClient(
            config=GraphitiLLMConfig(
                model=llm_config.medium_model,
                small_model=llm_config.small_model,
            )
        )


def _create_graphiti_embedder(llm_config: LLMConfig) -> Any:
    """Create OpenAI embedder (used regardless of LLM provider)."""
    from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig

    return OpenAIEmbedder(
        config=OpenAIEmbedderConfig(
            model=llm_config.embedder_model,
            embedding_dim=llm_config.embedding_dim,
        )
    )


async def create_graphiti_client(config: ExperimentConfig) -> Graphiti:
    """Create and initialize a Graphiti client connected to the experiment Neo4j."""
    from graphiti_core.driver.neo4j_driver import Neo4jDriver

    driver = Neo4jDriver(
        uri=config.graphiti_neo4j_uri,
        user=config.graphiti_neo4j_user,
        password=config.graphiti_neo4j_password,
    )

    llm_client = _create_graphiti_llm_client(config.llm_config)
    embedder = _create_graphiti_embedder(config.llm_config)

    graphiti = Graphiti(
        graph_driver=driver,
        llm_client=llm_client,
        embedder=embedder,
    )

    await graphiti.build_indices_and_constraints()
    return graphiti


def _get_token_usage(graphiti: Graphiti) -> dict[str, int]:
    """Extract token usage from Graphiti's tracker."""
    try:
        usage = graphiti.token_tracker.get_total_usage()
        return {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
        }
    except Exception:
        return {"input_tokens": 0, "output_tokens": 0}


async def ingest_episodes(
    graphiti: Graphiti,
    episodes: list[Episode],
    timing: TimingResult,
) -> None:
    """Add episodes to Graphiti, recording timing per episode."""
    for episode in episodes:
        body = f"User: {episode.user_message}\nAssistant: {episode.assistant_response}"
        async with timed(timing):
            await graphiti.add_episode(
                name=f"turn_{episode.turn_id[:8]}",
                episode_body=body,
                source=EpisodeType.message,
                source_description="Evaluation conversation",
                reference_time=episode.timestamp,
                group_id=episode.session_id,
            )


async def run_scenario_1_episodic(
    graphiti: Graphiti,
    episodes: list[Episode],
) -> dict[str, Any]:
    """Scenario 1: Episodic Memory — Store + Retrieve."""
    ingest_timing = TimingResult(label="graphiti_ingest")
    query_timing = TimingResult(label="graphiti_query")
    retrieval = RetrievalMetrics()

    # Ingest
    await ingest_episodes(graphiti, episodes, ingest_timing)

    # Query by each canonical entity name
    ground_truth = get_ground_truth()
    for canonical in ground_truth["canonical_entities"]:
        start = time.perf_counter()
        async with timed(query_timing):
            # Edge search for facts about this entity
            results = await graphiti.search(canonical)
        latency = (time.perf_counter() - start) * 1000

        # Expected: episodes that mention this canonical entity
        expected_ids = {ep.turn_id for ep in episodes if canonical in ep.entities}
        # Graphiti returns edges, not episodes — map source_node_uuid to check coverage
        returned_ids = {str(r.uuid) for r in results} if results else set()

        retrieval.add_query(
            query_text=canonical,
            expected_ids=expected_ids,
            returned_ids=returned_ids,
            latency_ms=latency,
        )

    token_usage = _get_token_usage(graphiti)

    return {
        "ingest": ingest_timing.to_dict(),
        "query": query_timing.to_dict(),
        "retrieval": retrieval.to_dict(),
        "token_usage": token_usage,
    }


async def run_scenario_2_semantic(
    graphiti: Graphiti,
    episodes: list[Episode],
) -> dict[str, Any]:
    """Scenario 2: Semantic Memory — Consolidation Quality.

    Examine what Graphiti auto-extracted after ingestion.
    """
    # Search for all entities (node search)
    nodes = await graphiti.search("*", config=NODE_HYBRID_SEARCH_RRF)

    entities = []
    for node in nodes:
        entities.append({
            "name": getattr(node, "name", str(node)),
            "summary": getattr(node, "summary", ""),
            "labels": getattr(node, "labels", []),
        })

    # Search for facts (edge search)
    edges = await graphiti.search("knowledge relationships")
    facts = []
    for edge in edges[:20]:
        facts.append({
            "fact": getattr(edge, "fact", str(edge)),
            "valid_at": str(getattr(edge, "valid_at", "")),
            "invalid_at": str(getattr(edge, "invalid_at", "")),
        })

    token_usage = _get_token_usage(graphiti)

    return {
        "entity_count": len(entities),
        "entities": entities[:20],
        "fact_count": len(facts),
        "facts": facts[:10],
        "token_usage": token_usage,
        "notes": "Graphiti auto-extracts entities and facts on add_episode — no separate consolidation step.",
    }


async def run_scenario_3_temporal(
    graphiti: Graphiti,
    episodes: list[Episode],
) -> dict[str, Any]:
    """Scenario 3: Temporal Queries."""
    query_timing = TimingResult(label="graphiti_temporal")
    results = []

    # Retrieve recent episodes at different time windows
    now = datetime.now(timezone.utc)
    for days in [7, 14, 30]:
        ref_time = now - timedelta(days=days)
        async with timed(query_timing):
            recent = await graphiti.retrieve_episodes(
                reference_time=now,
                last_n=50,
            )

        # Also test edge search with temporal semantics
        temporal_edges = await graphiti.search(f"discussed in the last {days} days")

        results.append({
            "recency_days": days,
            "episodes_retrieved": len(recent) if recent else 0,
            "temporal_edge_results": len(temporal_edges) if temporal_edges else 0,
        })

    token_usage = _get_token_usage(graphiti)

    return {
        "query_timing": query_timing.to_dict(),
        "temporal_queries": results,
        "token_usage": token_usage,
        "notes": "Graphiti supports bi-temporal model (valid_at/invalid_at) + episode retrieval by reference_time.",
    }


async def run_scenario_4_dedup(
    graphiti: Graphiti,
    episodes: list[Episode],
) -> dict[str, Any]:
    """Scenario 4: Entity Deduplication.

    Ingest episodes with entity name variations via conversation text
    and count how many unique entities Graphiti creates.
    """
    dedup = DedupMetrics()
    ground_truth = get_ground_truth()
    ingest_timing = TimingResult(label="graphiti_dedup_ingest")

    # Create episodes that naturally mention entity variations
    variation_count = 0
    for cluster in ENTITY_CLUSTERS:
        for i, variation in enumerate(cluster["variations"]):
            body = (
                f"User: Tell me about {variation} and how it works.\n"
                f"Assistant: {variation} is {cluster['description']}. "
                f"It's commonly used in our project infrastructure."
            )
            async with timed(ingest_timing):
                await graphiti.add_episode(
                    name=f"dedup_test_{cluster['canonical']}_{i}",
                    episode_body=body,
                    source=EpisodeType.message,
                    source_description="Dedup test conversation",
                    reference_time=datetime.now(timezone.utc),
                    group_id="dedup_test",
                )
            variation_count += 1

    # Count unique entities Graphiti created
    all_nodes = await graphiti.search("*", config=NODE_HYBRID_SEARCH_RRF)
    unique_count = len(all_nodes) if all_nodes else 0

    dedup.raw_mentions = variation_count
    dedup.unique_entities_created = unique_count
    dedup.expected_canonical = ground_truth["total_canonical_count"]

    token_usage = _get_token_usage(graphiti)

    return {
        "dedup": dedup.to_dict(),
        "ingest_timing": ingest_timing.to_dict(),
        "token_usage": token_usage,
        "notes": "Graphiti uses three-tier dedup: vector similarity + BM25 + LLM reasoning.",
    }


async def run_scenario_5_lifecycle(
    graphiti: Graphiti,
    episodes: list[Episode],
) -> dict[str, Any]:
    """Scenario 5: Consolidation Lifecycle.

    With Graphiti, add_episode handles extraction + dedup + edge creation
    in one step. There is no separate promotion/consolidation step.
    """
    ingest_timing = TimingResult(label="graphiti_lifecycle_ingest")

    # Ingest 10 episodes
    await ingest_episodes(graphiti, episodes[:10], ingest_timing)

    # Check what Graphiti produced — entities, edges, communities
    nodes = await graphiti.search("*", config=NODE_HYBRID_SEARCH_RRF)
    edges = await graphiti.search("relationships")

    token_usage = _get_token_usage(graphiti)

    return {
        "ingest_timing": ingest_timing.to_dict(),
        "entities_created": len(nodes) if nodes else 0,
        "facts_created": len(edges) if edges else 0,
        "token_usage": token_usage,
        "lifecycle_steps": [
            "1. add_episode() — extracts entities, resolves/deduplicates, creates edges",
            "2. No separate promotion step needed",
            "3. Entities are immediately searchable via hybrid search",
            "4. Contradiction detection auto-invalidates superseded facts",
        ],
        "notes": "Graphiti collapses working→episodic→semantic into a single add_episode call.",
    }


async def run_scenario_6_scaling(
    graphiti: Graphiti,
    episodes: list[Episode],
) -> dict[str, Any]:
    """Scenario 6: Scaling."""
    ingest_timing = TimingResult(label="graphiti_scale_ingest")
    checkpoints = [100, 250, 500]
    checkpoint_results = []

    for i, episode in enumerate(episodes):
        body = f"User: {episode.user_message}\nAssistant: {episode.assistant_response}"
        async with timed(ingest_timing):
            await graphiti.add_episode(
                name=f"scale_{episode.turn_id[:8]}",
                episode_body=body,
                source=EpisodeType.message,
                source_description="Scaling test",
                reference_time=episode.timestamp,
                group_id=episode.session_id,
            )

        if (i + 1) in checkpoints:
            query_timing = TimingResult(label=f"graphiti_query_at_{i+1}")
            for query_text in ["Neo4j", "Python", "FastAPI"]:
                async with timed(query_timing):
                    await graphiti.search(query_text)

            token_usage = _get_token_usage(graphiti)

            checkpoint_results.append({
                "episodes_ingested": i + 1,
                "ingest_mean_ms": round(ingest_timing.mean, 2),
                "query": query_timing.to_dict(),
                "token_usage": token_usage,
            })

    return {
        "ingest_total": ingest_timing.to_dict(),
        "checkpoints": checkpoint_results,
    }


async def clear_graphiti_data(graphiti: Graphiti) -> None:
    """Clear all data from the experiment Neo4j instance."""
    driver = graphiti.driver
    if driver is None:
        return
    async with driver.session() as session:
        await session.run("MATCH (n) DETACH DELETE n")
```

- [ ] **Step 2: Commit**

```bash
git add scripts/experiment/graphiti_runner.py
git commit -m "feat(eval-02): add Graphiti runner — 6 scenarios against Graphiti"
```

---

## Task 6: Report Generator — JSON + Markdown Output

**Files:**
- Create: `scripts/experiment/report.py`

- [ ] **Step 1: Create report.py**

Create `scripts/experiment/report.py`:

```python
"""Format experiment results as JSON and markdown."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import orjson


def save_json_results(
    results: dict[str, Any],
    output_dir: Path,
    run_id: str,
) -> Path:
    """Save full results as timestamped JSON.

    Args:
        results: Complete experiment results dict.
        output_dir: Directory for output files.
        run_id: Unique run identifier.

    Returns:
        Path to the saved JSON file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{run_id}.json"
    path.write_bytes(orjson.dumps(results, option=orjson.OPT_INDENT_2))
    return path


def format_markdown_report(results: dict[str, Any]) -> str:
    """Format results as markdown tables for pasting into the experiment report.

    Args:
        results: Complete experiment results dict.

    Returns:
        Markdown string with comparison tables.
    """
    config = results.get("config", {})
    scenarios = results.get("scenarios", {})

    lines = [
        f"## Experiment Run: {results.get('run_id', 'unknown')}",
        "",
        f"**Date:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"**LLM:** {config.get('llm', 'unknown')} (medium: {config.get('llm_model', '?')}, small: {config.get('small_model', '?')})",
        f"**Embedder:** {config.get('embedder', 'unknown')}",
        f"**Episodes:** {config.get('episodes', '?')} quality, {config.get('scale_episodes', '?')} scaling",
        "",
    ]

    # Scenario 1: Episodic Retrieval
    if "episodic_retrieval" in scenarios:
        s = scenarios["episodic_retrieval"]
        lines.extend([
            "### Scenario 1: Episodic Memory — Store + Retrieve",
            "",
            "| Metric | Seshat | Graphiti |",
            "|--------|--------|---------|",
        ])
        seshat = s.get("seshat", {})
        graphiti = s.get("graphiti", {})

        si = seshat.get("ingest", {})
        gi = graphiti.get("ingest", {})
        lines.append(f"| Ingest p50 (ms) | {si.get('p50_ms', '-')} | {gi.get('p50_ms', '-')} |")

        sq = seshat.get("query", {})
        gq = graphiti.get("query", {})
        lines.append(f"| Query p50 (ms) | {sq.get('p50_ms', '-')} | {gq.get('p50_ms', '-')} |")
        lines.append(f"| Query p95 (ms) | {sq.get('p95_ms', '-')} | {gq.get('p95_ms', '-')} |")

        sr = seshat.get("retrieval", {})
        gr = graphiti.get("retrieval", {})
        lines.append(f"| Avg Precision | {sr.get('avg_precision', '-')} | {gr.get('avg_precision', '-')} |")
        lines.append(f"| Avg Recall | {sr.get('avg_recall', '-')} | {gr.get('avg_recall', '-')} |")
        lines.append("")

    # Scenario 4: Entity Dedup
    if "entity_dedup" in scenarios:
        s = scenarios["entity_dedup"]
        lines.extend([
            "### Scenario 4: Entity Deduplication",
            "",
            "| Metric | Seshat | Graphiti |",
            "|--------|--------|---------|",
        ])
        sd = s.get("seshat", {}).get("dedup", {})
        gd = s.get("graphiti", {}).get("dedup", {})
        lines.append(f"| Raw Mentions | {sd.get('raw_mentions', '-')} | {gd.get('raw_mentions', '-')} |")
        lines.append(f"| Unique Entities | {sd.get('unique_entities_created', '-')} | {gd.get('unique_entities_created', '-')} |")
        lines.append(f"| Dedup Ratio | {sd.get('dedup_ratio', '-')} | {gd.get('dedup_ratio', '-')} |")
        lines.append(f"| Expected Canonical | {sd.get('expected_canonical', '-')} | {gd.get('expected_canonical', '-')} |")
        lines.append("")

    # Scenario 6: Scaling
    if "scaling" in scenarios:
        s = scenarios["scaling"]
        lines.extend([
            "### Scenario 6: Scaling",
            "",
            "| Checkpoint | Seshat Ingest (ms) | Graphiti Ingest (ms) | Seshat Query p50 | Graphiti Query p50 |",
            "|------------|-------------------|---------------------|-----------------|-------------------|",
        ])
        s_checks = s.get("seshat", {}).get("checkpoints", [])
        g_checks = s.get("graphiti", {}).get("checkpoints", [])
        for sc, gc in zip(s_checks, g_checks):
            ep = sc.get("episodes_ingested", "?")
            si = sc.get("ingest_mean_ms", "-")
            gi = gc.get("ingest_mean_ms", "-")
            sq = sc.get("query", {}).get("p50_ms", "-")
            gq = gc.get("query", {}).get("p50_ms", "-")
            lines.append(f"| {ep} | {si} | {gi} | {sq} | {gq} |")
        lines.append("")

    # Cost comparison
    cost = results.get("cost", {})
    if cost:
        lines.extend([
            "### Cost Comparison",
            "",
            "| Metric | Seshat | Graphiti |",
            "|--------|--------|---------|",
            f"| LLM Input Tokens | {cost.get('seshat', {}).get('input_tokens', '-')} | {cost.get('graphiti', {}).get('input_tokens', '-')} |",
            f"| LLM Output Tokens | {cost.get('seshat', {}).get('output_tokens', '-')} | {cost.get('graphiti', {}).get('output_tokens', '-')} |",
            f"| Estimated Cost (USD) | ${cost.get('seshat', {}).get('estimated_cost_usd', '-')} | ${cost.get('graphiti', {}).get('estimated_cost_usd', '-')} |",
            "",
        ])

    return "\n".join(lines)


def save_markdown_report(
    results: dict[str, Any],
    output_dir: Path,
    run_id: str,
) -> Path:
    """Save markdown report fragment.

    Args:
        results: Complete experiment results dict.
        output_dir: Directory for output files.
        run_id: Unique run identifier.

    Returns:
        Path to the saved markdown file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{run_id}-report.md"
    path.write_text(format_markdown_report(results))
    return path


def print_summary(results: dict[str, Any]) -> None:
    """Print a console summary of the experiment results."""
    print("\n" + "=" * 70)
    print("GRAPHITI EXPERIMENT RESULTS")
    print("=" * 70)
    print(format_markdown_report(results))
    print("=" * 70)
```

- [ ] **Step 2: Commit**

```bash
git add scripts/experiment/report.py
git commit -m "feat(eval-02): add report generator — JSON + markdown output"
```

---

## Task 7: Main Script — CLI Entry Point + Orchestrator

**Files:**
- Create: `scripts/graphiti_experiment.py`

- [ ] **Step 1: Create the main experiment script**

Create `scripts/graphiti_experiment.py`:

```python
#!/usr/bin/env python
"""Graphiti vs Seshat Experiment — EVAL-02 / FRE-147.

Compares Graphiti against the current Seshat Neo4j backend across 6 scenarios:
  1. Episodic Memory — Store + Retrieve
  2. Semantic Memory — Consolidation Quality
  3. Temporal Queries
  4. Entity Deduplication
  5. Consolidation Lifecycle
  6. Scaling

Usage:
    python scripts/graphiti_experiment.py --llm openai --scenarios 1,2,3,4,5,6
    python scripts/graphiti_experiment.py --llm both
    python scripts/graphiti_experiment.py --llm anthropic --scenarios 4 --episodes 20
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Disable Graphiti telemetry BEFORE any graphiti imports
os.environ["GRAPHITI_TELEMETRY_ENABLED"] = "false"

# Add src to path for personal_agent imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from experiment.config import ANTHROPIC_CONFIG, OPENAI_CONFIG, ExperimentConfig, LLM_CONFIGS
from experiment.data_loader import generate_synthetic_episodes, load_real_episodes
from experiment.metrics import ScenarioResult
from experiment.report import print_summary, save_json_results, save_markdown_report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Graphiti vs Seshat Experiment")
    parser.add_argument(
        "--llm",
        choices=["openai", "anthropic", "both"],
        default="openai",
        help="LLM provider for Graphiti (default: openai)",
    )
    parser.add_argument(
        "--scenarios",
        default="1,2,3,4,5,6",
        help="Comma-separated scenario numbers to run (default: all)",
    )
    parser.add_argument("--episodes", type=int, default=50, help="Episode count for quality tests")
    parser.add_argument("--scale-episodes", type=int, default=500, help="Episode count for scaling test")
    parser.add_argument(
        "--output",
        default="telemetry/evaluation/graphiti",
        help="Output directory for results",
    )
    parser.add_argument("--neo4j-uri", default="bolt://localhost:7687")
    parser.add_argument("--graphiti-neo4j-uri", default="bolt://localhost:7688")
    return parser.parse_args()


async def run_experiment(config: ExperimentConfig) -> dict[str, Any]:
    """Run the full experiment for a single LLM configuration."""
    from experiment import graphiti_runner, seshat_runner

    run_id = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H-%M") + f"-{config.llm}"
    print(f"\n{'='*60}")
    print(f"Starting experiment run: {run_id}")
    print(f"LLM: {config.llm_config.name}")
    print(f"Medium model: {config.llm_config.medium_model}")
    print(f"Small model: {config.llm_config.small_model}")
    print(f"Scenarios: {config.scenarios}")
    print(f"{'='*60}\n")

    # Load test data
    telemetry_dir = Path("telemetry/evaluation")
    real_episodes = load_real_episodes(telemetry_dir, max_episodes=config.episodes)
    print(f"Loaded {len(real_episodes)} real episodes from telemetry")

    if len(real_episodes) < config.episodes:
        # Supplement with synthetic if not enough real data
        supplement = generate_synthetic_episodes(
            count=config.episodes - len(real_episodes),
            days_span=30,
        )
        quality_episodes = real_episodes + supplement
        print(f"Supplemented with {len(supplement)} synthetic episodes")
    else:
        quality_episodes = real_episodes

    scale_episodes = generate_synthetic_episodes(count=config.scale_episodes, days_span=30)
    print(f"Generated {len(scale_episodes)} synthetic episodes for scaling")

    # Connect backends
    seshat_service = await seshat_runner.create_seshat_service(config)
    graphiti_client = await graphiti_runner.create_graphiti_client(config)
    print("Connected to both backends\n")

    scenarios: dict[str, Any] = {}

    # Run scenarios
    if 1 in config.scenarios:
        print("--- Scenario 1: Episodic Memory — Store + Retrieve ---")
        seshat_s1 = await seshat_runner.run_scenario_1_episodic(seshat_service, quality_episodes)
        await graphiti_runner.clear_graphiti_data(graphiti_client)
        graphiti_s1 = await graphiti_runner.run_scenario_1_episodic(graphiti_client, quality_episodes)
        scenarios["episodic_retrieval"] = {"seshat": seshat_s1, "graphiti": graphiti_s1}
        print("  Done.\n")

    if 2 in config.scenarios:
        print("--- Scenario 2: Semantic Memory — Consolidation Quality ---")
        seshat_s2 = await seshat_runner.run_scenario_2_semantic(seshat_service, quality_episodes)
        graphiti_s2 = await graphiti_runner.run_scenario_2_semantic(graphiti_client, quality_episodes)
        scenarios["semantic_consolidation"] = {"seshat": seshat_s2, "graphiti": graphiti_s2}
        print("  Done.\n")

    if 3 in config.scenarios:
        print("--- Scenario 3: Temporal Queries ---")
        seshat_s3 = await seshat_runner.run_scenario_3_temporal(seshat_service, quality_episodes)
        graphiti_s3 = await graphiti_runner.run_scenario_3_temporal(graphiti_client, quality_episodes)
        scenarios["temporal_queries"] = {"seshat": seshat_s3, "graphiti": graphiti_s3}
        print("  Done.\n")

    if 4 in config.scenarios:
        print("--- Scenario 4: Entity Deduplication ---")
        await graphiti_runner.clear_graphiti_data(graphiti_client)
        seshat_s4 = await seshat_runner.run_scenario_4_dedup(seshat_service, quality_episodes)
        graphiti_s4 = await graphiti_runner.run_scenario_4_dedup(graphiti_client, quality_episodes)
        scenarios["entity_dedup"] = {"seshat": seshat_s4, "graphiti": graphiti_s4}
        print("  Done.\n")

    if 5 in config.scenarios:
        print("--- Scenario 5: Consolidation Lifecycle ---")
        await graphiti_runner.clear_graphiti_data(graphiti_client)
        seshat_s5 = await seshat_runner.run_scenario_5_lifecycle(seshat_service, quality_episodes)
        graphiti_s5 = await graphiti_runner.run_scenario_5_lifecycle(graphiti_client, quality_episodes)
        scenarios["consolidation_lifecycle"] = {"seshat": seshat_s5, "graphiti": graphiti_s5}
        print("  Done.\n")

    if 6 in config.scenarios:
        print("--- Scenario 6: Scaling ---")
        await graphiti_runner.clear_graphiti_data(graphiti_client)
        seshat_s6 = await seshat_runner.run_scenario_6_scaling(seshat_service, scale_episodes)
        await graphiti_runner.clear_graphiti_data(graphiti_client)
        graphiti_s6 = await graphiti_runner.run_scenario_6_scaling(graphiti_client, scale_episodes)
        scenarios["scaling"] = {"seshat": seshat_s6, "graphiti": graphiti_s6}
        print("  Done.\n")

    # Assemble results
    results = {
        "run_id": run_id,
        "config": {
            "llm": config.llm,
            "llm_model": config.llm_config.medium_model,
            "small_model": config.llm_config.small_model,
            "embedder": config.llm_config.embedder_model,
            "episodes": config.episodes,
            "scale_episodes": config.scale_episodes,
        },
        "scenarios": scenarios,
    }

    # Clean up experiment data from Seshat (don't pollute production graph)
    await seshat_runner.clean_experiment_data(seshat_service)

    # Disconnect
    await seshat_service.disconnect()
    await graphiti_client.close()

    return results


def main() -> None:
    args = parse_args()
    scenarios = [int(s) for s in args.scenarios.split(",")]
    output_dir = Path(args.output)

    llm_configs = ["openai", "anthropic"] if args.llm == "both" else [args.llm]

    all_results = []
    for llm in llm_configs:
        config = ExperimentConfig(
            llm=llm,
            scenarios=scenarios,
            episodes=args.episodes,
            scale_episodes=args.scale_episodes,
            output_dir=output_dir,
            neo4j_uri=args.neo4j_uri,
            graphiti_neo4j_uri=args.graphiti_neo4j_uri,
        )

        results = asyncio.run(run_experiment(config))

        # Save outputs
        run_id = results["run_id"]
        json_path = save_json_results(results, output_dir, run_id)
        md_path = save_markdown_report(results, output_dir, run_id)
        print(f"\nResults saved: {json_path}")
        print(f"Report saved: {md_path}")

        print_summary(results)
        all_results.append(results)

    if len(all_results) > 1:
        print("\n" + "=" * 60)
        print("A/B COMPARISON: OpenAI vs Anthropic")
        print("=" * 60)
        print("See individual report files for detailed comparison.")
        print("Key: Compare dedup ratios, retrieval precision, and cost across runs.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Make the script executable**

Run:
```bash
chmod +x scripts/graphiti_experiment.py
```

- [ ] **Step 3: Verify the script parses arguments**

Run:
```bash
cd /Users/Alex/Dev/personal_agent
python scripts/graphiti_experiment.py --help
```

Expected: Help text showing all CLI options.

- [ ] **Step 4: Commit**

```bash
git add scripts/graphiti_experiment.py
git commit -m "feat(eval-02): add main experiment script — CLI orchestrator"
```

---

## Task 8: Dry Run — Verify End-to-End With Small Dataset

**Files:**
- No new files — verification task.

- [ ] **Step 1: Ensure both Neo4j containers are running**

Run:
```bash
docker compose up -d neo4j neo4j-experiment
docker compose ps
```

Expected: Both `neo4j` (7687) and `neo4j-experiment` (7688) healthy.

- [ ] **Step 2: Verify OPENAI_API_KEY is set**

Run:
```bash
echo $OPENAI_API_KEY | head -c 10
```

Expected: Shows first 10 chars of the key (e.g., `sk-proj-xx`).

- [ ] **Step 3: Run a minimal dry run (scenario 4 only, 5 episodes)**

Run:
```bash
cd /Users/Alex/Dev/personal_agent
python scripts/graphiti_experiment.py \
  --llm openai \
  --scenarios 4 \
  --episodes 5 \
  --scale-episodes 10 \
  --output telemetry/evaluation/graphiti
```

Expected: Scenario 4 (dedup) runs, outputs JSON + markdown, console summary shows dedup ratios for both backends.

- [ ] **Step 4: Inspect results**

Run:
```bash
ls telemetry/evaluation/graphiti/
cat telemetry/evaluation/graphiti/*-report.md
```

Expected: Report markdown with dedup comparison table.

- [ ] **Step 5: Inspect Graphiti graph in browser**

Open `http://localhost:7475` in browser. Run Cypher: `MATCH (n) RETURN n LIMIT 25`. Verify Graphiti created nodes with its schema.

- [ ] **Step 6: Fix any issues discovered during dry run**

Address errors, adjust imports, fix API mismatches. This is expected — the dry run is the verification step.

- [ ] **Step 7: Commit fixes**

```bash
git add -u
git commit -m "fix(eval-02): dry run fixes"
```

---

## Task 9: Full Run — OpenAI + Anthropic A/B

**Files:**
- No new files — execution task.

- [ ] **Step 1: Run full experiment with OpenAI**

Run:
```bash
cd /Users/Alex/Dev/personal_agent
python scripts/graphiti_experiment.py \
  --llm openai \
  --episodes 50 \
  --scale-episodes 500
```

Expected: All 6 scenarios complete. Results saved to `telemetry/evaluation/graphiti/`. Estimated runtime: 15-30 min (dominated by LLM calls in scenarios 4 and 6).

- [ ] **Step 2: Clear Graphiti graph between runs**

Open `http://localhost:7475`, run: `MATCH (n) DETACH DELETE n`.

- [ ] **Step 3: Run full experiment with Anthropic**

Run:
```bash
cd /Users/Alex/Dev/personal_agent
python scripts/graphiti_experiment.py \
  --llm anthropic \
  --episodes 50 \
  --scale-episodes 500
```

Expected: Same scenarios, different LLM. Compare quality and cost vs OpenAI run.

- [ ] **Step 4: Review results**

Run:
```bash
ls -la telemetry/evaluation/graphiti/
```

Compare the two report files side by side.

---

## Task 10: Write Findings — Fill Experiment Report

**Files:**
- Modify: `docs/research/GRAPHITI_EXPERIMENT_REPORT.md`

- [ ] **Step 1: Read the JSON results from both runs**

Read the two JSON files from `telemetry/evaluation/graphiti/`.

- [ ] **Step 2: Fill in the Findings section of the experiment report**

Update `docs/research/GRAPHITI_EXPERIMENT_REPORT.md` with:
- Quantitative results from all 6 scenarios (use the markdown report fragments)
- Qualitative observations from inspecting the Graphiti graph in Neo4j Browser
- Cost comparison across OpenAI vs Anthropic
- Attribution: framework vs embeddings/vector search
- Memory type coverage analysis (which of the 6 types map to Graphiti?)

- [ ] **Step 3: Write the Recommendation section**

Check one of:
- [ ] Keep current Neo4j (Graphiti adds complexity without sufficient benefit)
- [ ] Migrate to Graphiti (clear improvement in quality/latency/maintenance)
- [ ] Hybrid (use Graphiti for temporal queries, keep Neo4j for graph traversal)
- [ ] Keep Seshat + add embeddings (improvement is from vector search, not framework)
- [ ] Evolve memory types (taxonomy doesn't match how memory actually works)

Include rationale grounded in the data.

- [ ] **Step 4: Note secondary finding on model downgrade**

Add a section on whether `entity_extraction_role` can be downgraded from `claude_sonnet` to a cheaper model based on extraction quality observed in the experiment.

- [ ] **Step 5: Commit the completed report**

```bash
git add docs/research/GRAPHITI_EXPERIMENT_REPORT.md
git commit -m "docs(eval-02): complete Graphiti experiment report with findings and recommendation"
```

---

## Summary

| Task | Description | Model Tier |
|------|-------------|------------|
| 1 | Infrastructure — Docker + Dependencies | Tier-3: Haiku |
| 2 | Data Loader — Telemetry + Synthetic | Tier-2: Sonnet |
| 3 | Metrics Module | Tier-2: Sonnet |
| 4 | Seshat Runner — 6 scenarios | Tier-2: Sonnet |
| 5 | Graphiti Runner — 6 scenarios | Tier-2: Sonnet |
| 6 | Report Generator | Tier-2: Sonnet |
| 7 | Main Script — CLI + Orchestrator | Tier-2: Sonnet |
| 8 | Dry Run — End-to-End Verification | Tier-2: Sonnet |
| 9 | Full Run — OpenAI + Anthropic A/B | Tier-1: Opus (interpret results) |
| 10 | Write Findings — Experiment Report | Tier-1: Opus |

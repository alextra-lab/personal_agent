# Kibana Dashboards — Slice 2: Expansion

## Prerequisites

- Elasticsearch accessible at `http://localhost:9200`
- Kibana accessible at `http://localhost:5601`
- Index pattern: `agent-*`

---

## Dashboard 1: Expansion & Decomposition

**Purpose:** Visualize how often the agent expands (HYBRID/DECOMPOSE)
vs. stays calm (SINGLE) and whether expansion helps.

### Visualizations

| Viz | Type | Field | Notes |
|-----|------|-------|-------|
| Decomposition strategy distribution | Pie chart | `strategy` where `event: gateway_output` | Shows SINGLE/HYBRID/DECOMPOSE/DELEGATE split |
| Expansion over time | Time series | `strategy` where `event: gateway_output` | Filter strategy != SINGLE |
| Sub-agent spawn rate | Time series | Count where `event: sub_agent_complete` | Shows expansion volume |
| Sub-agent success rate | Metric | `success` where `event: sub_agent_complete` | Percentage true |
| Sub-agent duration distribution | Histogram | `duration_ms` where `event: sub_agent_complete` | Latency profile |
| Expansion budget utilization | Time series | `expansion_budget` where `event: expansion_budget_computed` | Budget over time |

### Saved Search

- Filter: `event: gateway_output OR event: sub_agent_complete OR event: expansion_budget_computed`
- Sort: `@timestamp` descending

---

## Dashboard 2: Context Budget

**Purpose:** Monitor context window utilization and trimming frequency.

### Visualizations

| Viz | Type | Field | Notes |
|-----|------|-------|-------|
| Token count distribution | Histogram | `total_tokens` where `event: context_budget_applied` | See typical context sizes |
| Trimming rate | Metric | Count where `event: context_budget_applied AND trimmed: true` / total | % of requests that need trimming |
| Overflow actions | Tag cloud | `overflow_action` where `event: context_budget_applied AND trimmed: true` | Which trim strategies fire |
| Budget utilization over time | Time series | `final_tokens / available` where `event: context_budget_applied` | Utilization ratio |

---

## Dashboard 3: Delegation Outcomes

**Purpose:** Track Stage B delegation success and learning.

### Visualizations

| Viz | Type | Field | Notes |
|-----|------|-------|-------|
| Delegation volume by agent | Bar chart | `target_agent` where `event: delegation_outcome_recorded` | Which agents get work |
| Success rate by agent | Metric | `success` grouped by `target_agent` | Per-agent success |
| Rounds needed trend | Line chart | `rounds_needed` where `event: delegation_outcome_recorded` | Should trend down |
| Missing context frequency | Tag cloud | `what_was_missing` where `success: false` | What to improve |
| Satisfaction distribution | Histogram | `user_satisfaction` where `event: delegation_outcome_recorded` | 1-5 rating |

---

## Dashboard 4: Memory Comparison (Graphiti Experiment)

**Purpose:** Compare Neo4j vs Graphiti recall quality during experiment.

> **Note:** These events (`memory_recall_*`) will only appear once the Graphiti
> experiment is executed (post Slice 2 data accumulation). This dashboard is
> pre-configured to be ready when the experiment runs.

### Visualizations

| Viz | Type | Field | Notes |
|-----|------|-------|-------|
| Recall latency comparison | Dual line chart | `duration_ms` grouped by `backend` where `event: memory_recall_*` | Neo4j vs Graphiti |
| Result count comparison | Bar chart | `result_count` grouped by `backend` | Retrieval volume |
| Relevance score comparison | Box plot | `avg_relevance` grouped by `backend` | Quality metric |

---

## Setup Instructions

1. Navigate to Kibana > Stack Management > Index Patterns
2. Verify `agent-*` pattern exists (created in Slice 1)
3. Import dashboards via Management > Saved Objects > Import
4. Or create manually using the visualization specs above

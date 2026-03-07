# ADR-0030: Captain's Log Deduplication & Self-Improvement Pipeline

**Status**: Proposed  
**Date**: 2026-03-07  
**Deciders**: Project owner  

---

## Context

The Captain's Log generates a `CaptainLogEntry` with an optional `ProposedChange` after every completed task. After 145+ reflections, these entries accumulate with significant duplication — the same class of improvement (e.g., "add retry logic", "improve error handling", "reduce response latency") appears repeatedly across entries because there is no mechanism to detect that an equivalent proposal already exists.

### Current ProposedChange model

```python
class ProposedChange(BaseModel):
    what: str   # Free text
    why: str    # Free text
    how: str    # Free text
```

No category, no component tag, no semantic fingerprint. Every reflection generates a fresh entry regardless of existing proposals. This creates three problems:

1. **Signal-to-noise**: The project owner must manually review entries that largely repeat the same ideas
2. **No consolidation**: 7 entries saying "add concurrency control" in different words don't converge into one actionable item
3. **No pipeline**: Proposals sit in `AWAITING_APPROVAL` status indefinitely with no path to becoming tracked work items

### Opportunity: Self-improvement loop

The agent already has:
- `InsightsEngine` that analyzes patterns and generates `CONFIG_PROPOSAL` entries (weekly)
- Linear integration via MCP (`plugin-linear-linear`) for project management
- `CaptainLogEntryType` enum with `REFLECTION`, `CONFIG_PROPOSAL`, `HYPOTHESIS`, `OBSERVATION`, `IDEA`
- A brainstem scheduler that can trigger periodic jobs

The missing piece is a pipeline that: categorizes → deduplicates → promotes → creates tracked work → (optionally) self-implements.

---

## Decision

Extend the Captain's Log data model with structured categorization, implement semantic deduplication, and build a pipeline that promotes high-confidence, frequently-observed proposals into Linear backlog items.

### 1. Categorization taxonomy

Add structured fields to `ProposedChange`:

```python
class ChangeCategory(str, Enum):
    """Taxonomy of improvement types."""
    PERFORMANCE = "performance"         # Latency, throughput, resource usage
    RELIABILITY = "reliability"         # Error handling, retries, fallbacks
    CONCURRENCY = "concurrency"         # Parallelism, queue management, throttling
    KNOWLEDGE_QUALITY = "knowledge"     # Entity extraction, graph health, dedup
    COST = "cost"                       # Token usage, API costs, compute efficiency
    UX = "ux"                           # Response quality, conversational flow
    OBSERVABILITY = "observability"     # Logging, metrics, dashboards
    ARCHITECTURE = "architecture"       # Structural changes, new subsystems
    SAFETY = "safety"                   # Governance, permissions, risk

class ChangeScope(str, Enum):
    """Which subsystem the change targets."""
    LLM_CLIENT = "llm_client"
    ORCHESTRATOR = "orchestrator"
    SECOND_BRAIN = "second_brain"
    CAPTAINS_LOG = "captains_log"
    BRAINSTEM = "brainstem"
    TOOLS = "tools"
    TELEMETRY = "telemetry"
    GOVERNANCE = "governance"
    INSIGHTS = "insights"
    CONFIG = "config"
    CROSS_CUTTING = "cross_cutting"

class ProposedChange(BaseModel):
    what: str
    why: str
    how: str
    category: ChangeCategory | None = None       # NEW
    scope: ChangeScope | None = None              # NEW
    fingerprint: str | None = None                # NEW: semantic dedup key
    seen_count: int = Field(default=1, ge=1)      # NEW: merge counter
    first_seen: datetime | None = None            # NEW: earliest occurrence
    related_entry_ids: list[str] = Field(         # NEW: merged entries
        default_factory=list
    )
```

The `category` and `scope` fields are populated by the LLM during reflection (added to the DSPy `GenerateReflection` signature as constrained enum outputs). The `fingerprint` is computed deterministically from `category + scope + normalized_what`.

### 2. Deduplication strategy

#### Fingerprint computation

```python
def compute_proposal_fingerprint(
    category: ChangeCategory,
    scope: ChangeScope,
    what: str,
) -> str:
    """Deterministic fingerprint for dedup.

    Combines category, scope, and a normalized version of the 'what' field.
    Two proposals are considered duplicates if they share the same fingerprint.
    """
    normalized = _normalize_text(what)  # lowercase, remove stopwords, stem
    key = f"{category.value}:{scope.value}:{normalized}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]
```

#### Merge logic

Before writing a new entry, `CaptainLogManager.save_entry()` checks for existing `AWAITING_APPROVAL` entries with the same fingerprint:

- **Match found**: Increment `seen_count`, update `related_entry_ids`, keep the original entry (don't create a new file). Optionally append new `supporting_metrics` if they differ.
- **No match**: Write new entry as normal.

This reduces 7 entries about "add concurrency control" to 1 entry with `seen_count: 7`.

#### Fuzzy matching (Phase 2)

For proposals where the LLM generates slightly different category/scope combinations, add an optional embedding-based similarity check using the existing memory service infrastructure. If cosine similarity of `what` fields exceeds 0.85 and `scope` matches, treat as duplicate.

### 3. Promotion pipeline

A new scheduled job (`PromotionPipeline`) runs weekly (configurable) and promotes proposals that meet promotion criteria:

```python
class PromotionCriteria(BaseModel):
    """Criteria for promoting a proposal to a Linear backlog item."""
    min_seen_count: int = 3               # Observed at least N times
    min_age_days: int = 7                  # First seen at least N days ago
    max_existing_linear_issues: int = 20   # Don't flood the backlog
    excluded_categories: list[ChangeCategory] = []  # Skip certain categories
```

#### Pipeline stages

```
┌──────────────────────┐
│  Captain's Log Entry │
│  (per-task)          │
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│  Categorize + Dedup  │
│  (on write)          │
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│  Accumulate          │
│  (seen_count grows)  │
└──────────┬───────────┘
           │  weekly scheduler trigger
           ▼
┌──────────────────────┐
│  Promotion Check     │
│  (meets criteria?)   │
└──────────┬───────────┘
           │ yes
           ▼
┌──────────────────────┐
│  Create Linear Issue │
│  (via MCP)           │
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│  Update CL Entry     │
│  status → APPROVED   │
│  + linear_issue_id   │
└──────────────────────┘
```

#### Linear issue creation

When a proposal is promoted, create a Linear issue via the MCP `save_issue` tool:

```python
issue = {
    "title": f"[{proposal.category.value}] {proposal.what[:80]}",
    "team": "FrenchForest",
    "description": _format_linear_description(proposal, entry),
    "priority": _map_seen_count_to_priority(proposal.seen_count),
    "labels": ["Improvement"],
    "state": "Backlog",
    "project": "2.3 Homeostasis & Feedback",  # or appropriate project
}
```

Priority mapping: `seen_count >= 10` → High, `>= 5` → Normal, `>= 3` → Low.

The Linear issue description includes:
- The proposal (what/why/how)
- `seen_count` and date range
- Supporting metrics summary
- Links to related Captain's Log entry IDs

#### Tracking field

Add to `CaptainLogEntry`:

```python
class CaptainLogEntry(BaseModel):
    # ... existing fields ...
    linear_issue_id: str | None = Field(
        None,
        description="Linear issue ID if this proposal was promoted to backlog"
    )
```

### 4. Self-implementation (future — gated)

Once the promotion pipeline is proven, a future phase can add **autonomous implementation** for low-risk categories:

- `CONFIG` scope changes (threshold adjustments, timeout tuning) → agent modifies config files and creates a PR
- `OBSERVABILITY` scope changes (add a log statement, add a metric) → agent implements and runs tests

This requires:
- Governance gate: only `CONFIG` and `OBSERVABILITY` scopes eligible
- Human approval via Linear issue status (must be moved to "Approved" before agent acts)
- Automated test validation before committing changes
- Captain's Log entry tracking the implementation attempt and outcome

**This phase is explicitly out of scope for the initial implementation** but the data model supports it.

---

## Alternatives Considered

### A. Embedding-only deduplication (no categories)

Use vector similarity on the full `what` text to detect duplicates without a taxonomy.

*Pros*: No enum maintenance, catches semantic similarity across different wordings.  
*Cons*: Requires embedding infrastructure for every write. Similarity threshold is fragile — "add retries to LLM client" and "add retries to entity extraction" are semantically similar but may be distinct improvements. Categories provide a human-interpretable grouping that embeddings alone don't.

### B. Manual curation only (no pipeline)

Keep the current model. The project owner reviews proposals manually and creates Linear issues by hand.

*Pros*: Zero implementation cost. Human judgment on every promotion.  
*Cons*: Doesn't scale. 145 entries already, growing by ~10/day. The project owner becomes the bottleneck. Duplicates accumulate. The feedback loop never closes.

### C. LLM-based dedup at read time (InsightsEngine clusters proposals)

Instead of deduping on write, let the weekly InsightsEngine scan all proposals and cluster similar ones.

*Pros*: Simpler write path. More sophisticated clustering possible.  
*Cons*: Doesn't prevent accumulation — 100+ duplicate files still exist on disk. Higher LLM cost (scanning all entries weekly). Write-time dedup is simpler and more efficient.

**Chosen approach combines write-time fingerprint dedup (fast, deterministic) with the option for read-time fuzzy matching later (Phase 2).**

---

## Consequences

**Positive:**
- Proposals consolidate automatically — `seen_count` surfaces the most important improvements
- Categories enable filtering, dashboarding, and prioritization without reading free text
- Linear integration creates a real backlog from agent observations (closed feedback loop)
- Project owner reviews Linear issues (familiar tool) instead of JSON files
- Foundation for autonomous self-improvement in future phases
- Backward compatible — old entries without category/scope/fingerprint load fine (all new fields optional)

**Negative:**
- DSPy signature change required (add `category` and `scope` outputs) — may affect reflection quality
- Fingerprint normalization is imperfect — some genuine duplicates may have different fingerprints
- Linear issue creation adds an external dependency to the scheduled pipeline
- Risk of over-promotion if `min_seen_count` threshold is too low (mitigated by configurable criteria)

---

## Acceptance Criteria

- [ ] `ProposedChange` model extended with `category`, `scope`, `fingerprint`, `seen_count`, `first_seen`, `related_entry_ids`
- [ ] `ChangeCategory` and `ChangeScope` enums defined and documented
- [ ] DSPy `GenerateReflection` signature updated to produce `category` and `scope`
- [ ] `CaptainLogManager.save_entry()` performs fingerprint-based dedup before writing
- [ ] Existing entries without new fields load correctly (backward compatibility)
- [ ] `PromotionPipeline` scheduled job creates Linear issues for qualifying proposals
- [ ] Linear issues include proposal details, seen_count, metrics, and CL entry references
- [ ] `CaptainLogEntry.linear_issue_id` tracks promoted entries
- [ ] Unit tests for fingerprint computation, dedup logic, and promotion criteria
- [ ] Integration test: 5 similar proposals → 1 entry with `seen_count: 5` → 1 Linear issue

---

## Implementation Timing

**When**: Mid Phase 2.3, after ADR-0029 (Inference Concurrency Control) is implemented.

**Rationale**: The dedup and categorization work depends on reliable Captain's Log reflections. If reflections are failing due to inference contention (ADR-0029), the categorization data will be incomplete. Fix the plumbing first, then improve the data model.

**Sequence**:
1. ADR-0029: Inference Concurrency Control (unblocks reliable reflections)
2. ADR-0030 Part 1: Categorization + dedup (model changes, DSPy signature update)
3. ADR-0030 Part 2: Promotion pipeline + Linear integration (scheduled job)
4. Future: Self-implementation for low-risk scopes (gated, requires governance approval)

---

## Links and References

- `src/personal_agent/captains_log/models.py` — current `ProposedChange` and `CaptainLogEntry` models
- `src/personal_agent/captains_log/reflection_dspy.py` — DSPy signature for reflection generation
- `src/personal_agent/captains_log/manager.py` — entry persistence (dedup integration point)
- `src/personal_agent/insights/engine.py` — InsightsEngine (weekly pattern analysis)
- `docs/plans/PHASE_2.3_PLAN.md` — Phase 2.3 plan (feedback loops and insights)
- `docs/architecture/HOMEOSTASIS_MODEL.md` — Section 4.5: Learning & Self-Modification Pace
- ADR-0014: Structured Metrics in Captain's Log (precedent for adding structured fields)
- ADR-0010: Structured LLM Outputs via Pydantic (DSPy adoption rationale)

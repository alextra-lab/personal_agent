# ADR-0037: Recall Controller — Implicit Memory Recall Path

**Date:** 2026-03-28
**Status:** Accepted
**Deciders:** Alex (project lead)
**Linear Issue:** FRE-155
**Depends on:** EVAL-08 (Slice 3 priority ranking — rank 4, should-have)
**Blocks:** FRE-156 (Slice 3 implementation plan)

---

## Context

CP-19 has **never passed across 4 evaluation runs** — the only critical path with a 0% pass rate. The prompt "Going back to the beginning — what was our primary database again?" is classified as `CONVERSATIONAL` instead of `MEMORY_RECALL`. CP-28 ("Given everything we've discussed...") fails for the same reason.

### Root Cause: Two Compounded Failures

**Failure 1 — Classification gap:** Stage 4's `_MEMORY_RECALL_PATTERNS` (in `request_gateway/intent.py`) are anchored on **explicit recall phrases**: "do you remember", "what have I discussed", "recall our", etc. Implicit backward-reference cues — "again", "going back", "earlier", "what was our" — don't match any pattern. The classifier falls through to `CONVERSATIONAL` (confidence 0.7).

**Failure 2 — "Lost in the Middle" recall failure:** When classified as `CONVERSATIONAL`, context assembly (`request_gateway/context.py`, line 98–100) skips memory enrichment entirely. The agent must rely on raw attention over conversation history. Research shows models don't use available context uniformly well — facts positioned in the middle of long contexts are under-attended. The fact "primary database is PostgreSQL" was established in Turn 2 of a 10-turn session, yet the agent claimed not to know.

The second opinion (GPT-5.4) correctly identifies this as a dual failure requiring a dual fix: classification gap + recall scaffolding.

### Impact

- **~8% of queries affected:** Any backward-reference that doesn't use explicit recall phrasing is misclassified.
- **Impact grows with session length:** At short session lengths, the agent may compensate via conversation history. At longer sessions, "Lost in the Middle" makes this increasingly unreliable.
- **Telemetry blind spot:** Misclassified queries produce incorrect `intent_classified.task_type` events, making routing analysis unreliable.

### Design Inputs

| Document | Key contribution |
|----------|-----------------|
| `docs/research/evaluation-orchestration-analysis.md` | CP-19 root cause analysis, cross-run data |
| `docs/research/evaluation-run-04-second-opinion-response.md` | Recommendation E (session fact lookup), Q3 answer (hybrid classifier) |
| `docs/research/evaluation-run-04-second-opinion-proposed-remediation.md` | §8 Recall controller design, §8.1–8.4 |
| `docs/research/EVAL_08_SLICE_3_PRIORITIES.md` | Rank 4 (should-have), effort S, independent of other tracks |
| ADR-0025 (`memory-recall-intent-detection`) | Prior art: explicit recall patterns, broad recall path |
| ADR-0036 (`expansion-controller`) | Companion ADR — expansion enforcement (separate concern) |

---

## Decision

Introduce a **Recall Controller** — a lightweight post-classification refinement stage (Stage 4b) that detects implicit backward-reference cues and, when corroborated by session history, reclassifies `CONVERSATIONAL` → `MEMORY_RECALL` with session fact evidence attached.

The recall controller supplements (does not replace) the existing Stage 4 intent classifier.

---

## Key Design Decisions

### Decision 1: Stage 4b (post-classification refinement), not modifying Stage 4

**Options considered:**

| Option | Description | Verdict |
|--------|-------------|---------|
| A. Expand Stage 4 patterns | Add implicit cues directly to `_MEMORY_RECALL_PATTERNS` | Rejected — high false-positive risk without corroboration |
| B. Stage 4b refinement | New post-classification step that validates cues against session history | **Selected** — preserves Stage 4's clean deterministic boundary |
| C. Replace Stage 4 with LLM classifier | Use an LLM call for intent classification | Rejected — adds 3–5s latency, breaks deterministic pipeline contract |
| D. Post-hoc retry after answer | If answer seems wrong, retry with `MEMORY_RECALL` | Rejected — doubles latency for every false negative |

**Selected approach: B (Stage 4b).** The recall controller runs only when Stage 4 classifies as `CONVERSATIONAL` and ambiguity cues are detected. It validates cues against session history before reclassifying, keeping the false-positive rate low.

**Rationale:**

Adding implicit cues like "again" or "earlier" directly to Stage 4 (Option A) would cause false positives on non-recall uses: "Let's try a different approach again", "I mentioned earlier that I want..." — these are conversational, not recall. The key insight from the second opinion is that implicit cues need **corroboration from session history** to be meaningful. That corroboration step doesn't belong in Stage 4's stateless regex matcher — it needs access to `session_messages`, which Stage 4 doesn't receive.

Stage 4b sits between Stage 4 (intent classification) and Stage 5 (decomposition assessment) in the pipeline, receiving both the intent result and session messages.

**Pipeline position:**

```
Stage 4:  Intent Classification (stateless, regex)
     ↓
Stage 4b: Recall Controller (NEW — only fires on CONVERSATIONAL + cue match)
     ↓
Stage 5:  Decomposition Assessment
```

### Decision 2: Recall cue detection patterns

The recall controller uses a **separate pattern set** from Stage 4. These patterns detect implicit backward-reference cues that suggest the user is asking about something previously established in the conversation.

**Cue categories:**

| Category | Patterns | Example |
|----------|----------|---------|
| Temporal back-reference | `again`, `earlier`, `before`, `previously` | "what was our database again?" |
| Positional back-reference | `going back`, `back to`, `at the beginning`, `at the start` | "Going back to the beginning..." |
| Possessive prior-decision | `what was our`, `what did we`, `what were the` | "what was our primary database?" |
| Explicit request | `remind me`, `refresh my memory` | "remind me what we decided" |
| Resumptive reference | `the X we discussed`, `the X we mentioned`, `the X we talked about` | "the framework we discussed" |

**Cue pattern (regex):**

```python
_RECALL_CUE_PATTERNS: re.Pattern[str] = re.compile(
    r"(?i)"
    # Temporal back-reference with interrogative context
    r"(?:what\s+(?:was|were|is)\s+(?:our|the|that)\s+\w+\s+again)"
    r"|(?:(?:going|go)\s+back\s+(?:to\s+)?(?:the\s+)?(?:beginning|start|earlier))"
    r"|(?:(?:back\s+to|earlier)\s+(?:when|where|what)\s+)"
    r"|(?:at\s+the\s+(?:beginning|start)\s*[,—–-])"
    # Possessive prior-decision
    r"|(?:what\s+(?:was|were|is)\s+(?:our|the)\s+(?:primary|main|original|first|chosen|selected|preferred))"
    r"|(?:what\s+did\s+(?:we|I)\s+(?:decide|pick|choose|settle|go\s+with|land\s+on))"
    # Explicit memory request
    r"|(?:remind\s+me\s+(?:what|which|about|of))"
    r"|(?:refresh\s+my\s+memory)"
    # Resumptive reference
    r"|(?:the\s+\w+\s+(?:we|I)\s+(?:discussed|mentioned|talked\s+about|decided\s+on|chose|picked))",
)
```

**Design constraints:**

- Patterns require **interrogative context** (question mark, "what", "which") or **explicit backward framing** ("going back", "earlier"). Bare "again" at end-of-sentence is not sufficient — "Let's try again" must not trigger.
- Patterns are deliberately **narrower** than Stage 4's explicit recall patterns. Stage 4 catches "do you remember X" with high confidence. Stage 4b catches "what was our X again?" where the recall intent is implied, not stated.
- False positives are acceptable at the cue level because they must pass the session fact corroboration check (Decision 3) before reclassification occurs.

### Decision 3: Session fact extraction and injection format

When recall cues are detected, the controller performs a **lightweight session fact scan** — searching recent conversation turns for facts that match the query's noun phrases. This is a conversation-history scan, not a Neo4j query.

**Session fact extraction algorithm:**

1. **Extract target noun phrases** from the user message: "primary database", "caching layer", "framework", etc.
2. **Scan `session_messages`** (most recent first) for turns containing those noun phrases.
3. **Extract fact candidates:** For each matching turn, extract the sentence(s) containing the noun phrase and a brief contextual summary.
4. **Score candidates** by recency (newer = higher) and specificity (exact phrase match > partial).
5. **Return top 3 candidates** (cap prevents over-injection).

**Why conversation history, not Neo4j:** Session-scoped facts ("we chose PostgreSQL in Turn 2") live in `session_messages`, which is already loaded in the pipeline. Neo4j stores cross-session semantic memory. CP-19 is a within-session recall failure — the fact is in the conversation, the model just under-attends it. The session fact scan is the right tool for this scope.

**Injection format:**

```python
@dataclass(frozen=True)
class RecallCandidate:
    """A session fact candidate for recall injection."""

    fact: str            # "Primary database is PostgreSQL"
    source_turn: int     # Turn index in session_messages
    noun_phrase: str     # "primary database" (matched phrase)
    confidence: float    # 0.0–1.0 (recency × specificity)

@dataclass(frozen=True)
class RecallResult:
    """Output of the recall controller."""

    reclassified: bool                # Whether intent was changed
    original_task_type: TaskType      # Pre-reclassification type
    trigger_cue: str                  # Which cue pattern matched
    candidates: list[RecallCandidate] # Session fact candidates (max 3)
```

**Context injection:** When the recall controller reclassifies the intent, candidates are attached to the `IntentResult` (via a new `recall_context` field on `GatewayOutput`) and injected into the LLM context during Stage 6+7 assembly:

```
## Session Fact Recall
The user appears to be referring to something discussed earlier in this session.
Relevant facts from the conversation:
- Turn 2: "Primary database is PostgreSQL" (matched: "primary database")

Use these facts to answer accurately. Do not claim you don't know or don't remember.
```

This gives the model explicit evidence rather than relying on it to find the fact via attention.

### Decision 4: Interaction with existing MemoryProtocol

The recall controller operates **independently of MemoryProtocol** for session-scoped facts. The interaction points are:

| Scenario | What happens |
|----------|-------------|
| Session fact found → reclassify to MEMORY_RECALL | Context assembly calls `recall_broad()` (existing path) AND injects session fact candidates. The model gets both Neo4j memory context and pinpointed session facts. |
| Session fact NOT found → no reclassification | Intent stays `CONVERSATIONAL`. No memory query. (Cue was a false positive — e.g., "Let's go back to discussing the architecture" where no specific fact is referenced.) |
| Cross-session recall needed | Falls through to existing `MEMORY_RECALL` handling. If the user says "what did we discuss last week?", Stage 4's explicit patterns already catch this. The recall controller is for within-session implicit references. |

**No changes to MemoryProtocol interface.** The recall controller uses `session_messages` (already available in the pipeline) and produces `RecallResult` as metadata. The existing `recall_broad()` path handles memory enrichment when the intent is (re)classified as `MEMORY_RECALL`.

**Future interaction (Slice 3 stretch — proactive memory):** If Seshat embeddings (EVAL-08 rank 2) are implemented, the recall controller could optionally query `MemoryProtocol.recall()` with extracted noun phrases for cross-session corroboration. This is a future enhancement, not part of the initial implementation.

### Decision 5: Supplements the intent classifier — does not replace it

The recall controller is an **additive refinement**, not a replacement for Stage 4.

**What stays the same:**

- Stage 4 (`intent.py`) is unchanged. All existing `_MEMORY_RECALL_PATTERNS` remain. Explicit recall phrases ("do you remember", "what have I discussed") are still caught at Stage 4 with confidence 0.9.
- The `classify_intent()` function remains stateless and regex-only.
- Context assembly for `MEMORY_RECALL` calls `recall_broad()` as before.

**What changes:**

- A new Stage 4b runs after Stage 4 and before Stage 5 in the pipeline.
- Stage 4b only fires when Stage 4 returns `CONVERSATIONAL`. All other classifications pass through unchanged.
- If Stage 4b reclassifies, the `IntentResult` is replaced with a new one: `task_type=MEMORY_RECALL`, `confidence=0.85` (lower than Stage 4's 0.9 — reflects the indirect evidence), `signals=["recall_cue_reclassified", "<cue_pattern>"]`.
- The `GatewayOutput` gains a `recall_context: RecallResult | None` field for telemetry and context injection.

**Why not replace Stage 4's patterns?** The two stages serve different roles:

| | Stage 4 | Stage 4b |
|---|---|---|
| Trigger | Explicit recall phrases | Implicit backward-reference cues |
| Evidence | Pattern match alone is sufficient | Pattern match + session fact corroboration required |
| State | Stateless (raw message only) | Stateful (needs session_messages) |
| False-positive risk | Low (specific phrases) | Moderate (cues are ambiguous without corroboration) |
| Confidence | 0.9 | 0.85 |

Keeping them separate follows the principle of progressive refinement: cheap stateless classification first, more expensive stateful refinement only when needed.

---

## Recall Controller Architecture

### Control Flow

```
                    ┌──────────────────────────────────┐
                    │    Stage 4 Output                 │
                    │    IntentResult(task_type=...)     │
                    └──────────────┬───────────────────┘
                                   │
                    ┌──────────────▼───────────────────┐
                    │    task_type == CONVERSATIONAL?    │
                    └──┬───────────────────────────┬──┘
                       │                           │
                    NO │                      YES  │
                       │                           │
          ┌────────────▼────────────┐    ┌────────▼──────────────┐
          │  Pass through unchanged │    │  Check recall cues    │
          └─────────────────────────┘    └────────┬──────────────┘
                                                  │
                                   ┌──────────────▼───────────────┐
                                   │    Cue patterns match?        │
                                   └──┬───────────────────────┬──┘
                                      │                       │
                                   NO │                  YES  │
                                      │                       │
                     ┌────────────────▼──────┐    ┌──────────▼───────────────┐
                     │  Pass through          │    │  Extract noun phrases    │
                     │  (cues were absent)    │    │  Scan session_messages   │
                     └───────────────────────┘    └──────────┬───────────────┘
                                                             │
                                              ┌──────────────▼───────────────┐
                                              │    Session facts found?       │
                                              └──┬───────────────────────┬──┘
                                                 │                       │
                                              NO │                  YES  │
                                                 │                       │
                                ┌────────────────▼──────┐    ┌──────────▼───────────────┐
                                │  Pass through          │    │  Reclassify to           │
                                │  (false-positive cue)  │    │  MEMORY_RECALL           │
                                │                        │    │  Attach RecallResult     │
                                └───────────────────────┘    └──────────────────────────┘
```

### Three-gate design

The recall controller has three sequential gates, each reducing false positives:

1. **Gate 1 — Task type gate:** Only `CONVERSATIONAL` classifications enter the controller. All other types (including `MEMORY_RECALL` from Stage 4) bypass it entirely. This means the controller runs on ~60–70% of messages (conversational is the default fallback), but exits immediately for most of them.

2. **Gate 2 — Cue pattern gate:** Regex match against `_RECALL_CUE_PATTERNS`. Most conversational messages don't contain backward-reference cues. This gate filters out ~95% of `CONVERSATIONAL` messages.

3. **Gate 3 — Session fact gate:** Noun phrase extraction + session history scan. Only reclassifies if a matching fact is found. This is the precision gate — it prevents "Let's go back to the architecture discussion" from being misclassified when no specific fact is referenced.

**Expected false-positive rate after all three gates:** Very low. A message must (1) fail all Stage 4 explicit patterns, (2) contain an implicit cue, and (3) reference a noun phrase that appears as a stated fact in prior turns. False positives at this level are harmless — they produce extra memory enrichment, not incorrect answers.

### Code Location

New module: `src/personal_agent/request_gateway/recall_controller.py`

Existing module changes:

| File | Change |
|------|--------|
| `request_gateway/pipeline.py` | Add Stage 4b call between Stage 4 and Stage 5 (~5 lines) |
| `request_gateway/types.py` | Add `RecallCandidate`, `RecallResult` dataclasses; add `recall_context` field to `GatewayOutput` |
| `request_gateway/context.py` | Inject session fact candidates into LLM context when `recall_context` is present |

No changes to: `request_gateway/intent.py`, `memory/protocol.py`, `orchestrator/executor.py`.

**Estimated size:** ~120–150 lines for `recall_controller.py` (cue patterns, noun phrase extraction, session scan, result construction).

---

## Telemetry

### New Events

| Event | Fields | When |
|-------|--------|------|
| `recall_controller_skipped` | `original_task_type` | Stage 4b skipped (non-CONVERSATIONAL input) |
| `recall_cue_detected` | `cue_pattern`, `message_excerpt` | Cue pattern matched (Gate 2 passed) |
| `recall_session_scan` | `noun_phrases`, `turns_scanned`, `candidates_found` | Session history scanned (Gate 3) |
| `recall_reclassified` | `original_type`, `new_type`, `trigger_cue`, `top_candidate_fact`, `confidence` | Intent reclassified to MEMORY_RECALL |
| `recall_cue_false_positive` | `cue_pattern`, `reason` (no_noun_phrase, no_session_match) | Cue detected but no corroborating session fact found |

### New Metrics

| Metric | Definition | Purpose |
|--------|-----------|---------|
| **Recall reclassification rate** | % of CONVERSATIONAL intents reclassified to MEMORY_RECALL | Measures controller activation frequency |
| **Recall cue false-positive rate** | % of cue detections that don't lead to reclassification | Monitors Gate 3 precision |
| **CP-19 class pass rate** | % of implicit backward-reference prompts correctly classified | Primary success metric |

---

## Risks and Mitigations

### Risk 1: Over-reclassification inflates MEMORY_RECALL count

If cue patterns are too broad, conversational messages get reclassified, triggering unnecessary `recall_broad()` Neo4j queries.

**Mitigation:**
- Three-gate design (task type → cue → session fact) minimizes false positives.
- `recall_broad()` is read-only and fast on small graphs. Over-triggering costs latency, not correctness.
- Track `recall_cue_false_positive` rate. If consistently high, narrow cue patterns.

### Risk 2: Noun phrase extraction is brittle

Simple noun phrase extraction (split on spaces, look for capitalized words or known entity patterns) may miss complex references or match irrelevant words.

**Mitigation:**
- Start with simple extraction (capitalized nouns, known entity type keywords from ADR-0025's `_ENTITY_TYPE_KEYWORDS`).
- Fall back to matching the last 2–3 content words before "again"/"earlier" as the target phrase.
- Session scan uses substring matching, not exact equality — "primary database" matches "our primary database is PostgreSQL".
- Track `candidates_found` to detect extraction quality issues.

### Risk 3: Session scan adds latency

Scanning all session messages for noun phrases could be slow for long sessions.

**Mitigation:**
- Scan most recent N turns first (configurable, default 20). CP-19's fact was in Turn 2 of 10 — within any reasonable window.
- Exit on first match (for reclassification decision). Collect up to 3 candidates for evidence injection.
- `session_messages` is already loaded in memory — no I/O cost.
- Expected latency: <5ms for 20-turn scan with simple string matching.

### Risk 4: Recall controller and Stage 4 patterns drift apart

If Stage 4's patterns are updated without considering Stage 4b's cues, gaps or overlaps may develop.

**Mitigation:**
- Both pattern sets are in `request_gateway/` — proximity encourages co-maintenance.
- Test suite should include cross-stage cases: messages that should be caught by Stage 4 (explicit), Stage 4b (implicit), and neither (true conversational).
- Telemetry tracks both `memory_recall_pattern` (Stage 4) and `recall_cue_reclassified` (Stage 4b) signals, making overlap visible.

---

## Impact on Existing Components

### No changes required

- Stage 4 intent classifier (`intent.py`) — unchanged
- MemoryProtocol interface (`protocol.py`) — unchanged
- Memory service / protocol adapter — unchanged
- Orchestrator / executor — unchanged
- Expansion controller (ADR-0036) — independent concern
- Brainstem / homeostasis — unrelated

### Changes required

| Component | Nature of change |
|-----------|-----------------|
| New: `request_gateway/recall_controller.py` | Core component (~120–150 lines) |
| `request_gateway/pipeline.py` | Add Stage 4b call (~5 lines) |
| `request_gateway/types.py` | Add `RecallCandidate`, `RecallResult` types; extend `GatewayOutput` |
| `request_gateway/context.py` | Inject session fact candidates when `recall_context` is present |

---

## What This ADR Does NOT Cover

1. **Cross-session recall** — If the user asks about something from a prior session, that's an existing `MEMORY_RECALL` + `recall_broad()` concern. The recall controller is scoped to within-session implicit references.
2. **Embedding-based recall** — Seshat embeddings (EVAL-08 rank 2) would enable semantic matching for recall. That's a future enhancement, not a prerequisite.
3. **LLM-based classification** — Using an LLM to classify ambiguous intents is rejected for latency reasons. If the three-gate heuristic proves insufficient, reconsider in a separate ADR.
4. **Adversarial eval variants** — Paraphrased CP-19 prompts ("again", "earlier", "at the start", "remind me", "what did we say") should be added to the evaluation harness as implementation work, not architecture.

---

## Implementation Priority

This component is independent of both the expansion controller (ADR-0036, Track A) and Seshat enhancements (Track B). It can be parallelized with either.

| Order | Work | Rationale |
|-------|------|-----------|
| 1 | Add `RecallCandidate`, `RecallResult` types | Types-first design (Cherny principle) |
| 2 | Implement `recall_controller.py` (cue patterns, noun extraction, session scan) | Core logic |
| 3 | Wire Stage 4b into `pipeline.py` | Integration point |
| 4 | Update `context.py` to inject session fact candidates | LLM context enrichment |
| 5 | Add telemetry events | Observability |
| 6 | Revise CP-19 evaluation assertions (classification + answer correctness) | Validation |
| 7 | Add CP-19 adversarial variants (6+ paraphrases) | Robustness testing |

**Estimated effort:** S (≤1 week), as confirmed by EVAL-08 ranking.

---

## References

- Evaluation Orchestration Analysis: `docs/research/evaluation-orchestration-analysis.md`
- Second Opinion Response (GPT-5.4): `docs/research/evaluation-run-04-second-opinion-response.md`
- Second Opinion Remediation Plan: `docs/research/evaluation-run-04-second-opinion-proposed-remediation.md`
- EVAL-08 Slice 3 Priorities: `docs/research/EVAL_08_SLICE_3_PRIORITIES.md`
- ADR-0025 Memory Recall Intent Detection: `docs/architecture_decisions/ADR-0025-memory-recall-intent-detection.md`
- ADR-0036 Expansion Controller: `docs/architecture_decisions/ADR-0036-expansion-controller.md`
- "Lost in the Middle" (Liu et al., 2023): https://arxiv.org/abs/2307.03172
- Anthropic — Building Effective Agents: https://www.anthropic.com/research/building-effective-agents

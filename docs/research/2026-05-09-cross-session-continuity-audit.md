# Cross-session continuity audit (FRE-346)

> **Date**: 2026-05-09
> **Trigger**: FRE-227 paused after surfacing the Need 1 (user wiki) / Need 2 (agent cross-session continuity) split. This audit answers: do existing systems already serve Need 2?
> **Method**: read-only inventory of `memory/`, `captains_log/`, `insights/`, `request_gateway/stages/context_assembly`, and `brainstem/` (3 parallel Explore agents, 2026-05-09). No code changes.
> **Inputs preserved**: companion note `docs/research/2026-05-09-personal-vault-reflection.md` (the FRE-227 design exploration paused for this audit).

## Executive summary

**Most of Need 2 is already served, but with one large schema-ready gap and two well-defined surface gaps.**

The data the agent needs is being captured in three places (Neo4j Turn nodes, Captain's Log captures + reflections, Insights events). The gap is not in capture — it's in **synthesis** (a session-level narrative summary slot exists but is never populated) and **surfacing** (Captain's Log + Insights are written for humans and never re-read by the agent).

Three concrete tickets emerge:

| Gap | Shape | Recommended ticket |
|---|---|---|
| `SessionNode.session_summary` field exists, is always `None` | LARGE, schema-ready | **High-value**: implement session-end narrative summarisation. Tier-2:Sonnet. |
| Captain's Log reflections never reach future sessions | MEDIUM, design needed | Surface relevant past reflections in `context_assembly`. Tier-1:Opus. |
| Insights flow only to Linear + dashboards (humans), never to the agent | MEDIUM, design needed | Surface relevant insights in `context_assembly`. Tier-1:Opus. |

Two smaller observations are noted but not yet ticketed (no speculative builds): no hypothesis/idea memory shape, and no formal "resumable task state" surface. Both might be subsumed by other work; we wait for them to bite.

**Implication for FRE-227**: Need 2 has well-defined fixes that don't require a generic "agent scratchpad" / "Obsidian vault for the agent." FRE-227 (the personal wiki / Need 1) can be evaluated on its own merits, decoupled from cross-session continuity.

---

## Inventory: what each system actually does

### 1. Memory subsystem (`memory/`)

**Capture is rich; retrieval is entity-anchored.**

- `MemoryProtocol` (`memory/protocol.py:20–85`) defines six `MemoryType` values: WORKING (ephemeral, not persisted), EPISODIC (timestamped interactions, full prose), SEMANTIC (extracted facts), PROCEDURAL (learned patterns), PROFILE (long-lived traits), DERIVED (summaries/aggregations). The `Episode` dataclass preserves `user_message` and `assistant_response` as raw strings, plus `tools_used` and `entities` lists.
- `MemoryService.create_conversation()` (`memory/service.py:306`) writes a Neo4j `:Turn` node with full prose properties (`user_message` line 352, `assistant_response` line 353). Turn nodes have **no embedding** — only entities do.
- `query_memory()` (`service.py:939`) returns turns with intact narrative; vector search is entity-only via `db.index.vector.queryNodes('entity_embedding', ...)` (lines 1072–1087). Time-window querying via `recency_days` parameter (lines 1010–1019).
- `promote_entity()` (`service.py:1827`) upgrades an entity to `memory_type='semantic'`. **Original Turn nodes remain fully retrievable** — promotion preserves prose, doesn't replace it.
- `:Session` nodes (`memory/models.py:117`) have a `session_summary` property — **but it is always `None`**. See the consolidator below.

**What's missing**: Turn-level embeddings (so narrative similarity search is impossible — only entity-name match works). No node type for in-progress hypotheses or working thoughts. No annotation layer for revisiting past narratives with new context.

### 2. Captain's Log (`captains_log/`)

**Captures structured events + DSPy reflections; both land in Postgres + ES; neither is read back by the agent.**

- `TaskCapture` (`captains_log/capture.py:40–75`) writes per-turn JSON with `user_message`, `assistant_response`, `steps`, `tools_used`, `outcome`, `metrics_summary`, `tool_results`. Lands in `telemetry/captains_log/captures/YYYY-MM-DD/<trace>.json` and ES `agent-captains-captures-YYYY-MM-DD`. Doc-id = trace_id (idempotent).
- `reflection_dspy.py:74–155` runs DSPy ChainOfThought per-turn (background task). Inputs: user message excerpt, telemetry summary, metrics summary, failure excerpt, FRE-301 `[ITERATION LIMIT HIT]` marker. Outputs: `rationale`, `proposed_change_what/why/how`, `proposed_change_category/scope`, `impact_assessment`. Lands in `telemetry/captains_log/CL-*.json` + ES `agent-captains-reflections-YYYY-MM-DD`.
- Reflections become `CaptainLogEntry(category=CONFIG_PROPOSAL)` and feed the promotion pipeline → Linear (humans).

**What's missing**: No code path in `request_gateway/` or `orchestrator/` queries `agent-captains-captures-*` or `agent-captains-reflections-*` during context assembly for the next session. ADR-0030 scopes Captain's Log as "self-reflection for improvement proposals," not "context injection." The corpus is observation-only; the agent never re-reads its own observations.

### 3. Insights engine (`insights/`)

**Six sub-engines analyse patterns; outputs flow to Linear + ES dashboards; the agent never consults them.**

Sub-engines (from `insights/engine.py`):

1. Resource correlation (CPU/memory vs success rate) — `correlation` insights
2. Delegation patterns (success rate, missing-context terms) — `delegation` insights
3. Cost anomalies (3σ spike detection) — `anomaly` insights
4. Graph staleness (entity tier decay) — `graph_staleness` insights
5. Feedback patterns (Linear approval/rejection) — `feedback_summary`/`feedback_category` insights
6. Usage trends (peak hour, mention patterns) — `trend` insights

Outputs publish to event bus (`stream:insights.pattern_detected`, `stream:insights.cost_anomaly`), index to ES `agent-insights-YYYY-MM-DD`, and convert to `CaptainLogEntry` proposals routed to Linear via the promotion pipeline.

**What's missing**: same as Captain's Log — no surface that re-injects insights into agent context. The agent doesn't know "you've had a 40% success rate on `MEMORY_RECALL` this week." That signal exists; humans see it; the agent doesn't.

### 4. Context assembly (`request_gateway/`)

**Sessions start cold. The "previous-session-gist" slot is wired but always empty.**

- `assemble_context()` (`request_gateway/context.py:247–336`) pulls four kinds of input:
  - Session in-flight history (`messages.extend(session_messages)` line 282)
  - State document (system message)
  - Memory query (intent-driven; `MEMORY_RECALL` → `recall_broad`; proactive → `suggest_relevant`; otherwise entity-name match on capitalized words)
  - Stage 4b recall context (session-fact candidates from recall_controller)
- `recall_broad` returns entities by type **and** `recent_sessions` with their `session_summary` (line 130). The shape is there. The data is `None`.
- New session creation (`service/app.py:142–161`): `SessionModel(messages=[])` — empty list, no pre-fill. No fetch of prior summaries, reflections, or insights.
- No code path search hits for: `previous_session`, `last_session`, `carry_forward`, `continuation`, `session_gist`, `session_recap`, `thought_trail`, `scratchpad`, `note_to_self`. One hit on `session_summary`: the consolidator marks it `None` with a comment.

### 5. Brainstem (`brainstem/`)

**Schedules consolidation, freshness, monitors. Consolidation extracts entities + creates session structure; explicitly defers narrative summarisation.**

- `BrainstemScheduler._lifecycle_loop` (`brainstem/scheduler.py:352`) dispatches: hourly disk check, daily archive (2 AM UTC), weekly purge + ES cleanup (Sun 3 AM), weekly freshness review, daily Linear feedback poll, daily quality monitoring (5 AM), daily skill-routing threshold monitor (FRE-335).
- Consolidator (`second_brain/consolidator.py:70–254`): reads 7-day captures → entity extraction → builds Turn + Session nodes. Line 300:
  ```python
  session_node = SessionNode(
      session_id=session_id,
      ...
      dominant_entities=[],
      session_summary=None,  # Generated lazily in future
  )
  ```
- Promotion pipeline (`captains_log/promotion.py:171–230`): scans Captain's Log entries, filters by `seen_count >= 3` and age, promotes to Linear. **Not** a session-summary generator.
- Mode manager (`brainstem/mode_manager.py:85–125`): NORMAL/ALERT/DEGRADED/LOCKDOWN/RECOVERY transitions are **stateless across sessions**. No "this is a continuation of yesterday" signal.

---

## Use-case mapping

The starter set from FRE-346, walked against the inventory.

### UC-1: "I'm halfway through a refactor — here's where I stopped and what I was thinking."

- **Memory**: Turn nodes preserve the prose (✅ data captured). But retrieval requires entity-name match — agent has no signal "this is a resumable task state."
- **Captain's Log**: Captures the messages (✅), but never re-read.
- **Verdict**: **PARTIAL.** Data exists, retrieval signal absent. Resumption would require either tagging certain turns as "in-progress" or surfacing relevant reflections at session start.

### UC-2: "Three weeks ago we decided X for reason Y; remember that decision verbatim."

- **Memory**: Turn nodes preserve full prose; `recency_days` time-window query supports it. Vector search is entity-only — must hit on `X` or `Y` as an extracted entity. Decisions whose subjects ARE entities work; decisions whose subjects are abstract (e.g. "the auth approach") may not.
- **Captain's Log**: Reflections may have captured the rationale + proposal, queryable in ES.
- **Verdict**: **PARTIAL.** Works for entity-anchored decisions, fails for unstructured rationale or pattern-shaped decisions.

### UC-3: "I had an idea about Z while talking yesterday; bring it back when relevant."

- **Memory**: If Z became an extracted entity, semantic memory will surface it. Abstract ideas (`a refactoring approach`, `a new evaluation harness shape`) won't be extracted.
- **Verdict**: **GAP.** Abstract ideas/hypotheses fall through entity extraction.

### UC-4: "Track an evolving hypothesis across many sessions."

- **Memory**: No mechanism. Memory is fact-shaped (entities + relationships), not claim-shaped (hypothesis with evolving confidence over time).
- **Captain's Log**: Reflections are per-turn snapshots, not evolving threads.
- **Insights**: Pattern detection is statistical, not hypothesis-tracking.
- **Verdict**: **GAP.** Real and substantial. Research-shaped need not served by entity extraction.

### UC-5: "Remember a long conversation's gist, not just the extracted entities."

- **Memory**: `SessionNode.session_summary` field exists. Always `None`. The consolidator explicitly defers it ("Generated lazily in future").
- **Verdict**: **GAP** — the largest, best-shaped one. Schema slot exists; consolidation pipeline runs; only the summarisation step is missing.

### UC-6: "Notes the agent left for itself before delegating to a sub-agent or external delegate."

- **Memory**: No surface. `DelegationPackage` carries context for the delegation itself, not "notes for next session."
- **Captain's Log**: Reflections aren't authored intent — they're post-hoc analysis.
- **Verdict**: **SMALL GAP.** Narrow; might be subsumed by either UC-5 (session summaries) or by FRE-227 (Need 1) if the user-side wiki has an `/agent-notes/` corner.

---

## Gap list, ranked

### G1 (LARGE, schema-ready) — `SessionNode.session_summary` is never populated

**Evidence**: `second_brain/consolidator.py:300`, `memory/models.py:117`, `request_gateway/context.py:130`. The shape is wired through to context assembly. Only the synthesis step is missing.

**Why it matters**: This single gap is the largest cause of Need 2 use-case failures. UC-1, UC-2 (the rationale-shaped half), UC-5 are all fixed or substantially helped by populating this field.

**Shape of the fix**: end-of-session DSPy summariser (or simpler heuristic) that condenses the session's turns into a short prose summary + dominant_entities update. Hooked into the existing consolidation pipeline (event-driven via `stream:request.captured`).

**Risks**: cost of summarisation × number of sessions; quality drift if summariser is too small; potential PII/sensitivity concerns about what gets summarised. All manageable.

→ **Proposed ticket: file as Tier-2:Sonnet (the design is mostly clear; implementation is the bulk).**

### G2 (MEDIUM) — Captain's Log reflections never reach future sessions

**Evidence**: no code path queries `agent-captains-reflections-*` during context assembly. ADR-0030 scopes Captain's Log as observation-only.

**Why it matters**: the agent is generating high-quality per-turn reflections (DSPy ChainOfThought with rationale + proposed-change suggestions) and never reading its own work. UC-1 (resumable task state), UC-3 (abstract ideas), UC-4 (evolving hypotheses) all benefit from the agent re-encountering its own past observations.

**Shape of the fix**: a recall layer that queries reflections matching the current session's intent or entities, surfaces a small (budget-aware) selection in context assembly. Probably a new Stage 6 sub-step.

**Risks**: token cost; prompt bloat; selection criteria need design (intent-match? entity-match? embedding similarity over rationale text?). This is genuinely a design exercise.

→ **Proposed ticket: file as Tier-1:Opus (design first, then implement).**

### G3 (MEDIUM) — Insights are written for humans, never re-injected into agent context

**Evidence**: insights flow to ES dashboards + Linear (via promotion). No re-injection path.

**Why it matters**: the agent's own pattern analysis (success rate by task type, delegation success patterns, cost anomalies) is invisible to it. Self-improvement loop is broken — the agent observes itself but doesn't act on the observations without human relay.

**Shape of the fix**: similar to G2 — a context-assembly surface that pulls actionable insights, probably gated by relevance to current request. Could be quite small (1-2 high-priority insights per session).

**Risks**: avoiding noise (most insights aren't actionable for this turn); preventing insight thrashing; budget cost.

→ **Proposed ticket: file as Tier-1:Opus.**

### G4 (SMALL, observation only) — No hypothesis/idea memory shape

**Evidence**: UC-3, UC-4. Memory is fact-shaped (Entity + Relationship), not claim-shaped.

**Why it matters less than G1–G3**: the felt frequency may be low; G1 (session summaries) and G2 (reflection surfacing) may absorb most of this gap before it's noticed in isolation.

**Recommendation**: do not file yet. If after G1+G2+G3 the gap remains visible, file then with concrete examples. Ties to FRE-226 (self-updating skills) which is a related "agent learns over time" surface.

### G5 (SMALL, observation only) — No formal "resumable task state"

**Evidence**: UC-1, UC-6.

**Why it matters less**: G1 + G2 substantially help UC-1; UC-6 is narrow. May be subsumed by FRE-227 if user-vault is built. May also be addressed by enriching the existing TodoWrite / planning surfaces with cross-session persistence — but that's a separate domain.

**Recommendation**: do not file yet. Reassess after G1.

---

## What this means for FRE-227

FRE-227 was paused because the ticket conflated Need 1 (user knowledge vault) and Need 2 (agent cross-session continuity). This audit shows:

- Need 2 has three well-defined fixes (G1, G2, G3). None require a generic "agent scratchpad" or "Obsidian vault for the agent." All work within existing infrastructure (Neo4j Sessions, ES Captain's Log corpus, Insights pipeline).
- Need 1 (the personal wiki / Karpathy LLM Wiki shape) is therefore a clean separate concern: a user-authored markdown document store that doesn't compete with memory.

**Recommendation for FRE-227**: keep paused. After G1 ships and the session-summary signal is live, revisit FRE-227 with a sharper question: *do we still want a user-authored wiki on top of agent-generated session summaries?* The answer is probably yes (different content, different author, different lifecycle), but the design will be clearer with G1 in place to compare against.

---

## Out of scope for this audit

- Implementing any of the gap fixes (G1/G2/G3). Each is a separate ticket with its own approval gate.
- Quantitative validation (e.g., "how often does the user actually invoke UC-2?"). The audit is structural, not empirical. If empirical validation is wanted, that's a Captain's Log + Insights project (and ironically the kind of thing G3's surfacing would help with).
- ADR-0030 amendment. G2 + G3 collectively imply that Captain's Log + Insights aren't pure observation surfaces anymore — they should also feed back. If G2+G3 ship, ADR-0030 should be amended in-place (similar to ADR-0052 amendment for FRE-213).

---

## Followups filed

| Linear | Title | Priority / Tier | State |
|---|---|---|---|
| FRE-347 | Generate session_summary in consolidator (G1) | High / Tier-2:Sonnet | Needs Approval |
| FRE-348 | Surface relevant Captain's Log reflections in context assembly (G2) | Medium / Tier-1:Opus | Needs Approval |
| FRE-349 | Surface actionable Insights in agent context (G3) | Medium / Tier-1:Opus | Needs Approval |

G4, G5 — observations, no ticket.

## Acceptance check

- [x] Every UC in the starter set mapped to existing systems or named gaps (UC-1..UC-6 above).
- [x] Research note committed to `docs/research/` (this file).
- [x] MASTER_PLAN.md updated (FRE-346 → Recently Completed; FRE-347/348/349 → Needs Approval).
- [x] No code changes made.

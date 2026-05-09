# ADR-0067: Reflection Surfacing in Context Assembly

**Status**: Accepted
**Date**: 2026-05-09
**Deciders**: Project owner
**Related**: ADR-0030 (Captain's Log dedup + promotion pipeline — write side); FRE-346 (cross-session continuity audit § Gap G2); FRE-348 (this implementation)

---

## Context

The Captain's Log generates a structured DSPy reflection per turn (`rationale`, `proposed_change_*`, `failure_path_fix_*`, `category`, `scope`, `seen_count`) and persists it to disk + Elasticsearch (`agent-captains-reflections-YYYY-MM-DD`). ADR-0030 wired the write side: dedup by fingerprint, promotion to Linear, dashboard surfacing for humans.

**The agent never re-reads its own reflections.** ADR-0030 explicitly scopes Captain's Log as "self-reflection for improvement proposals" surfaced to humans, not to the agent. This was correct for the initial corpus but is increasingly counterproductive: the FRE-346 audit showed three cross-session use cases (UC-1 resumable refactor state, UC-3 abstract-idea recovery, UC-4 evolving-hypothesis tracking) that fall through the current surfaces. Memory captures entities, not reasoning; session_summary (FRE-347) captures narrative gist, not the agent's own analytical observations.

Reflections are exactly the right shape for the missing surface — concise, time-stamped, already-deduplicated rationale with seen_count signal. They just need to flow back.

## Decision

Surface a small, recency- and relevance-bounded slice of past reflections into context assembly (Stage 6), as a system-message section labeled clearly as past observations, not current directives.

### Selection algorithm (v1)

For each new turn, select up to 3 reflections matching ALL of:

1. **Recency**: `created_at` within the last 14 days (configurable via `AGENT_REFLECTION_RECALL_RECENCY_DAYS`).
2. **Persistence signal**: `seen_count >= 2` — single-instance reflections are noise; recurring patterns are signal.
3. **Has actionable content**: `proposed_change_what` non-empty OR `failure_path_fix_what` non-empty. Pure-rationale reflections without a proposal are too granular.
4. **Relevance**: `rationale` OR `proposed_change_what` matches at least one capitalized entity hint from the current user message (reusing `_capitalized_entity_hints()` from `request_gateway/context.py`). This is intentionally a coarse filter for v1; embedding similarity is Phase 2.
5. **Not already tracked-and-resolved**: skip reflections where `linear_issue_id` is set AND the linked issue's state is Done/Cancelled. (Tracked-and-pending issues are still useful to surface — the agent should know it's previously flagged a problem that still exists.)

Order by `seen_count DESC, created_at DESC`. Cap at 3 results.

### Surface format

A single system-message section, injected between memory_context and recall_context in `assemble_context()`:

```
## Recent reflections from your prior work

These are signals from your earlier sessions, not directives. The current turn may
warrant a different approach — use these only as context.

- 2026-05-08 (seen 5x, performance/llm_client): Tool retries inflate latency
  when LLM returns 429. Proposed: add jittered backoff in respond().
- 2026-05-05 (seen 3x, knowledge/second_brain): Entity extraction fails on
  short conversations. Proposed: skip extraction below 50-char threshold.
- 2026-05-03 (seen 2x, ux/orchestrator): Hit iteration limit on memory recall
  twice. → tracked as FRE-301.
```

Format invariants (anti-thrash safeguards):

- The header text **must** explicitly frame these as signals, not directives. Prompt language is the primary anti-thrash mechanism.
- Each entry shows: date, seen_count, category/scope tags, truncated rationale (~120 chars), proposed change summary (~80 chars), and Linear ticket reference if tracked.
- Hard cap: 3 entries × ~250 chars + header ≈ 850 chars (~210 tokens). Negligible relative to memory_context.
- Reflections with `linear_issue_id` set explicitly say "→ tracked as FRE-XXX" so the agent knows the human is already aware.

### Surface point in the pipeline

New helper module `src/personal_agent/captains_log/recall.py` exporting `async def query_relevant_reflections(...)`. Hook in `request_gateway/context.py:assemble_context()` after the memory query, before the recall_context section, so the layout is:

```
1. Session history (existing)
2. State document (existing)
3. Memory context (existing — entities, sessions)
4. Reflection recall (NEW — this ADR)
5. Recall controller fact candidates (existing)
6. User message (existing)
```

### Failure mode

`query_relevant_reflections` MUST never raise to the caller. ES query timeout, missing index, malformed document → log a warning and return `[]`. Context assembly proceeds without reflections; the agent sees the world it always saw before this ADR.

### Settings

| Field | Default | Env |
|---|---|---|
| `reflection_recall_enabled` | `True` | `AGENT_REFLECTION_RECALL_ENABLED` |
| `reflection_recall_recency_days` | `14` | `AGENT_REFLECTION_RECALL_RECENCY_DAYS` |
| `reflection_recall_max_results` | `3` | `AGENT_REFLECTION_RECALL_MAX_RESULTS` |
| `reflection_recall_min_seen_count` | `2` | `AGENT_REFLECTION_RECALL_MIN_SEEN_COUNT` |

Disabling the kill-switch returns the system to its pre-ADR-0067 behaviour (no reflection surfacing).

### Telemetry

Each context assembly that runs reflection recall emits one structured log:

```
event: reflection_recall_completed
trace_id, session_id
candidates_considered: int
selected_count: int (0..max_results)
selected_entry_ids: list[str]
elapsed_ms: float
```

This is the substrate for the post-deploy eval (see below).

## Eval strategy

The acceptance criterion in FRE-348 reads "Eval shows measurable improvement on at least one Need-2 use case." Running the eval before deployment requires synthetic prompts that match historical reflections — reverse-engineering the dataset, brittle. Instead:

1. **Pre-merge**: ship the surfacing with the structured telemetry above + unit tests for the selection algorithm. Verify the path is wired end-to-end against a fixture ES.
2. **Post-deploy (1–2 weeks of real usage)**: file a follow-up ticket to analyse the `reflection_recall_completed` events alongside subsequent agent behaviour. Specifically: when reflections WERE surfaced, did the agent take a different action than its un-surfaced baseline would predict, and was that action better? Quantify on at least 5 captured turns.

This is documented as the path to closing the FRE-348 acceptance criterion, not deferred indefinitely.

## Alternatives considered

### A. Embedding similarity over rationale text

Use vector embeddings of `rationale` + current user message to score relevance, top-k by cosine.

*Pros*: catches semantic similarity that capitalized-name match misses. Better recall.
*Cons*: requires embedding every reflection at index time (back-population for the existing corpus); query-time embedding cost; fragile threshold tuning. Phase 2 path; v1 ships with the simpler heuristic.

### B. Surface reflections inside memory_context (single channel)

Don't add a new section — fold reflections into the existing `_format_broad_recall_context` output as another `type: "reflection"` item.

*Pros*: one channel, simpler.
*Cons*: memory_context items are budget-trimmed silently and are framed as "facts" — semantically wrong for "your past observations." A separate section is honest about provenance.

### C. Surface only failure-path fixes, not proposals

Limit to `failure_path_fix_what` reflections (FRE-244 Phase 2). Surgical, low-thrash.

*Pros*: explicit "do this differently next time" signal; almost no anti-thrash risk.
*Cons*: too narrow. Misses UC-3 (abstract idea recovery) and UC-4 (evolving hypothesis), which are exactly the gaps that motivated FRE-346.

**Chosen**: include both proposals and failure-path fixes (filter by `proposed_change_what` non-empty OR `failure_path_fix_what` non-empty). Anti-thrash safeguards are in the framing prose, not in the filter.

### D. Surface reflections only on MEMORY_RECALL or follow-up intents

Restrict surfacing to specific TaskTypes.

*Pros*: avoids surfacing reflections for unrelated turns.
*Cons*: the entity-hint relevance filter already accomplishes this — a turn with no entity hints relevant to past reflections returns zero, and we skip the section entirely. Adding intent gating is redundant complexity.

## Consequences

**Positive:**
- Closes Gap G2 from FRE-346. Three use cases (UC-1, UC-3, UC-4) gain a viable retrieval path.
- Self-improvement loop closes: agent's own observations re-enter its context.
- Foundation for G3 (FRE-349 Insights surfacing) — same channel pattern, different source.

**Negative:**
- Per-turn ES query adds 50–200 ms latency (acceptable; same ES instance handles existing queries).
- Anti-thrash relies on prompt framing. If the framing prose proves insufficient, we adjust the language or kill-switch.
- Eval is post-deploy, so pre-merge confidence is structural, not behavioural.

## Acceptance criteria

- [x] ADR (this document).
- [ ] `captains_log/recall.py` with `query_relevant_reflections(...)` and unit tests covering: empty result, single match, max-results truncation, ES error → empty, kill-switch.
- [ ] `request_gateway/context.py:assemble_context()` integrates the new section.
- [ ] Settings fields wired with kill-switch default `True`.
- [ ] `.env.example` documents the four new env vars.
- [ ] Structured telemetry `reflection_recall_completed` emitted.
- [ ] mypy + ruff clean.
- [ ] No regression in existing context_assembly tests.
- [ ] Follow-up ticket filed for post-deploy eval (1–2 week window).

## Links

- FRE-346 audit: `docs/research/2026-05-09-cross-session-continuity-audit.md` § Gap G2
- ADR-0030: `docs/architecture_decisions/ADR-0030-captains-log-dedup-and-self-improvement-pipeline.md` (write side)
- ADR-0040: `docs/architecture_decisions/ADR-0040-linear-async-feedback-channel.md` (Linear feedback channel — promotion pipeline)
- Code: `captains_log/reflection_dspy.py` (DSPy signature — what's stored), `captains_log/manager.py` (write path), `request_gateway/context.py` (read-side hook point)

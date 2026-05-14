# FRE-343 — Personal Time-Window Retrieval — Design Spec

**Date:** 2026-05-14
**Status:** Approved (brainstorming complete; ready for writing-plans)
**Linear:** [FRE-343](https://linear.app/frenchforest/issue/FRE-343) — `Approved`, Tier-2 Sonnet
**Depends on:** FRE-213 (owner identity primitive, shipped 2026-05-09)
**Related:** FRE-342 (person dedup excludes user_id-bound :Person, shipped 2026-05-12), FRE-230 (geolocation — separate, follows independently)
**ADR:** Amends [ADR-0052 — Seshat owner identity primitive](../../architecture_decisions/ADR-0052-seshat-owner-identity-primitive.md) — no new ADR required.

---

## Problem

After FRE-213 every authenticated user has a stable `:Person {user_id}` node in Neo4j, but the memory layer has no way to answer "what did **we** talk about last week?" scoped to *that* user. `search_memory` is visibility-filtered (PUBLIC / GROUP) but not user-scoped — it cannot distinguish "what is in the shared graph" from "what this user actually participated in."

General-knowledge queries continue to use the full shared graph (the agent should still surface what *anyone* contributed about, e.g., the Acropolis). The gap is the **explicit personal exception path**: when the user's phrasing scopes to themselves ("we", "I", "my", "remind me"), the agent should retrieve *their* turns.

---

## Goals

1. Personal-history retrieval by `user_id` × time window, with optional topic substring.
2. Default behavior (`search_memory` on the shared graph) is **unchanged**.
3. Schema change is additive — forward-compatible with FRE-230 (Location) and any future provenance edges.
4. Tighten the type invariant: going forward, every Turn has a `user_id`. `user_id=None` at write time becomes a loud bug, not a silent fallback.
5. One-time backfill recovers ~5 days of authenticated history (and pre-FRE-213 orphans, attributed to the deployment owner).
6. The LLM picks the right tool from phrasing — supported by a complementary skill doc (ADR-0067 pattern).

---

## Non-goals

- **Geolocation** (FRE-230) is a parallel, separate effort. The PARTICIPATED_IN schema is forward-compatible: `(:Turn)-[:OCCURRED_AT]->(:Location)` is a future additive edge; no rework needed here.
- **Lazy / on-query backfill** — rejected (muddies write/read boundary; adds latency to the first personal query that touches each Turn).
- **NL date parsing** — the tool takes integer `days_ago`; the LLM does the reasoning ("last Tuesday" → integer N) using a cheat-sheet in the skill doc.

---

## Architecture decisions

| Decision | Choice | Rationale |
|---|---|---|
| Schema | Option B — `(:Person)-[:PARTICIPATED_IN]->(:Turn)` edge | W3C-PROV / Neo4j community pattern; shared entities stay shared; no denormalization on Turn. See `docs/research/2026-05-09-graph-identity-multi-user-patterns.md` §6. |
| Edge write site | At Turn save time, inside `MemoryService.create_conversation` | Reliable, atomic with Turn MERGE, same Neo4j session. Lazy-on-query rejected. |
| Tool surface | New `recall_personal_history` tool (not a flag on `search_memory`) | Clearer LLM intent; FRE-337/ADR-0067 evidence that dedicated tools beat buried flags. `search_memory` keeps shared semantics. |
| Skill | Complementary `personal-history-recall.md` SKILL.md | ADR-0067 pattern (tool + skill with `nudge:` and trigger keywords) — same combo that beat baseline in FRE-337 eval. |
| `user_id` type | `TaskCapture.user_id: UUID` (non-optional) | Verified that `get_request_user` always resolves a `user_id` (CF Access or owner-email fallback or 401). The `None` branch was a historical defensive read; making it a real bug. |
| Backfill | One-shot script, idempotent | ~5 days of authenticated turns + pre-FRE-213 orphans. Lazy backfill rejected. |
| Backfill attribution | `sessions.user_id` if set → that user's `:Person`; else → owner's `:Person` via `settings.agent_owner_email` | Preserves correct multi-user attribution for sessions created after FRE-213; orphans claimed by owner per user decision. |
| Rollout | One PR, no feature flag, `git revert` is rollback | Write and read halves are mutually dependent; flag overhead not justified. |
| Privacy | None new — `:Person {user_id}` already exists; PARTICIPATED_IN is a structural edge in the same Neo4j visibility scope as the Turn it links | LOCKDOWN-safe: data scope is *narrower* than `search_memory`. |

---

## Section 1 — Schema

Add a single new edge type:

```cypher
(:Person {user_id})-[:PARTICIPATED_IN {created_at, backfilled?}]->(:Turn {turn_id})
```

- Cardinality: one edge per (user, turn) pair.
- Properties: `created_at` (timestamp; equals `Turn.timestamp` for backfilled edges), `backfilled: bool` (only set when written by the backfill script — distinguishes provenance for future audits).
- Existing constraint `person_user_id_unique` on `:Person.user_id` guarantees MERGE target uniqueness.
- No properties added to `:Turn`. The W3C-PROV separation stays clean.
- Forward-compatible with FRE-230: `(:Turn)-[:OCCURRED_AT]->(:Location)` is additive — the personal query just adds a JOIN clause.

---

## Section 2 — Write path

### 2a. Tighten `TaskCapture.user_id` (`src/personal_agent/captains_log/capture.py`)

```python
# Before
user_id: UUID | None = None

# After
user_id: UUID
```

Drop the `@field_validator("user_id", mode="before")` `None` branch if it exists. Pydantic raises on construction with missing `user_id` — a real bug, not a silent fallback.

### 2b. Drop the `None` branch in the consolidator (`src/personal_agent/second_brain/consolidator.py`)

```python
# Before
visibility = "group" if getattr(capture, "user_id", None) else "public"

# After
visibility = "group"  # all new captures have a user_id; visibility="public" stays a read-time concept for pre-FRE-213 turns
```

### 2c. Extend `MemoryService.create_conversation` (`src/personal_agent/memory/service.py`)

New required parameter `user_id: UUID`. Inside the existing single Neo4j session, after the Turn MERGE block (`service.py:334`) and before the entity loop, run:

```cypher
MATCH (p:Person {user_id: $user_id})
MATCH (t:Turn {turn_id: $turn_id})
MERGE (p)-[r:PARTICIPATED_IN]->(t)
ON CREATE SET r.created_at = $timestamp
```

`MATCH` (not `MERGE`) on `:Person` — the node must exist. If it doesn't, that's a logic bug — fail loud rather than silently create a name-less `:Person`.

### 2d. Call-site update

`consolidator.py:481` becomes:
```python
await self.memory_service.create_conversation(turn, user_id=capture.user_id, visibility=visibility)
```

Cost: one extra Cypher round-trip per Turn write, same session, same transaction shape. Latency is invisible because writes happen at consolidation time (background), not on the hot path.

---

## Section 3 — Read path: the tool

### Tool definition (`src/personal_agent/tools/personal_history.py`)

```python
from personal_agent.tools.types import ToolDefinition, ToolParameter

recall_personal_history_tool = ToolDefinition(
    name="recall_personal_history",
    description=(
        "Retrieve the connected user's own past turns within a time window. "
        "Use ONLY when the user explicitly refers to their personal history — "
        "phrasing like 'we talked about', 'what did I ask', 'remind me what I said', "
        "'my conversation last week'. For general knowledge questions "
        "('what do we know about X', 'tell me about Y'), use search_memory instead — "
        "that searches the full shared graph."
    ),
    category="memory",
    parameters=[
        ToolParameter(name="days_ago", type="number", required=True,
                      description="How many days back to look (1 = last 24h, 7 = past week). Range 1..365."),
        ToolParameter(name="topic", type="string", required=False, default=None, json_schema=None,
                      description="Optional substring filter — narrows turns by user_message text (case-insensitive)."),
        ToolParameter(name="limit", type="number", required=False, default=None, json_schema=None,
                      description="Max turns to return, 1..50 (default 10)."),
    ],
    risk_level="low",
    allowed_modes=["NORMAL", "ALERT", "DEGRADED", "LOCKDOWN", "RECOVERY"],
    requires_approval=False,
    requires_sandbox=False,
    timeout_seconds=30,
    rate_limit_per_hour=None,
)
```

### Executor logic

1. Resolve `ctx.user_id`. If missing → `ToolExecutionError("missing_user_id — this is a bug; report it (FRE-343)")`. Loud failure.
2. Validate: `1 <= days_ago <= 365`; `limit = min(max(limit or 10, 1), 50)`.
3. Compute cutoff: `cutoff = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()`. **Important**: `Turn.timestamp` is stored as an ISO 8601 string (`service.py:350` calls `.isoformat()`), not a Neo4j temporal. The Cypher comparison `t.timestamp >= $cutoff` is a lexical string compare — which is chronologically correct only when both operands are ISO 8601 in the same timezone (UTC). The executor must pass `cutoff` as a string in the same form.
4. Issue Cypher:

```cypher
MATCH (p:Person {user_id: $user_id})-[:PARTICIPATED_IN]->(t:Turn)
WHERE t.timestamp >= $cutoff
  AND ($topic IS NULL OR toLower(t.user_message) CONTAINS toLower($topic))
OPTIONAL MATCH (t)-[:DISCUSSES]->(e:Entity)
RETURN t.turn_id      AS turn_id,
       t.timestamp    AS timestamp,
       t.session_id   AS session_id,
       t.user_message AS user_message,
       t.summary      AS summary,
       collect(DISTINCT e.name) AS entities
ORDER BY t.timestamp DESC
LIMIT $limit
```

5. Return:

```python
{
    "turns": [
        {
            "turn_id": str,
            "timestamp": iso8601_str,
            "session_id": str,
            "user_message": str,  # truncated to 300 chars
            "summary": str,
            "entities": list[str],
        },
        ...
    ],
    "total": int,
    "window_days": int,
    "user_id": str,  # for trace correlation, not display
}
```

### Registration

- `src/personal_agent/tools/__init__.py` — add the definition + executor.
- `config/governance/tools.yaml` — add entry (see Section 7).

---

## Section 4 — Backfill

### Script: `scripts/backfill_participated_in.py`

Idempotent, one-shot. Algorithm:

1. Open Postgres + Neo4j connections (reuse `settings`).
2. Resolve `OWNER_UUID = await get_or_create_user_by_email(db, settings.agent_owner_email)`. Cache it.
3. Stream `SELECT session_id, user_id FROM sessions ORDER BY created_at`.
4. For each session, pick `target_uid`:
   - `target_uid = session.user_id` if not NULL, else `OWNER_UUID`.
5. Run one Cypher per session:

```cypher
MATCH (t:Turn {session_id: $session_id})
MATCH (p:Person {user_id: $target_uid})
MERGE (p)-[r:PARTICIPATED_IN]->(t)
ON CREATE SET r.created_at = t.timestamp,
              r.backfilled = true
```

6. If `MATCH (p:Person {user_id: $target_uid})` returns nothing:
   - For `OWNER_UUID`: fail loud (means FRE-213 bootstrap never ran on this DB — real bug).
   - For other users: log warning, skip. Their `:Person` will be MATCH'd on next sign-in; the script does not bootstrap them.
7. Emit summary: edges created, edges already existed, sessions skipped (no matching Person), Turns with no Session (expected ~0).

### Run surface

Add to `Makefile`:
```makefile
backfill-participated-in:
	uv run python -m scripts.backfill_participated_in
```

Run once post-merge.

### Safety

- Read-only on Postgres.
- MERGE-only on Neo4j (`ON CREATE SET` ensures re-runs are no-ops).
- `r.backfilled = true` flag distinguishes backfilled from live edges for any future audits.

---

## Section 5 — Skill doc

`docs/skills/personal-history-recall.md` — author-side XML pilot inside the markdown body (per Anthropic prompt-engineering guidance cited in commit `4b67b5c` / FRE-337).

### Frontmatter

```yaml
---
name: personal-history-recall
description: Retrieve the connected user's own past turns within a time window via recall_personal_history tool. Use only when the user refers to *their* history; for general questions, use search_memory.
when_to_use: When the user's phrasing scopes to themselves — 'we talked about', 'what did I ask', 'remind me what I said', 'last week', 'yesterday', 'days ago'. Not for general knowledge questions ('what do we know about X') — those stay on search_memory.
tools: [recall_personal_history]
nudge: "Match the user's scoping. 'We/I/my/us' → recall_personal_history. 'What do we know about X' → search_memory (shared graph)."
keywords:
  - what did we
  - what did I
  - we talked about
  - we discussed
  - did we
  - remind me what
  - last time we
  - my conversation
  - my history
  - I told you
  - I mentioned
  - I asked
  - last week
  - yesterday
  - earlier this week
  - days ago
---
```

### Body structure

```
# SKILL: personal-history-recall

## What this skill does
One paragraph framing the personal-vs-shared boundary.

## When to use vs search_memory
<when_to_use>
  Use recall_personal_history when the user scopes to themselves: 'we', 'I', 'my', 'remind me'.
  Use search_memory when the user asks a general question: 'what do we know about X', 'tell me about Y'.
  The shared graph is the default; personal-history is an explicit, opt-in narrowing.
</when_to_use>

## Worked examples

<example>
  User: What did we talk about last Tuesday?
  Today: Wednesday. "Last Tuesday" → 8 days ago.
  Call: recall_personal_history(days_ago=8)
</example>

<example>
  User: Remind me what I told you about the Athens trip.
  Topic: "Athens"; window: last month is a safe default for "remind me what".
  Call: recall_personal_history(days_ago=30, topic="Athens")
</example>

<anti_example>
  User: What do we know about the Acropolis?
  This is a general knowledge question — surface anyone's contributions.
  Call: search_memory(query_text="Acropolis")
  Do NOT call recall_personal_history — that would hide shared knowledge from other users.
</anti_example>

## Time-phrase cheat sheet
| Phrase | days_ago |
|---|---|
| yesterday | 1 |
| earlier this week | 5 |
| last week | 7 |
| last month | 30 |
| last quarter | 90 |

## Returned shape
(documented JSON shape for the LLM to anticipate)

## Notes
- The tool fails loudly if ctx.user_id is missing — that is a bug, report it.
- For purely topical recall ("what's a good Greek restaurant"), prefer search_memory.
```

### Pilot note

This skill is the first to use semantic XML (`<when_to_use>`, `<example>`, `<anti_example>`) inside the markdown body. Existing skills (17 files) use plain markdown — the FRE-337 XML refactor only applied to the runtime injection layer. If the eval shows attention-improvement, follow-up work can retrofit other skills via a Wave G ticket.

---

## Section 6 — Tests

Mirrors `src/`. One pytest at a time (hook enforced).

### Unit tests

1. **`tests/personal_agent/memory/test_participated_in_edge.py`**
   - Call `MemoryService.create_conversation(turn, user_id=uid)` against a Neo4j fixture; assert edge exists and links the correct `:Person`.
   - Missing `:Person` raises (loud failure).
   - Idempotency: calling twice MERGEs once.

2. **`tests/personal_agent/tools/test_recall_personal_history.py`**
   - User A's turns returned for User A's `ctx.user_id`; User B's turns are not.
   - Time-window filter excludes turn outside `days_ago`.
   - Missing `ctx.user_id` raises `ToolExecutionError` with the "this is a bug" message.
   - Optional `topic` filter narrows by `user_message` substring (case-insensitive).
   - `days_ago` bounds (1..365), `limit` bounds (1..50).

3. **`tests/personal_agent/captains_log/test_capture_user_id_required.py`**
   - `TaskCapture(user_id=None)` raises a Pydantic validation error after Section 2a.

4. **`tests/personal_agent/memory/test_create_conversation_user_id_propagation.py`**
   - Consolidator call-site passes `user_id`.
   - Issued Cypher contains `MATCH (p:Person {user_id: $user_id})` and `MERGE (p)-[:PARTICIPATED_IN]->(t)`.
   - Mock-driver style, like `test_visibility.py:329`.

### Integration test

5. **`tests/integration/test_personal_history_e2e.py`** (marker `integration`)
   - Full stack: `/chat` as User A about "Athens" → consolidate → `recall_personal_history` via the same authenticated path returns the Athens turn.
   - Parallel User B session does not leak into User A's results.

### Backfill test

6. **`tests/scripts/test_backfill_participated_in.py`**
   - Seed Neo4j Turns lacking edges + Postgres mixed `sessions.user_id` (some NULL, some set).
   - Run script → (a) sessions with `user_id` got the right `:Person` edge, (b) NULL sessions got owner edge, (c) re-running is idempotent.

### Existing tests touched

7. **`tests/personal_agent/memory/test_visibility.py`**
   - `test_create_conversation_default_public` updated: `user_id=None` path no longer exists; assert raising or refactor to require `user_id`.
   - Other cases (~3) get a `user_id=test_uid` argument added; behavior unchanged.

### Quality gates

`make test`, `make mypy`, `make ruff-check`, `make ruff-format` — all clean. No new mypy/ruff regressions.

---

## Section 7 — Telemetry, governance, rollout

### Telemetry — three new structlog events

| Event | Where | Fields |
|---|---|---|
| `participated_in_edge_written` | `MemoryService.create_conversation` after MERGE | `trace_id`, `turn_id`, `user_id`, `was_backfilled=false` |
| `personal_history_recalled` | Tool executor return | `trace_id`, `turn_count`, `days_ago`, `topic_set: bool`, `user_id` |
| `backfill_participated_in_edges` | Backfill script, one per session | `session_id`, `user_id_source: 'session' \| 'owner_fallback'`, `edges_created`, `edges_existed` |

All carry `trace_id`. No new ES index template fields needed — existing log-level mappings cover them.

### Governance (`config/governance/tools.yaml`)

```yaml
recall_personal_history:
  risk_level: low
  allowed_modes: [NORMAL, ALERT, DEGRADED, LOCKDOWN, RECOVERY]
  requires_approval: false
  requires_sandbox: false
  rate_limit_per_hour: null
  cost_budget: null
```

Same shape as `search_memory` — low-risk read of the connected user's own data. LOCKDOWN-safe (scope is *narrower* than `search_memory`).

### Rollout

- One PR, no feature flag. Write and read halves are mutually dependent.
- Backfill: `make backfill-participated-in` once post-merge.
- Rollback: `git revert` (FRE-265 / ADR-0063 precedent).

### ADR amendment

Extend ADR-0052 with §Personal-history retrieval (2026-05-14):
- Schema decision (`(:Person)-[:PARTICIPATED_IN]->(:Turn)`).
- W3C-PROV rationale + link to `docs/research/2026-05-09-graph-identity-multi-user-patterns.md`.
- `user_id` non-optional invariant going forward.
- Link to FRE-343.

### MASTER_PLAN.md

On ship: add to Recently Completed, bump Last updated, remove FRE-343 from Immediately Actionable.

---

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Tool over-trigger: LLM uses `recall_personal_history` for general questions, hiding shared knowledge | Skill nudge explicit; `<anti_example>` in body; tool description leads with the boundary; eval after merge |
| `:Person` MATCH fails for new CF Access user before they sign in | Auth flow calls `get_or_provision_user_person` on first request → `:Person` exists before the first Turn is consolidated |
| Backfill on a corrupted DB (orphan Sessions, missing owner Person) | Loud failure for missing owner; warn-and-skip for missing user `:Person`; summary report at end |
| Test for `user_id=None` path that no longer exists | Refactor `test_visibility.py::test_create_conversation_default_public` to assert the new contract |
| Susan/Erika/Laurent's `:Person` not yet provisioned at backfill time | Their NULL sessions become orphan-attributed to owner; if they sign in later, their *new* turns get correct edges. Manual cleanup is one Cypher away. |

---

## Acceptance criteria (FRE-343 ticket)

- [x] Design decision: Option B (PARTICIPATED_IN edge) — this spec amends ADR-0052
- [ ] Implementation: provenance edge MERGE on Turn save
- [ ] New tool `recall_personal_history` for personal time-window retrieval
- [ ] Agent correctly answers "what did we talk about yesterday?" scoped to ctx.user_id
- [ ] General-knowledge queries (`search_memory`) unaffected
- [ ] Tests: personal query returns only ctx.user_id's entities; other users' turns not surfaced
- [ ] Backfill script + Makefile target
- [ ] Skill doc `personal-history-recall.md` with XML-piloted body
- [ ] ADR-0052 amendment
- [ ] `make test` + `make mypy` + `make ruff-check` clean

---

## Open questions

None blocking. (User decisions captured in Architecture decisions table above.)

---

*Next step: invoke `superpowers:writing-plans` to produce an implementation plan from this spec.*

# The session-summary gem: from broken artifact to KG opportunity

> **Status:** exploration complete, design undeveloped — the germ. A fresh session reads this and goes
> straight to deep design. **By:** cc-explore (read-only). **Date:** 2026-07-22.
> **Origin:** master's every-turn re-summarization flag (`telemetry/explore_task_session_summary_2026-07-22.md`).
> **Parked (Lane bridge §Aging):** the summary's role in **live context compaction** — separate deep-dive
> (`orchestrator/context_compressor.py`, `compression_manager.py`, `within_session_compression`,
> `compressor` role). Do that *after* this KG thread.

The session summary is **correctly-conceived, wrongly-built, and currently-unused**. The opportunity is
not "fix the bug" — it's: once corrected, place it in an **additive-only lane** and it becomes a KG
capability we don't have today. The design lever is **placement, not summary quality**.

---

## 0. Module boundaries — `insights` vs `captains_log` vs `second_brain`

Three modules are easy to conflate; they use **different substrates** and do **different jobs**. The
summarizer lives in `second_brain`. `captains_log`'s *capture* layer is its input; everything else here
is out of scope.

| Module | Raison d'être (docstring) | Reads (input) | Produces → lands in | Model | Cadence |
|---|---|---|---|---|---|
| **`captains_log`** *(capture layer)* | "self-reflection and improvement proposals" — but this layer is the **raw turn store** | live turn (`orchestrator/executor.py:2219`) | `TaskCapture` → disk jsonl `telemetry/captains_log/captures/` + ES `agent-captains-captures-*` (`capture.py:220`) | — | **every turn** (request path) |
| **`captains_log`** *(reflection layer)* | agent self-reflection + config proposals | telemetry / captures | `CaptainLogEntry` (REFLECTION / CONFIG_PROPOSAL) via `CaptainLogManager` | `captains_log` role = **claude_sonnet** | reflection/proposal events *(trigger not traced here)* |
| **`second_brain`** | "Background consolidation and **memory building**" (`__init__.py`) — the **writer into the KG** | `TaskCapture` via `read_captures` (disk/ES) | **Neo4j KG** (`memory/`): entities/relationships **+** `SessionNode.session_summary` | ER = `entity_extraction` = **gpt-5.4-mini**; summary = `captains_log` = **claude_sonnet** | **per-turn** consolidation (`brainstem/scheduler.py`) |
| **`insights`** (`InsightsEngine`) | "proactive pattern detection and recommendations" (`__init__.py`) | **aggregates**: ES `TelemetryQueries`, Postgres `api_costs` (`CostTrackerService`), Neo4j, `SysgraphRepository` (`engine.py:95-129`) | `Insight`/`CostAnomaly` → **Captain's Log proposals** (`engine.py:671`) | `insights` role = **claude_sonnet** | periodic (`analyze_patterns(days=7)`) |

**Reading it:** two independent substrates. **Raw turn text** (`TaskCapture`) fans into `second_brain`
(entities *and* the session summary, same bytes, same pass). **Telemetry/cost aggregates** fan into
`insights`. They rejoin only at *output* — both can write proposals/graph nodes, but they never share a
*read*. `captains_log` is the **source** of the capture stream (`second_brain`'s input), plus a
separate reflection-entry layer that the summarizer never touches.

- **Overlap that matters:** the session summary and the KG entities are derived from the **same
  `ordered` capture list in the same consolidation pass** (`consolidator.py:422→429`) — mini extracts
  the entities that get *used*; Sonnet re-reads the same bytes for the summary that *doesn't*.
- **No overlap:** `insights` (different substrate) and the `captains_log` **reflection** layer are out
  of scope for the summarizer discussion.

---

## A. The discovered issues (all grounded — file:line)

Pipeline: `second_brain/` is the **writer** into the Neo4j KG (`memory/service.py:1`). Per turn, one
background consolidation pass (`brainstem/scheduler.py on_request_captured` → `_should_consolidate` →
`_trigger_consolidation` → `consolidator.py consolidate_recent_captures`) runs, over the same
`TaskCapture` bytes, both entity extraction and the session summary.

**Issue 1 — Written every turn, wholesale.** `consolidator.py:429` calls `generate_session_summary`
for every session with a new turn; `session_summary.py:_build_prompt` re-reads ALL captures (cap 20
turns) from scratch; `create_session` (`service.py:1135` MERGE SET) overwrites the field each pass.
ADR-0024 decided the opposite — **"Deferred — generated lazily … not at consolidation time"**
(`ADR-0024:218`, cost flagged `:150`). Shipped code (FRE-347) inverted it.

**Issue 2 — Value-inverted model spend.** The *consumed* artifact (entities → KG) runs on **gpt-5.4-mini**
incrementally (`config/model_roles.yaml:36/84`, dedup `consolidator.py:241 turn_exists`). The
*unconsumed* summary runs on **claude_sonnet** wholesale, every turn (`captains_log` role).

**Issue 3 — The 200-char cap discards 88% of assistant text.** `session_summary.py:_format_excerpt`
clips `user_message[:200]` + `assistant_response[:200]` over ≤20 turns; no tool calls/results/thinking.
Empirical (ES `agent-captains-captures-*`, 90d, n=2,247): assistant median **734**, p75 1,566, p90
3,117, max 35,201; **68% exceed 200 chars; 88% of all assistant text is discarded.** Symmetric 200/200
is fine for the ~32% lookup/ack turns and wrong for the ~68% substantive turns — where the
**assistant** carries the semantic load (user turns are short/deictic). Pedagogic north star (ADR-0024:
*"an agent cannot challenge thinking patterns it cannot observe"*) ⇒ the digest must preserve
**outcome/trajectory**, which lives in the response. Rule: **weight what was answered, not just asked.**

**Issue 4 — The field is off BOTH retrieval paths (write-only).**
- *Not vectorized.* `SessionNode` has no embedding (`models.py:188`); summary written as plain string;
  no `generate_embedding` on it. Only vector index = `entity_embedding` (`service.py:788`),
  Entity/Claim only. Sessions in no vector index.
- *Not queried by content.* The only recall query over `:Session` is the broad/recency scan
  (`service.py:4337`): returns `session_id, dominant_entities, turn_count, started_at ORDER BY
  started_at DESC LIMIT 10` — **`session_summary` not projected.** The one reader
  (`request_gateway/context.py:133 session.get("session_summary")`) always gets `None`. Proactive path
  has a `kind="session_summary"` `Literal` but `memory/proactive.py _build_payload_for_row` only emits
  `"episode"`/`"entity"` — **dead branch.**
- *What carries session recall today = `dominant_entities`* (vectorized + projected) — the *"just a
  list of dominant entities"* the summary docstring meant to improve on. The prose layer was never
  wired.

**Consequence:** a Sonnet call every turn produces a truncated digest that nothing reads. Fixing the
producer is assumed; the real question is what a *corrected* summary is worth — answered by the lanes.

---

## B. The 6 lanes (the art of the possible)

Principle: the summary is a **coarse, session-grain, episodic-outcome** artifact. It hurts in exactly
one lane — the fine-grained fact ranker (redundant with entities, lossy-derived vector, ranking/budget
pollution; cf. ADR-0100). Keep it out of that lane → additive by construction. Each lane below is a
distinct role that is additive or noise-reducing.

**Lane 1 — Two-stage / lazy hydration** (owner's seed; touches `request_gateway/context.py`,
`memory/service.py:4337`). Base query ranks facts (entities/claims/turns); winners back-edge to their
`:Session`; *then* fetch those sessions' summaries as enrichment — never in the ranked pool. Variants:
**on-demand tool recall** (`recall_session_outcome(topic)` pulled only on a continuity need, zero
per-turn cost — new tool in `tools/`); **progressive disclosure** (return summaries as the *map* first,
zoom into turns/facts only for sessions the agent marks relevant — inverts push→pull). Zero pollution
by construction.

**Lane 2 — Gating / pre-filter** (would sit in the broad/proactive query path, `memory/service.py`).
Use the summary to decide *which* sessions are worth expensive turn-level traversal. Only ever
*removes* candidates → improves precision. Anti-pollution.

**Lane 3 — Continuity / anti-re-litigation** (north-star native; new logic near context assembly).
Match the incoming query against recent session summaries; on collision surface "last session we
concluded X — revisit or build on it?" Cheap; **impossible today** (no unit spans a session's outcome).
Directly serves *don't re-open settled ground / observe question-chaining*.

**Lane 4 — Cross-session synthesis** (net-new capability; likely a new module consuming Session
summaries). Cluster summaries → reconstruct a multi-session **thread/arc**; feed a longitudinal
**learning model** (topics over time, open threads, recurring confusions). Summary → learning map,
never → fact retrieval. Most north-star-aligned; nothing today spans sessions.

**Lane 5 — Verification oracle** (post-assembly check; could live in `observability/joinability` or a
new check). Cross-check retrieved facts against their source session's summary → drift/extraction-error
flag; ground "last time we decided X" claims instead of hallucinating continuity. Quality signal, not
a candidate.

**Lane 6 — UX / owner-facing** (PWA `seshat-pwa` + a gateway read endpoint). Session-browser label +
auto-titling. **Cannot hurt recall** (not in the recall path); ships value *before* any retrieval
question is settled; independently justifies keeping the summary well-populated. The safe floor.

**Aging bridge (holds the parked compaction thread):** the summary as what *survives* aging — recent
sessions recalled by raw turns; old sessions, after turns are evicted/aged, recalled *only* by their
summary. Graceful decay: detail for recent, gist for old. Where the KG and compaction threads meet.

**Strongest only-helps + new-functionality picks:** Lane 3, Lane 1 (progressive disclosure), Lane 4,
Lane 6 (floor). Lanes 3/4 and the aging bridge are capabilities the KG lacks entirely, not "better
recall of the same thing."

---

## C. Before building — settle first

1. **Diagnostic (measure-don't-assert).** Pull real recall misses. Any *session-outcome* misses the
   entity/turn grains structurally couldn't catch? Yes → build. Misses are "facts about X" →
   entity/claim quality is the lever, not summaries. No session-grain misses → don't build.
2. **Pick lane(s).** Lane 6 = safe floor; Lanes 1/3/4 = capability plays.
3. **Corrected producer** (assumed): trigger (lazy/idle/end, per ADR-0024), 200-char cap
   (archetype-aware; ~1,500 for a median reply, ~3,800 for p90), and — if any retrieval lane — **embed
   and/or project it** (today neither).
4. **Embed summary vs richer source?** Summary is a lossy 2nd-order derivative; embedding Turn/Claim
   (closer to source) may capture the value without a lossy session vector. Don't assume the summary is
   the unit.

## D. Pointers (fast re-entry)

- Producer: `second_brain/session_summary.py`; `second_brain/consolidator.py:386` (`_consolidate_sessions`),
  `:429` (summary call); trigger `brainstem/scheduler.py`.
- KG store/retrieval: `memory/service.py` — write `:1135`; broad recall `:4337`; vector index
  `entity_embedding` `:788`; SessionNode model `memory/models.py:188`.
- Broken consumer: `request_gateway/context.py:133`. Proactive dead-branch: `memory/proactive.py`
  `_build_payload_for_row`.
- Intent: `docs/architecture_decisions/ADR-0024-session-graph-model.md` (`:62,:120,:150,:154,:218`).
- Origin tickets: FRE-347 / FRE-346 (G1).
- Compaction (parked): `orchestrator/context_compressor.py`, `compression_manager.py`,
  `within_session_compression`, `compressor` role.

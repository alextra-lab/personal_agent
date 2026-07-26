# Deep-dive queue — harness self-analysis

*Opened 2026-07-26 during the objectives/functionality/requirements session. Running list — entries
get added as we find them. Everything measured here was checked against live cloud-sim prod or the
source, not recalled.*

**Entry shape:** what it is · why it's flagged · what we established · what the dive must answer.

**Not all entries are defects.** Entry 1 is a *decision already taken*. Entry 4 is an *asset* — the
substrate the plan depends on. Read the tag on each heading.

| # | Entry | Kind |
|---|-------|------|
| 1 | Reflection recall — cross-session context injection | **decided: disable, then remove** |
| 2 | KG quality / `quality_monitor` | defect — *THE* dive |
| 3 | Homeostasis / brainstem | unclear — three unlike things in one pillar |
| 4 | The capture substrate | **asset — core of the idea** |

---

## 1. Reflection recall — cross-session injection into conversation context

> **DECIDED 2026-07-26 (owner): disable and remove.** Not a dive — an execution item.
> The material below is the evidence base for the removal ticket and the ADR-0067 supersession.
>
> **Two separate acts.**
> 1. **Disable (do before restart):** `AGENT_REFLECTION_RECALL_ENABLED=false` in `.env`. Config only,
>    no PR. The harness is stopped; setting this now is what prevents it resuming.
> 2. **Remove (PR):** module `captains_log/recall.py` (273 lines) · call site
>    `request_gateway/context.py:309–325` · 4 settings fields (`settings.py` 2294/2304/2313/2324) ·
>    `tests/personal_agent/captains_log/test_recall.py` · stale comment ref
>    `scripts/study/baseline_harness.py:63`. **No other consumers — verified.** Nothing downstream breaks.
> 3. **ADR-0067 is Accepted — it must be superseded, not silently deleted.**
>
> **Provenance vs delivery — the reason this is worth a written record.** ADR-0067 (Accepted
> 2026-05-09, deciders: project owner; impl FRE-348, driver FRE-346) authorised something narrower:
> let the agent re-read *its own analytical observations* for three continuity use cases — resumable
> refactor state, abstract-idea recovery, evolving-hypothesis tracking. Its own framing: *"Memory
> captures entities, not reasoning; session_summary captures narrative gist, not the agent's own
> analytical observations."* What shipped injects the **most-repeated operational complaints**,
> cross-session, proper-noun-matched, truncated to fragments, as `role: system`, including rejected
> and already-implemented items. Approved intent and delivered behaviour diverged with nothing
> checking in between. **That divergence is the reusable lesson — carry it into the requirements work.**

**What** ADR-0067 / FRE-348 · `captains_log/recall.py` → `request_gateway/context.py:309–325`

**Why flagged** It injects the harness's *technical* self-reflections into *any* conversation,
drawn from *every* session, as a `role: system` message. Content is about how turns executed —
proposed subsystem changes, failure fixes, missing skills — not about what the conversation is about.

**⚠ Restart-relevant.** `reflection_recall_enabled` defaults `True` with no `.env` override. This
resumes the moment the gateway comes back.

**What we established**

- No session scoping. `session_id` is passed in but its docstring says *"for log correlation"* — it
  never reaches the query. `_build_query()` takes only `entity_hints`, `recency_days`, `min_seen_count`.
- Scope: **all sessions, last 14 days**, index `agent-captains-reflections-*`.
- Selection: capitalized-entity overlap between your message and `rationale` /
  `proposed_change.what` / `failure_path.fix_what`. No proper nouns in your message → skipped entirely.
- Ranking: **`seen_count` descending**, then recency. Most-repeated complaint wins over most relevant.
- Cap 3. Each rendered as one bullet, hard-truncated: rationale 120 chars, proposed.what 80,
  fix_what 80. `title` is dropped — the one field that might carry subject matter.
- Only `status == "approved"` is excluded. The enum is
  `awaiting_approval · approved · rejected · implemented` — so **rejected and implemented
  reflections stay eligible for 14 days.** Approving is currently the only way to stop one recurring.
- Delivered under a header saying *"not directives… use these only as context."*

**Mismatch at the root** `recall.py`'s own docstring says it exists for *subject* continuity —
"resumable refactor state, abstract idea recovery, evolving hypothesis." But the reflection generator
(`generate_reflection_entry` → DSPy `GenerateReflection`) only emits operational output. Consumer and
generator were built for different payloads and wired together because both said "reflection."

**Questions the removal closes — and the one it does not**

Closed by the decision: whether the surface should exist · scoping · `role: system` as the channel ·
why rejected proposals kept recurring.

**Still open, and now homeless:** ADR-0067 named three real gaps — resumable refactor state,
abstract-idea recovery, evolving-hypothesis tracking — and argued that neither entity memory nor
session digests cover them. Removing this surface does not make those gaps untrue. Whether they still
matter, and whether ADR-0124's digests are the right owner, is a live question that should not
disappear with the code.

---

## 2. Knowledge-graph quality — `quality_monitor` *(THE deep dive)*

**What** ADR-0060 (**Superseded 2026-07-02**) · `second_brain/quality_monitor.py` (643 lines, FRE-23)

**Why flagged** This is where the original blow-up happened, and the root diagnosis was never acted on.

**What we established**

- `ConsolidationQualityMonitor` with `check_entity_extraction_quality()` and `check_graph_health()`;
  dataclasses `QualityReport`, `GraphHealthReport`, `Anomaly`, `GraphQualityAnomaly`,
  `GraphStalenessReviewSummary`.
- Per the 2026-06-26 design brief: ~8 health conditions detected daily, each breach auto-promoted to
  a Linear ticket. Produced duplicate low-value tickets that recurred however they were dispositioned
  (FRE-423/424/425/428/429/430, FRE-446).
- The brief's diagnosis — the **category error** — is the important part and remains unfixed:
  a **condition** is a level (continuously true, no natural count); a **proposal** is a discrete idea
  (decided once). Forcing both through one promotion mechanism makes duplicates *the correct output of
  the wrong model*. You cannot dedup your way out of a type mismatch.
- ADR-0105 fixed convergence, dedup, observability and loop-closure. It did **not** touch this.
- Unresolved contradiction: the brief says `promotion.py` is *"the broken layer — replace"*;
  ADR-0105 D1 says *"build on `promotion.py`; do not rebuild it."*

**The dive must answer**

- Conditions and proposals — one pipeline or two?
- What is the KG-quality signal actually *for*, and who consumes it?
- Does ADR-0060 being superseded mean this was absorbed, retired, or orphaned?
- The brief's reframe — *detection is a trigger, the deliverable is an investigated proposal* —
  adopt, or reject on cost grounds?

---

## 3. Homeostasis / brainstem

**What** ADR-0055 (status reads **"Proposed — In Review"**, 2026-04-24, FRE-246) ·
`brainstem/` — `sensors/`, `mode_manager.py`, `consumers/mode_controller.py`, `optimizer.py`

**Why flagged** Three unlike things in one pillar, one of them dead, and the ADR status is stale.

**What we established**

- The code exists and is exported via `brainstem/__init__.py` with a `get_mode_manager()` singleton.
  *(Not confirmed: whether it's instantiated at service startup.)* ADR status is documentation drift —
  same class FRE-582 caught on ADRs 0090/0091/0053.
- **`sensors/`** — genuine collection (metrics daemon, request monitor, platform sensors).
- **`mode_manager` + `mode_controller`** — a state machine that changes agent behaviour.
  `evaluate_transitions(sensor_data)` reads rules from governance config and flips mode.
- **`optimizer.py` is dormant by its own docstring:** *"not wired into any runtime loop, retained for
  the local-inference deployment path."* Host CPU/memory gates are disabled under remote inference
  (FRE-326).
- Worth noting what the optimizer *was*: *"analyzes telemetry patterns, detects false positives, and
  generates data-backed proposals for the scheduler's thresholds, with shadow A/B evaluation."*
  That is close to "tell me which parameters to modify" — built, then stranded by a deployment change.

**The dive must answer**

- **What does a mode change actually gate?** Claimed once in conversation and withdrawn as unverified —
  `governance/` contains only `models.py`, no policy module. Trace it.
- Is the mode state machine running in production right now?
- Is the dormant optimizer worth reviving under remote inference, or deleting?
- Should collection, control, and proposal-generation be three pillars rather than one?

---

## 4. The capture substrate — the reconstructable record *(ASSET, not a defect — core of the idea)*

**What** `captains_log/capture.py` → Elasticsearch `agent-captains-captures-*` · Postgres
`sessions.messages` · disk `telemetry/captains_log/captures/`

**Why flagged** Everything else in this queue is something wrong. This is something *right*, and it is
the foundation the owner's idea rests on. **Capture is LLM-free and upstream of every consumer** —
`capture.py` writes during request processing with no model call. Insights, reflection, context
quality and `quality_monitor` are all downstream of it. So the whole gas factory can be switched off
without losing the raw material, and anything built later can be **developed and back-tested against
real history** rather than waiting to accumulate new data.

**What we established (measured 2026-07-26 against cloud-sim prod)**

*ES `agent-captains-captures-*` — 8,880 docs — the real record.* Per turn:
`user_message` · `assistant_response` · `steps[]` (structured, e.g. `{type: llm_call, description}`) ·
`tool_results[]` (`{tool_name, success, output}` — **failures captured via the `success` flag**) ·
`tools_used[]` · `input/output/total_tokens` · `duration_ms` · `outcome` · `timestamp` · `trace_id` ·
`session_id` · `user_id` · `memory_context_used` · `memory_conversations_found` · `eval_mode`.
Sibling index `agent-captains-captures-subagents` (195 docs).

*Postgres `sessions.messages` (JSONB)* — 1,249 sessions, 1,246 with messages, **4,796 messages**.
Roles `user`/`assistant` only; each carries role, content, `metadata.source`, `trace_id`, timestamp.
**No tools, no steps, no results.** Conversation text only.

*Postgres `captains_log_captures`* — **0 rows.** Defined in `init.sql`, nothing writes to it, and its
DDL has no `assistant_response` column — it was never a viable reconstruction path. Dead schema.

*Disk `telemetry/captains_log/captures/`* — 6.5 MB of `CL-*.json`. Partial.

**Coverage — verified, not assumed.** July: Postgres 38 sessions / 249 messages vs ES **40 distinct
sessions / 165 capture docs**. ES covers *more* sessions than Postgres holds, so capture is not
dropping turns. The monthly decline — Apr 1,402 · May 1,064 · Jun 225 · Jul 165 — is **usage, not
breakage**.

**Retention.** `session_retention_days = 180`; **`purged = 0`** — nothing aged out yet; oldest session
2026-04-15. Capture indices reach back to April with no observed ILM deletion.

**Two risks**

1. **ES is a single point of failure.** Postgres holds the conversation but none of the operational
   detail. An ILM policy that ever deletes the capture indices destroys reconstructability. This needs
   a deliberate retention decision, not an accident.
2. **`agent-logs-*` already has a hole** — indices run to `2026.05.10`, resume `2026.06.27`. ~6 weeks
   missing. Captures are unaffected, but historical *telemetry* reasoning has a gap.

**The dive must answer**

- **What is the deliberate retention policy for captures?** Currently accidental. This is the decision
  that determines whether the idea has a substrate in two years.
- Is a capture genuinely sufficient to reconstruct a turn, or is something missing — notably the
  **assembled context / system prompt**? `capture.py` defines a second model carrying
  `system_prompt_chars`, `skill_index_block_chars`, `context_message_count`, `context_chars`,
  `context_messages[]`, `memory_in_context`, `mode`, `model_role`. Where does that land, and is it
  joined to the turn capture?
- Delete the dead `captains_log_captures` table, or was it meant to be the durable copy?
- Should the reconstructable record be promoted to a **first-class, named substrate** with its own
  contract, rather than a by-product of a subsystem being switched off?
- What is the actual on-disk/index cost of retaining it indefinitely?

---

## Not yet dived, noted in passing

- **ADR-0055 / 0060 status drift** is a symptom of a wider pattern — ADR statuses not tracking code.
  FRE-582 reconciled 0090/0091/0053 once; nothing keeps it true.
- **Context quality Phase 2** (`context_quality_governance_enabled`, default `False`, no override) —
  built, wired, never switched on. Its setting says *"flip after 14 days of Phase 1 telemetry validates
  signal quality."* Phase 1 shipped 2026-04-27. The validation never happened. Not a dive on its own
  yet, but it is a decision someone owes an answer to.

# Brainstorm brief — the session summarizer, redesign

*Assembled 2026-07-26 by cc-master at the owner's request, for a dedicated brainstorming session.
Everything measured here was checked against live cloud-sim prod or source on 2026-07-26. Where a
number was measured wrong earlier in the day, the correction is stated rather than the number quietly
replaced.*

**This is not an ADR and decides nothing.** It exists so the session starts from evidence instead of
rebuilding it. Read §6 before re-deriving anything — several plausible-looking measurements are traps.

---

## 1. Why the session is being held

The owner's position, 2026-07-26:

> The entire session summarizer process needs to be redone. It is unrealistic to think we should
> continuously read all turns of a session to maintain a summary. We probably need chunk summaries —
> or no summaries and use the knowledge graph. The objective's simplicity masks the complexity of an
> implementation.

That objection is structural, not a defect report. Today the producer regenerates **wholesale** from
all captures on every attempt, so a growing session costs its entire history on every regeneration and
a session that never ends has monotonically growing input forever.

---

## 2. What the backing ADR actually specified

**ADR-0124 — Session-summary producer correction and phased consumption.** Accepted 2026-07-23,
amended twice: **Amendment A** (2026-07-23, conversation-scoped input, tool payloads out of D2) and
**Amendment B** (2026-07-24, conversation-*only*; `tool_evidence` basis and `status_contradiction`
correction moved to a future verification oracle). Chain FRE-947 → 948 → 949 → 950 → 951; Phase 4
unfiled.

**The digest was never designed as a browsable artifact.** It is a context-management precursor:

| Phase | What it is | Status |
|---|---|---|
| 0 | The producer | shipped (FRE-947) |
| 1 | Human-readable digest in the session browser | shipped |
| **2a** | **Offline replay analysis** over historical sessions — digests per query, staleness incidence, duplication rate, annotation ratio | **FRE-949, never run** |
| **2** | **Fact-first hydration.** Ranking unchanged; ranked winners back-edge to their `Session`; digests attach *afterwards as annotation*. Touches `request_gateway/context.py` and `budget.py` trim order | FRE-950, parked |
| 3 | Anti-re-litigation. Digest supplies the *content* of the nudge, never the detection | FRE-951, parked |

Two consequences the session should hold onto:

- **"Digest vs knowledge graph" is not the real fork.** Phase 2 is already *fact-first* — the graph
  ranks and wins, the digest attaches as annotation. The sharper question is whether ranked facts need
  a narrative annotation layer at all.
- **The 250-token bound is a context-budget number, not a summary-quality number.** It derives from D4
  (*annotation may never exceed the tokens of the facts it annotates*) sized against a 448-token p50
  assembled context, where five digests ≈ **74% of everything the model reads**. D3 states plainly it
  is provisional, *"to be set empirically by a compression curve"* — which was never produced.

---

## 3. What was measured today

### Delivery

| | |
|---|---|
| Session nodes | **127** |
| Digest written | **6** (turn counts 2, 2, 2, 2, 3, 8 — one is *"Casual morning greeting exchange"*) |
| Correctly skipped, single-turn | 66 |
| **Marked handled, no digest, 2–17 turns** | **46** |
| Stuck failing | 9 (avg 5.8 turns) |

The system succeeded on trivial sessions and failed on substantial ones. Of 61 qualifying sessions,
**6 got a digest — 10%**.

### Cost

446 billed calls over 14 days, **$14.19** — about **$2.37 per delivered digest**. 90% of the spend
($12.79, 289 calls) came from the 9 stuck sessions.

### Why generation failed

Output-token distribution across the 446 calls:

| Output tokens | Calls |
|---|---|
| 0–500 | 147 |
| 500–1,000 | 31 |
| 1,000–1,500 | 7 |
| 1,500–2,000 | 7 |
| **2,000–2,500** | **254 (57%)** |

`_MAX_OUTPUT_TOKENS = 2048`. The mode is the ceiling — the signature of running out of room and being
cut off mid-structure, which yields unparseable output. Mean output was **1,338 tokens against a
250-token bound**.

Raising the ceiling does not fix it: with more room the structure completes and then fails the
250-token check instead. Both failure reasons are one fact — **the model does not produce a 250-token
digest when asked**, and nobody has established that it can.

### Why it never converged

The success path and the skip path both set `summary_generated_at`, which makes a session permanently
clean. **The failure path sets only the reason and the counter, never the timestamp** — so a failed
session is dirty again the instant the sweep ends. Idleness is a *qualification*, not a cooldown: it
becomes more true with silence. All three gates stay open forever, evaluated every 300s.

### The input defect (fixed, PR #680, merged `fc4553c9`)

The producer read a non-durable local directory (7 files on the live host) while the durable copy sat
in Elasticsearch. The 46 write-offs were readable the whole time. **The 46 remain retired** — recovery
is a deliberate act costing ~46 model calls, and was left to the owner.

### The mechanism defect (open — FRE-995 / FRE-996)

`tools=None`, no `response_format`. The digest schema is asked for **in English prose** and parsed
afterwards, even though `DigestItem`, `Correction`, `SessionDigest` already exist as Pydantic models
and `LiteLLMClient.respond()` already accepts `tools` and `response_format`. Across the whole source
tree, exactly one call site passes any structured-output directive, and only the weak
`{"type": "json_object"}` form. **Entity extraction — the KG write path — follows the same pattern.**

---

## 4. Candidate directions

**A. Chunk summaries.** Summarise ranges of turns and compose. Bounds input per call, but compounds
error — a wrong reading at turn 40 is inherited permanently with no repair point. The standard
resolution is a **hybrid**: cheap incremental deltas plus periodic full rebuild triggered by
accumulated change, not a clock (keyframes, log compaction). The explore note reaches this fork
independently.

**B. No summaries — the knowledge graph carries it.** The graph already accumulates entities and facts
per turn. If the digest's job is recording what a session established, the graph may already answer
it. **The only direction that removes the subsystem rather than repairing it**, and therefore the one
that should be argued hardest before it is dismissed.

**C. Keep the artifact, fix the mechanism.** Structured-output contract (FRE-996), bounded schema
(item counts and character limits, which a model *can* obey, unlike a token budget it cannot perceive),
truncation as its own observable failure, no re-issue of identical deterministic calls.

These are not exclusive. C is design-independent and survives A or B.

---

## 5. Questions the session owes an answer to

1. **What is the unit?** Sessions never end (`SESSION_CLOSED` defined, never emitted). Is the unit a
   session, a thread, an idle window, a chunk? ADR-0124 records a **High**-rated cross-session
   supersession risk: a thread left open in session A and settled in session C is never revisited,
   because regeneration triggers only on A's own new turns — so open threads accumulate as permanent
   false-open state, and the Phase-3 consumer eventually asserts "we never settled X" about something
   settled weeks ago.
2. **Who consumes it, and does the consumer justify the producer?** Two consumer-less producers have
   now been paid for. Phase 2a (FRE-949) is the offline measurement that answers this, costs no live
   spend, and has never been run.
3. **Incremental or wholesale — and if hybrid, what triggers rebuild?**
4. **Is annotation-on-facts worth its context share** at a 448-token p50, or does it dwarf what it
   annotates?
5. **What is the retention policy for captures?** Currently accidental. It decides whether any of this
   has a substrate in two years.
6. **Do ADR-0067's three continuity gaps** — resumable refactor state, abstract-idea recovery,
   evolving-hypothesis tracking — still matter, and are digests their owner? Removing reflection recall
   did not make them untrue.

---

## 6. Measurement traps — do not re-derive these wrongly

Master hit three of these today. They are recorded so the session doesn't.

- **`summary_failure_reason` is last-write-wins.** It is the *most recent* failure, not the historical
  cause. A session showing `schema_invalid` at 390 attempts did not fail that way throughout — most of
  those accumulated under `budget_denied`, and the reason was overwritten once a budget reset let calls
  reach the model again. Reading it as a failure distribution is wrong.
- **`summary_attempt_count` counts sweeps, not model calls.** The producer allows one retry per sweep,
  so real call volume is up to **2×** any figure derived from it.
- **The counter and the cost ledger disagree by design.** 959 recorded failed sweeps vs 446 billed
  calls; the ~513 difference is attempts rejected at the budget gate before reaching the provider —
  free, but still counted.
- **`_cat/indices docs.count` inflates via nested documents.** The capture corpus is **2,856**, not the
  8,880 quoted in the 2026-07-26 explore note — that figure came from `_cat` while `_count` on the same
  index returns a third of it (1,412 vs 433 on `2026-05-11`). Session-coverage claims in that note are
  unaffected and were verified exact.
- **The Cypher exclusion predicate is correct.** Tested directly against the live graph; it excludes
  properly. The defect was never the query. Do not re-litigate it.
- **The reason-based terminality split is correct design**, per the owner: a budget denial genuinely is
  transient. The defect is that the transient path has **no bound of any kind** — no backoff, no
  ceiling, no awareness of when the condition could clear.

---

## 7. Environment state going in

Session summary, insights, insights wiring, feedback polling and reflection recall are all disabled in
`.env`; reflection is throttled to a 1000-day interval (no real off-switch exists — FRE-990). All 127
sessions were marked non-eligible in Neo4j on 2026-07-26, so a restart cannot re-arm the loop even on
unchanged code. Forensic counters were deliberately preserved. The gateway is stopped. **$0 background
spend since the halt.**

Nothing in this brief requires the harness to run.

---

## 8. Have these in the room

| ADR | Why |
|---|---|
| **0124** + Amendments A, B | the summarizer, and its two scope contractions |
| **0092** | compaction — the other half of context management; its frozen-reset action never fires on gateway turns |
| **0100** | relevance-bounded recall — Phase 2 annotates *ranked winners*, so recall decides what gets annotated |
| **0098 / 0097** | memory substrate and taxonomy — what direction B would use |
| **0067** | superseded-pending; claimed three continuity gaps digests are the candidate owner of |
| **0105** | self-improvement pipeline — the condition-vs-proposal category error is the same shape as digest-vs-fact |

Companion reading: `2026-07-26-session-analyzer-pillar.md` and
`2026-07-26-harness-self-analysis-deep-dive-queue.md` (PR #679) — the independent-ground constraint in
particular applies here unchanged.

---

## 9. Master's opinion, offered as input not conclusion

**Run Phase 2a (FRE-949) before choosing.** It is the measurement ADR-0124 already specified as the
gate on Phase 2, it is offline, it costs no live spend, and it answers question 2 — *does annotation
earn its place* — which is upstream of every other fork. Choosing between chunking and the graph
without it is choosing an implementation for a consumer nobody has measured.

**Do the design-independent work regardless** (direction C, FRE-995/996). A schema contract, a bounded
schema, and truncation as an observable signal hold under every candidate design, including the one
where the digest ceases to exist.

**Hold the compression curve (FRE-994).** It sizes an artifact whose existence is in question, and it
would currently draw its curve over a corpus where 57% of generations truncate — a curve of the defect,
not of compression.

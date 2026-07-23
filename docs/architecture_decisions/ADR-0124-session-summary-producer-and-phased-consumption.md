# ADR-0124: Session-summary producer correction and phased consumption

**Status:** Proposed
**Date:** 2026-07-23
**Deciders:** Owner (architect), cc-adrs (Opus)
**Tags:** memory, second-brain, knowledge-graph, retrieval, telemetry, privacy

---

## Context

**What is the issue we're addressing?**

`SessionNode.session_summary` was introduced by ADR-0024 as the prose layer above
`dominant_entities` — the thing that could answer "what was that session about?" without
traversing every turn. The producer shipped (FRE-347), and then nothing consumed it. It has been
running ever since: writing every turn, on a frontier model, from mutilated input, into a field no
query projects.

Every claim below was verified against source or measured on the live system during this ADR's
discussion. They are stated here so implementation does not re-derive them.

**How it runs today**

- `second_brain/consolidator.py:429` calls `generate_session_summary` for every session that
  received a new turn. In the current deployment `brainstem/scheduler.py:300` returns `True`
  unconditionally (`resource_gating_enabled=false`), so consolidation fires on every captured
  request. The summary is regenerated wholesale each time.
- ADR-0024 resolved the opposite: *"`session_summary`: Deferred — generated lazily in future, not
  at consolidation time"* (ADR-0024 § Open question resolution). Shipped code inverted the
  decision.
- `session_summary.py:131` resolves its model via `resolve_role_model_key("captains_log")` — it
  borrows another subsystem's role and has no knob of its own. Live model is `claude-sonnet-5`.
- Input is clipped to 200 characters of user text and 200 of assistant text (`_USER_EXCERPT_CHARS`
  / `_ASSISTANT_EXCERPT_CHARS`), capped at 20 turns, and includes **no tool calls or tool results**
  — even though `TaskCapture` carries `tools_used`, `tool_results`, `steps` and `outcome` in full.
- `memory/service.py:1140` sets `s.session_summary = $session_summary` **unconditionally** on every
  session MERGE. `generate_session_summary` returns `None` on budget denial, timeout or model
  error, and the consolidator passes that through — so **a transient failure erases the previous
  good summary.** No failures have occurred in the measured window, but `BudgetDenied` is a
  reachable path. This is a live bug independent of everything else in this ADR.

**Measured behaviour (30–90 day windows, live system)**

| Measurement | Value |
|---|---|
| Summary generations | 159 in 30 days (~5/day), 100% success, 0 budget denials |
| Output length | 308–668 chars (target was 200–500) |
| Sessions in graph | 121 — turn count min 1, **median 1**, p90 7, p99 12, **max 17** |
| Single-turn vs multi-turn | 62 single-turn, 59 multi-turn |
| `Turn` nodes | 2,276, **100%** carrying their own summary and `key_entities` |
| `(Session)-[:DISCUSSES]->(Entity)` | 3,328 edges across 112 of 120 sessions |
| User message size | p50 58 chars, p90 189 — **already below the 200-char clip** |
| Assistant response size | p50 1,847 chars, p90 4,014, max 6,998 — the clip discards ~89% |
| Tool results, per result | p50 523 B, p90 4,613 B, max 153,334 B; 90% fit in 5,000 B |
| Tool results, per session | p50 2 B, p90 4,563 B, max 240,184 B |
| Total input per session | p50 27 B, p90 8,116 B, **max 266,539 B (~67k tokens)** |
| Turns carrying tool results | 63% |
| Assembled context occupancy | 1,283 budget evaluations in 90 days, **`trimmed=false` on all 1,283**; total_tokens p50 448, p90 1,683, p99 4,497, **max 5,949** against a 120,000 ceiling |

The 20-turn cap has never been reached. The context-budget trim has never fired. The clip is
symmetric and wrong in an asymmetric way: it barely touches user messages while discarding the
assistant text where a session's outcome lives.

**Two claims from the backing research that do not hold**

- *"A sessions read surface already exists, so the UI lane is a field addition."* `gateway/session_api.py:100`
  `list_sessions` reads **Postgres only** (`SessionRepository.list_recent`). The `sessions` table
  has no summary column (`service/models.py:204-232`) and `title` is synthesised as the first 60
  characters of the first user message (`:799`). The summary lives in Neo4j. Phase 1 is a
  cross-substrate read, not a projection.
- *"It spends a cloud model every turn"* is structurally true but materially small: ~5 calls/day at
  ~1k prompt tokens is cents per month. **Phase 0's justification is correctness and unblocking, not
  cost.** Stating otherwise invites a reader to falsify the ADR in one query.

**Constraints taken as given**

- Prompt caching does not bite. Breakpoints sit on the system message, the last tool definition, and
  the last frozen message before the current user turn; recall content is appended in the turn's
  volatile tail, after the final breakpoint. Nothing in this ADR mutates the cached prefix.
- The absent `session_summary` model role is a known constraint, deliberately deferred until it
  converges with structured conversation context. Not addressed here.
- Do not evaluate summariser quality on the current producer's output. Comparing anything against
  200-char-clipped, tool-blind input measures nothing, because the input is mutilated before the
  comparison starts.

**What needs to be decided** — the owner's framing: what the producer is allowed to read, what it is
allowed to emit, and when it runs. Plus how consumption is sequenced, and what would falsify the
sequencing.

---

## Decision

Correct the producer along three axes — trigger, input, output — and wire it to consumption in
phases, with the expensive and risky lanes behind an explicit gate.

The single sentence that governs the whole design:

> **The digest encodes the epistemic state left behind by an episode. It does not retell the
> episode.**

### D1 — When it runs: debounced idle, with derived freshness

The summary is a **derived read model**, not a live artifact. The question is not "when does a
session end" — sessions are Postgres rows with `last_active_at` that resume indefinitely, and
session-end is not observable. The question is "when is the projection stale."

- A periodic sweep regenerates a session's digest once it has been quiet past an idle threshold
  (configurable, starting 10–15 minutes).
- **Freshness is derived, not flagged.** `SessionNode.ended_at` already carries the last turn's
  timestamp and is rewritten every consolidation pass. Add one field, `summary_generated_at`. Dirty
  is `summary_generated_at IS NULL OR summary_generated_at < ended_at`; idle is
  `ended_at < now − threshold`. No `summary_dirty` column, no revision counter, **no Postgres
  migration** — the lifecycle state lives in the same substrate as the artifact it describes.
- **Concurrency** reuses the consolidator's existing single-flight guard, plus a re-read of
  `ended_at` immediately before write (write only if unchanged). Not cross-database locking: a
  Postgres row lock cannot serialise a Neo4j mutation.
- **Resumption regenerates wholesale** from canonical captures. Never incremental patching of the
  prior summary.
- **The summary is decoupled from the per-turn session write.** `create_session` must stop owning
  `session_summary`, or the next turn after a sweep will NULL the fresh digest. This also fixes the
  live clobber bug.

### D2 — What it reads: everything, with one privacy conditional

- Full user text and full assistant text, all turns. The 200-char clip and the 20-turn cap are
  removed outright.
- **Full tool results** — name, arguments, status, error and payload. No per-result cap, no
  summariser-specific input ceiling.
- **Profile-aware egress.** Sessions carry an `execution_profile` of `local` or `cloud`
  (`service/models.py:228`). For a cloud-profile session the tool bytes already reached a cloud
  provider when the primary model called the tool, so the summariser adds no exposure. For a
  **local-profile session those bytes have never left the machine**, and shipping them to a cloud
  summariser in a background job the user never sees is a new egress path. Local-profile sessions
  therefore contribute tool **name, arguments, status and error only — never payload.**
- **When payload is withheld, the contract must say so** — *"tool payloads were unavailable for this
  session; do not infer contradiction from their absence"* — or the summariser commits the
  fabrication error described in D3 against its own missing evidence.
- **Oversized input fails visibly.** Estimate tokens before dispatch; if the model's real limit
  would be exceeded, raise and record the reason. Never silently truncate. The check is
  pre-dispatch, so a doomed session costs an estimate and a log line, not a model call. Log on
  transition rather than every sweep tick.
- **Minimum-turns floor of 2.** Single-turn sessions produce no digest. Every `Turn` already carries
  a populated summary and key entities; a one-turn session digest is not merely redundant, it is a
  **diverging artifact describing the same event**, free to contradict the record it duplicates. At
  two turns, genuine session-level relation appears that neither turn expresses alone ("A was
  rejected after X was discovered; B was chosen"). This removes 51% of generations.

### D3 — What it emits: two artifacts, four optional slots, verifiable provenance

The producer emits **two artifacts in one call, stored independently**:

- **`session_label`** — ≤90 characters. A distinguishing noun phrase, not a compressed digest. No
  outcome claims unless the outcome is identifying. Replaces the first-60-characters title hack.
- **`session_digest`** — a structured record with four **optional** slots, shape determined by
  content rather than by a session-type classifier:

| Slot | Contains | Notes |
|---|---|---|
| `established` | Facts and observations that survived the interaction | Filtered hardest — the slot most at risk of re-deriving the entity/claim layer |
| `decisions` | Conclusions that materially constrain future reasoning, including rejected alternatives *and their reasons* | Where anti-re-litigation gets its signal |
| `unresolved` | Unfinished state a future session could wrongly treat as settled | Timestamped (see Risks) |
| `corrections` | High-confidence contradictions between evidence and narration | **Usually empty. That scarcity is a feature and a monitoring signal.** |

All slots may be empty; empty slots are omitted on render. There is deliberately **no
`intent → trajectory → outcome` schema** — that describes tasks, and this corpus is substantially
conversational and topic-drifting. A 17-turn session that ran diet principles → a salad → a
ratatouille → coaching a couscous has no single intent, and is normal rather than pathological.

**Provenance is structural and verifiable.** Each item carries a `basis` tag
(`tool_evidence | user_statement | assistant_reasoning | mixed`). Because `basis` is a
model-assigned label and nothing stops a model tagging its own inference as evidence, it is backed
by an enforcement step: **every item tagged `tool_evidence`, and every `corrections` entry, must
carry a verbatim span locatable in the canonical capture.** A validator rejects a digest whose
claimed evidence string is not found. This turns the evidence-versus-interpretation invariant from
a prompt instruction into a machine-checkable one.

**Error-flagging is precision-first, deliberately asymmetric.** A missed error is recoverable from
raw evidence; a false error writes self-confirming state into the graph and feeds its own
supposed correction into future reasoning. A digest may assert the agent was wrong only on
(a) direct contradiction of the *same proposition* by authoritative evidence, or (b) an explicit,
evidenced self-correction within the session. Weak conflict, partial or failed tool calls, multiple
readings, state that changed over time, and disagreement with a subjective judgment are **not**
errors — they belong in `unresolved` or are omitted. **Never infer error from absent evidence.**

**Compute state, generate meaning.** Turn count, duration, tool invocation and success/failure
counts, and `dominant_entities` are queryable and remain structured properties. They are never
regenerated in prose, so they cannot be hallucinated and cost no tokens.

**Length is bounded by marginal utility, not characters.** Include an item only if its expected
future value exceeds the cost of displacing retrieved evidence. Starting budget ~180 tokens target
/ 250 hard maximum, to be set empirically by a compression curve. Digest length is **not**
proportional to turn count.

**Storage is structured; rendering is derived.** The structured record is canonical. Consumers
receive dense labelled prose assembled at read time (no stored rendered field, no second staleness
surface). If the digest is ever embedded, the embedding text is another derived projection — the
canonical record is never deformed to suit one index.

**Definition of wrong.** A digest is wrong when it causes a future reader to believe something was
established, rejected, resolved or contradicted that the canonical session does not support, or
when it omits a consequential conclusion needed to avoid repeating settled work.

### D4 — How it is consumed: phases, and what pollution actually means

**The claim "zero pollution by construction" is retired.** The defensible claim is **zero ranking
interference**. Phase 2 cannot alter which facts win; it can still alter what the model reads.

The mechanism argued during design — annotation displacing facts through the token-aware trim — is
**empirically dead here**. `request_gateway/budget.py apply_budget` trims history → memory context →
tool definitions, and the memory drop is **all-or-nothing** (`memory_context = None`, every ranked
fact discarded together). But context occupancy maxes at 5,949 tokens against a 120,000 ceiling and
has never trimmed in 1,283 evaluations. The cliff is ~20× away from firing.

**The real mechanism is the inverse, and both design reviews missed it.** At a p50 assembled context
of 448 tokens, five digests at ~250 tokens each are **~74% of everything the model reads**. The
annotation does not crowd facts against a ceiling; it **dwarfs the facts it annotates in a
nearly-empty window.** The fix is that the bound must be *relative*:

> **Annotation may never exceed the token count of the facts it annotates.**

Scale-free, no tuned constant, correct at any context size.

Additional live mechanisms, each with its mitigation:

- **Stale `unresolved` re-injection** — now the leading risk (see Risks table).
- **Pseudo-consensus through duplication** — several ranked facts from one session cause its digest
  to restate their shared conclusion, lending apparent corroboration. Mitigation: attach each
  session's digest **exactly once**, regardless of how many of its facts won.
- **Provenance collapse** — the model cannot distinguish a retrieved atomic fact from an LLM-authored
  retrospective synthesis. Mitigation: the rendered annotation is explicitly labelled as derived
  synthesis, not presented as equivalent evidence.
- **Indirect instruction contamination** — a new path created by D2. Attacker-influenceable web or
  file content → tool result → digest → *a future session's context*, arriving with the authority of
  the system's own memory. Low likelihood in a single-user system; recorded because the path is new.

**Structural invariant, enforced rather than monitored:**

> **No derived session artifact may ever cause primary retrieved evidence to be discarded.**

Implemented by inserting annotation as a trim tier **above** memory context, so the order becomes
history → **annotation** → memory context → tool definitions. This converts an unreachable
empirical risk into a guarantee that survives 20× growth, and costs one change to a priority list.

**Phases**

| Phase | Scope | Gate |
|---|---|---|
| **0** | Producer correction: D1 + D2 + D3. Includes the clobber fix (prerequisite) and `summary_generated_at`. | None — unconditional |
| **1** | Session-browser UI: label + digest surfaced to the human. Cross-substrate read (Neo4j) from a Postgres-backed endpoint. Not in the recall path. | Phase 0 exit criteria |
| **2** | Fact-first hydration. Ranking unchanged; ranked winners back-edge to their `Session`; digests attached afterwards as annotation. Preceded by an **offline replay analysis** (Phase 2a) over historical sessions measuring digests per query, staleness incidence, duplication rate and annotation ratio. | Phase 1 exit criteria |
| **3** | Anti-re-litigation. Detection via entity overlap on the always-current `DISCUSSES` edges; the digest supplies the *content* of the nudge, never the detection. Requires its own precision gate — it does not inherit approval from Phase 2. | Phase 2 exit criteria **and** its own precision gate |
| **4** | **Deferred** behind the measure-first diagnostic: digest-based pre-filtering (a mechanism that *removes* candidates) and cross-session synthesis. These are what would justify embedding sessions. | Diagnostic showing a concrete failure class only these fix |

Phase ordering is unchanged from the research proposal, and a reviewer's suggestion to swap 2 and 3
is rejected: it rested on the budget cliff (dead), and it inverts the risk. **Phase 2 only adds
context; Phase 3 changes behaviour.** A bad annotation is noise the model may ignore; a false
"we already settled this" nudge actively suppresses work the user wants to do.

**Explicitly out of scope:** the progressive-disclosure inversion, where digests become the primary
index and facts are zoomed into second. That makes digest quality gate *all* recall — a poor digest
would no longer annotate badly, it would hide good facts entirely. It converges with the constructed-context
workstream and belongs there. The verification-oracle lane moves to the fact-verifier workstream.

---

## Alternatives Considered

### Option 1: Lazy generation on first read (ADR-0024's original resolution)

**Description:** Generate nothing until something asks for a session's digest.

**Pros:**
- Zero cost for sessions never recalled
- No scheduler, no sweep, no new wake mechanism
- Is what ADR-0024 actually decided

**Cons:**
- Places a frontier-model call **inside the read path**, which for Phases 2 and 3 is the
  latency-critical user-turn path
- Distributes cache-miss handling into three separate consumers, each needing request coalescing to
  stop concurrent readers generating the same digest
- Optimises away a cost that measurement shows is ~5 calls/day

**Why Rejected:** ADR-0024 resolved this before the consumers were known. The planned steady state is
frequent reads on the latency-critical path against one write per quiet period — textbook
materialised-view territory. Lazy generation does not remove the population decision, it relocates
it to the most expensive place and multiplies it. Two independent design reviews, one of which had
no exposure to the author's position, reached debounced-idle by the same route.

### Option 2: Incremental summarisation — summarise (previous digest + new turns)

**Description:** On resumption, patch the existing digest with the new turns rather than regenerating.

**Pros:**
- Bounded input regardless of session length; answers "what about long sessions" without a cap
- Mirrors the incremental, deduplicated design already used for entity extraction

**Cons:**
- Each pass summarises a summary: early detail decays and errors become permanent inputs to every
  later generation
- Not reproducible — cannot be re-derived when prompts or models improve

**Why Rejected:** Drift is unrecoverable and the cost it saves is not a real constraint here (max
session = 17 turns, p90 = 7). Wholesale regeneration is `f(canonical captures)` rather than
`f(previous digest, delta)`, which is self-correcting. Retained only as a hypothetical fallback if a
future session ever exceeds the model's context window.

### Option 3: Bounded tool excerpts — clip each tool result to a fixed budget

**Description:** Include tool results up to a per-result cap (e.g. 5,000 bytes head+tail), which
covers ~90% of results.

**Pros:**
- Bounds the only unbounded input dimension
- Cheap and deterministic

**Cons:**
- **Selection bias:** large results are large because they are evidence-dense, so a size-triggered
  clip targets precisely the material that could contradict the agent
- The head/tail justification comes from log compaction, where errors cluster at initialisation and
  in trailing stack traces. This corpus is `self_telemetry_query` (161), `query_elasticsearch` (133),
  `read_file` (120), `search_memory` (76), `web_search` (75) — **query result sets and file reads**,
  where the decisive record sits at an arbitrary index

**Why Rejected:** Decisively, because truncation is not merely lossy — **it fabricates
contradictions.** A summariser instructed to check narration against evidence, handed a truncated
payload, reads absence-of-evidence as evidence-of-absence: it fails to find the detail the agent
cited and concludes the agent hallucinated it, writing a false accusation permanently into the graph
where nothing downstream can distinguish it from a real catch. This yields the inversion that
governs D2: **names-only is safer than truncation despite carrying less information**, because (a) is
honestly silent while (b) is confidently wrong. It is also why withheld payloads under the
profile-aware rule drop to names-only rather than to excerpts.

### Option 4: A single artifact serving both the UI and retrieval

**Description:** One `session_summary` string, as today, read by both the session browser and the
recall path.

**Pros:**
- One field, one generation, no schema change
- Is the current shape

**Cons:**
- The two audiences want opposite things: a short identifying label for navigation versus a dense,
  provenance-tagged epistemic record for machine reasoning
- Produces exactly today's artifact — too verbose to scan, too narratively shaped and lossy to reason
  over

**Why Rejected:** The compromise fails both consumers, and the marginal cost of the alternative is
zero: both artifacts are generated in the same call from the same evidence and merely stored
separately.

### Option 5: Turn the producer off entirely until a consumer exists

**Description:** Stop generating. Build the producer when the first consumer is actually being built.

**Pros:**
- Strictly dominates on cost — the artifact is currently unread
- Spends no design effort on an artifact with no consumer
- Honest: an unconsumed artifact is a liability, not an asset

**Cons:**
- Forfeits the forcing function — Phase 1 makes quality human-visible before anything consumes it
  automatically
- Leaves the clobber bug and the every-turn trigger in place
- Produces no corpus with which to judge whether downstream lanes are worth building

**Why Rejected on balance, not dismissed.** The producer fix is small, has standalone value
(it stops the every-turn spend, fixes a live data-loss bug, and yields inspectable artifacts), and
generates the dataset needed to decide whether any downstream lane deserves to exist. But the
underlying discipline is retained as this ADR's kill condition: **do not invent a consumer to
justify an artifact.** If Phase 1 shows the digest conveys nothing beyond existing turn summaries
and entity edges, stop there.

### Option 6: Swap Phase 2 and Phase 3

**Description:** Ship anti-re-litigation before fact-first hydration, on the grounds that it exposes
digests to the model only on collision rather than on every recall.

**Pros:**
- Lower exposure frequency
- Would validate digest utility in a narrower setting first

**Cons:**
- Rested on the budget-cliff risk, which measurement shows cannot fire
- Puts the more complex build first, confounding "is the digest good" with "is the collision
  detector good"
- Entity-overlap collisions may be too rare at ~5 sessions/day to produce validation data

**Why Rejected:** It inverts the risk. Phase 2 only adds context; Phase 3 *changes behaviour*. On the
consequence-of-being-wrong axis Phase 2 is the safer thing to ship first.

---

## Consequences

### Positive Consequences

- The producer stops spending a frontier model on every turn for an artifact nobody reads, and stops
  discarding ~89% of the assistant text that carries a session's outcome.
- A live data-loss bug is fixed: a transient generation failure no longer erases the stored summary.
- 51% of generations disappear via the minimum-turns floor, and each removed generation eliminates a
  diverging duplicate of a `Turn` node that already works.
- The digest becomes the only artifact in the graph that carries `decisions`, `unresolved` and
  `corrections` — none of which any existing representation expresses.
- Provenance becomes machine-checkable rather than aspirational, so a fabricating producer fails a
  validator rather than silently poisoning the graph.
- The session browser gets a real label instead of the first 60 characters of the first message.
- Every phase has a stated exit criterion and the workstream has an explicit, non-pejorative kill
  condition.

### Negative Consequences

- **Complexity increase:** a new sweep worker, a derived-freshness predicate, a two-artifact
  structured output with per-item provenance, a span validator, and a profile-aware input
  conditional. Today's producer is a single prompt.
- **Cross-substrate read in Phase 1:** the Postgres-backed session endpoint must reach Neo4j for the
  label and digest. This is the first such join in that endpoint.
- **Generation task is materially harder:** four slots plus provenance plus a label, on a model that
  cannot be tuned independently (no `session_summary` role). Schema violations become possible where
  today only prose length could fail.
- **Legacy rows:** all 121 existing sessions carry a summary, including all 62 single-turn ones.
  Applying the floor requires deciding what happens to those, rather than assuming a clean slate.
- **The digest is eventually consistent by design.** A session summarised ~15 minutes after it goes
  quiet is stale for that window, and the anti-re-litigation consumer is exposed at exactly its
  highest-value moment — the immediately-consecutive session. Mitigated but not eliminated by
  entity-overlap detection being real-time fresh.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| **Cross-session supersession of `unresolved`** — a thread left open in session A is settled in session C; nothing revisits A's digest, because regeneration is triggered only by A's own new turns and a concluded session never gets more. Open threads accumulate as permanent false-open state, and the anti-re-litigation consumer eventually asserts "we never settled X" about something settled weeks ago — the exact inverse of its purpose. | **High** (structurally guaranteed to occur) | Phase 0 stamps every `unresolved` item with its session's timestamp so consumers phrase the nudge as *"as of that session, X was open"* rather than asserting present tense. Phase 3's entity-overlap machinery then checks whether a later session's `decisions` settle an earlier session's `unresolved`. The timestamp must ship in Phase 0 or the Phase 3 fix has nothing to stand on. |
| **Proportional dominance** — annotation outweighing the facts it annotates in a small context (five digests ≈ 74% of a p50 context) | High | Relative bound: annotation may never exceed the tokens of the facts it annotates. Measured in the Phase 2a replay before anything ships. |
| **Fabricated corrections** poisoning the graph self-confirmingly | High | Precision-first Tier A/B standard; verbatim-span validation; never infer error from absent evidence; `corrections` rate monitored as a drift signal. |
| **Instruction contamination** via tool output surviving into a digest and then into a future session's context | Low likelihood, high blast radius | Recorded as a new path created by D2. Single-user system; no mitigation built in Phase 0 beyond the profile-aware conditional. Revisit if the system ever becomes multi-user or ingests untrusted content routinely. |
| **Egress** — local-profile tool payloads reaching a cloud summariser | Medium | Profile-aware input policy; withheld-evidence clause in the contract so the summariser does not reason about what it was not given. |
| **Provenance collapse** — the model treating derived synthesis as retrieved fact | Medium | Rendered annotation explicitly labelled as derived; `basis` tags retained in the structured record. |
| **Pseudo-consensus** from one session's digest restating several of its own ranked facts | Medium | Attach each session's digest exactly once per recall. |
| **Budget-cliff eviction** of the entire memory block | Low likelihood (never fired in 1,283 evaluations; ~20× headroom), catastrophic if it fires | Structural, not monitored: annotation trims before memory context. |
| **Schema violation** producing no digest at all | Medium | Validator with one retry and a defined degradation; a malformed response must not silently become a missing summary. |

---

## Implementation Notes

**Files affected (Phase 0):**

- `src/personal_agent/second_brain/session_summary.py` — input policy, two-artifact structured
  output, provenance tags, pre-dispatch token check, profile-aware conditional
- `src/personal_agent/second_brain/consolidator.py` — stop calling the summariser per pass
- `src/personal_agent/memory/service.py` — `create_session` must stop writing `session_summary`
  (**the clobber fix — prerequisite, lands first or together**); new `summary_generated_at`; sweep
  query for dirty-and-idle sessions
- `src/personal_agent/memory/models.py` — `SessionNode`: `summary_generated_at`, `session_label`,
  structured digest
- `src/personal_agent/brainstem/scheduler.py` — the idle sweep, reusing the existing single-flight
  guard
- `src/personal_agent/config/settings.py` — idle threshold, digest token budget

**Later phases:** `gateway/session_api.py` and the PWA (Phase 1); `memory/service.py` broad recall
and `request_gateway/context.py` plus `budget.py` trim order (Phase 2); a new collision path
(Phase 3).

**Sequencing constraints:**

- The clobber fix is a **hard prerequisite** for the fail-loud input policy. Until
  `create_session` stops owning the field, "fail explicitly" means "delete silently."
- `summary_generated_at` and the `unresolved` timestamps must ship in Phase 0 even though nothing
  reads them until Phases 2 and 3.
- No session embeddings in Phase 0. No embedding at all before the Phase 4 diagnostic.

**Legacy data:** the floor applies to sessions summarised after this ships (discriminated by
`summary_generated_at`). Existing single-turn summaries are pre-existing rows; whether to null them
is an implementation decision to be taken deliberately, not by side effect.

**Evaluation population is a real constraint.** The corpus holds 59 multi-turn sessions. A paired
evaluation is a **directional discriminator with a facts-only trivial baseline**, not a powered lift
measurement, and must be reported as such. Where a criterion below asks for an existence proof
rather than a rate, that is why.

---

## Verification / Acceptance Criteria

**Phase 0 — producer**

- **AC-1** — A multi-turn session receives **at least one and strictly fewer generations than it has
  turns** over its lifetime. · **Check:** count `session_summary_generated` events per `session_id`
  in `agent-logs-*` against that session's `turn_count` in Neo4j. · *Fails if* any multi-turn session
  shows generations ≥ turns (trigger not moved) **or** zero generations (sweep not firing).
- **AC-2** — A generation failure leaves the previously stored digest **intact**. · **Check:** force a
  failure (oversized input or injected budget denial) on a session that already has a digest; read
  the Neo4j property. · *Fails if* the field becomes null or changes.
- **AC-3** — No session summarised after this ships has `turn_count = 1` with a digest; every
  multi-turn session quiet past the threshold has one. · **Check:** two Cypher counts keyed on
  `summary_generated_at` being non-null. · *Fails if* either count is non-zero.
- **AC-4** — The digest can reference content that appears **only** in a tool result and nowhere in
  the assistant text. · **Check:** replay a session whose tool output contains a distinctive token
  the assistant never echoes; assert the token can surface in the digest. · *Fails if* the digest can
  only ever contain assistant-narrated content — which a tool-blind producer cannot avoid.
- **AC-5** — Every item tagged `tool_evidence` and every `corrections` entry carries a verbatim span
  present in the canonical capture. · **Check:** automated validator over all stored digests,
  string-containment against the capture record. · *Fails if* any claimed span is absent.
- **AC-6** — On a labelled set of sessions containing **no** contradiction between evidence and
  narration, `corrections` is empty. · **Check:** manual labelling of a stratified sample; assert. ·
  *Fails if* any clean session yields a correction.
- **AC-7** — For a local-profile session (payloads withheld), the digest asserts no correction and
  makes no claim of contradiction. · **Check:** run a local-profile session with narration; inspect
  `corrections` and the digest text. · *Fails if* absence of evidence produces an asserted error.
- **AC-8** — Structured properties (turn count, duration, tool counts) **equal** the values computed
  deterministically from captures. · **Check:** compare node properties against a recount from the
  capture record. · *Fails if* any property disagrees, or if the generated digest text restates them.
- **AC-9** — A digest whose session received turns after generation is **mechanically detectable as
  stale** by a consumer without re-reading captures. · **Check:** append a turn to a summarised
  session; assert `summary_generated_at < ended_at` and that a consumer marks it stale. · *Fails if*
  staleness is not detectable from stored state.

**Phase 1 — UI**

- **AC-10** — On a blind review of ~50 multi-turn digests against their source sessions, a
  meaningful fraction convey session-level state **not already present** in the turn summaries,
  entity edges or the label. · **Check:** blind human review with the existing artifacts shown
  alongside. · *Fails if* the digest is routinely redundant — which triggers the kill condition
  below rather than a fix.

**Phase 2 — hydration**

- **AC-11** — Ranked fact IDs **and their order** are byte-identical with hydration enabled versus
  baseline. · **Check:** offline replay diff over the historical corpus. · *Fails if* any query
  differs.
- **AC-12** — Annotation is discarded **before** memory context under budget pressure. · **Check:**
  construct a context that fits without annotation and exceeds with it; assert the ranked facts
  survive and the annotation is dropped. · *Fails if* the memory block is evicted.
- **AC-13** — Annotation tokens never exceed the tokens of the facts they annotate. · **Check:**
  assertion in the hydration path plus the replay measuring the ratio per turn. · *Fails if* the
  ratio exceeds 1 on any turn.
- **AC-14** — A recall whose ranked facts all come from one session attaches that session's digest
  **exactly once**. · **Check:** replay; count attached digests against distinct parent sessions. ·
  *Fails if* any digest is attached more than once.
- **AC-15** — Paired evaluation shows facts+digest **beating** facts-only on a predefined
  session-context task class, with no regression in baseline factual correctness and no increase in
  unsupported claims. · **Check:** paired replay, arms A (facts only) / B (facts + digest), scored on
  outcome recall, error preservation, evidence fidelity, unresolved-state recall. · *Fails if* B is
  merely not-worse. **Annotation must earn its tokens.**

**Phase 3 — anti-re-litigation**

- **AC-16** — ≥90% precision on a labelled re-litigation set (true reopening · same entities
  different issue · genuinely unresolved · superseded decision · explicit correction) before any
  nudge is user-visible. · **Check:** labelled replay set. · *Fails below 90%, and* fails outright if
  any nudge claims "settled" where the latest evidence says superseded or unresolved.

**Kill condition (not a failure state).** If Phase 1 review plus the Phase 2 paired evaluation show
facts-only ≥ facts+digest on memory-dependent tasks while the user still finds the digest useful for
browsing, **stop after Phase 1**: keep the producer correction and the human-visible surface, and
kill hydration, anti-re-litigation, session embeddings, pre-filtering and cross-session synthesis.
That outcome means the artifact belongs in the human navigation plane, not the machine reasoning
plane. Do not invent a consumer to justify it.

**Seam owner:** master, at the integration gate. AC-15 is the assembled-intent criterion — it holds
only once Phases 0, 1 and 2 have all landed, and no child ticket closing can satisfy it alone. This
ADR does not close because its last child merged.

---

## References

- [ADR-0024: Session-Centric Graph Model for Behavioral Memory](ADR-0024-session-graph-model.md) — introduced `session_summary`; its open-question resolution deferred lazy generation, which shipped code inverted (Accepted — Partially Implemented)
- [ADR-0087: Memory Recall Quality Measurement Program](ADR-0087-memory-recall-quality-measurement-program.md) — measurement-first posture this ADR's evaluation criteria inherit (Accepted 2026-06-27)
- [ADR-0098: Memory Substrate and Lifecycle Architecture](ADR-0098-memory-substrate-and-lifecycle-architecture.md) — substrate and retention model this artifact lives in (Accepted 2026-06-27; §D1 superseded by ADR-0115, §D2/§D4/§D7 remain Accepted)
- [ADR-0100: Relevance-Bounded Recall](ADR-0100-relevance-bounded-recall.md) — the ranking path Phase 2 must leave unchanged (Accepted)
- `docs/research/2026-07-22-session-summary-kg-opportunity.md` — backing research; §B is the design space, §E the decision-ready addendum this ADR argues rather than transcribes
- FRE-946 — ADR ticket
- FRE-347 / FRE-346 — the original producer implementation
- Producer: `src/personal_agent/second_brain/session_summary.py`, `second_brain/consolidator.py:386-440`
- Write path and clobber bug: `src/personal_agent/memory/service.py:1133-1148`
- Trigger: `src/personal_agent/brainstem/scheduler.py:262-302`
- Budget trimming: `src/personal_agent/request_gateway/budget.py:187-320`
- Session read surface: `src/personal_agent/gateway/session_api.py:99-133`, `service/models.py:204-232`

---

## Status Updates

### 2026-07-23 - Proposed
**Changed By:** cc-adrs (Opus)
**Reason:** Authored following a four-fork design debate with the owner, each fork independently
reviewed by two frontier models. Three positions held by the author at the outset were overturned
by that process: displacement-through-trimming (refuted by occupancy measurement), bounded tool
excerpts (refuted by the fabrication argument), and a single output artifact (refuted by consumer
divergence).

---

**Template Version:** 1.1
**Based On:** [Michael Nygard's ADR pattern](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions)

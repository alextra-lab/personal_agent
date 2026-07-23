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
  characters of the first user message (`session_api.py:799`, `_extract_title`). The summary lives in Neo4j. Phase 1 is a
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
- **Concurrency** reuses the consolidator's existing single-flight guard, plus an **atomic
  conditional write**: the sweep captures `ended_at` when it reads the session, and the write is a
  single Cypher statement whose `MATCH` is predicated on `s.ended_at` still equalling the captured
  value. A read-then-write pair is *not* sufficient — re-reading before writing leaves a
  time-of-check-to-time-of-use window in which a new turn can land, and the write would then publish
  a digest built from captures that are already stale. The comparison and the mutation must be the
  same statement. Not cross-database locking either: a Postgres row lock cannot serialise a Neo4j
  mutation.
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
carry a verbatim span plus a locator** — the capture id and the field within it (which tool result,
or which turn's assistant text). The validator resolves the locator and requires the span to occur
*at that location*, not merely somewhere in the session. Bare containment is not sufficient: a
common word appears everywhere and would pass while supporting nothing. This turns the
evidence-versus-interpretation invariant from a prompt instruction into a machine-checkable one.

**Error-flagging is precision-first, deliberately asymmetric.** A missed error is recoverable from
raw evidence; a false error writes self-confirming state into the graph and feeds its own
supposed correction into future reasoning. Two tiers may be asserted, and nothing else:

- **Tier A — direct contradiction.** Authoritative evidence contradicts the *same proposition* the
  agent asserted. A Tier-A correction carries **two** located spans: the contradicted claim in the
  assistant text, and the contradicting evidence.
- **Tier B — explicit evidenced self-correction.** The agent itself corrected the record within the
  session, and the correction is supported by evidence in the capture. Carries the located span of
  the self-correction.

**Tier C — not errors, never asserted as corrections.** Weak or partial conflict, failed or
incomplete tool calls, multiple defensible readings, state that legitimately changed over time, and
disagreement with a subjective judgment or recommendation. These belong in `unresolved`, or are
omitted. **Never infer error from absent evidence.**

Note that Tier A and Tier B do not both require payloads: a contradiction between "the command
succeeded" and a recorded error *status* is Tier A on status alone, and Tier B needs only the
session's own text. So a payload-withheld session can still legitimately carry corrections — what it
must never do is assert one that depends on payload content it was not given.

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

**Why Rejected:** Because **unmarked** truncation fabricates contradictions. A summariser instructed
to check narration against evidence, handed a silently truncated payload, reads absence-of-evidence
as evidence-of-absence: it fails to find the detail the agent cited and concludes the agent
hallucinated it, writing a false accusation into the graph that nothing downstream can distinguish
from a real catch.

The honest qualification: that failure is **not intrinsic to truncation**. Marking the clip and
applying the same "do not infer from omitted evidence" contract used for withheld payloads would
defuse it. But doing so collapses the option's rationale — a payload the summariser is instructed
not to reason about the absence of is, for correction purposes, a payload it does not have, which is
option (a) with extra tokens and a worse story about which bytes survived. What remains against (b)
is then the selection bias above, which is sufficient on its own: a size-triggered clip preferentially
discards the evidence-dense results.

This yields the inversion that governs D2: **names-only is safer than unmarked truncation despite
carrying less information**, because (a) is honestly silent while (b) is confidently wrong. It is
also why withheld payloads under the profile-aware rule drop to names-only rather than to excerpts.

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

**Why Rejected:** The compromise fails both consumers. The generation cost of the alternative is
genuinely nil — both artifacts come from one call over the same evidence — though the storage and
schema cost is not zero, and D3 spends it deliberately.

Note the stronger version of this option is not today's prose string but *one structured record
carrying both a label field and digest fields, rendered per consumer*. That is close to what D3
adopts, and the difference is mostly framing: D3 stores them as two named artifacts on the same node
rather than one nested record, because the label has a different lifetime — it stays useful and
correct even when the digest is stale, absent under the single-turn floor, or withheld after a
generation failure.

### Option 5: Minimal correction — stop generating, fix the clobber bug, build the producer when a consumer exists

**Description:** Land only the two unambiguous defects: stop calling the summariser on every
consolidation pass, and stop `create_session` writing `session_summary` unconditionally. Leave the
field unpopulated. Design and build the producer at the moment the first consumer is actually being
written.

This is deliberately stated as the *strong* version of "turn it off." It leaves neither the
every-turn spend nor the data-loss bug in place, so it cannot be dismissed on those grounds.

**Pros:**
- Strictly dominates on cost — the artifact is currently unread, and this stops paying for it
- Fixes the live data-loss bug immediately, with a diff a fraction of Phase 0's size
- Spends no design effort on output shape, provenance or slots until a consumer's needs are known
- Honest: an unconsumed artifact is a liability, and this ADR's own kill condition concedes the
  artifact may never earn a machine consumer

**Cons:**
- Forfeits the forcing function — Phase 1 makes quality human-visible *before* anything consumes it
  automatically, which is what de-risks every later phase
- Produces no corpus of corrected digests, so the decision about whether downstream lanes are worth
  building would be taken on the same absence of evidence that produced today's situation
- The consumer and the producer would then be designed together under delivery pressure, which is
  how output shape gets fitted to one consumer's convenience rather than to the artifact's purpose

**Why Rejected — narrowly, and this is the closest alternative.** An earlier draft rejected this on
the grounds that it "destroys the only evidence that could settle the question." That was wrong and
is withdrawn: the canonical captures persist, and corrected digests could be generated offline at any
later point without the producer running in production. Nothing is destroyed and no decision is
deferred indefinitely.

What actually separates this option from the decision is narrower and worth stating honestly. It is
a **sequencing preference**, not a correctness argument:

- Under Option 5, the producer's output shape is designed at the same time as its first consumer,
  under that consumer's delivery pressure. The consistent failure mode there is that the artifact
  gets fitted to whatever the first consumer found convenient, which is how a digest ends up shaped
  for a human reader and then handed to a retrieval path — the exact history that produced today's
  artifact.
- Phase 1 exists to make quality visible *before* anything consumes the digest automatically. That
  forcing function only works if a corrected producer is running against real sessions ahead of the
  machine consumers.

Both are judgement calls about sequencing, and a reasonable reader could take the other side. The
decision goes to phased correction because the cost of being wrong is one extra Phase-0 build,
whereas Option 5's failure mode is an artifact shaped by its first consumer and then inherited by
every later one.

**The discipline behind this option is retained rather than discarded**, as this ADR's kill
condition: *do not invent a consumer to justify an artifact.* If Phase 1 shows the digest conveys
nothing beyond the existing turn summaries and entity edges, the correct outcome is to stop at
Phase 1 — which lands close to Option 5's end state, reached with evidence instead of assumption.

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
| **Instruction contamination** via tool output surviving into a digest and then into a future session's context | High blast radius; likelihood **not** reduced by single-user operation — the corpus already includes web search and file reads, whose content is not authored by the user | Gated, not accepted. **AC-20 blocks Phase 2** — the point at which a contaminated digest first reaches a model automatically — on an adversarial fixture set proving directives in tool output neither survive into the digest nor alter it. Phase 0 and Phase 1 are unaffected because a digest read only by a human is not an injection path. |
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

**How will we know this decision actually delivered — not just merged?**

**Gate mapping.** A phase may not begin until **every** AC listed for the preceding phase passes;
partial passes do not open a gate.

| Gate | Requires |
|---|---|
| Start Phase 1 | AC-1 … AC-13 (Phase 0) |
| Start Phase 2a — offline replay analysis | AC-14, AC-15 (Phase 1) |
| Turn Phase 2 hydration on for the model | AC-16 … AC-20 |
| Start Phase 3 build | AC-21 |
| Make the Phase 3 nudge **user-visible** | AC-22 — an **intra-phase** gate: Phase 3 may be built and evaluated offline beforehand, but must not surface to the user until it passes |
| Start Phase 4 | AC-23 — the measure-first diagnostic |

**Fixture discipline, applying to every criterion below that uses one.** Fixture and sample sets are
**selected and written down before the producer is tuned or the arm is run**, and are drawn by a
stated rule (random over the eligible population, or exhaustive) — never chosen after seeing output.
A criterion evaluated on a post-hoc sample has not been met.

**Corpus feasibility is itself a gate.** The corpus holds 121 sessions, 59 multi-turn. Before any
criterion that names a sample size, the eligible population must be counted and reported. **If the
corpus cannot supply it, that is a finding to surface, not a reason to shrink the criterion** — the
permitted response is a pre-registered synthetic supplement, labelled as such in the result.

### Phase 0 — producer

- **AC-1** — Generation frequency tracks quiet periods, not turns. For each multi-turn session,
  generations ≤ number of idle gaps exceeding the threshold, plus one; and a session whose turns all
  arrived inside one idle window is generated **exactly once**. · **Check:** `session_summary_generated`
  counts per `session_id` in `agent-logs-*`, joined against inter-turn gaps computed from the
  captures. · *Fails if* counts exceed the quiet-period bound — which catches both per-turn
  generation and the "every turn but one" evasion that a bare `< turn_count` bound permits.
- **AC-2** — No session is left behind its own activity. · **Check:** Cypher for sessions where
  `ended_at < now − threshold AND (summary_generated_at IS NULL OR summary_generated_at < ended_at)`,
  excluding sessions carrying a recorded terminal generation failure. · *Fails if* any row returns.
  The `IS NULL` disjunct is required: in Cypher a comparison against NULL yields NULL and the row is
  silently dropped, so a never-summarised session escapes a bare `<` scan.
- **AC-3** — A session that receives new turns after being summarised is regenerated on the next
  quiet period, and the content reflects the new turns. · **Check:** summarise, append turns carrying
  a distinctive fact, wait past threshold, re-read. · *Fails if* `summary_generated_at` does not
  advance, or advances while the content ignores the appended turns.
- **AC-4** — A generation failure is inert and loud: the stored digest **and** label are unchanged,
  `summary_generated_at` does **not** advance, a failure event is emitted, and the session remains
  eligible for retry. · **Check:** force a failure (oversized input, injected budget denial) on a
  session that already has a digest; assert all four. · *Fails if* any of them is violated —
  particularly if freshness advances, which would mark a failed session clean forever.
- **AC-5** — Oversized input is rejected **before** any model call. · **Check:** construct a session
  exceeding the model's input limit; assert a failure event naming the reason, and assert **zero**
  model-call telemetry for that attempt. · *Fails if* a model call is issued, or the input is
  silently truncated.
- **AC-6** — Two sweeps racing one session cannot publish a stale digest. · **Check:** drive two
  concurrent sweeps, injecting a new turn **between the conditional match and the mutation**; assert
  the stale write is refused and the stored digest's source captures match the current `ended_at`. ·
  *Fails if* a read-then-write pair passes where an atomic conditional write would not — the test
  must distinguish the two, not merely observe a single surviving property value.
- **AC-7** — No session summarised after this ships has `turn_count = 1` with a digest; every
  multi-turn session quiet past the threshold has one, except those carrying a recorded terminal
  failure. Additionally, each session's stored deterministic metadata — turn count, duration, tool
  invocation and success/failure counts — **equals** a recount from the capture record. · **Check:**
  two Cypher counts keyed on `summary_generated_at`, plus a property-versus-recount comparison over
  all sessions. · *Fails if* either count returns non-zero, or any property disagrees with its
  recount — which is what "compute state, generate meaning" means in practice, and what a producer
  that lets the model author its own counts would violate.
- **AC-8** — Input completeness. For a predefined fixture set spanning multi-result turns, failed
  calls, long assistant responses and both execution profiles, the assembled prompt contains: every
  turn in the session; the **full, untruncated** user and assistant text of each; and for every tool
  invocation its name, **arguments**, status and error. Cloud-profile sessions additionally contain
  each payload **byte-equal** to the capture; local-profile sessions contain **no** payload. ·
  **Check:** assert over the assembled prompt, comparing against the capture record. · *Fails if*
  any turn, field, argument or error is missing, any payload differs from source, or either
  profile's payload rule is violated.
- **AC-9** — Tool-only facts survive into the digest. On a predefined set of ≥5 sessions across
  different tools, each containing a decision-relevant fact present **only** in tool output, the
  digest reproduces that fact in **all** of them. · **Check:** fixtures fixed in advance; the facts
  are chosen to be consequential for the session outcome, so marginal-utility filtering is not a
  legitimate reason to omit them. · *Fails if* any is missing — a narration-only producer cannot
  pass.
- **AC-10** — Every digest item carries a `basis` tag, and tagging discriminates. · **Check:** schema
  validation for tag presence across all stored digests; plus, on a predefined labelled set of ≥40
  items spanning all four basis values, agreement between emitted tag and labelled truth ≥85% with
  **no single tag value exceeding 60% of emissions** unless the labelled truth is equally skewed. ·
  *Fails if* any item is untagged, or if tagging collapses onto one value — the evasion that
  tag-presence alone cannot catch.
- **AC-11** — Every `tool_evidence` item and every `corrections` entry carries a span **and a
  locator**, and the span occurs at that location. · **Check:** validator resolves each locator to
  the named capture and field, then requires the span there. · *Fails if* any locator is absent or
  unresolvable, or the span is not found at the cited location — bare containment anywhere in the
  session does not pass.
- **AC-12** — **Corrections fire when they should and stay silent when they should not.** On a
  predefined labelled set: positives comprising ≥6 Tier-A contradictions **and** ≥4 Tier-B evidenced
  self-corrections; negatives comprising ≥12 Tier-C cases drawn from the full range D3 names —
  weak/partial conflict, failed or incomplete calls, ambiguous readings, legitimately changed state,
  and disagreement with a subjective judgment. The producer emits a correction for **every** positive
  and **none** of the negatives. · **Check:** hand-labelled fixtures, fixed before tuning. · *Fails
  if* any negative yields a correction, **or** any positive yields none. A producer that never emits
  corrections fails the positives; a naive contradiction detector fails the Tier-C negatives.
- **AC-13** — A local-profile session asserts no correction **that depends on withheld payload
  content**, while still emitting corrections available from status, error or self-correction. ·
  **Check:** a local-profile fixture pair — one session whose only contradiction lives in a withheld
  payload (must yield no correction), one whose contradiction is visible in tool status (must yield
  one). · *Fails if* absent evidence produces an asserted error, **or** if the producer suppresses
  corrections that D3 permits without payloads.

### Phase 1 — UI

- **AC-14** — The surface works end to end. · **Check:** the session list renders label and digest
  for a session whose digest lives in Neo4j while its row lives in Postgres; a session with no digest
  (single-turn, or failed) renders without error and without a stale or placeholder digest; the
  generated label replaces the first-60-characters title. · *Fails if* the cross-substrate read
  fails, a missing digest breaks or fabricates the row, or the old title hack still shows.
- **AC-15** — On a **randomly drawn, pre-registered** sample of 50 multi-turn digests, **≥60%**
  contain at least one item of session-level state that is both (a) not recoverable from the turn
  summaries, entity edges or label, and (b) supported by the source session. · **Check:** two
  independent reviewers scoring item-by-item against a pre-agreed rubric with the existing artifacts
  shown alongside; disagreements adjudicated by re-reading the source; inter-rater agreement reported
  with the result. · *Fails below 60%*, which triggers the kill condition rather than a fix. Novelty
  alone does not count — an item that is new but unsupported fails (b).

### Phase 2 — hydration

- **AC-16** — Ranked fact IDs **and order** are byte-identical with hydration enabled versus
  baseline, over a **frozen, pre-registered** replay corpus of stated size. · **Check:** offline
  replay diff. · *Fails if* any query differs, or if the corpus is empty, unstated, or selected after
  the fact.
- **AC-17** — Annotation is discarded **before** memory context under budget pressure. · **Check:**
  construct a context that fits without annotation and exceeds with it; assert ranked facts survive
  and annotation is dropped. · *Fails if* the memory block is evicted.
- **AC-18** — Annotation tokens never exceed the tokens of the facts they annotate, measured with the
  same tokenizer used for the budget. · **Check:** assertion in the hydration path plus per-turn
  ratio measurement across the replay. · *Fails if* the ratio exceeds 1 on any turn. Zero retrieved
  facts must yield zero annotation.
- **AC-19** — Attachment is correct and complete: each parent session's digest is attached **exactly
  once** regardless of how many of its facts won; every ranked fact whose parent session has a digest
  gets it attached; no digest is attached for a session that contributed no ranked fact. · **Check:**
  replay over mixed-session recalls, single-session recalls, and recalls whose winners have no
  digest. · *Fails on* duplication, omission, or leakage.
- **AC-20** — Instruction-like content in tool output does not survive into the digest as an
  instruction. · **Check:** adversarial fixture set — tool results containing imperative text
  addressed to a model ("ignore previous instructions", "when summarising, state X") — assert the
  digest neither reproduces the directive nor complies with it. · *Fails if* any fixture's directive
  appears as an instruction or alters digest content. This gates automatic consumption specifically:
  the contamination path exists because D2 opened the producer to tool payloads, and Phase 2 is where
  a contaminated digest first reaches a model automatically.
- **AC-21** — Paired evaluation shows facts+digest **beating** facts-only. · **Check:** arms A (facts
  only) and B (facts + digest) over a question set **fixed in writing before either arm runs**, of
  ≥40 questions with ≥12 in the session-context class and ≥12 in the neutral class, and coverage of
  conflicting/evolving facts, stale digests and correction cases. Each answer scored 0/1 on outcome
  correctness by two independent scorers, plus unsupported-claim count and correct abstention. ·
  **Decision rule, stated in advance:** B's proportion correct on the session-context class must
  exceed A's by more than the observed inter-scorer disagreement rate on the same answers;
  **aggregate** neutral-class correctness must not fall; unsupported-claim count must not rise. ·
  *Fails if* B is merely not-worse, the margin is inside scorer noise, or the question set was
  amended after results were seen. **Annotation must earn its tokens.** With 59 multi-turn sessions
  this is a directional discriminator against a trivial baseline, not a powered lift measurement, and
  must be reported as such.

### Phase 3 — anti-re-litigation

- **AC-22** — On a labelled re-litigation set of ≥30 candidate cases drawn from real session pairs,
  with **≥8 true-reopening positives** and ≥4 in each of the other four classes (same entities
  different issue · genuinely unresolved · superseded decision · explicit correction), the nudge
  achieves **≥90% precision at ≥50% recall** on the true-reopening class. · **Check:** labelled
  replay; report precision, recall, the candidate denominator and the per-class counts. · *Fails
  below either threshold* — precision alone is gameable by a system that almost never nudges, and
  per-class minima prevent a two-positive set making one lucky nudge look like 100% precision —
  **and** fails outright if any nudge claims "settled" where the latest evidence says superseded or
  unresolved. Corpus feasibility must be established first per the rule above; a pre-registered
  synthetic supplement is permitted and must be labelled in the result.

### Phase 4 — gate

- **AC-23** — Phase 4 does not begin without a diagnostic naming a **concrete observed failure class**
  that only candidate pre-filtering or cross-session synthesis fixes. · **Check:** the diagnostic
  cites specific recall misses from real traffic and shows why Phases 0–3 cannot address them. ·
  *Fails if* the justification is capability-shaped ("embedding would let us…") rather than a
  demonstrated miss.

**Kill condition (not a failure state).** If Phase 1 review plus the Phase 2 paired evaluation show
facts-only ≥ facts+digest on memory-dependent tasks while the user still finds the digest useful for
browsing, **stop after Phase 1**: keep the producer correction and the human-visible surface, and
kill hydration, anti-re-litigation, session embeddings, pre-filtering and cross-session synthesis.
That outcome means the artifact belongs in the human navigation plane, not the machine reasoning
plane. Do not invent a consumer to justify it.

**Seam owner:** master, at the integration gate. AC-21 is the assembled-intent criterion — it holds
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
- Session read surface: `src/personal_agent/gateway/session_api.py:99-133` (`list_sessions`, Postgres-only) and `gateway/session_api.py:799-814` (`_extract_title`, the first-60-characters hack)
- Postgres session row (no summary column): `src/personal_agent/service/models.py:204-232`

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

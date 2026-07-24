# ADR-0124: Session-summary producer correction and phased consumption

**Status:** Accepted — 2026-07-23 (owner), **amended 2026-07-23 (Amendment A — conversation-scoped input; tool payloads removed from D2; D3 corrections narrowed to payload-free kinds), further amended 2026-07-24 (Amendment B — the summariser is conversation-*only*: tool metadata removed from D2's input entirely; the `tool_evidence` basis and the `status_contradiction` correction removed to the verification oracle; `corrections` reduced to `self_correction`).** Implementation chain FRE-947 → FRE-948 → FRE-949 → FRE-950 → FRE-951; Phase 4 unfiled, gated on AC-24.
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
- The summariser has no model role of its own, resolving through `captains_log`. **The role's
  creation is in scope here** (D2) because this ADR locates egress control at the role binding; the
  *model choice* for that role remains deferred until it converges with structured conversation
  context, and the role therefore lands defaulted to today's behaviour.
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

  **What this does and does not guarantee.** It prevents publishing a digest once a *known* newer
  turn has landed — i.e. once consolidation has advanced `ended_at`. It cannot prevent a race against
  a turn that has been captured but not yet consolidated, because `ended_at` does not yet reflect it.
  That residual window is the consolidation lag (seconds) against the idle threshold (10–15 minutes),
  so it is bounded and small, and the next sweep corrects it because the session becomes dirty again.
  This is a staleness bound, not a linearisability claim, and should not be described as one.
- **Resumption regenerates wholesale** from canonical captures. Never incremental patching of the
  prior summary.
- **The summary is decoupled from the per-turn session write.** `create_session` must stop owning
  `session_summary`, or the next turn after a sweep will NULL the fresh digest. This also fixes the
  live clobber bug.

### D2 — What it reads: the whole conversation *(narrowed by Amendment A; conversation-only by Amendment B)*

- Full user text and full assistant text, all turns. The 200-char clip and the 20-turn cap are
  removed outright.
- **Tool activity as metadata** — name, status, error. **No payloads** (Amendment A). *(Amendment B:
  tool metadata is removed from the producer's input **entirely** — see the Amendment B section. The
  producer reads user and assistant text only; no tool name, status or error reaches the prompt.
  Tool invocation and success/failure counts remain available as **computed** structured properties
  per D3's "compute state, generate meaning" — derived from the captures, never fed to the generator.)*
  Bounded by construction, so no per-result cap and no summariser-specific input ceiling are needed.
- **Egress is governed by the role binding, not by a per-session branch.** *(Amendment A: with tool
  payloads removed, the producer's input is conversation text the primary model already processed, so
  there is no new egress path at all. The role decision below stands on its own merits — an
  independently tunable summariser — and the reasoning is kept for the record.)* The producer reads
  the whole conversation for every session, unconditionally. Whatever it reads goes to whichever model backs
  its role — so the question "do these bytes leave the machine?" is answered by *which deployment
  that role is bound to*, and it is answered once, in configuration, rather than re-decided per
  session.

  This is deliberate, and it replaces an earlier draft of this ADR that branched per session on
  `SessionModel.execution_profile` (local vs cloud). That draft was wrong twice over. Mechanically,
  the column is **vestigial** — hard-coded to `"local"` at both creation sites, read back by nothing,
  formally retired by ADR-0121 T5 (FRE-920) when the model-selection store became the source of
  truth (`service/app.py:290` records this in comment); had it shipped, every session would have
  classified local, every payload would have been withheld, and this decision would have silently
  negated itself. More importantly it was wrong **conceptually**: ADR-0121 removed the execution
  "Path" and made the user select models and roles directly, so where a given model is served is no
  longer a construct the system exposes or reasons about. Re-deriving a local/cloud binary from the
  selection store in order to branch on it would reintroduce precisely the abstraction that ADR was
  written to remove.

  **This makes a dedicated `session_summary` role a Phase 0 deliverable, not a deferred nicety.**
  The control point named above does not currently exist: the summariser has no role of its own and
  resolves through `captains_log` (`session_summary.py:131`), so "bind the summariser to a deployment
  whose egress you accept" is unactionable — you would be re-binding reflection at the same time. A
  decision that locates control at the role binding is incoherent while the role is absent, so the
  role is created here:

  - `config/model_roles.yaml` — add `session_summary` to **both** the `roles` block (`{ all: … }`)
    and the `bindings` block (`{ deployment: … }`), alongside the existing `entity_extraction` and
    `captains_log` entries.
  - `config/config_guard.py` — add it to `_ROLE_HEADER_RE`'s known-role alternation
    (`entity_extraction|captains_log|insights|compressor|embedding|reranker`), or the guard rejects
    the new key as an orphan.
  - **Default it to `claude_sonnet`**, the model the summariser already resolves to today, so
    behaviour is unchanged the moment it lands and the role's introduction is observably a no-op.
  - `budget_role` stays `captains_log` for now (passed explicitly at `session_summary.py:152`);
    splitting cost attribution is a separate, smaller decision and is not taken here.

  **What remains deferred is the model *choice*, not the role.** Whether a cheaper model can do this
  job — the open question in the backing research, with precedent in `entity_extraction`'s tested
  move to `gpt-5.4-mini` and in the `compressor` role already doing structured summarisation on
  `gpt-5.4-mini` — stays deferred until it converges with structured conversation context, and must
  not be judged on the current producer's output in any case. Creating the role is precisely what
  turns that future question into a config flip rather than a code change.

- **If evidence is ever unavailable, the contract must say so.** Captures can be incomplete — a
  truncated record, a turn whose assistant text was never stored, a capture written during a failure.
  Whenever the producer's input is missing conversation it would normally have, the prompt must state
  it — *"part of this session's transcript was unavailable; do not infer content from its absence"* —
  or the summariser fabricates to fill the gap. *(Amendment B: with tool metadata and
  `status_contradiction` both removed, there is no tool evidence whose absence could be misread into a
  false contradiction; the rule reduces to the general stance that absence of evidence is not evidence
  of absence, which AC-13 checks over the conversation-only input.)*
- **Oversized input fails visibly.** Estimate tokens before dispatch; if the model's real limit
  would be exceeded, raise and record the reason. Never silently truncate. The check is
  pre-dispatch, so a doomed session costs an estimate and a log line, not a model call. Log on
  transition rather than every sweep tick.
- **Minimum-turns floor of 2.** Single-turn sessions produce no digest. Every `Turn` already carries
  a populated summary and key entities; a one-turn session digest is not merely redundant, it is a
  **diverging artifact describing the same event**, free to contradict the record it duplicates. At
  two turns, genuine session-level relation appears that neither turn expresses alone ("A was
  rejected after X was discovered; B was chosen"). This removes 51% of generations.

### D3 — What it emits: two artifacts, four optional slots, verifiable provenance *(corrections narrowed by Amendment A; conversation-only by Amendment B)*

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
(`user_statement | assistant_reasoning | mixed`) *(Amendment B removed `tool_evidence`)*. Because
`basis` is a model-assigned label and nothing stops a model tagging its own inference as evidence, it
is backed by an enforcement step: **every `corrections` entry must carry a verbatim span plus a
locator** — the capture id and the turn's assistant text the correction is grounded in *(Amendment B:
the locator names a turn's assistant text only; the tool-result field it once allowed is gone with
`tool_evidence` and `status_contradiction`)*. The validator resolves the locator and requires the
span to occur *at that location*, not merely somewhere in the session. Bare containment is not
sufficient: a common word appears everywhere and would pass while supporting nothing. This turns the
evidence-versus-interpretation invariant from a prompt instruction into a machine-checkable one.

**Error-flagging is precision-first, deliberately asymmetric.** A missed error is recoverable from
raw evidence; a false error writes self-confirming state into the graph and feeds its own
supposed correction into future reasoning. **After Amendment B a single kind may be asserted, and
nothing else:**

- **`self_correction` — explicit evidenced self-correction.** The agent itself corrected the record
  within the session, and the correction is supported by evidence *in the conversation* — the
  assistant's own corrective text, which the user saw. Carries the located span of the
  self-correction.
- **`status_contradiction` — REMOVED by Amendment B.** Adjudicating narration against a tool's own
  status or error is *verification*, and verification belongs to the downstream verification oracle
  (Lane 5 → Workstream 4), not the summariser. The raw status/error remains durably captured in the
  turn records, so the oracle reads it directly later; the summariser carrying it preserved nothing
  and added a false-positive surface to a ~250-token digest. *(This reverses the Amendment A
  reconciliation note below, which had retained `status_contradiction` as the payload-free survivor
  of the old "Tier A"; Amendment B relocates that survivor to the oracle with the rest of
  verification.)*

**Tier C — not errors, never asserted as corrections.** Weak or partial conflict, failed or
incomplete tool calls, multiple defensible readings, state that legitimately changed over time, and
disagreement with a subjective judgment or recommendation. These belong in `unresolved`, or are
omitted. **Never infer error from absent evidence.**

`self_correction` needs only the session's own conversation text, so it survives on the
conversation-only input intact. This matters wherever a capture is incomplete: a self-correction the
user saw remains legitimate to record, and a producer must not suppress it merely because some other
evidence is missing. What it must never do is assert a correction that depends on content it was not
given.

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

---

## Amendment A — 2026-07-23: the digest is built from the conversation, not from tool payloads

**Status of this amendment:** Accepted, same day as the ADR, before the Phase-0 behaviour reached
steady state. It **narrows D2 and D3**; D1 and D4 are untouched. Raised by the owner during
deployment of FRE-947.

> **Superseded in part by Amendment B (2026-07-24).** Amendment B further narrows this amendment: it
> removes tool metadata from the producer's input entirely and relocates `status_contradiction` to
> the verification oracle, so `corrections` reduces to `self_correction`. Where this section describes
> tool metadata or `status_contradiction` as *retained*, read it as Amendment A's state at the time,
> since superseded. See the **Amendment B** section below.

### What was wrong

The original D2 fed the summariser **full tool payloads**. The sole thing that consumed them was
D3's `corrections` slot, whose Tier A rule adjudicates the agent's narration against tool evidence.
Every other slot — `established`, `decisions`, `unresolved` — is built from the conversation.

That is a **verification** capability, and this ADR had already scoped verification *out*: §D4
states plainly that "the verification-oracle lane moves to the fact-verifier workstream." The ADR
therefore contradicted its own boundary — it exported the verification lane and then re-imported the
expensive half of it through a slot definition. The design debate that produced Tier A argued the
"two independent witnesses" case persuasively and was never held against the boundary already drawn.

The owner's framing is the correcting one, and it is the more fundamental statement: **the knowledge
graph is the user's memory.** What a person retains from a session is the conversation — what was
asked, what came back, what was decided. Tool output reached the user *through* the assistant's
response; that narration is what shaped their understanding and is therefore what belongs in memory.
A fact the assistant never surfaced was never learned, and is not a memory to record.

### The corrected policy

**D2 is narrowed.** The producer reads:

- **full user text and full assistant text, every turn** — unchanged, and still the substantive fix
  (the 200-character clip discarded ~89% of assistant text);
- **tool activity as metadata only** — name, status, error. Bounded, conversational, and it preserves
  real episode content such as the 27 `nonexistent_tool` invocations in the corpus, a signal that
  lives entirely in names and statuses;
- **no tool payloads.**

**D3 is narrowed.** `corrections` drops the **payload-fed** adjudication and keeps only the two
**payload-free** kinds — `self_correction` (was Tier B) and `status_contradiction` (narration denied
by a tool's own status/error, the survivor of the old Tier A). Both are grounded *within the
conversation and tool metadata* the user's episode contains; the verification lane that reads full
payloads leaves for the fact-verifier workstream. Tier C is unchanged: still never a correction.

> **Reconciliation note (2026-07-23, owner-approved during FRE-953).** The paragraphs above and the
> table below originally read *"Tier B only / Tier A removed."* That was an over-statement: this
> amendment kept **AC-13**, which explicitly requires the `status_visible` case — narration denied by
> a tool's status/error with no self-correction — to still yield a correction, and that case is
> payload-free. "Remove Tier A" therefore means *remove the payload-fed half of it*, not remove
> status-based contradiction. The two surviving kinds are renamed to self-describing values
> (`self_correction`, `status_contradiction`) so nothing is mislabelled, and the shipped producer
> (FRE-953) carries exactly these two. The withdrawal of the payload-fed verification lane is
> unchanged.
>
> **Reversed by Amendment B (2026-07-24).** This note retained `status_contradiction` because AC-13
> required the `status_visible` case. Amendment B removes that requirement — status-based
> contradiction is verification and moves to the oracle — so `corrections` reduces to `self_correction`
> alone and AC-13's fixture drops the `status_visible` case. See the Amendment B section.

The absent-evidence rule survives untouched and now covers a wider case: captures can be incomplete,
and a summariser comparing narration against evidence will read absence as contradiction unless told
otherwise.

### What this buys, beyond correctness

Three problems the original design had to *manage* now cease to exist:

- **Egress.** The producer's input is conversation text the primary model has already processed.
  There is no new egress path, so nothing to govern, branch on, or bind. The two earlier corrections
  in this ADR's history — the vestigial `execution_profile` column, then the retired local/cloud
  concept itself — were both attempts to manage an exposure that should never have been created.
- **Instruction contamination.** The path ran attacker-influenceable web/file content → tool payload
  → digest → a future session's context, arriving with the authority of the system's own memory. With
  payloads gone the path is **removed, not mitigated**, and its Phase-2 gate becomes unnecessary.
- **Input size.** The canonical-serialisation comparison, the pre-dispatch ceiling and the ~67k-token
  worst case were all payload-driven. Conversation-only input is a few KB at p90.

### What is genuinely lost

Stated plainly rather than minimised:

1. **Payload-fed contradictions** — the agent misreading a tool *payload* (as opposed to its status),
   undetected. This is verification and is now wholly the verification workstream's to deliver. Note
   the narrower, payload-free `status_contradiction` case is **retained** (it fires on tool status/error
   alone — see the D3 reconciliation note); only the payload-reading half leaves.
2. **Facts appearing only in tool output and never narrated.** Real, but thin: assistant responses run
   p50 1,847 characters, so narration is substantial — and by the framing above, an unnarrated fact is
   not the user's memory either.

### Forward note — the verification oracle's evidence, and why payloads are not deleted

The owner's direction (2026-07-23) for a future verification oracle leans **away from an external
process** and toward a **VO-dump** shape: full tool responses and other heavy artifacts (large text,
binaries) written to a dedicated location with its own lifecycle, read on demand by the oracle. Two
properties motivate it — heavy evidence gets retention management independent of the memory
substrate, and it adds no core-harness infrastructure.

This is **recorded as direction, not decided here**; the oracle's design belongs to its own ADR after
its own research. Three observations are worth carrying into that work:

- **The store substantially exists.** `TaskCapture.tool_results` already persists full payloads to
  disk (`telemetry/captains_log/captures/`) and to `agent-captains-captures-*`. The work is less
  "build a dump" than "split the record": a light capture on the memory path, heavy evidence with its
  own retention. ADR-0069's R2 artifact store is the natural home for bytes, which is what keeps the
  no-new-infrastructure property real.
- **Splitting by storage is stronger than splitting by formatter.** This amendment is currently
  enforced by the producer's prompt builder choosing to omit payloads — a convention a future
  refactor can silently undo. If heavy evidence lives outside the record the memory path reads, the
  separation becomes structural and cannot regress by accident.
- **Retention sets the oracle's reach.** Purging dumps aggressively means facts whose evidence has
  aged out are permanently unverifiable. That is probably the right trade — verification is most
  valuable near the time of extraction — but it should be a stated consequence of the retention
  window, not a discovery.

**Constraint this amendment therefore carries: tool payloads continue to be captured and stored.**
Only their delivery to the summariser stops. "Payloads are not memory" must not slide into "payloads
are not needed."

### Consequential changes elsewhere in this ADR

| Section | Change |
|---|---|
| D2 | Tool payloads removed from the input policy; tool metadata (name, status, error) retained |
| D3 | `corrections` keeps two payload-free kinds — `self_correction` (was Tier B) and `status_contradiction` (payload-free survivor of Tier A, required by AC-13); the payload-fed adjudication leaves for the verification workstream (see the D3 reconciliation note) |
| Risks — instruction contamination | Path removed rather than gated; the row and its Phase-2 gate no longer apply |
| Risks — egress | No longer applicable; the producer's input carries no bytes the primary turn did not already send |
| AC-8 | Payload equality and canonical-serialisation clauses dropped; tool metadata completeness retained |
| AC-9 | Withdrawn — it required a tool-only fact to reach the digest, which this amendment deliberately prevents |
| AC-12 | Positives are `self_correction` cases only (≥8); the payload-fed contradiction cases are removed. `status_contradiction` is exercised by AC-13, not AC-12 |
| AC-13 | Retained: incomplete captures must produce silence, not invention, and must not suppress the corrections that survive — specifically the `status_contradiction` (`status_visible`) case, which fires on tool status/error alone |
| AC-21 | Withdrawn — the injection path it gated no longer exists |

Withdrawn criteria are struck rather than renumbered, so AC references in the implementation chain
(FRE-947 → FRE-951) remain stable.

---

## Amendment B — 2026-07-24: the summariser is conversation-only

**Status of this amendment:** Accepted — owner-agreed in session on 2026-07-23, formalised here. It
**further narrows D2 and D3**; D1 and D4 are untouched. It closes the loose end Amendment A left:
Amendment A removed tool *payloads* from the producer's input but kept two tool reaches — the
`tool_evidence` basis and the `status_contradiction` correction — and **AC-10 was deferred** as a
result. Design agreed with the owner and recorded in
`docs/superpowers/specs/2026-07-23-adr-0124-amendment-b-summarizer-conversation-only-design.md`.

### Principle (owner)

The summary is built on **what the user actually received** — the assistant's responses plus the
user's own text. The assistant already consumed the tools and folded them into its reply, so that
reply *is* the record of what happened. Re-injecting tools lets the summariser re-derive facts
differently than the assistant actually presented — a summary of something the user never received.
The summariser must therefore neither **source content from** tools nor **adjudicate against** them.

### Decision

1. **Remove `tool_evidence` as a basis value.** Tools-as-source violates fidelity. `basis` collapses
   to the conversation-grounded values `user_statement`, `assistant_reasoning`, `mixed`.
2. **Remove the `status_contradiction` correction.** Tools-as-adjudication is *verification* work,
   which belongs to the downstream **verification oracle** (Lane 5 → Workstream 4), not the
   summariser. `corrections` reduces to `self_correction` alone. This reverses the Amendment A
   reconciliation note, which had retained `status_contradiction` as the payload-free survivor of the
   old "Tier A"; Amendment B relocates that survivor to the oracle with the rest of verification.
3. **Remove tool metadata from the producer's input entirely.** This is the change that makes the
   principle *structural* rather than a prompt convention. "Keep tool metadata in the input but treat
   it as inert to the output" does not hold for a language model: anything in the prompt is in the
   model's attention, so a tool status or error left in-context can still surface as a digest item — a
   tool-sourced item, exactly what removing `tool_evidence` is meant to prevent. With
   `status_contradiction` gone there is **no remaining output slot that reads tool metadata**, so the
   metadata has no consumer and is dropped from the prompt. The input is user text + assistant text,
   all turns, and nothing tool-derived.
4. **`self_correction` is conversation-only.** FRE-953 had allowed a self-correction's evidence to be
   "a tool error or the conversation." Amendment B restricts it to the **conversation** — the
   assistant correcting itself in its own text, which the user saw. This is what lets AC-11's locator
   grammar drop every `tool_result[...]` target.
5. **Do not add a tool-error flag.** Considered and rejected: most tool errors are *recovered from*,
   so a judgment-free "produced amid tool errors" flag is mostly false positives and would pollute a
   ~250-token digest; separating harmful from benign errors *is* the judgment that belongs to the
   oracle; and the raw tool status/error is **already durably captured in the turn records**, so the
   oracle reads it directly later. The summariser carrying it preserves nothing and only adds noise.

### What the computed properties still carry

Removing tool metadata from the *prompt* does not remove it from the graph. Tool invocation and
success/failure counts remain **computed** structured properties on the session node (D3, "compute
state, generate meaning") — derived deterministically from the captures, never generated from the
prompt. Amendment B stops feeding tool data to the *generator*; it does not stop *counting* it.

### What is genuinely lost

1. **Status-based contradiction detection** — narration denied by a tool's own status or error (e.g.
   "the command succeeded" against a recorded failure). This was the payload-free survivor Amendment A
   kept; it is verification and now leaves wholly to the oracle, which reads the same status/error
   from the durable turn records.
2. **The un-narrated tool signal** Amendment A valued — e.g. the 27 `nonexistent_tool` invocations
   that "live in names and statuses." By Amendment B's stricter principle this is not memory: an
   agent-internal tool failure the user never saw is not part of what they retained. Where it *was*
   narrated, it survives in the assistant text, which is kept.

Both align with Amendment A's forward note: verification evidence lives in the durably-stored turn
records (the future VO-dump), read by the oracle on demand — not carried in the memory digest.

### Consequences

- **AC-10 unblocks.** With nothing tool-sourced left to label, the payload/tool-derived fixture
  problem dissolves; AC-10's discrimination check is redefined over the three conversation bases and
  is a Phase-0 gate criterion again.
- **Phase 1 (FRE-948) proceeds** on the simplest possible producer.

### Consequential changes elsewhere in this ADR

| Section | Change |
|---|---|
| D2 | Tool metadata (name/status/error) removed from the input entirely; input is user + assistant text only |
| D3 — `basis` | `tool_evidence` removed; three conversation bases remain |
| D3 — enforcement | span+locator required for `corrections` only; locator names a turn's assistant text |
| D3 — corrections | `status_contradiction` removed to the oracle; `self_correction` is the only kind |
| D3 reconciliation note (under Amendment A) | reversed — its retention of `status_contradiction` no longer holds |
| Risks — fabricated corrections | standard is `self_correction` only (conversation-grounded) |
| AC-8 | asserts absence of all tool metadata (name/status/error), not only payloads |
| AC-10 | redefined over three bases; un-deferred; a stored `tool_evidence` value now fails it |
| AC-11 | `tool_evidence` dropped; locator grammar is conversation-only (`tool_result[...]` removed) |
| AC-12 | positives are `self_correction` only; the `status_contradiction`/AC-13 cross-reference removed |
| AC-13 | the `status_visible` fixture case removed; fixture reduces to a pair |

### Verification / acceptance of this amendment

Amendment B's own acceptance is discriminating and outcome-level, not a restatement of the edits:

- **No retired value survives where a digest is produced or stored.** Over all digests generated after
  Amendment B ships, **zero** items carry `basis = tool_evidence` and **zero** `corrections` entries
  carry `status_contradiction`. · **Check:** a Cypher scan of `session_digest` records keyed on
  `summary_generated_at` after the ship SHA. · *Fails if* any such item exists — which a build that
  edited the prose but left the schema enum or the producer prompt intact would produce.
- **The prompt is conversation-only.** AC-8 (as amended) asserts the assembled prompt contains no tool
  name, status, error, argument or payload. · *Fails if* any tool-derived token appears — the
  regression back to feeding tool data to the generator.
- **The deferred discrimination check runs.** AC-10 (as amended) is constructed and passes over the
  three conversation bases. · *Fails if* it cannot be built because a tool-sourced basis is still
  emitted.

Each criterion can fail against a half-finished implementation, and the reconciliation is not complete
until all three hold. **That seam is owned by the Amendment B implementation ticket**, not by any
single edit to this document — the ADR change alone does not deliver Amendment B.

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
- **Selection bias:** large results are plausibly large *because* they carry more distinct records,
  so a size-triggered clip preferentially discards the material most able to contradict the agent.
  Stated as a mechanism, not a measurement — we have result-size distributions but have not measured
  evidence density against size, and the argument should not be read as though we had
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
also why, wherever evidence is genuinely unavailable, the producer is told it is absent rather than
handed a partial substitute.

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
rather than one nested record, because the label is useful in states where the digest is not: absent
under the single-turn floor, and withheld after a generation failure. (An earlier draft also claimed
the label "stays correct when the digest is stale" — that is false and withdrawn: both are generated
from the same captures at the same moment and go stale together.)

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
  structured output with per-item provenance, and a span validator. Today's producer is a single
  prompt.
- **Cross-substrate read in Phase 1:** the Postgres-backed session endpoint must reach Neo4j for the
  label and digest. This is the first such join in that endpoint.
- **Generation task is materially harder:** four slots plus provenance plus a label, on a model that
  gains a role of its own for the first time (defaulted to today's model, so no behaviour change on
  landing). Schema violations become possible where today only prose length could fail.
- **Legacy rows:** all 121 existing sessions carry a summary, including all 62 single-turn ones.
  Applying the floor requires deciding what happens to those, rather than assuming a clean slate.
- **The digest is eventually consistent by design.** A session summarised ~15 minutes after it goes
  quiet is stale for that window, and the anti-re-litigation consumer is exposed at exactly its
  highest-value moment — the immediately-consecutive session. Mitigated but not eliminated by
  entity-overlap detection being real-time fresh.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| **Cross-session supersession of `unresolved`** — a thread left open in session A is settled in session C; nothing revisits A's digest, because regeneration is triggered only by A's own new turns and a concluded session never gets more. Open threads accumulate as permanent false-open state, and the anti-re-litigation consumer eventually asserts "we never settled X" about something settled weeks ago — the exact inverse of its purpose. | **High** (occurs whenever a thread outlives its session, which the corpus shows is common; not strictly guaranteed) | Phase 0 stamps every `unresolved` item with its session's timestamp so consumers phrase the nudge as *"as of that session, X was open"* rather than asserting present tense. Phase 3's entity-overlap machinery then checks whether a later session's `decisions` settle an earlier session's `unresolved`. The timestamp must ship in Phase 0 or the Phase 3 fix has nothing to stand on. |
| **Proportional dominance** — annotation outweighing the facts it annotates in a small context (five digests ≈ 74% of a p50 context) | High | Relative bound: annotation may never exceed the tokens of the facts it annotates. Measured in the Phase 2a replay before anything ships. |
| **Fabricated corrections** poisoning the graph self-confirmingly | High | Precision-first correction standard (`self_correction` only after Amendment B — conversation-grounded); verbatim-span validation; never infer error from absent evidence; `corrections` rate monitored as a drift signal. |
| ~~**Instruction contamination**~~ *(no longer applicable — Amendment A)* via tool output surviving into a digest and then into a future session's context | High blast radius; likelihood **not** reduced by single-user operation — the corpus already includes web search and file reads, whose content is not authored by the user | Gated, not accepted. **AC-21 blocks Phase 2** — the point at which a contaminated digest first reaches a model automatically — on an adversarial fixture set proving directives in tool output neither survive into the digest nor alter it. Phase 0 and Phase 1 are unaffected because a digest read only by a human is not an injection path. |
| ~~**Egress**~~ *(no longer applicable — Amendment A; the producer's input carries no bytes the primary turn did not already send)* — full tool payloads (file contents, command output, query results) reach whichever provider serves the summariser's role | Medium; unchanged in kind from the primary turn, which already sent those bytes to its own model | Governed at the **role binding** per ADR-0121, not by a per-session branch — the deliberate consequence being that egress is a configuration decision the owner makes once. Sharpened by the deferred role split: the only available binding is `captains_log`, shared with reflection, so the knob is currently coarser than the decision deserves. |
| **Provenance collapse** — the model treating derived synthesis as retrieved fact | Medium | Rendered annotation explicitly labelled as derived; `basis` tags retained in the structured record. |
| **Pseudo-consensus** from one session's digest restating several of its own ranked facts | Medium | Attach each session's digest exactly once per recall. |
| **Budget-cliff eviction** of the entire memory block | Low likelihood (never fired in 1,283 evaluations; ~20× headroom), catastrophic if it fires | Structural, not monitored: annotation trims before memory context. |
| **Schema violation** producing no digest at all | Medium | Validator with one retry; on second failure the attempt is recorded as a failure per the terminal-failure rule (reason stored, session stays dirty, counted in population-check output) rather than silently yielding no digest. AC-4 covers the inert-and-loud behaviour; the retry count itself is an implementation choice, not a load-bearing decision. |

---

## Implementation Notes

**Files affected (Phase 0):**

- `src/personal_agent/second_brain/session_summary.py` — input policy, two-artifact structured
  output, provenance tags, pre-dispatch token check
- `src/personal_agent/second_brain/consolidator.py` — stop calling the summariser per pass
- `src/personal_agent/memory/service.py` — `create_session` must stop writing `session_summary`
  (**the clobber fix — prerequisite, lands first or together**); new `summary_generated_at`; sweep
  query for dirty-and-idle sessions
- `src/personal_agent/memory/models.py` — `SessionNode`: `summary_generated_at`, `session_label`,
  structured digest
- `src/personal_agent/brainstem/scheduler.py` — the idle sweep, reusing the existing single-flight
  guard
- `src/personal_agent/config/settings.py` — idle threshold, digest token budget
- `config/model_roles.yaml` — new `session_summary` role in both the `roles` and `bindings` blocks,
  defaulted to `claude_sonnet`
- `src/personal_agent/config/config_guard.py` — add `session_summary` to `_ROLE_HEADER_RE`'s
  known-role alternation, or the guard flags the new key as an orphan

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
| Start Phase 1 | AC-1 … AC-14 (Phase 0) |
| Start Phase 2a — offline replay analysis | AC-15 (Phase 1 works). AC-16 is evaluated *during* 2a, not before it: 2a is what produces the digests-per-query, staleness, duplication and annotation-ratio numbers AC-16 and AC-19 read. |
| Turn Phase 2 hydration on for the model | AC-17 … AC-21 |
| Start Phase 3 build | AC-22 |
| Make the Phase 3 nudge **user-visible** | AC-23 — an **intra-phase** gate. The phase table's "Phase 3 requires its own precision gate" means exactly this: Phase 3 may be built and evaluated offline once AC-22 passes, but must not surface to the user until AC-23 does. |
| Start Phase 4 | AC-24 — the measure-first diagnostic |

**Fixture discipline, applying to every criterion below that uses one.** Fixture and sample sets are
**selected and written down before the producer is tuned or the arm is run**, and are drawn by a
stated rule (random over the eligible population, or exhaustive) — never chosen after seeing output.
A criterion evaluated on a post-hoc sample has not been met.

**"Recorded terminal failure" is a defined, reported state, not an escape hatch.** A session may be
excluded from the population checks only if it carries a stored failure reason *and* an attempt
count at or above the retry limit. Oversize input is terminal only once it has been retried and
failed deterministically; a budget denial is never terminal, since it is transient by nature (this
is why AC-4 requires failures to stay retryable). **Every population check reports the count and
reasons of excluded sessions alongside its result** — so an implementation that marks sessions
terminal to make a check pass makes that visible in the same output rather than hiding it.

**Corpus feasibility is itself a gate.** The corpus holds 121 sessions, 59 multi-turn. Before any
criterion that names a sample size, the eligible population must be counted and reported. **If the
corpus cannot supply it, that is a finding to surface, not a reason to shrink the criterion** — the
permitted response is a pre-registered synthetic supplement, labelled as such in the result.

### Phase 0 — producer

- **AC-1** — Generation frequency tracks quiet periods, not turns. For each multi-turn session,
  generations ≤ number of idle gaps exceeding the threshold, plus one; and a session whose turns all
  arrived inside one idle window is generated **exactly once**. · **Check:** `session_summary_generated`
  counts per `session_id` in `agent-logs-*`, joined against inter-turn gaps computed from the
  captures. Additionally, for a fixture session the **first** generation occurs no earlier than the
  idle threshold after its last turn. · *Fails if* counts exceed the quiet-period bound — which
  catches both per-turn generation and the "every turn but one" evasion a bare `< turn_count` bound
  permits — **or** if generation fires before the threshold elapses, which an eager implementation
  satisfying only the count bound would do.
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
  concurrent sweeps against one session whose `ended_at` advances between the two sweeps' reads;
  assert the sweep holding the older captured value has its write **refused**, and that
  `summary_generated_at` reflects only the winning write. · *Fails if* both writes are accepted, or
  if the implementation is a re-read followed by an unconditional write — the test must distinguish
  those two, so it asserts refusal of the loser rather than merely observing that one value survives
  in a single property.
- **AC-7** — No session summarised after this ships has `turn_count = 1` with a digest; every
  multi-turn session quiet past the threshold has one, except those carrying a recorded terminal
  failure. Additionally, `turn_count` on each session summarised after this ships **equals** a
  recount from its captures. · **Check:** two Cypher counts keyed on `summary_generated_at`, plus a
  property-versus-recount comparison scoped to those sessions. · *Fails if* either count returns
  non-zero, or any `turn_count` disagrees with its recount. Scoped deliberately: `SessionNode`
  carries `turn_count` today but not duration or tool success/failure counts, and D3's
  compute-don't-generate rule does not by itself justify adding aggregate columns or backfilling
  history — if those properties are added later, this criterion extends to them then.
- **AC-8** — Input completeness. For a predefined fixture set spanning multi-result turns, failed
  calls and long assistant responses, the assembled prompt contains every turn in the session and the
  **full, untruncated** user and assistant text of each. **Amendment B:** the prompt carries **no tool
  metadata at all** — not payloads or arguments (already excluded by Amendment A), and not tool name,
  status or error — so the criterion asserts the **absence** of every tool-derived token. · **Check:**
  assert over the assembled prompt against the capture record. · *Fails if* any turn, or any turn's
  full user or assistant text, is missing or altered, **or if any tool name, status, error, argument
  or payload appears in the prompt** — the second direction is what catches a regression back to
  feeding tool data to the generator.
- **AC-9** — **WITHDRAWN by Amendment A.** It required a fact present only in tool output to reach
  the digest, which the amendment deliberately prevents: an unnarrated fact is not part of the
  conversation and therefore not the user's memory. Retained as a numbered entry so AC references in
  the implementation chain stay stable.

- **AC-10** — Every digest item carries a `basis` tag, and tagging discriminates. **Amendment B
  redefines this over the three conversation bases** (`user_statement | assistant_reasoning | mixed`);
  with `tool_evidence` removed there is no tool-sourced label to fixture, which is what unblocks this
  criterion (it was deferred under Amendment A). · **Check:** schema validation for tag presence
  across all stored digests; plus, on a predefined labelled set of ≥40 items spanning **all three**
  basis values (each value represented by ≥8 items), agreement between emitted tag and labelled truth
  ≥85% with **no single tag value exceeding 60% of emissions** unless the labelled truth is equally
  skewed. · *Fails if* any item is untagged, **if any stored item carries the retired `tool_evidence`
  value**, or if tagging collapses onto one value — the evasion that tag-presence alone cannot catch.
- **AC-11** — Every `corrections` entry carries a span **and a locator**, and the span occurs at that
  location. **Amendment B:** `tool_evidence` items no longer exist, and the locator grammar names a
  **turn's assistant text only** — the `tool_result[N].error` / `tool_result[N]` targets are removed
  with `status_contradiction`, since `self_correction` is grounded in the conversation. · **Check:**
  validator resolves each locator to the named capture and the cited turn's assistant text, then
  requires the span there. · *Fails if* any locator is absent, unresolvable, or names a tool-result
  field, or the span is not found at the cited location — bare containment anywhere in the
  session does not pass. **Stated limitation:** this proves the citation resolves, not that the span
  *supports* the proposition. A fabricated item citing a real but irrelevant span at a valid locator
  passes this check. Mechanical entailment is not available to us, so semantic support is carried by
  AC-12's labelled fixtures and AC-16's human review; AC-11 is a necessary condition that makes the
  cheap failure mode — invented citations — impossible, and is claimed as nothing more.
- **AC-12** — **Corrections fire when they should and stay silent when they should not.** On a
  predefined labelled set: positives comprising ≥8 `self_correction` positives (evidenced
  self-corrections; **Amendment B:** `self_correction` is the *only* correction kind — the
  `status_contradiction` cases are removed to the verification oracle, not exercised here or in
  AC-13); negatives comprising ≥12
  Tier-C cases drawn from the full range D3 names —
  weak/partial conflict, failed or incomplete calls, ambiguous readings, legitimately changed state,
  and disagreement with a subjective judgment. The producer emits a correction for **every** positive
  and none of the negatives. Each emitted `self_correction` additionally carries the located span of
  the **supporting evidence** in the conversation, not merely of the self-correction sentence. ·
  **Check:** hand-labelled
  fixtures, fixed before tuning. · *Fails if* **any** negative yields a correction (precision is
  absolute here), or if fewer than **80%** of positives yield one, or if any `self_correction` lacks
  its evidence span. The recall floor is 80% rather than 100% deliberately: D3 accepts that a missed
  error is recoverable, so demanding perfect recall would contradict the precision-first stance and
  penalise a justifiably conservative producer. It is not 0% because that is the degenerate
  never-emit implementation this criterion exists to catch.
- **AC-13** — Missing evidence produces silence, not invention, and does not suppress the correction
  that survives it. **Amendment B** removes the `status_contradiction` (`status_visible`) case, so the
  fixture is a **pair**. · **Check:** two fixtures built on captures with deliberately incomplete
  records — one whose only possible contradiction would need evidence absent from the conversation
  (must yield **no** correction), and one containing an explicit evidenced **self-correction** in the
  session's own text (must yield one). · *Fails if* absent evidence produces an asserted correction,
  **or** if the surviving `self_correction` path is suppressed. Both directions matter: a producer
  that invents corrections from gaps fails the first case, and one that goes mute whenever any evidence
  is missing fails the second.

- **AC-14** — The summariser resolves through its **own** role, and introducing that role changed no
  behaviour. · **Check:** `session_summary.py` resolves `session_summary`, not `captains_log`;
  `config/model_roles.yaml` carries the key in **both** the `roles` and `bindings` blocks; the
  config guard accepts it (no orphan-key failure); and the deployment key it resolves to is
  byte-identical to what `captains_log` resolved to before the change. · *Fails if* the producer
  still resolves another subsystem's role, if the guard rejects or ignores the key, or if the
  resolved model differs from today's — the role must land as an observable no-op, or it has
  smuggled in the model change this ADR defers.

### Phase 1 — UI

- **AC-15** — The surface works end to end. · **Check:** the session list renders label and digest
  for a session whose digest lives in Neo4j while its row lives in Postgres; a session with no digest
  (single-turn, or failed) renders without error and without a stale or placeholder digest; the
  generated label replaces the first-60-characters title. · *Fails if* the cross-substrate read
  fails, a missing digest breaks or fabricates the row, or the old title hack still shows.
- **AC-16** — On a **randomly drawn, pre-registered** sample of 50 multi-turn digests, **≥60%**
  contain at least one item of session-level state that is both (a) not recoverable from the turn
  summaries, entity edges or label, and (b) supported by the source session. · **Check:** two
  independent reviewers scoring item-by-item against a pre-agreed rubric with the existing artifacts
  shown alongside; disagreements adjudicated by re-reading the source; inter-rater agreement reported
  with the result. · *Fails below 60%*, which triggers the kill condition rather than a fix. Novelty
  alone does not count — an item that is new but unsupported fails (b).

### Phase 2 — hydration

- **AC-17** — Ranked fact IDs **and order** are byte-identical with hydration enabled versus
  baseline, over a **frozen, pre-registered** replay corpus of **at least 30 queries** spanning
  recalls whose winners have digests, lack digests, and span multiple sessions. · **Check:** offline
  replay diff. · *Fails if* any query differs, or if the corpus is under 30, unstated, lacks any of
  the three classes, or was selected after the fact — a one-convenient-query replay proves nothing.
- **AC-18** — Annotation is discarded **before** memory context under budget pressure. · **Check:**
  construct a context that fits without annotation and exceeds with it; assert ranked facts survive
  and annotation is dropped. · *Fails if* the memory block is evicted.
- **AC-19** — Annotation tokens never exceed the tokens of the facts they annotate, measured with the
  same tokenizer used for the budget. · **Check:** assertion in the hydration path plus per-turn
  ratio measurement across the replay. · *Fails if* the ratio exceeds 1 on any turn. Zero retrieved
  facts must yield zero annotation.
- **AC-20** — Attachment is correct and complete: each parent session's digest is attached **exactly
  once** regardless of how many of its facts won; every ranked fact whose parent session has a digest
  gets it attached; no digest is attached for a session that contributed no ranked fact. · **Check:**
  replay over mixed-session recalls, single-session recalls, and recalls whose winners have no
  digest. · *Fails on* duplication, omission, or leakage.
- **AC-21** — **WITHDRAWN by Amendment A.** It gated Phase 2 on proving that directives embedded in
  tool output could not survive into the digest. With payloads no longer reaching the producer, the
  path does not exist to be gated. Retained as a numbered entry so AC references stay stable.

- **AC-22** — Paired evaluation shows facts+digest **beating** facts-only. · **Check:** arms A (facts
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

- **AC-23** — On a labelled re-litigation set of ≥30 candidate cases drawn from real session pairs,
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

- **AC-24** — Phase 4's two lanes are gated **separately**, each on a diagnostic naming a concrete
  observed failure class **that Phases 0–3 as shipped demonstrably do not address**. · **Check:** the
  diagnostic cites specific recall misses from real traffic, and shows for each that the shipped
  Phase 0–3 mechanisms were exercised and failed on it. · *Fails if* the justification is
  capability-shaped ("embedding would let us…") rather than a demonstrated miss, or if a diagnostic
  for one lane is used to open the other — candidate pre-filtering and cross-session synthesis are
  different mechanisms with different risks, and a miss that synthesis would fix is not evidence for
  a filter that *removes* candidates. Deliberately **not** phrased as "only these two mechanisms
  could fix it": proving a negative over unknown alternatives is not achievable, and the gate is
  about demonstrated need, not exclusivity.

**Kill condition (not a failure state).** If Phase 1 review plus the Phase 2 paired evaluation show
facts-only ≥ facts+digest on memory-dependent tasks while the user still finds the digest useful for
browsing, **stop after Phase 1**: keep the producer correction and the human-visible surface, and
kill hydration, anti-re-litigation, session embeddings, pre-filtering and cross-session synthesis.
That outcome means the artifact belongs in the human navigation plane, not the machine reasoning
plane. Do not invent a consumer to justify it.

**Seam owner:** master, at the integration gate. AC-22 is the assembled-intent criterion — it holds
only once Phases 0, 1 and 2 have all landed, and no child ticket closing can satisfy it alone. This
ADR does not close because its last child merged.

---

## References

- [ADR-0024: Session-Centric Graph Model for Behavioral Memory](ADR-0024-session-graph-model.md) — introduced `session_summary`; its open-question resolution deferred lazy generation, which shipped code inverted (Accepted — Partially Implemented)
- [ADR-0087: Memory Recall Quality Measurement Program](ADR-0087-memory-recall-quality-measurement-program.md) — measurement-first posture this ADR's evaluation criteria inherit (Accepted 2026-06-27)
- [ADR-0098: Memory Substrate and Lifecycle Architecture](ADR-0098-memory-substrate-and-lifecycle-architecture.md) — substrate and retention model this artifact lives in (Accepted 2026-06-27; §D1 superseded by ADR-0115, §D2/§D4/§D7 remain Accepted)
- [ADR-0100: Relevance-Bounded Recall](ADR-0100-relevance-bounded-recall.md) — the ranking path Phase 2 must leave unchanged (Accepted)
- `docs/research/2026-07-22-session-summary-kg-opportunity.md` — backing research; §B is the design space, §E the decision-ready addendum this ADR argues rather than transcribes; Lane 5 → Workstream 4 is the verification oracle Amendment B relocates verification to
- `docs/superpowers/specs/2026-07-23-adr-0124-amendment-b-summarizer-conversation-only-design.md` — Amendment B design, owner-agreed in session and formalised here
- FRE-946 — ADR ticket
- FRE-955 — Amendment B ticket
- FRE-347 / FRE-346 — the original producer implementation
- Producer: `src/personal_agent/second_brain/session_summary.py`, `second_brain/consolidator.py:386-440`
- Write path and clobber bug: `src/personal_agent/memory/service.py:1133-1148`
- Trigger: `src/personal_agent/brainstem/scheduler.py:262-302`
- Budget trimming: `src/personal_agent/request_gateway/budget.py:187-320`
- Session read surface: `src/personal_agent/gateway/session_api.py:99-133` (`list_sessions`, Postgres-only) and `gateway/session_api.py:799-814` (`_extract_title`, the first-60-characters hack)
- Postgres session row (no summary column): `src/personal_agent/service/models.py:204-232`

---

## Status Updates

### 2026-07-24 - Amendment B (Accepted)
**Changed By:** Owner (architect), via cc-adrs (Opus)
**Reason:** The summariser is conversation-only. Amendment A removed tool payloads but kept two tool
reaches — the `tool_evidence` basis and the `status_contradiction` correction — and deferred AC-10.
Amendment B removes both, removes tool metadata from the producer's input **entirely** (keeping the
principle structural rather than a prompt convention a model can violate on any generation), restricts
`self_correction` evidence to the conversation, and rejects a tool-error flag. All tool-derived
verification relocates to the future verification oracle (Lane 5 → Workstream 4), which reads the
durably-stored status/error directly. `corrections` reduces to `self_correction`; `basis` to the
three conversation values. AC-10 is un-deferred and redefined over the three bases, unblocking Phase 1
(FRE-948). D1 and D4 untouched.

### 2026-07-23 - Amendment A (Accepted)
**Changed By:** Owner (architect), via cc-adrs (Opus)
**Reason:** Raised by the owner during FRE-947 deployment: the digest should summarise the
conversation, because the knowledge graph is the user's memory and the conversation is what a person
retains. Investigation found a sharper reason — the ADR scoped the verification-oracle lane out to
the fact-verifier workstream, then re-imported it through D3's Tier-A `corrections` rule, which was
the sole consumer of the full tool payloads D2 fed the producer. D2 and D3 narrowed accordingly; D1
and D4 untouched. Removes the egress surface and the instruction-contamination path outright rather
than governing them. Tool payloads continue to be stored — only their delivery to the summariser
stops — against a future verification oracle reading them from a lifecycle-managed dump.

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

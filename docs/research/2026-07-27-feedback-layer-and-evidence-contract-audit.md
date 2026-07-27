# Audit — the feedback layer, and the evidence a verification layer would need

*Session: adr seat, 2026-07-26 into 2026-07-27, owner-directed. Backing ticket FRE-999.
Everything below was read against `main` at `24c1c27a`, the live prod `.env`, and Linear on
2026-07-27. Claims inherited from other documents rather than verified here are marked as such.*

**This audit decides nothing.** It is the evidence base for ADR-0125. It exists so the decision
argues from measurements rather than from impressions, and so the measurements survive the session.

---

## 1. Why the audit was run

The session opened as a redesign of the session summarizer (brief:
`2026-07-26-session-summarizer-brainstorm-brief.md`). It developed into something broader when the
owner named the real problem:

> *"I see the potentially premature convergence of two dimensions of the project. We are asking is
> the harness working well, optimized, efficient, cost-effective — and second, is the results that
> are being produced accurate and useful to the user."*

and then, on the feedback-analysis layer specifically:

> *"I have disabled all the feedback analysis processing because I think it has become a spaghetti
> mess. Not uniform, coherent and of one mind."*

The audit's job was to establish whether that reading is correct, and what it implies.

---

## 2. Headline: the two dimensions were never distinguished, and the instrumentation proves it

| | **Dimension 1 — harness health** | **Dimension 2 — output quality** |
|---|---|---|
| Question | optimised, efficient, cost-effective? | factually true, complete, useful? |
| Consumer | the owner as **operator** | the owner as **user / student** |
| Evidence needed | telemetry | conversation + tool records + KG |
| Failure mode | slow, expensive, flaky | wrong, ungrounded, useless |
| Cadence | continuous / background | post-session |

The decisive evidence is what a capture records about memory recall — in full
(`captains_log/capture.py:69-70`):

```
memory_context_used: bool = False
memory_conversations_found: int = 0
```

A boolean and a count. **No identities.** And the only place per-turn entity identities are
persisted is `telemetry/compaction.py`, whose comment reads: *"Maps session_id → set of entity_ids
dropped in the most recent compaction."*

> **The system durably records which facts it discarded, and not which facts it relied on.**

That asymmetry is not a missing field. It is the signature of a capture layer instrumented to
observe *whether the machine ran* — counts, booleans, durations, CPU, step totals, iteration caps —
and never *whether the output was true*. Dimension 2 has no evidence base because nobody ever
declared dimension 2 existed.

Three downstream confusions follow from the missing distinction, each independently observed:

- **Reflection recall** — a dimension-1 producer's output injected into dimension-2 surface
  (user-facing context). Killed by the owner 2026-07-26, see §5.
- **The session digest** — a dimension-2 artifact scoped by a dimension-1 constraint (a token
  budget), producing the 250-token bound that ADR-0124 D3 itself records as provisional and
  never empirically derived.
- **`status_contradiction`** — a dimension-2 *verification* function that was originally specified
  inside a dimension-2 *summarizer*. ADR-0124 Amendment B correctly extracted it to "a future
  verification oracle." That extraction looks in hindsight like scope reduction; it was the first
  time the verification layer was named as distinct.

---

## 3. The reflection producer — audited, with a retraction

### 3.1 What it is given

Complete input list of the `GenerateReflection` DSPy signature
(`captains_log/reflection_dspy.py:92-135`): `user_message`, `trace_id`, `steps_count`,
`final_state`, **`reply_length: int`**, `telemetry_summary`, `metrics_summary` (cpu%, duration),
`failure_excerpt`, `had_errors`, `prompt_manifest`.

The assistant's response is not an input. Only its length in characters.

### 3.2 What it is told to do

The signature's own instruction block, verbatim:

> *Analyzes task execution telemetry to identify patterns, issues, and opportunities for
> optimization.*
> **Focus areas:** performance patterns · error patterns · tool usage patterns · mode/governance
> interactions · optimization opportunities (caching, parallelization)

All five focus areas are infrastructure.

### 3.3 The retraction

An earlier reading in this session treated §3.1 and §3.2 as a defect — a producer asked to judge
turn quality while shown a character count instead of the answer. **The owner corrected it:
reflection was only ever intended as a system-side producer.**

Measured against that intent the design is correct. The five focus areas are on target,
`reply_length` is an appropriate dimension-1 input, and ADR-0105's measured output distribution
(performance 43%, observability 23%, reliability 11%) is a correctly-scoped producer doing its job,
not miscalibration.

**The retraction stands and is recorded deliberately** — the finding was wrong, and the corrected
version is more useful: the gap is not a bad dimension-1 producer, it is that **dimension 2 has no
producer at all.**

### 3.4 What survives the audit

| Finding | Location | Severity |
|---|---|---|
| `trace_id` passed as a prompt input — a random hex string that can only cost tokens and invite confabulation | `reflection_dspy.py:93` | low, free to fix |
| manual fallback path truncates the user message to 200 chars | `reflection.py:411` | medium |
| per-turn scope only — cross-session patterns structurally invisible | whole module | **structural** |
| duplication: 832 distinct fingerprints of 942, "topically concentrated", ~1,800 awaiting pile | ADR-0105 measurement (inherited) | **structural** |

The last two are one finding. A per-turn infrastructure monitor sees *some* slow operation in every
turn, so it emits a textually novel and substantively identical performance proposal each time. The
generation-time dedup work (FRE-721) treats the symptom; the input scope is the cause.

### 3.5 Telemetry capture, on efficiency

`_summarize_telemetry` (`reflection.py:637`) is deterministic prose assembly — no model call,
ADR-0014-compliant, cheap. It renders LLM call counts, tool names, failures, durations correctly.
Two silent caps (`errors[:3]`, `content[:200]`) are minor. **This part is fine.** The efficiency
question the owner raised has a reassuring answer for dimension 1; the problem is coverage for
dimension 2.

---

## 4. ADR-0105's convergence shipped — the loop is one deploy short

The owner's "spaghetti" reading is right about the *file tree* and wrong about the *dataflow*.
ADR-0105 diagnosed the identical complaint on 2026-07-02:

> *"'Insights Engine' and 'Reflection Insights' read as two systems but are already one... The
> split is **historical** (reflection first via ADR-0010; the engine later via FRE-24), **not
> designed** — and it manifests as two disjoint dashboards for one funnel."*

Verified state of its build wave:

| Ticket | What | State |
|---|---|---|
| FRE-714 | T1 isolated `sysgraph` store | Done |
| FRE-715 | T2 producer convergence, `source` discriminator, single entrypoint | Done |
| FRE-716 | T3 bidirectional linkage + verbatim substance | Done |
| FRE-718 | T6 Postgres tuning | Done |
| FRE-719 | T5 one funnel dashboard | Done |
| FRE-720 | T0 separation probe | Done |
| FRE-721 | T7 generation-time dedup | Done |
| **FRE-717** | **T4 loop closure — the assembled seam (AC-6)** | **Awaiting Deploy since 2026-07-08** |

And the loop is code-complete: `brainstem/jobs/outcome_ingestion.py` (158 lines) sweeps promoted
tickets, classifies outcomes from Linear state, records them in `sysgraph`, and updates the
realized-value signal — which `promotion.py:458` reads back (*"Rank by realized value and drop
suppressed (source, category) pairs"*).

> **The full arc exists in code and has never run end to end once.** Then everything was switched
> off.

Why it still *reads* as spaghetti: ADR-0105 deliberately chose "converge, don't rebuild" to avoid
regressing the shipping ADR-0040 loop. Correct at the time — but it means the file layout still
tells the two-product story while the dataflow is one funnel. Reading the tree gives you the old
system.

**Package boundaries that misdescribe the dataflow.** `captains_log` is 6,537 lines holding four
unrelated concerns: reflection *generation*, *promotion to Linear*, *Linear feedback polling*, and
*context recall*. The last is not self-improvement at all. `second_brain` (3,568 lines) mixes
user-knowledge extraction with the session digest with graph quality monitoring.

**Enforcement gap.** `source: ProposalSource | None` is nullable (`captains_log/models.py:130`), so
ADR-0105's AC-1 ("every produced proposal labeled by source") is a convention, not a type.

---

## 5. Reflection recall — why it was toxic, and why it is now forbidden by construction

Disabled 2026-07-26. The prod `.env` comment records the reason:

> *"...every session in a fortnight into conversation context, **including rejected proposals**.
> Owner decision 2026-07-26 (PR #679): disable, then remove properly — ADR-0067 is Accepted and
> must be superseded, not deleted."*

The mechanism is worse than noise. Selection required `proposed_change_what` non-empty and
`seen_count >= 2` — recurrence treated as *signal*. But a rejected proposal recurs *because* it was
rejected and its generating condition persists. **The filter preferentially surfaced ideas the
owner had already declined**, into the system prompt of unrelated conversations. Meanwhile
ADR-0124 Phase 3 exists to *prevent* re-litigation: the anti-re-litigation feature and the
re-litigation engine were two phases of one programme.

The relevance filter was capitalized-entity overlap (ADR-0067 selection policy, item 4), giving the
general rule:

> **A recall surface that guesses relevance and lands in the system prompt has asymmetric cost — a
> miss is invisible, a false positive taxes every turn it fires on.** Hedging the label ("past
> observations, not current directives") is a wish, not a mechanism.

**It could not be built today.** ADR-0105 isolates `sysgraph` by engine *precisely so
self-improvement data can never touch the user KG*; reflections classify as `output_kind = finding`
and route there (ADR-0106, **superseded by ADR-0115** 2026-07-11). Reflection recall took material
the architecture says must live in an isolated store and injected it into user context.

**Two live risks:**

1. `reflection_recall_enabled` **defaults to `True`** (`settings.py:2307`). Only the prod `.env`
   turns it off. A fresh deploy, a test stack, or a rebuilt image without that line re-enables the
   pollution silently.
2. ADR-0067 still reads **Status: Accepted** while its feature is disabled — and there are **two
   different ADRs numbered 0067** in the directory (`ADR-0067-reflection-surfacing-in-context-assembly.md`
   and `ADR-0067-skill-nudge-injection.md`).

---

## 6. The 200-character clip is a codebase-wide idiom

The figures that condemn it are recorded in the digest producer's own docstring
(`second_brain/session_summary.py:14-19`): user messages sit at **p50 58 chars** — already below
the cut — while assistant responses sit at **p50 1,847**, so the old clip "barely touched the user
text while discarding roughly **89% of the assistant text** where a session's outcome lives."

> **Provenance caveat** (raised in codex review): that docstring *asserts* the figures without
> carrying the query, dataset, or calculation behind them. It is a recorded project figure, not an
> independently reproducible measurement. Re-derive before sizing anything on it.

**The enumeration below is known to be incomplete.** Nine sites were found by this audit; codex
review of ADR-0125 found two more, so the working count is **at least eleven** and no exhaustive
sweep has been done. This is why ADR-0125 D5 specifies a guard rather than a list of edits.

Found by this audit: `request_gateway/context.py:240` · `memory/proactive.py:133` ·
`second_brain/consolidator.py:611` · `second_brain/entity_extraction.py:1089` ·
`captains_log/reflection.py:158, 411, 764` · `request_gateway/recall_controller.py:392` ·
`request_gateway/state_document.py:101`

Found subsequently in codex review: `orchestrator/executor.py:3442`
(`conv.summary or conv.user_message[:200]`) · `captains_log/reflection_dspy.py:434`

**The first one is live in the context-assembly path and is worse than what the owner rejected:**

```python
"summary": ep.get("summary") or ep.get("user_message", "")[:200]
```

When an episode has no digest, assembled context receives the **user message** clipped to 200
characters and **no assistant text at all**. The superseded summarizer kept 200 characters of the
answer; this fallback keeps zero. Against measured digest delivery — 6 digests across 61 qualifying
sessions — **this is what the model sees for roughly 90% of recalled episodes.**

`consolidator.py:587` matches that exact signature (`summary == user_message[:200]`) to *detect
extraction failure*. The codebase treats the string shape as a known failure sentinel while
shipping it as the default elsewhere.

---

## 7. Provenance and supersession already exist; usage does not

| Capability | Exists as | State |
|---|---|---|
| Origin — which turn/session | `source_episode_ids`, `source_turn_ids`, `originating_session_id`, `originating_trace_id` | populated, never surfaced |
| Origin — *kind* of source | `source_type`, 5-value vocabulary ordered by trustworthiness, per-source confidence defaults (`memory/weight.py`) | **hardcoded `"conversation"`** at `entity_extraction.py:591` — carries zero bits |
| What a claim replaced, and why | supersession chain: `superseded_by`, retained originals, `correction` vs `evolution`, bitemporal `observed_at` (`memory/supersession.py`, ADR-0098 D2) | works, never surfaced |
| Trust that moves with experience | `corroboration_count`, `last_confirmed` (`KnowledgeWeight`) | no populator found |
| **How a claim was used** | — | **does not exist** |

The supersession machinery is genuinely good — explicitly *not* last-write-wins, losers retained as
audit records, three-way FRESH / SUPERSEDE / REJECT adjudication. What it lacks is any signal from
consumption. Nothing records that a claim was recalled into a turn, or what happened afterwards.

That missing edge is the single change that pays into both dimensions: trust calibration for the
memory layer (dimension 1) and the evidence base for verification (dimension 2).

---

## 8. Taxonomy — designed in four ADRs, absent from storage

Designed across ADR-0097 (**Proposed** — "hypothesis, held loosely"), ADR-0098 (Accepted — Claims +
lifecycle), ADR-0106 → **ADR-0115** (`output_kind` / class axis, **Implemented 2026-07-12**),
ADR-0109 (Accepted — nine entity types, extended by FRE-782).

Measured state, from ADR-0106's own Context section, **dated 2026-07-02 and inherited, not
re-measured here**:

- **all 7,581 `:Entity` nodes carried `class=None`**
- ~23% (1,718) had **empty descriptions**
- *"the System-vs-User machinery today lives only in the extraction prompt and nowhere in storage
  or query"*
- `second_brain/taxonomy.py` is **58 lines**

> **Important caveat.** ADR-0115 reached **Implemented on 2026-07-12** — ten days *after* that
> measurement — with its assembled seam proven live on a sanctioned turn. So class emission is live
> for new writes, and the `class=None` figure describes the **legacy corpus**, not current write
> behaviour. Whether the legacy nodes were backfilled is unknown and was not checked. **Do not cite
> the 7,581 figure as the state of the graph today.** A fresh count is owed before any decision
> leans on it.

The structural argument in this section does not depend on that count: it is about which cuts serve
which consumers, and about the cost of a wrong cut. But the "taxonomy delivers no retrieval benefit
today" claim is now **unproven** and should be treated as open.

**Where a taxonomy demonstrably helps.** Supersession uses facets as a claim-slot taxonomy and the
label materially moves behaviour: base cosine floor **0.83** with no facet, **0.60** when facets
agree, **0.95** when they differ (`memory/supersession.py:39-46`). One categorical label shifts the
matching bar by 35 points, deterministically and for free.

**Where it works against the process.**

1. A **misclassification is worse than no classification** — unclassified degrades recall,
   misclassified makes retrieval confidently search the wrong partition. The classifier here is the
   extraction LLM that hardcodes `source_type` and leaves 23% of descriptions empty.
2. **A wrong cut destroys rather than degrades.** ADR-0106 states this outright: the boundary *"is
   **miscut** today — it keys on **subject** ('is it about the machine') when it should key on
   **output kind**"*, and the named consequence is that the prompt would stamp durable
   harness-knowledge as `System` and discard it, *"throwing away the pedagogical crown jewel."*
3. **An open domain outgrows a fixed taxonomy.** FRE-770's three-rater agreement study hit a
   genuine 3-way split on one creative-writing artifact, which is why FRE-782 added a ninth entity
   type. Each extension owes a migration.
4. **Write-time routing is first-write-wins for *location*.** ADR-0098 deliberately killed
   first-write-wins for claim *content*; ADR-0106/0115 then introduced write-time dispatch, and a
   misdispatched item is in the wrong store permanently. Same principle, opposite answer, two ADRs
   apart.

**Proposed resolution** (does not require overturning ADR-0106/0115): **write-time dispatch only
for isolation boundaries that should never be crossed** — where a forgettable read-time filter is
genuinely weaker — and **read-time facets for everything retrieval-related**, where the cut may be
wrong and must stay revisable.

**The governing principle:** *derive the taxonomy from the questions its consumer must answer, never
from an ontology of what things are.* Ontology-first design is how a taxonomy comes to work against
the process — see Ranganathan's facet analysis (one hierarchy cannot serve multiple query types) and
Shirky, *Ontology is Overrated* (2005), whose failure conditions — open domain, changing corpus,
fallible classifier — describe this system exactly.

---

## 9. The verification layer — feasibility bounds and required inputs

**Definition, per the owner (2026-07-27):** verification means *the model producing factually true
and complete output*, assessed **post-session**. It is explicitly not about whether the harness is
optimised, cost-efficient, or fit for purpose.

### 9.1 Feasibility

The general oracle — verify any claim against the world — is not implementable and should stop
being implied. The bounded form is standard practice:

- **FActScore** (Min et al. 2023, arXiv:2305.14251) — decompose a generation into atomic facts,
  verify each against a knowledge source, report the supported proportion. This is the method.
- **RAGAS faithfulness** (Es et al. 2023, arXiv:2309.15217) — is the answer entailed by the context
  it was given.
- **AIS / Attributable to Identified Sources** (Rashkin et al. 2021, arXiv:2112.12870) — the formal
  framing for "this sentence is supported by that record."

**The hard boundary, which belongs in the spec rather than being discovered later:** verification is
relative to **the evidence the agent actually had**. Never against the world. Verdicts are
three-valued — **CONFIRMED / REFUTED / UNVERIFIABLE** — with `UNVERIFIABLE` first-class and never
silently treated as a pass. (This mirrors the evidence contract master already applies by hand at
the acceptance gate; the oracle is that same function pointed at turns instead of tickets.)

### 9.2 Completeness is the harder half

Completeness needs a reference set, and only three of four sources exist:

| Reference | Checkable? | Requires |
|---|---|---|
| the user's question — was a multi-part question fully answered | weakly | input 1 |
| **tool payloads** — tool returned 10 rows, answer discussed 3 without saying so | **yes, and this is the strong one** | input 4 |
| recall — relevant facts retrieved and not used | yes | input 5 |
| the world | no — `UNVERIFIABLE` | — |

### 9.3 Required inputs — what capture must durably hold

| # | Input | Why verification needs it | Status |
|---|---|---|---|
| 1 | user message, full | the question completeness is judged against | present; threatened by §6 |
| 2 | assistant response, full text | the claims to be extracted | present |
| 3 | reasoning / thinking trace | diagnose *why* a claim was wrong | partial — known undercount |
| 4 | per tool call: name, arguments, status, **full result payload**, ordering | adjudicate "I did X" and "the data says Y" | present — confirmed §10, resolved 2026-07-27 (FRE-1000) |
| 5 | **identities of recalled memory items** (+ scores) | check assertions about stored knowledge; detect available-but-unused facts | **missing — a boolean and a count only** (§2) |
| 6 | the assembled context actually sent | establishes what the model *could* have used | **missing — confirmed §10, resolved 2026-07-27 (FRE-1000)**; category checklist + opaque hash only |
| 7 | trace / session / turn ids | joinability | present; known `user_id` gap on older captures |
| 8 | model + params | attribute a failure mode | present |

**Two real gaps: #5 and #6 (both certain as of 2026-07-27); #4 confirmed present.** #5 is the same
change as the usage edge in §7; #6 should be built alongside it as one capture surface (§10).

### 9.4 Cost posture

A post-session verification pass is an additional model call per session. It should be **sampled,
not exhaustive** — one in N sessions is sufficient for a measurement instrument. Sampling is a
design choice here, not a compromise, and it matters given the 2026-07-25 cost incident.

---

## 10. Limitations — what this audit did not establish

Stated explicitly so the ADR does not over-claim, and so a reader can bound the conclusions.

- **Tool-payload retention (§9.3 #4) was not verified.** Whether full tool result payloads survive
  in durable storage, or only name/status/error metadata, is unconfirmed. This is the one input map
  entry that could change the capture chain's size.
  **Resolved 2026-07-27 (FRE-1000): YES, full payloads are durably retained, inline.**
  `TaskCapture.tool_results[].output` is fed from `dr["tool_layer_output"]`
  (`orchestrator/executor.py:4966`), which is `result.output` straight from tool execution
  (`orchestrator/tool_dispatch.py:256`) — the same object before the intra-turn tool-result-digest
  pass (`orchestrator/tool_result_digest.py`) ever runs. That digest pass operates only on the
  separate `tool_results` transcript batch that becomes `ctx.messages` (executor.py:5018-5040,
  by its own docstring: "so the verbatim bytes of a digested result never enter ctx.messages") —
  it never touches `ctx.tool_results`, the list `TaskCapture` is built from. Both durable sinks
  preserve it whole: disk write is an unmodified `orjson.dumps` of the full model
  (`capture.py:219-223`), and the ES path only JSON-stringifies `output`/`arguments` for mapping
  compatibility (`es_indexer.py:normalize_capture_doc_for_es`), never truncates. Confirmed live
  against `agent-captains-captures-2026-07-2*`: sampled `tool_results[].output` sizes up to 20,451
  chars (`read_skill`), 6,712 chars (`bash`), 3,923 chars (`web_search`) — no clipping observed.
  **Size implication: none.** No new capture work is needed for item 4; it is already satisfied by
  the existing `TaskCapture.tool_results` shape. (Out of scope for this ticket, noted for a future
  one: payloads are stored inline rather than by artifact-store pointer, so an unusually large tool
  result is a growth vector worth watching, not a correctness gap today.)
- **`corroboration_count` populators were not exhaustively searched.** None found; absence not
  proven.
- **Whether within-session compression re-summarises its own prior summary** — the drift question
  for the live window — was not settled. `_assemble_compressed` replaces the middle with the
  summary, and a note at `within_session_compression.py:420` implies summaries persist as
  assistant-role messages, so on a subsequent compaction a prior summary plausibly falls into the
  middle again. Likely, unverified. It matters only for dimension 1.
- **Node counts, class coverage, and the description-emptiness figure in §8 are inherited from
  ADR-0106's Context section dated 2026-07-02**, not re-measured against the live graph. ADR-0115
  reached Implemented on 2026-07-12, so those figures predate live class emission and are very
  likely stale for new writes. Whether legacy nodes were backfilled was not checked. A fresh count
  is owed before any decision relies on taxonomy coverage.
- **Whether ADR-0061's mechanism B re-summarises its own prior summary was not settled**, and its
  status line records that the soft trigger was retired on 2026-07-22 (FRE-…), so its live
  behaviour differs from ADR-0092's description of it. Dimension-1 only; not pursued.
- **The `user_id` capture gap in §9.3 #7 is inherited** from earlier measurement, not re-verified.
- **No claim is made about whether long sessions actually degrade from context pressure.** That
  hypothesis was raised and explicitly left untested; the p50 assembled context of 448 tokens
  suggests recall starvation rather than overflow, but the two were not disentangled.
- **Whether `prompt_manifest` (FRE-409) satisfies §9.3 #6 was flagged "likely" but not confirmed.**
  **Resolved 2026-07-27 (FRE-1000): NO, it does not.** Two independent reasons. First, `prompt_manifest`
  (`captains_log/prompt_manifest.py`) is not a durable per-turn record at all — it is a 3-line string
  built on demand inside `generate_reflection_entry` (`captains_log/reflection.py:288`) purely as an
  input to the dimension-1 reflection producer's prompt, and is discarded after that call; it is
  never written to `TaskCapture` or any other durable store. Second, even its ingredients —
  `prompt_component_ids` / `prompt_static_prefix_hash` / `prompt_dynamic_hash`, durably logged per
  model call on `model_call_completed` (`llm_client/telemetry.py:153-156`) — do not reach the bar.
  `component_ids` is a fixed 9-entry taxonomy of system-prompt *section categories*
  (`llm_client/prompt_identity.py:47-57`: `tool_awareness`, `deployment_context`, `operator_stanza`,
  `skill_index`, `skill_bodies`, `memory_section`, `artifact_builder_planning_note`,
  `tool_use_rules`, `decomposition_instructions`) recording only coarse presence/absence of a
  section, never which specific memory items, skill bodies, or conversation slice it contained. The
  two hashes are one-way SHA-256 (16 hex chars) — sufficient to detect whether two prompts were
  byte-identical (their designed purpose, ADR-0078 cache-erosion measurement), not to retrieve or
  reconstruct what the content was. The full assembled prompt text itself (`full_prompt` in
  `llm_client/client.py:551` / `litellm_client.py:719`) is passed into the hash function and never
  persisted anywhere. Confirmed live against `agent-logs-*`: sampled `orchestrator.primary`
  `model_call_completed` events return `component_ids` no finer than the 9-category list (e.g.
  `['tool_awareness', 'deployment_context', 'operator_stanza', 'skill_index', 'memory_section',
  'tool_use_rules']`), and `prompt_static_prefix_hash == prompt_dynamic_hash` on every sampled
  primary-turn call — the two hashes are not even distinguishing static from dynamic content in
  practice, let alone standing in for it. **Size implication: real, and probably the larger of the
  two capture-chain items.** Item 6 is an unresolved gap, not a satisfied one: durably recording
  "what the model could have used" needs the admitted content at item-identity granularity (which
  memory items survived trimming — the same admission-point requirement AC-3 already states for
  item 5, which conversation slice was in the window, which skill bodies were loaded), not a
  category checklist plus a hash. The implementation ticket should treat items 5 and 6 as one
  capture surface rather than building a second, narrower one.

---

## 11. What this audit concludes

1. The two dimensions are real, were never distinguished, and the capture layer is the proof.
2. The dimension-1 machine is not incoherent — it is a converged pipeline **one deploy short of
   ever having closed its loop**, wearing a file layout that describes the pre-convergence world.
3. Dimension 2 has no producer, no evidence base, and one deliberately-deferred design (the oracle).
4. The urgent, cheap, and reversible-only-now work is the **evidence contract** — recording what a
   turn must hold so dimension-2 verification becomes possible later.
5. The oracle itself should stay deferred. Its feasibility bounds are known and should constrain
   the future decision rather than be decided now.

---

## References

- `docs/research/2026-07-26-session-summarizer-brainstorm-brief.md` — the brief this session opened from
- ADR-0105 — self-improvement pipeline convergence, `sysgraph` isolation (Accepted 2026-07-02)
- ADR-0115 — two-axis emission/persistence/dispatch (supersedes ADR-0106, 2026-07-11)
- ADR-0124 + Amendments A and B — session-summary producer, conversation-only scope
- ADR-0098 — memory substrate and lifecycle; D2 living-claim supersession
- ADR-0067 — reflection surfacing in context assembly (Accepted; feature disabled 2026-07-26)
- ADR-0061 — within-session progressive context compression (mechanism B)
- ADR-0092 — the four compaction mechanisms, verified against code
- Min et al. 2023, *FActScore*, arXiv:2305.14251
- Es et al. 2023, *RAGAS*, arXiv:2309.15217
- Rashkin et al. 2021, *Measuring Attribution in Natural Language Generation*, arXiv:2112.12870
- Shirky, C. 2005, *Ontology is Overrated: Categories, Links, and Tags*
- Ranganathan, S. R., *Colon Classification* — facet analysis

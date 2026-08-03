# FRE-1131 — Alignment audit of the context and memory-injection space (explore seat, Fable, 2026-08-03)

Read-only throughout. Deployed revision verified against the running container by file hash: main tip
`41e76267` — which already includes FRE-1115's code despite its board state (finding F7). Every live
claim below is a query output from this session; code claims carry file:line at that revision. Unit of
analysis per the commission: the seam between pieces of work, not the inside of either.

---

## Verdict — does the shipped set compose?

**No — but not because the pieces conflict. They compose into three disconnected systems that each
assume one of the others is doing the remembering.**

1. **A window system** that hard-caps visible history at 5 exchanges (`service/app.py:323-324`,
   `conversation_max_history_messages=10`, verified live in-container) and then carries a full
   compaction/budget/reset stack downstream that this cap starves into permanent idleness.
2. **A storage system** that durably keeps everything the window discards — Postgres
   `sessions.messages` (unbounded, A0), ES captures, Neo4j Turn nodes (written per-turn, +6s lag,
   measured today 4/4) — with no path back into a turn except tools the model isn't told to use for
   this purpose.
3. **A knowledge-graph system** that becomes the *sole* memory past five exchanges — a dependency no
   ADR states, no prompt tells the model about, and the recall programme's tickets only partially
   address.

The individual mechanisms mostly work as built (the FRE-1116 probes showed 6+/8 correct answers).
What does not exist is a single stated policy connecting them: **eviction is silent, permanent from
the model's point of view, unrecoverable by design, and the model is simultaneously instructed
"Do NOT say you have no memory" (`executor.py:2408-2409`).** The parts of a coherent design all exist
in the tree today — trim machinery, full stores, retrieval tools — they have never been connected as
one decision.

---

## F1 — The 10-message slice starves every growth-triggered mechanism; measured, not inferred

The commissioning claim, now grounded live (all ES counts provisional per FRE-1051; the per-turn
`cache_reset_decision` emit serves as the same-index oracle — it records, so its zero-siblings mean
"did not fire", not "lost"):

| Mechanism | Fire event | Count since 07-23 |
|---|---|---|
| A — gateway budget trim (ADR-0047) | `context_compaction` / `compaction_applied` | **0** |
| B — within-session hard gate (ADR-0061) | `within_session_compressed` | **0** |
| C — tool-result digest (ADR-0085) | `tool_result_digested` | **0** (flag off, by design) |
| D — frozen reset (ADR-0081 D3) | `frozen_reset_fired` | **0** |
| D's per-turn evaluator (FRE-944) | `cache_reset_decision` | **94** |

The evaluator's own payload proves *why*: max `accumulated_tokens` ever observed **2,835** against a
48,000 ceiling (5.9%); max `turns_since_reset` **6** — pinned there forever because it counts user
messages in the sliced list. Two structural corollaries no prior ticket states:

- **`cache_reset_min_run_turns_local=12` is unsatisfiable by construction.** The counter cannot
  exceed ~6 while the slice is 10. On the local backend the scheduler can *never* fire, at any
  session length, regardless of every other parameter.
- **The scheduler said "reset now" six times (reason `optimum`, cloud backend) and nothing acted.**
  FRE-944 hoisted only the *logging* onto the live path (`executor.py:3427`, "evaluate + log only");
  the acting call site (`executor.py:3677`) sits below the gateway branch return at `:3646` —
  157/157 observed turns take that branch. The telemetry reports "holding" per turn on a path where
  a `True` verdict is discarded.

FRE-942's recorded conclusion ("becomes real when sessions get longer") is therefore **false** and
should be corrected on the ticket: length cannot reach any gate. Its 1,283-evaluation zero is fully
explained by the slice.

**Also structurally idle:** `salient_highlights` (hot-tier content, set only by the reset that never
runs) is permanently `""`; Stage 7's phase 3 can never fire because `tool_definitions` is hardcoded
`None` at `context.py:695`; ADR-0047's dropped-entity feedback cache, A-marker and incident record
are all downstream of a trim that has never engaged.

## F2 — Half of Stage 6's output is built, stamped as delivered, and discarded

The executor rebuilds its message list from `SessionManager` (`executor.py:3251`) and **never reads
`gw.context.messages`**. Consequences, code-verified and measured:

- The state document (`context.py:623-625`) and the "## Session Fact Recall … Do not claim you don't
  know" system message (`context.py:651-658`) never reach the model. The recall controller (Stage
  4b) — **the only consumer of un-sliced history in the entire pipeline** (`app.py:366`) — has
  reclassified **5 turns ever** (`recall_reclassified` count), and on all 5 its output went nowhere.
- `session_facts_injected=True` is stamped at `context.py:648` *about the discarded list* and
  forwarded verbatim into the turn-evidence record (`executor.py:1271-1272`), where a
  SESSION_FACT_SECTION candidate is marked `admitted` on the strength of that stamp
  (`turn_evidence.py:628-629`). The evidence contract ADR-0125 built specifically to remove
  admitted-vs-seen ambiguity contains a false-admission path by construction. Currently latent —
  zero `session_fact` items in the last 300 evidence-bearing captures — but it costs nothing to be
  honest and it will bite the first time anyone builds on session facts.
- What *does* survive Stage 6/7 into the model: `memory_context` + `recall_candidates` only. So
  Stage 7's trim (were it ever to fire) governs a message list that is thrown away — the budget
  system and the delivery system are measuring different objects.

## F3 — Within-turn growth is the real axis, and it is ungoverned

Assembled context at turn entry is ~2.8K and cannot grow. But per-turn `input_tokens` on captures
since 07-01 (n=193): **median 25,657 · p90 115,216 · max 796,801**, accumulated across ≤7 primary
calls/turn (`assembled_context.primary_call_count` avg 2.3, max 7). The growth is tool results
appended verbatim mid-turn (`executor.py:5504`) — exactly the case FRE-942 named. What governs it:

- B-hard is the only live governor and has fired **0** times — its estimator cannot see
  `reasoning_content` (FRE-908 finding, still true) and it measures against `0.85 ×` the *selected
  model's* window while Stage 7 measures against a 120K budget and D against `0.5 × 96K`. **Three
  mechanisms, three denominators** — ADR-0081 D3's stated intent (reset at 0.50 *before* the 0.85
  backstop) is arithmetically incoherent across them.
- C — the mechanism designed for precisely this shape — is parked (flag off; the FRE-486
  truncation-amnesia objection stands; the decomposition alternative in the parking analysis was
  never ticketed).

So the compaction stack points at conversational growth, which cannot happen, while tool-result
growth — which measurably reaches 6× a local context window — has no live governor. This is the
inversion the audit was commissioned to find: **the mechanisms and the load are on opposite axes.**

## F4 — ADR-0081's tier hierarchy: one tier of three actually exists

Live state of the hierarchy the ADR describes ("Implemented" header):

| Tier | Design | Live reality |
|---|---|---|
| Hot — salient highlights (D3) | distilled tail | slot exists, **permanently empty** (F1) |
| Warm — frozen narrative (D2) | compressed old turns | **the raw 10-slice, verbatim** — the narrative re-derivation was deleted with FRE-941 |
| Cold — full history (D5) | on-demand retrieval | **never built**; approved as FRE-465 only on 08-03 |

Shipping the warm tier without the cold tier did exactly what the commission suspected: the system
discards what it cannot recover, silently, from turn 6. And the graph work was *not* designed with
that dependency in view — nothing in the recall scoring anticipates being the only memory (the topic
subscore that ranks cross-session recall is computed from the last 3 turns *of the sliced list*,
`context.py:78-88`, so the ranking signal degrades exactly when the KG becomes load-bearing).

**On the owner's mid-audit question — "storing session files and letting Seshat decide":** storage
is already solved three times over (Postgres unbounded, ES captures, Neo4j per-turn within seconds).
The missing piece is *only* the affordance: no in-prompt statement that visible history is truncated
and searchable, and no tool aimed at the verbatim transcript. `recall_personal_history` has, through
FRE-1119, become roughly 40% of the cold tier by accident (99.8% turn reachability post-backfill —
verified live at 2,341/2,346; lexical rank + `topic_matched` labels per turn). FRE-465 and FRE-1121
are now converging on the same tool from different directions — **they should be executed as one
design, not two** (FRE-1121's own failure clause — "grows a second embedding path" — is exactly what
building them separately produces).

## F5 — ADR status headers misreport the space (census of 23 ADRs)

The ADR-0061 defect (Implemented-while-inert) recurs. Full census in the appendix table; the ones
that matter:

- **ADR-0081** — header "D2/D3 Implemented + live"; D3's action half is structurally uninvoked
  (`frozen_reset_fired` = 0, code comment at `executor.py:5674-5687` admits it), D5/D6 never built,
  header says nothing per-decision. The exact ADR-0061 pattern, uncorrected.
- **ADR-0092** — "Implemented (all five tickets shipped)"; 2 of its 3 in-scope mechanisms (A, D) are
  permanently zero, so its verification items 2/4/5 are unsatisfiable. A green observability surface
  over mechanisms that cannot fire is worse than none — it manufactures confidence.
- **ADR-0104** — reads *Proposed* while 3 of its 4 retrieval arms have run in production since
  07-07 via `.env` overrides (code defaults `False`). The live recall architecture is governed by an
  env file, not an adjudicated ADR. ADR-0100 has the same shape (live via `.env:670,686`).
- **ADR-0115** — "Implemented" but the read side (`structural_class_predicate_enabled`) defaults off
  with no override: the class axis is written on every entity and never read. Write-only circuit.
- **ADR-0096** — Accepted, zero implementing code (`memory_access_dedup_enabled`, `access_path`: 0
  hits in src/).
- Hygiene: `AGENT_REFLECTION_RECALL_ENABLED=false` (`.env:745`) is an orphan — no matching setting
  exists (deleted under ADR-0125 D2); ADR-0125's header (Accepted) contradicts its own status log
  (Proposed).

## F6 — The in-flight recall programme: composes, with three seams to manage

- **FRE-1118 is scoped one layer too narrow.** It addresses KG-recall confidence (floors, hidden
  score, the prompt line). But the owner-experienced failure — a confident answer over a gap — has a
  *second* source this audit measured: window amnesia at turn 6. "I have no record" and "I can't see
  it in my window but can search" are different absence classes needing different renders. If 1118
  ships without the truncation affordance, the model will honestly report "no memory" for things
  that are one tool call away. **Sequence FRE-465's affordance with or before FRE-1118's prompt
  change** — they edit the same prompt region and their wordings interact. Note FRE-1122's fixture
  (correctly) probes KG-absence only; window-absence probes don't exist yet.
- **FRE-1119's fallback is adequately self-describing** (per-turn `topic_matched` flags,
  `tools/personal_history.py:163-195`) — the feared trade of false absence for unlabeled false
  presence did not happen. Non-finding.
- **FRE-1114 confirmed as invariant-only**: renderer already filters empties; measured waste 18
  slots/56 turns (~6%), population → ~0 once the 1115 repair runs. The *real* cap problem is the one
  FRE-1116 named: post-1061 the cap counts half-rows (episodes dominating admission 196 vs 97 in the
  last 300 turns' items). Its re-aim should also absorb the decorative-gate cleanup: `min_score=0.3`
  admits candidates the `diminishing_score_floor=0.35` immediately terminates — the live tail
  population (0.347–0.357) sits inside a 0.01-wide band where caps, not scores, decide survival
  while telemetry attributes drops to scores.
- FRE-1120 folds its model-visible signal into 1118's design (both tickets already say so);
  FRE-1126 is independent (no cancellation); FRE-1122 correctly sequenced pre-1118.
- **Verified non-finding:** the window→graph handoff has no timing gap — Turn consolidation is
  per-turn, ~6s (Postgres 4 user turns today / Neo4j 4 Turn nodes, latest +6s).

## F7 — Process seams the audit tripped over

- **The deploy unit is "rebuild from tip", not "ticket".** The 16:27Z container includes FRE-1115
  (merged 16:24Z) although the board says Awaiting Deploy — verified by file hash. Any rebuild
  silently deploys everything merged since the last one; "Awaiting Deploy" is not a gate, it is a
  label. Corollary right now: **FRE-1115's code half is live but its repair script has not run — the
  graph still holds 1,406 empty-description entities** (live count; census was 1,399). The remedy
  sitting unexecuted while the ticket reads deployed-ish is FRE-1127's exact pattern, live again.
- **Attachment turns silently drop the entire volatile block** — memory section, skill bodies,
  highlights (`_inline_volatile_with_outcome:1323-1324` returns NO_TARGET on block-form content,
  i.e. every image/PDF turn). Owner-visible as "it forgot everything the turn I sent a photo."
  Code-verified; the `inline_outcome` field exists on the evidence record but isn't ES-queryable
  (items don't carry it) — worth one measurement before fixing.
- Stance double-injection: a curated behavioural target that is also a recalled entity renders its
  affect string twice (`context.py:640-641` → `executor.py:2398-2429`). Cosmetic, real.
- Sub-agents receive zero context by design-comment ("Primary agent will enrich context",
  `expansion.py:66-74`) — nothing does. Worth an explicit decision rather than an accident.

---

## What should change, in order

1. **Decide the window policy as an owner-level design decision (ADR-worthy), not a config default.**
   Everything above is downstream of `conversation_max_history_messages=10` — a value no ADR
   adjudicated. The coherent options: (a) small window + built cold tier + truncation affordance
   (MemGPT shape — matches the owner's stated intent); (b) large window + the trim/reset machinery
   actually engaged. Today we have the costs of both and the benefits of neither.
2. **Execute FRE-465 + FRE-1121 as one design**, folding the truncation-affordance wording into the
   FRE-1118 prompt work (same region, interacting semantics). FRE-1122's baseline should gain 2–3
   window-absence probes so the affordance is measurable.
3. **Truth-in-reporting batch (small, cheap, high-trust):** delete or wire the discarded Stage-6
   surfaces (state document, Session Fact Recall, recall controller — 5 fires, 0 deliveries, and it
   is the only reader of full history); derive `session_facts_injected` from the wire form; mark
   `cache_reset_decision` as evaluate-only in its own payload or gate the emit.
4. **ADR hygiene batch:** correct 0081/0092 headers per-decision; adjudicate 0104 (and 0100) to
   match production; decide 0115's read side (enable or state write-only intent); disposition 0096;
   delete the orphan env var; reconcile 0125's header/log.
5. **Run the FRE-1115 repair script** (master, ask-first path already exists) — 1,406 orphans are
   sitting in the live graph with the fix deployed around them.
6. **Re-open the within-turn governance question** FRE-942 half-answered: the tail-band fix landed
   on a gate that has never fired while measured per-turn input reaches 796K. Either make B-hard's
   estimator honest (count `reasoning_content`, reconcile denominators) or revisit C's parking with
   the decomposition alternative. Fold the FRE-1129-style denominator unification in.

## Draft tickets (for the owner to route — this seat files nothing)

- **T1 (ADR trigger, Tier-1):** "The conversation window is an unadjudicated config default that
  silently governs the memory architecture — decide the window/cold-tier policy." Body: F1+F4 above.
- **T2 (Tier-2):** "Stage 6 assembles messages the executor never reads — wire or delete; evidence
  stamps session-fact admission from the discarded list." Body: F2. Includes the recall controller
  disposition (sole full-history reader).
- **T3 (Tier-3):** "Telemetry reports compaction mechanisms as holding on paths where they cannot
  act." Body: F1/F3 emit fixes.
- **T4 (Tier-1, ADR-hygiene):** "Five ADR status headers misreport the memory/context space." Body:
  F5 list.
- **T5 (Tier-2):** "Attachment turns drop the entire volatile block (memory, skills) — measure
  NO_TARGET frequency, then fix the inliner for block-form content." Body: F7.
- **Comment for FRE-942:** correct the recorded conclusion (cannot become real from length; the
  slice, not session brevity, explains the 1,283-eval zero).
- **Comment for FRE-1114:** confirm re-aim per F6 (cap semantics + decorative-gate cleanup).

## Method appendix

`_count` not `_cat` throughout; event/field names resolved against code and one raw document before
trusting any zero (three zeros in this session were name artifacts: `prompt_tokens`→`input_tokens`
era boundary, unindexed `recall_admission.items.source`, `recall_controller_*`→`recall_reclassified`);
ES counts treated as provisional per FRE-1051 with the per-turn `cache_reset_decision` emit as
same-index oracle; deployed code identified by container file hash, not board state; config read from
the running process (`/app/.venv/bin/python`), not the repo; Neo4j via cypher-shell with `_count`
verification; no substrate writes, no board mutations, no live turns fired, worktree re-pinned to
deployed tip `41e76267` on the owner's instruction.

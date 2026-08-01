# FRE-1116 — End-to-end analysis of the memory process (explore seat, Fable, 2026-08-01)

Read-only throughout. Worktree detached at `900354a1` (current main, deployed code). Every number below
is an actual query output from this session, not a record's claim about itself. Answer-level grading
used real owner probe turns whose ground truth exists independently in `sessions.messages`.

---

## Verdict — which layer dominates

**The measured degradation and the felt failures have two different owners, and neither is the corpus.**

1. **The delivered-token collapse (455→205/turn) is selection-layer geometry from the FRE-1061
   pair-split deploy, not decay.** Trim-event series (ES `agent-logs-*`, provisional per FRE-1051):
   token_estimate ran 327–455 through 07-29, then 283 on **07-30** and 207 on 07-31 — the exact deploy
   date — while mean candidates went ~20 → 34.4 → 46.6 and every stop became `recall_item_cap`.
   Pair-splitting turns each retrieved row into two candidates (entity + episode); the item cap stayed
   at 5; so five *half-rows* now fill slots that used to hold five full rows. Delivered tokens halve
   arithmetically. Answer quality on 07-30/31 probes stayed good.

2. **The user-visible failures concentrate in the absence/confidence layer (Q3) — the one with no
   ticket.** Every bad outcome observed end-to-end (harmony false-negative, clafoutis
   multi-search flail, meds first-attempt empty recall, melon near-miss) is the system unable to
   distinguish "I have it" / "I have something near it" / "I have nothing" — in either direction.
   No code path can express absence; the subscore floors admit irrelevance by construction; the
   recall tools manufacture *false* absence; an embedder failure silently empties recall; and the
   prompt instructs the model "Do NOT say you have no memory."

3. **The write-path corpus numbers (29%) are real but overstate answer-level impact.** All 1,399
   empty-description entities come from a single generator and mostly shadow described twins; the
   renderer already filters them; the knowledge for real owner facts is overwhelmingly present and
   well described. On real turns, 0 of 55 rendered entities were empty and 47 of 55 were clean.

4. **Capture is not lossy in the current era (Q4).** 190/198 July user turns have ES captures; the 8
   misses are failed/trivial turns (captures only write on completion). The "missing conversation"
   the owner remembers almost certainly never happened inside Seshat.

---

## Q4 — capture completeness (checked first; everything else rests on it)

Populations (all via `_count`/set-joins, not `_cat`): Postgres 1,257 sessions / 4,892 messages;
ES captures 1,988; Neo4j 2,338 Turn nodes; volume 120 files (07-19..07-31 retention window).

**July join** (198 user-message trace_ids from `sessions.messages` as oracle):
- in ES captures **190/198**, Neo4j Turn **187/198**, route_traces **193/198**, volume **120/129** of
  late-July.
- The 8 ES misses, verbatim: two image turns 07-02, "search the internet" ×2 + "healthcheck" (07-22),
  "Check their promo availabily…" + "Stuck?" (07-28), "You don't find the word clafoutis at all?"
  (07-31). Consistent with the known rule: failed turns produce no capture.

**The remembered clafoutis-recipe conversation:** searched `sessions.messages` (regex), ES captures,
ES logs, Neo4j, and the named volume for clafoutis/mascarpone/cherry/danish. Findings: a 07-02 image
turn where the assistant *identified* a clafoutis photo; the 07-25 Actimel+**mascarpone** ice-cream
adaptation thread; no recipe-adaptation conversation anywhere. The agent's own 07-31 investigation
(read from `sessions.messages`) reached the same verdict: "There was never a clafoutis recipe
adaptation conversation." Balance of evidence: off-system conversation or a blend of the image turn
and the mascarpone ice-cream thread — not capture loss.

Caveats that keep this honest: ES loses up to 83% of an event type on some days (FRE-1051), so log
zeros are provisional; the Postgres `captains_log_captures` table is **empty (0 rows)** — captures
exist only in ES + volume; volume retention is ~13 days; April-era captures lack `user_id` (1,169
docs) and read short.

**June consolidation gap (real, bounded):** Neo4j Turn nodes June = 169 vs 333 June user messages
(~50%); July = 187 vs 198 (94%). The June music-theory threads *are* extracted (entities exist,
below) but June turn coverage is half.

## Q1 — write path: what is stored

**Empty descriptions (18.7% of corpus): one generator, fully identified.** All 1,399 empty-description
entities (owner census) match one signature — `extractor_model IS NULL` AND `originating_trace_id IS
NOT NULL`, 1,398/1,399 DISCUSSES-linked, avg mention_count 1.8:

> `no_extractor=TRUE, no_trace=FALSE, count=1399` (single row — no other signature exists)

Mechanism (from code): `create_conversation`'s bare `MERGE (e:Entity {name: $name})`
(`memory/service.py:1271-1295`) mints name-only nodes before `create_entity` runs; they stay empty
forever when the extractor omitted a description, when `create_entity` deduped to a different
canonical name (leaving a case-variant orphan), or when it failed. Live specimens: "Parallel octaves"
(described) vs "Parallel Octaves" (∅), minted 16s apart on 06-13; "coroscanner" ∅ beside
"Coroscanner" (14 mentions, good description); "Tenerife South Airport" ∅ beside "Tenerife South".
Rate by first_seen month: Apr 20%, May 23%, Jun 11.8%, **Jul 9.1%** — declining but still minting.
These entities have no embedding (embed text requires a description), so they cannot enter the dense
retrieval arm — they reach selection only via the lexical arm and mention pins, then die at the
renderer.

**Self-referential descriptions (10% by owner census): mostly born bad, not corrupted later.** By a
conservative pattern, 89 current descriptions are turn-relative; only 8 have any
`EntityDescriptionVersion` history, and the whole archive holds 72 versions — so re-description
corruption is the rarer path. The likely root is the extraction prompt's own contract
(`entity_extraction.py:208`): "what makes this entity notable **here**?" — an invitation to describe
the conversation.

**The corruption feedback loop exists and was caught in the act.** Clafoutis description on 07-31 at
16:26 (agent's own message): "A French baked dessert, here described as a cherry-filled custard-like
dish." Live graph now: "…discussed as a cherry-filled custard-like dish **in the memory search
context**." Version chain confirms the good description was archived at 16:15 during the probe turn —
extraction re-described the entity from a turn *about searching memory*, and the living-description
update accepted it. The phrase "in the memory search context" appears in exactly 1 entity and exists
nowhere in the current codebase — it is conversation-leakage, not a prompt template.

**But the knowledge is there.** Spot-checks of derived facts, all present with usable descriptions:
"Renault Rafale Atelier Alpine E-Tech 4x4 300ch — …the user likes strongly…", "HKoenig HF250 —
self-refrigerated ice cream maker…", "George — the user's brother, mentioned in the context of his
birthday", "Rosuvastatine — the user's statin medication that was increased in dosage", Tenerife
(direct-flights context), Coroscanner (14 mentions), KilgourMD, Crete (46 mentions).

## Q2 — selection path, measured at the answer

**56 turns carry `recall_admission` records (post-07-27).** Aggregates:
- state: 53 present / 3 empty; admitted mean 5.13/turn (cap-bound).
- drops: `recall_candidate_cap` 449, `recall_item_cap` 70, `not_rendered` 18.
- **admitted kinds: episode 184 vs entity 70** — episodes dominate 72:28 (FRE-1021 at scale).
- Of 55 distinct admitted-and-rendered entities: **0 empty**, 8 self-referential-but-informative,
  47 clean. The renderer's emptiness filter works; the waste is upstream — 18 slots across 56 turns
  (~6%) admitted then never rendered (the FRE-1114 mechanism, live).

**Answer-level probe battery** (real owner probes; ground truth from earlier sessions):

| Probe (date) | Ground truth in DB | Path | Answer verdict |
|---|---|---|---|
| "Have we discussed making hummus?" (07-31) | 06-29, 07-28, 07-31 threads | proactive only (21 items, no tools) | **Correct**, detailed |
| "Did we talk about bad breath?" (06-30) | 06-29 halitosis thread | recall tool ×2 | **Correct** incl. exact date+quote |
| "What cars did we discuss…dealers?" (06-29) | Lexus NX450h+/RZ450e 06-12 | search_memory ×3 | **Correct** incl. lease month |
| "Have we made ratatouille?" (06-21) | 06-13 thread | both tools | **Correct** with date |
| "Have we discussed liver disfunction?" (07-05) | in bad-breath differential | proactive only | **Correct** |
| "What did we talk about a week ago?" (06-28) | prior week's sessions | recall tool | **Correct** |
| Meds change upset stomach (07-31, 1st) | statin thread 07-27 | proactive **empty** → tool rescue | Correct answer, wrong path |
| Same, reworded 37s later | same | proactive (32 cand, Ezetimibe admitted) | Correct |
| "Remind me of…harmony/harmonics" (06-30) | voice-leading threads 06-07/06-13; graph HAS Voice leading, Counterpoint, Fusion Interval, Parallel octaves | 4 tool calls, all missed | **FAILED** — false "no conversation found" |

Six-plus of eight correct at the answer, including through the post-07-30 "degradation" window. The
selection layer's inefficiencies are real; the answers mostly survive them because enough on-topic
content still lands and the on-demand recall tools compensate.

**The two selection paths have different economics.** "Have we discussed X" turns classify as
MEMORY_RECALL and take the broad branch (limit 20, no scoring, no cap-5): the hummus probe admitted 21
items with `score: null`, including LRU Cache / Cache Stampede / Cache Invalidation — irrelevant
pollution admitted un-scored alongside the hummus content. The proactive path admits ≤5 scored items.
Any cap/rank fix touches only the second path.

## Q3 — absence: no stage can say "nothing relevant exists," and the tools invent absence

**By construction (code, verified in worktree at deployed SHA):**
- Retrieval has no similarity gate — the vector query returns top-k unconditionally;
  `recall_similarity_floor` defaults 0.0 and doubles as the lexical-arm entry score (raising it would
  boost lexical candidates — the two uses are in tension).
- The admission threshold cannot express irrelevance: Neo4j cosine scores map to ≈0.5 for an
  *orthogonal* entity (0.45×0.5=0.225), recency floors at 0.5 (0.20×0.5=0.10), topic floors at 0.3
  (0.10×0.3=0.03) → an unrelated, undated entity scores ≈**0.355 > 0.3** min_score. FRE-1053's empty
  name bug pushes the topic term to 0.5.
- The score never reaches the model (deliberate — it rides a sibling map), so a 0.95 hit and a 0.36
  floor-passer render identically; and the rendered section appends: "Use this list to directly answer
  questions about what the user has previously discussed. **Do NOT say you have no memory.**"
- `EvidenceState.EMPTY` is telemetry-only; an empty recall adds nothing to the prompt and tells the
  model nothing.

**Empirically, in both directions:**
- *False presence:* cache-architecture entities admitted on a hummus question (above).
- *False absence:* the harmony probe — `recall_personal_history` returned `{"turns": [], "total": 0,
  "window_days": 90}` while the graph held the answer. Two verified causes: the tool anchors on
  `PARTICIPATED_IN` edges, which exist for only **385 of 2,338 turns** (Apr 0, May 64, Jun 164,
  Jul 157); and its `topic` filter is a literal substring on `user_message` — exactly **2** turns ever
  contain "harmon", so any paraphrase returns 0. The model then truthfully reported a false negative.
- *Silent absence:* the meds first attempt — `embedding_generation_failed` → zero-embedding
  short-circuit → `recall_admission: empty/0/0`, no retry, no signal. Fail-open into "no memory."

## Recommended order for the four parked tickets

**1. FRE-1063 (answer-level criterion) — first, and feed it this battery.** It is the instrument every
other fix should be graded by; this analysis is a working prototype of it (the probe battery + graph
obtainability check). Without it, the next fix gets judged on the record again. The melon case it
cites and the harmony case here are the same lesson from opposite directions.

**2. FRE-1115 (write path) — second, rescoped down from emergency to hygiene.** The census framing
("three in ten carry no usable knowledge") overstates answer impact: empties never render; knowledge
presence is high. But the fix is now *cheap and targeted*: (a) the single bare-MERGE generator in
`create_conversation` (+ the dedup case-variant orphan), (b) the extraction prompt's "notable here"
wording that seeds turn-relative descriptions, (c) note the living-description overwrite loop
(clafoutis) as a guard condition. Stops ongoing minting at ~9%/month.

**3. FRE-1114 (cap-before-filter) — third, but re-aim it.** The literal defect is real and measured
small (~6% of slots, and shrinking further once FRE-1115 stops the generator). The larger cap problem
found here is different: **after FRE-1061, cap-5 counts half-rows**, which is what halved delivered
tokens with 60% budget unused; and mention pins land description-less entities at the head of the
walk. Re-scope toward cap semantics post-pair-split (count rows or spend the token budget, not raw
items) with the filter-ordering fix folded in.

**4. FRE-1053 (empty-name topic hit) — keep, cheap, last.** Verified in source
(`"" in t` always true → 0.5 instead of 0.3). One-line guard; matters only at rank margins (the
melon turn's 0.002) — but with episodes already displacing entities 72:28, removing a systematic
+0.02 episode bias is worth the line.

**None should be closed.** All four address real defects. But none addresses the dominant layer.

## The unticketed gaps (the actual constraint, for the owner to route)

1. **Absence epistemics** — a relevance signal the model can see, a way to render "nothing relevant
   found" honestly, retirement of the "Do NOT say you have no memory" instruction, and a floor-free
   scoring path so irrelevance is expressible. (This is master's "absence" hunch from the ticket,
   now with evidence: it tested true.)
2. **`recall_personal_history` is structurally broken** — 16% edge coverage + literal-substring topic
   filter ⇒ confident false negatives, which the model relays. Same family: `search_memory`'s misses
   on paraphrase. The tools are the model's own fallback when proactive recall thins — they need to be
   at least as semantic as the proactive path.
3. **Embedder failure fails open into silent empty recall** — no retry, no downgrade signal.
4. Observation for master, not a finding: `api_costs` has no `main_inference` rows after 07-28 while
   turns clearly continued (route_traces to 07-31) — either the primary went local-only or cost
   booking broke; worth a look from the cost side.

## Watch items closed

- **FRE-1110 (build2):** PR #791 CI fully green — the repaired assertions discriminate and pass
  against live behavior. No red-assertion finding.
- **Stance hypothesis:** closed by master's measurement (200→213 tokens/turn across the 14:19 deploy);
  nothing here contradicts it. One structural flag, no action proposed: on the gateway path the
  behavioural-stance injection runs *before* `apply_budget`, whose phase-2 can null `memory_context`
  wholesale — the "always present" guarantee is budget-proof only on the legacy executor path.

## Method appendix (traps honored)

`_count` not `_cat` for every corpus size; field names resolved before trusting zeros (`@timestamp` vs
`timestamp`; `stop_reason` unpopulated before 07-30 — field birth, not absence); ES counts treated as
provisional against FRE-1051 loss; cost questions answered from Postgres; captures read from the named
volume, not the stale bind path; Neo4j Turn metadata read from top-level properties only; no
substrate writes, no board mutations, no live turns fired.
